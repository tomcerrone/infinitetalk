#!/usr/bin/env bash
# Provisioning PROD InfiniteTalk natif 720p (sans SeedVR2) : ComfyUI + 4 nodes +
# 7 modèles (aria2c -x16, reprenable) + SageAttention (arch 12.0 = Blackwell/5090)
# + démarre ComfyUI. Idempotent (skip ce qui existe déjà).
#
# Optim cold-start (network volume) : TOUT s'installe sous /workspace — ComfyUI,
# modèles, ET un venv Python qui persiste les packages pip (dont SageAttention
# compilé). Sur un volume réseau monté à /workspace, le 2e boot prend le
# FAST-PATH (sentinel .provisioned) et démarre ComfyUI en ~1-2 min sans rien
# réinstaller. Sans volume (disque éphémère), le comportement complet est inchangé.
set -uo pipefail
log(){ echo "[setup] $(date +%H:%M:%S) $*"; }
COMFY=/workspace/ComfyUI
VENV=/workspace/venv
SENTINEL=/workspace/.provisioned
BASE_PY="$(command -v python3 || command -v python)"

# venv (system-site-packages) sur /workspace = persiste les packages pip entre
# deux boots tout en héritant de torch/cuda de l'image (pas de réinstall lourde).
# Si la création échoue, fallback python système : les modèles restent persistés
# mais SageAttention est recompilé à chaque boot (gain partiel).
ensure_venv(){
  [ -x "$VENV/bin/python" ] && return 0
  log "création venv (system-site-packages) sur le volume"
  "$BASE_PY" -m venv --system-site-packages "$VENV" 2>/dev/null \
    || { log "WARN venv indisponible -> python système"; return 1; }
}
if ensure_venv; then PY="$VENV/bin/python"; else PY="$BASE_PY"; fi
log "python=$PY"

# IT_ATTENTION=sdpa (Phase 2, GPU non-Blackwell : 4090/A6000/L40S) : la sage de
# l'image est compilée sm_120 only — son kernel crasherait au runtime sur sm_89.
# En sdpa on ne lance PAS ComfyUI avec --use-sage-attention, on ne build/check
# PAS sage. Défaut (env absente) = sageattn, comportement strictement identique.
IT_ATTENTION="${IT_ATTENTION:-sageattn}"
SAGE_FLAG="--use-sage-attention"; [ "$IT_ATTENTION" = "sdpa" ] && SAGE_FLAG=""
sage_ok(){ [ "$IT_ATTENTION" = "sdpa" ] || "$PY" -c "import sageattention" 2>/dev/null; }

# Démarrage ComfyUI détaché (réutilisé par le fast-path ET le setup complet).
start_comfyui(){
  pkill -f "main.py --listen" 2>/dev/null; sleep 2
  cd "$COMFY" && setsid bash -c "exec $PY main.py --listen 0.0.0.0 --port 8188 $SAGE_FLAG > /workspace/comfyui.log 2>&1" </dev/null & disown
  for i in $(seq 1 80); do sleep 3; if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then log "COMFY_UP ($((i*3))s)"; return 0; fi; done
  log "WARN ComfyUI pas up après 240s"; return 1
}
model_ok(){ [ -s "$COMFY/models/diffusion_models/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors" ]; }

# FAST-PATH : volume déjà provisionné (venv + sage + modèle) -> skip tout le
# setup, ComfyUI direct (~1-2 min au lieu de ~12 min).
if [ -f "$SENTINEL" ] && [ "$PY" = "$VENV/bin/python" ] && sage_ok && model_ok; then
  log "FAST-PATH: volume provisionné -> start ComfyUI direct (pas de réinstall)"
  if start_comfyui; then echo "[setup] SETUP_PROD_DONE (fast-path)"; exit 0; fi
  log "fast-path KO -> bascule setup complet"
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git ffmpeg aria2 wget curl python3-venv >/dev/null 2>&1 || log "WARN apt"
# Réessayer le venv si python3-venv manquait au premier essai.
if [ "$PY" != "$VENV/bin/python" ] && ensure_venv; then PY="$VENV/bin/python"; log "venv OK après apt -> python=$PY"; fi

# 1) ComfyUI (robuste si dossier pré-existe non-vide)
if [ ! -d "$COMFY/.git" ]; then
  if [ -d "$COMFY" ] && [ -n "$(ls -A "$COMFY" 2>/dev/null)" ]; then
    log "ComfyUI dir non-vide sans .git -> clone temp + merge"
    rm -rf /tmp/_comfy_src; git clone --depth 1 https://github.com/comfyanonymous/ComfyUI /tmp/_comfy_src
    cp -rn /tmp/_comfy_src/. "$COMFY"/; rm -rf /tmp/_comfy_src
  else
    git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY"
  fi
fi
$PY -m pip install --no-cache-dir -q -r "$COMFY/requirements.txt" || log "WARN comfy reqs"
$PY -m pip install --no-cache-dir -q "huggingface_hub[cli]" safetensors || true

# 2) custom nodes (natif : WanVideoWrapper + KJNodes + VideoHelperSuite + Frame-Interpolation/RIFE)
ND="$COMFY/custom_nodes"; mkdir -p "$ND"
clone(){ local d="$ND/$(basename "$1")"; [ -d "$d/.git" ] || git clone --depth 1 "https://github.com/$1" "$d" || { log "WARN clone $1"; return 0; }
  [ -f "$d/requirements.txt" ] && $PY -m pip install --no-cache-dir -q -r "$d/requirements.txt" || true; }
clone kijai/ComfyUI-WanVideoWrapper
clone kijai/ComfyUI-KJNodes
clone Kosinkadink/ComfyUI-VideoHelperSuite
clone Fannovel16/ComfyUI-Frame-Interpolation

# 3) SageAttention 2 (arch 12.0=Blackwell/5090). Placée AVANT les modèles pour le
#    build d'image custom (IT_ENV_ONLY). La compilation exige un toolkit CUDA dont
#    la version correspond à celle de PyTorch (sinon "CUDA version mismatch") : on
#    sélectionne le nvcc qui matche torch (et PAS un /usr/local/cuda-12.8 résiduel).
TCU="$("$PY" -c 'import torch;print((torch.version.cuda or "").strip())' 2>/dev/null || true)"
log "torch.cuda=$TCU toolkits=[$(ls -d /usr/local/cuda* 2>/dev/null | tr '\n' ' ')]"
unset CUDA_HOME
for c in "/usr/local/cuda-$TCU" "/usr/local/cuda" /usr/local/cuda-*; do
  [ -x "$c/bin/nvcc" ] || continue
  NV="$("$c/bin/nvcc" --version 2>/dev/null | grep -oE 'release [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+')"
  if [ -z "$TCU" ] || [ "$NV" = "$TCU" ]; then export CUDA_HOME="$c"; break; fi
done
: "${CUDA_HOME:=/usr/local/cuda}"
export PATH="$CUDA_HOME/bin:$PATH"
log "CUDA_HOME=$CUDA_HOME nvcc=$("$CUDA_HOME/bin/nvcc" --version 2>/dev/null | grep -oE 'release [0-9.]+' | head -1)"
export TORCH_CUDA_ARCH_LIST="${SAGE_ARCH:-12.0}"; export MAX_JOBS="$(nproc)"
if [ "$IT_ATTENTION" = "sdpa" ]; then
  log "IT_ATTENTION=sdpa -> skip build/check SageAttention (GPU non-Blackwell)"
else
  if ! "$PY" -c "import sageattention" 2>/dev/null; then
    log "compile SageAttention (arch $TORCH_CUDA_ARCH_LIST, cuda=$CUDA_HOME)"
    "$PY" -m pip install --no-cache-dir --no-build-isolation "git+https://github.com/thu-ml/SageAttention.git" 2>&1 | tail -15 || log "WARN sage build"
  fi
  "$PY" -c "import sageattention; print('[setup] SAGE import OK')" 2>&1 | tail -1
fi

# Build d'image custom (IT_ENV_ONLY) : ComfyUI + nodes + venv + SageAttention sont
# prêts -> on s'arrête ici. Les modèles (33 Go) restent téléchargés au runtime
# (trop volumineux pour l'image ; un pull de 45 Go serait plus lent qu'un download
# HF direct). Au runtime, ce setup saute les installs (déjà présents dans l'image)
# et ne fait que le download des modèles + le démarrage -> cold-start ~2× plus
# court, sur N'IMPORTE QUEL cloud (Community inclus), sans network volume.
if [ "${IT_ENV_ONLY:-0}" = "1" ]; then
  touch /workspace/.env-ready
  echo "[setup] ENV_ONLY_DONE (image: ComfyUI+venv+sage prêts, sans modèles)"
  exit 0
fi

# 4) modèles via aria2c (multi-connexion, -c reprenable)
M="$COMFY/models"
mkdir -p "$M/diffusion_models/InfiniteTalk" "$M/text_encoders" "$M/vae" "$M/clip_vision" "$M/loras" "$M/wav2vec2" "$COMFY/input"
dl(){ # url dest fname
  [ -s "$2/$3" ] && { log "skip $3 ($(du -h "$2/$3"|cut -f1))"; return 0; }
  log "dl $3"; aria2c -x16 -s16 -c --summary-interval=0 --console-log-level=warn -d "$2" -o "$3" "$1" && log "ok $3" || log "WARN dl $3"; }
HF=https://huggingface.co
dl "$HF/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors" "$M/diffusion_models" "Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors"
dl "$HF/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors" "$M/diffusion_models/InfiniteTalk" "Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors"
dl "$HF/Kijai/wav2vec2_safetensors/resolve/main/wav2vec2-chinese-base_fp16.safetensors" "$M/wav2vec2" "wav2vec2-chinese-base_fp16.safetensors"
dl "$HF/Kijai/WanVideo_comfy/resolve/main/umt5-xxl-enc-bf16.safetensors" "$M/text_encoders" "umt5-xxl-enc-bf16.safetensors"
dl "$HF/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_bf16.safetensors" "$M/vae" "Wan2_1_VAE_bf16.safetensors"
dl "$HF/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" "$M/clip_vision" "clip_vision_h.safetensors"
dl "$HF/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" "$M/loras" "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
log "tailles:"; du -sh "$M"/*/ 2>/dev/null

# 5) démarrage ComfyUI + sentinel fast-path
start_comfyui
# Sentinel : provisioning complet réussi (ComfyUI up + sage importable + modèle
# présent) -> active le FAST-PATH au prochain boot sur ce volume.
if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && [ "$PY" = "$VENV/bin/python" ] && sage_ok && model_ok; then
  touch "$SENTINEL"; log "sentinel .provisioned créé -> fast-path actif au prochain boot"
else
  log "setup incomplet -> pas de sentinel (le prochain boot refera le setup)"
fi
echo "[setup] SETUP_PROD_DONE"

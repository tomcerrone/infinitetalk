#!/usr/bin/env bash
# ⚠ LEGACY R&D (variante 480p + SeedVR2, REJETÉE qualité 2026-06) — NE PAS UTILISER.
# Prod = setup-prod.sh (sa liste de modèles est LA référence ; celle-ci n'est plus maintenue).
# Provisioning pod : InfiniteTalk Single + SeedVR2 upscaler (480p->720p). aria2c -x16 (rapide/reprenable). Idempotent.
set -uo pipefail
log(){ echo "[prov] $(date +%H:%M:%S) $*"; }
COMFY=/workspace/ComfyUI
PY="$(command -v python || command -v python3)"
log "python=$PY"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git ffmpeg aria2 wget curl >/dev/null 2>&1 || log "WARN apt"

# 1) ComfyUI (robuste si le dossier pre-existe non-vide -> clone via temp + merge sans ecraser models/input)
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

# 2) custom nodes
ND="$COMFY/custom_nodes"; mkdir -p "$ND"
clone(){ local d="$ND/$(basename "$1")"; [ -d "$d/.git" ] || git clone --depth 1 "https://github.com/$1" "$d" || { log "WARN clone $1"; return 0; }
  [ -f "$d/requirements.txt" ] && $PY -m pip install --no-cache-dir -q -r "$d/requirements.txt" || true; }
clone kijai/ComfyUI-WanVideoWrapper
clone kijai/ComfyUI-KJNodes
clone Kosinkadink/ComfyUI-VideoHelperSuite
clone Fannovel16/ComfyUI-Frame-Interpolation
clone numz/ComfyUI-SeedVR2_VideoUpscaler

# 3) modeles via aria2c (multi-connexion, -c reprenable)
M="$COMFY/models"
mkdir -p "$M/diffusion_models/InfiniteTalk" "$M/text_encoders" "$M/vae" "$M/clip_vision" "$M/loras" "$M/wav2vec2" "$M/SEEDVR2" "$COMFY/input"
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
echo "[prov] PROVISION_DONE"

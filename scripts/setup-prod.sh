#!/usr/bin/env bash
# Provisioning PROD InfiniteTalk natif 720p (sans SeedVR2) : ComfyUI + 4 nodes +
# 7 modèles (aria2c -x16, reprenable) + SageAttention (arch 12.0 = Blackwell/5090)
# + démarre ComfyUI. Idempotent (skip ce qui existe déjà → réutilisable sur volume).
set -uo pipefail
log(){ echo "[setup] $(date +%H:%M:%S) $*"; }
COMFY=/workspace/ComfyUI
PY="$(command -v python || command -v python3)"
log "python=$PY"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git ffmpeg aria2 wget curl >/dev/null 2>&1 || log "WARN apt"

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

# 3) modèles via aria2c (multi-connexion, -c reprenable)
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

# 4) SageAttention 2 (arch GPU ; 12.0=Blackwell/5090)
export CUDA_HOME="${CUDA_HOME:-/usr/local/cuda-12.8}"; export PATH="$CUDA_HOME/bin:$PATH"
export TORCH_CUDA_ARCH_LIST="${SAGE_ARCH:-12.0}"; export MAX_JOBS="$(nproc)"
if ! $PY -c "import sageattention" 2>/dev/null; then
  log "compile SageAttention (arch $TORCH_CUDA_ARCH_LIST)"
  $PY -m pip install --no-cache-dir --no-build-isolation "git+https://github.com/thu-ml/SageAttention.git" 2>&1 | tail -8 || log "WARN sage build"
fi
$PY -c "import sageattention; print('[setup] SAGE import OK')" 2>&1 | tail -1

# 5) démarrage ComfyUI (détaché)
pkill -f "main.py --listen" 2>/dev/null; sleep 2
cd "$COMFY" && setsid bash -c "exec $PY main.py --listen 0.0.0.0 --port 8188 --use-sage-attention > /workspace/comfyui.log 2>&1" </dev/null & disown
for i in $(seq 1 80); do sleep 3; if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then log "COMFY_UP ($((i*3))s)"; break; fi; done
echo "[setup] SETUP_PROD_DONE"

#!/usr/bin/env bash
# ⚠ LEGACY R&D — NE PAS UTILISER EN PROD (CUDA_HOME hardcodé + liste de modèles
# partielle non maintenue). En prod, relancer setup-prod.sh (idempotent, reprend
# les downloads via aria2c -c exactement pareil).
# Reprise robuste : tue les downloads HF gelés, re-télécharge les poids manquants via aria2c (multi-connexion),
# compile Sage (arch SAGE_ARCH), démarre ComfyUI. Idempotent.
set -uo pipefail
log(){ echo "[recover] $*"; }
pkill -f setup-pod 2>/dev/null || true; pkill -f provision.sh 2>/dev/null || true; pkill -f "hf download" 2>/dev/null || true; sleep 2
M=/workspace/ComfyUI/models
get(){ # url dest outname
  if [ -s "$2/$3" ]; then log "skip $3 ($(du -h "$2/$3"|cut -f1))"; return 0; fi
  log "aria2c $3"; aria2c -x16 -s16 -c --summary-interval=0 --console-log-level=warn -d "$2" -o "$3" "$1" && log "ok $3" || log "WARN $3"
}
get "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/umt5-xxl-enc-bf16.safetensors" "$M/text_encoders" "umt5-xxl-enc-bf16.safetensors"
get "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_bf16.safetensors" "$M/vae" "Wan2_1_VAE_bf16.safetensors"
get "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" "$M/clip_vision" "clip_vision_h.safetensors"
get "https://huggingface.co/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" "$M/loras" "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
log "=== tailles ==="; du -sh "$M"/text_encoders/*.safetensors "$M"/vae/*.safetensors "$M"/clip_vision/*.safetensors "$M"/loras/*.safetensors 2>/dev/null

# Sage pour l'archi GPU
export CUDA_HOME=/usr/local/cuda-12.8; export PATH="$CUDA_HOME/bin:$PATH"; export TORCH_CUDA_ARCH_LIST="${SAGE_ARCH:-12.0}"; export MAX_JOBS="$(nproc)"
if ! python -c "import sageattention" 2>/dev/null; then
  log "compile sage arch $TORCH_CUDA_ARCH_LIST"; pip install --no-cache-dir --no-build-isolation "git+https://github.com/thu-ml/SageAttention.git" 2>&1 | tail -8 || log "WARN sage"
fi
python -c "import sageattention; print('[recover] SAGE import OK')" 2>&1 | tail -1

# ComfyUI
pkill -f "main.py --listen" 2>/dev/null || true; sleep 2
cd /workspace/ComfyUI && setsid bash -c "exec python main.py --listen 0.0.0.0 --port 8188 --use-sage-attention > /workspace/comfyui.log 2>&1" </dev/null & disown
for i in $(seq 1 50); do sleep 3; if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then log "COMFY_UP ($((i*3))s)"; break; fi; done
log "RECOVER_DONE"

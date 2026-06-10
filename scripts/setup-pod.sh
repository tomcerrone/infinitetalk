#!/usr/bin/env bash
# ⚠ LEGACY R&D — NE PAS UTILISER EN PROD. Remplacé par setup-prod.sh.
# Piège connu : CUDA_HOME=/usr/local/cuda-12.8 hardcodé ci-dessous = le bug
# "CUDA version mismatch" corrigé dans setup-prod.sh (sélection du nvcc qui
# matche torch). Réutiliser ce script pour un bench = retomber dessus.
# Setup complet d'un Pod (L40S ou 5090) : ComfyUI + nodes + modeles + SageAttention (arch auto) + demarrage.
# Usage : SAGE_ARCH=12.0 bash setup-pod.sh   (12.0=Blackwell/5090, 8.9=Ada/L40S)
set -uo pipefail
log(){ echo "[setup] $*"; }

# 1) ComfyUI + nodes + modeles (script de provisioning)
bash /workspace/provision.sh

# 2) SageAttention pour l'architecture du GPU
export CUDA_HOME=/usr/local/cuda-12.8; export PATH="$CUDA_HOME/bin:$PATH"
export TORCH_CUDA_ARCH_LIST="${SAGE_ARCH:-12.0}"; export MAX_JOBS="$(nproc)"
if ! python -c "import sageattention" 2>/dev/null; then
  log "compile SageAttention (arch $TORCH_CUDA_ARCH_LIST)"
  pip install --no-cache-dir --no-build-isolation "git+https://github.com/thu-ml/SageAttention.git" 2>&1 | tail -10 || log "WARN sage build"
fi
python -c "import sageattention; print('[setup] SAGE import OK')" 2>&1 | tail -1

# 3) Demarrage ComfyUI (detache)
pkill -f "main.py --listen" 2>/dev/null; sleep 2
cd /workspace/ComfyUI && setsid bash -c "exec python main.py --listen 0.0.0.0 --port 8188 --use-sage-attention > /workspace/comfyui.log 2>&1" </dev/null & disown
for i in $(seq 1 50); do sleep 3; if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then log "COMFY_UP ($((i*3))s)"; break; fi; done
log "SETUP_DONE"

#!/usr/bin/env bash
# Orchestre le setup pod : provisioning (modeles) -> SageAttention -> demarre ComfyUI. Lance en background, loggue dans setup.log.
exec > /workspace/setup.log 2>&1
set -x
cd /workspace
echo "[setup] $(date) START"

# 1) provisioning (ComfyUI + nodes + modeles)
bash /workspace/pod-setup-it-seedvr2.sh

# 2) SageAttention pour Blackwell/5090 (arch 12.0)
CU=/usr/local/cuda-12.8; [ -d "$CU" ] || CU=/usr/local/cuda
export CUDA_HOME="$CU"; export PATH="$CUDA_HOME/bin:$PATH"
export TORCH_CUDA_ARCH_LIST=12.0; export MAX_JOBS="$(nproc)"
if ! python -c "import sageattention" 2>/dev/null; then
  echo "[setup] compile SageAttention..."
  pip install --no-cache-dir --no-build-isolation "git+https://github.com/thu-ml/SageAttention.git" 2>&1 | tail -15 || echo "[setup] WARN sage build"
fi
python -c "import sageattention; print('[setup] SAGE_OK')" 2>&1 | tail -1 || echo "[setup] SAGE_FAIL"

# 3) demarrage ComfyUI (detache)
pkill -f "main.py --listen" 2>/dev/null; sleep 2
cd /workspace/ComfyUI && setsid bash -c "exec python main.py --listen 0.0.0.0 --port 8188 --use-sage-attention > /workspace/comfyui.log 2>&1" </dev/null & disown
for i in $(seq 1 120); do sleep 3; if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then echo "[setup] COMFY_UP ($((i*3))s)"; break; fi; done
echo "[setup] SETUP_ALL_DONE $(date)"

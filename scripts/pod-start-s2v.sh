#!/bin/bash
# dockerStartCmd pod Wan2.2-S2V : ComfyUI natif + modeles S2V + Lightning 4-step. Lance :8188, garde vivant.
exec > /workspace/provision.log 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
PY=python
echo "[boot] $(date) start S2V"
apt-get update -qq && apt-get install -y -qq git ffmpeg aria2 wget curl jq >/dev/null 2>&1
cd /workspace
[ -d ComfyUI/.git ] || git clone --depth 1 https://github.com/comfyanonymous/ComfyUI
cd /workspace/ComfyUI
$PY -m pip install --no-cache-dir -q -r requirements.txt
$PY -m pip install --no-cache-dir -q "huggingface_hub[cli]"
cd custom_nodes
for u in Kosinkadink/ComfyUI-VideoHelperSuite kijai/ComfyUI-KJNodes; do
  d=$(basename "$u"); [ -d "$d/.git" ] || git clone --depth 1 "https://github.com/$u" "$d"
  [ -f "$d/requirements.txt" ] && $PY -m pip install --no-cache-dir -q -r "$d/requirements.txt"
done
cd /workspace/ComfyUI
M=models; mkdir -p $M/diffusion_models $M/text_encoders $M/audio_encoders $M/vae $M/loras input
nohup $PY main.py --listen 0.0.0.0 --port 8188 > /workspace/comfyui.log 2>&1 &
echo "[boot] comfyui lance, download modeles S2V..."
HF=hf; command -v hf >/dev/null 2>&1 || HF=huggingface-cli
R=Comfy-Org/Wan_2.2_ComfyUI_Repackaged
g(){ local path="$1" dest="$2"; local out=$(basename "$path"); [ -s "$dest/$out" ] && { echo "skip $out"; return; }
  $HF download "$R" "$path" --local-dir /tmp/hf >/dev/null 2>&1 && mv -f "/tmp/hf/$path" "$dest/$out" && echo "done $out" || echo "FAIL $out"; }
g split_files/diffusion_models/wan2.2_s2v_14B_fp8_scaled.safetensors $M/diffusion_models
g split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors $M/text_encoders
g split_files/audio_encoders/wav2vec2_large_english_fp16.safetensors $M/audio_encoders
g split_files/vae/wan_2.1_vae.safetensors $M/vae
g split_files/loras/wan2.2_i2v_lightx2v_4steps_lora_v1_high_noise.safetensors $M/loras
g split_files/loras/wan2.2_t2v_lightx2v_4steps_lora_v1.1_high_noise.safetensors $M/loras
echo "[boot] modeles OK"; du -sh $M/*/* 2>/dev/null | grep -iE 'safetensors'
touch /workspace/s2v.ready
echo "[boot] READY $(date)"
wait

#!/bin/bash
# ⚠ LEGACY R&D (piste LTX, ABANDONNÉE) — NE PAS UTILISER. Prod = boot.sh.
# dockerStartCmd du pod LTX : provisionne ComfyUI + nodes + modeles, lance ComfyUI:8188, garde le conteneur vivant.
exec > /workspace/provision.log 2>&1
set -x
export DEBIAN_FRONTEND=noninteractive
PY=python
echo "[boot] $(date) start"
apt-get update -qq && apt-get install -y -qq git ffmpeg aria2 wget curl jq >/dev/null 2>&1
cd /workspace
[ -d ComfyUI/.git ] || git clone --depth 1 https://github.com/comfyanonymous/ComfyUI
cd /workspace/ComfyUI
$PY -m pip install --no-cache-dir -q -r requirements.txt
$PY -m pip install --no-cache-dir -q "huggingface_hub[cli]"
# custom nodes
cd custom_nodes
for u in kijai/ComfyUI-KJNodes Kosinkadink/ComfyUI-VideoHelperSuite city96/ComfyUI-GGUF chrisgoringe/cg-use-everywhere rgthree/rgthree-comfy; do
  d=$(basename "$u"); [ -d "$d/.git" ] || git clone --depth 1 "https://github.com/$u" "$d"
  [ -f "$d/requirements.txt" ] && $PY -m pip install --no-cache-dir -q -r "$d/requirements.txt"
done
cd /workspace/ComfyUI
M=models; mkdir -p $M/unet $M/text_encoders $M/vae $M/checkpoints $M/loras input
# Lance ComfyUI MAINTENANT (nodes prets) -> proxy 8188 up, modeles arrivent ensuite
nohup $PY main.py --listen 0.0.0.0 --port 8188 > /workspace/comfyui.log 2>&1 &
echo "[boot] comfyui lance, download modeles..."
HF=hf; command -v hf >/dev/null 2>&1 || HF=huggingface-cli
g(){ local repo="$1" path="$2" dest="$3" out="$4"; [ -s "$dest/$out" ] && { echo "skip $out"; return; }
  $HF download "$repo" "$path" --local-dir "$dest" >/dev/null 2>&1
  local got="$dest/$path"; [ -f "$got" ] && [ "$got" != "$dest/$out" ] && { mv -f "$got" "$dest/$out"; find "$dest" -mindepth 1 -type d -empty -delete 2>/dev/null; }; echo "done $out"; }
g unsloth/LTX-2.3-GGUF distilled/ltx-2.3-22b-distilled-Q5_K_M.gguf "$M/unet" ltx-2.3-22b-distilled-Q5_K_M.gguf
g unsloth/gemma-3-12b-it-GGUF gemma-3-12b-it-IQ4_XS.gguf "$M/text_encoders" gemma-3-12b-it-IQ4_XS.gguf
g Kijai/LTX2.3_comfy text_encoders/ltx-2.3_text_projection_bf16.safetensors "$M/text_encoders" ltx-2.3_text_projection_bf16.safetensors
g Kijai/LTX2.3_comfy vae/LTX23_video_vae_bf16.safetensors "$M/vae" LTX23_video_vae_bf16.safetensors
g Kijai/LTX2.3_comfy vae/LTX23_audio_vae_bf16.safetensors "$M/checkpoints" LTX23_audio_vae_bf16.safetensors
cp -n "$M/checkpoints/LTX23_audio_vae_bf16.safetensors" "$M/vae/" 2>/dev/null || true
g elix3r/LTX-2.3-22b-AV-LoRA-talking-head LTX-2.3-22b-AV-LoRA-talking-head-v1.safetensors "$M/loras" LTX-2.3-22b-AV-LoRA-talking-head-v1.safetensors
# ID-LoRA TalkVid (talking-head generique, preserve un visage de reference quelconque)
g AviadDahan/LTX-2.3-ID-LoRA-TalkVid-3K lora_weights.safetensors "$M/loras" id-lora-talkvid-ltx2.3.safetensors
echo "[boot] modeles OK"; du -sh $M/*/* 2>/dev/null
touch /workspace/ltx.ready
echo "[boot] READY $(date)"
# garde le conteneur vivant tant que ComfyUI tourne
wait
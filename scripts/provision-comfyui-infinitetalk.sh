#!/usr/bin/env bash
# InfiniteTalk Single (image+audio -> video 720p 9:16) — provisioning ComfyUI sur RunPod Pod.
# Base : runpod/pytorch (torch 2.7.1 / cu128). ComfyUI + nodes + modeles sous /workspace. Idempotent.
set -uo pipefail
log(){ echo "[prov] $*"; }

VOL="/workspace"; mkdir -p "$VOL"
COMFY="$VOL/ComfyUI"
PY="$(command -v python || command -v python3)"
log "python = $PY"
$PY -c "import torch; print('[prov] torch', torch.__version__, 'cuda?', torch.cuda.is_available())" || log "WARN torch import"

# 0) deps systeme
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq git ffmpeg aria2 wget curl >/dev/null 2>&1 || log "WARN apt"

# 1) ComfyUI
if [ ! -d "$COMFY/.git" ]; then
  log "clone ComfyUI -> $COMFY"; git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY"
else
  log "ComfyUI present"; git -C "$COMFY" pull --ff-only || true
fi
$PY -m pip install --no-cache-dir -q -r "$COMFY/requirements.txt" || log "WARN comfy reqs"
$PY -m pip install --no-cache-dir -q "huggingface_hub[cli]" || log "WARN hf cli"
HFCLI="huggingface-cli"; command -v hf >/dev/null 2>&1 && HFCLI="hf"

MODELS="$COMFY/models"
mkdir -p "$MODELS/diffusion_models/InfiniteTalk" "$MODELS/text_encoders" "$MODELS/vae" \
         "$MODELS/clip_vision" "$MODELS/loras" "$MODELS/wav2vec2"

# 2) custom nodes (mixlab volontairement EXCLU : URL-loader utile au worker prod, pas au test interactif)
NODES="$COMFY/custom_nodes"; mkdir -p "$NODES"
clone_node(){ local url="$1" name dir; name="$(basename "$url" .git)"; dir="$NODES/$name"
  if [ -d "$dir/.git" ]; then log "node ok: $name"; git -C "$dir" pull --ff-only || true
  else log "clone node: $name"; git clone --depth 1 "$url" "$dir" || { log "WARN clone $name"; return 0; }; fi
  [ -f "$dir/requirements.txt" ] && $PY -m pip install --no-cache-dir -q -r "$dir/requirements.txt" || true
}
clone_node https://github.com/kijai/ComfyUI-WanVideoWrapper
clone_node https://github.com/kijai/ComfyUI-KJNodes
clone_node https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite
clone_node https://github.com/Fannovel16/ComfyUI-Frame-Interpolation

# 3) modeles (~33 Go) — idempotent (skip si deja la)
hf_get(){ local repo="$1" path="$2" dest="$3" fname; fname="$(basename "$path")"
  if [ -s "$dest/$fname" ]; then log "skip $fname"; return 0; fi
  log "download $fname"
  $HFCLI download "$repo" "$path" --local-dir "$dest" || { log "WARN dl $fname"; return 0; }
  if [ -f "$dest/$path" ] && [ "$dest/$path" != "$dest/$fname" ]; then
    mv -f "$dest/$path" "$dest/$fname"; rmdir -p "$(dirname "$dest/$path")" 2>/dev/null || true; fi
}
hf_get Kijai/WanVideo_comfy_fp8_scaled "I2V/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors" "$MODELS/diffusion_models"
hf_get Kijai/WanVideo_comfy_fp8_scaled "InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors" "$MODELS/diffusion_models/InfiniteTalk"
hf_get Kijai/wav2vec2_safetensors "wav2vec2-chinese-base_fp16.safetensors" "$MODELS/wav2vec2"
hf_get Kijai/WanVideo_comfy "umt5-xxl-enc-bf16.safetensors" "$MODELS/text_encoders"
hf_get Kijai/WanVideo_comfy "Wan2_1_VAE_bf16.safetensors" "$MODELS/vae"
hf_get Comfy-Org/Wan_2.1_ComfyUI_repackaged "split_files/clip_vision/clip_vision_h.safetensors" "$MODELS/clip_vision"
hf_get Kijai/WanVideo_comfy "Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" "$MODELS/loras"

log "tailles modeles:"; du -sh "$MODELS"/* 2>/dev/null || true
echo "[prov] PROVISIONING_COMPLETE"
touch /workspace/prov.done

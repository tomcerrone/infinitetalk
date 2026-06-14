#!/usr/bin/env bash
# One-shot : miroir des 7 modèles HuggingFace (~33 Go) -> Cloudflare R2
# (bucket public `masscontent-models`). Objectif : indépendance vis-à-vis de
# HuggingFace (403 Xet anonymes, bridages) + boot RAPIDE depuis N'IMPORTE QUEL
# fournisseur GPU (R2 = egress gratuit, accessible partout) — cf. setup-prod.sh dl().
#
# À lancer UNE fois, sur un pod (bande passante datacenter, recommandé) OU en local.
# Idempotent : skip un modèle déjà présent sur R2 à la bonne taille (head_object).
# L'arborescence des clés R2 reproduit ComfyUI/models/ (consommée telle quelle par
# le dl() miroir-first de setup-prod.sh).
#
# Env requis : R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
# Env optionnel : HF_TOKEN (download authentifié, recommandé au scale).
# ⚠ Les credentials R2 ne doivent PAS rester sur un pod tiers après l'upload :
#   préférer un token R2 scopé Object-Write + révocation, ou un upload local.
set -euo pipefail
: "${R2_ENDPOINT:?manque R2_ENDPOINT}" "${R2_ACCESS_KEY_ID:?}" "${R2_SECRET_ACCESS_KEY:?}" "${R2_BUCKET:?}"
export HF_XET_HIGH_PERFORMANCE=1
PY="${PYTHON:-python3}"
"$PY" -m pip install -q "huggingface_hub[hf_xet]>=0.32" boto3 2>/dev/null || true

"$PY" - <<'PYEOF'
import os, boto3
from huggingface_hub import hf_hub_download
s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
BUCKET = os.environ["R2_BUCKET"]
# (repo HF, chemin HF, clé R2 = arbo ComfyUI/models). Garder synchro avec
# setup-prod.sh section 4 si un modèle change.
MODELS = [
  ("Kijai/WanVideo_comfy_fp8_scaled", "I2V/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors", "diffusion_models/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors"),
  ("Kijai/WanVideo_comfy_fp8_scaled", "InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors", "diffusion_models/InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors"),
  ("Kijai/wav2vec2_safetensors", "wav2vec2-chinese-base_fp16.safetensors", "wav2vec2/wav2vec2-chinese-base_fp16.safetensors"),
  ("Kijai/WanVideo_comfy", "umt5-xxl-enc-bf16.safetensors", "text_encoders/umt5-xxl-enc-bf16.safetensors"),
  ("Kijai/WanVideo_comfy", "Wan2_1_VAE_bf16.safetensors", "vae/Wan2_1_VAE_bf16.safetensors"),
  ("Comfy-Org/Wan_2.1_ComfyUI_repackaged", "split_files/clip_vision/clip_vision_h.safetensors", "clip_vision/clip_vision_h.safetensors"),
  ("Kijai/WanVideo_comfy", "Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors", "loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"),
]
for repo, hfpath, r2key in MODELS:
    print(f"[mirror] {r2key} : download HF…", flush=True)
    local = hf_hub_download(repo_id=repo, filename=hfpath)
    size = os.path.getsize(local)
    try:
        head = s3.head_object(Bucket=BUCKET, Key=r2key)
        if head["ContentLength"] == size:
            print(f"[mirror] skip {r2key} (déjà sur R2, {size/1e9:.1f} Go)", flush=True); continue
    except Exception:
        pass
    print(f"[mirror] upload {r2key} ({size/1e9:.1f} Go)…", flush=True)
    s3.upload_file(local, BUCKET, r2key)
    print(f"[mirror] OK {r2key}", flush=True)
print("[mirror] MIRROR DONE — 7 modèles sur R2", flush=True)
PYEOF

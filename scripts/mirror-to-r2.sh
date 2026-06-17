#!/usr/bin/env bash
# One-shot : miroir des 7 modèles HuggingFace (~33 Go) -> Cloudflare R2
# (bucket public `masscontent-models`). Objectif : indépendance vis-à-vis de
# HuggingFace (403 Xet anonymes, bridages) + boot RAPIDE depuis N'IMPORTE QUEL
# fournisseur GPU (R2 = egress gratuit, accessible partout) — cf. setup-prod.sh dl().
#
# À lancer UNE fois, sur un pod (bande passante datacenter, recommandé) OU en local.
# Idempotent : skip un modèle déjà présent sur R2 à la bonne taille (head_object) —
# AVANT même le download, pour qu'un re-run après coupure ne re-télécharge rien.
# L'arborescence des clés R2 reproduit ComfyUI/models/ (consommée telle quelle par
# le dl() miroir-first de setup-prod.sh).
#
# DOWNLOAD via aria2c (multi-connexion -x16, reprise -c, CDN HTTPS direct) plutôt que
# hf_hub_download : le transport Xet de hf_hub_download se fait rate-limiter (403) à
# l'échelle ET en anonyme (cause de l'échec « 0/7 en 18 min » du miroir local initial).
# Les 7 modèles sont PUBLICS → aria2c anonyme sur les URL resolve/ suffit (pas de
# HF_TOKEN requis). UPLOAD via boto3. Suppression locale APRÈS upload (disque : pic =
# plus gros fichier ~17 Go, pas 33 Go cumulés).
#
# Env requis : R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET.
# Env optionnel : PYTHON (défaut python3), MIRROR_TMP (défaut /tmp/r2mirror).
# ⚠ Les credentials R2 ne doivent PAS rester sur un pod tiers après l'upload :
#   préférer un token R2 scopé Object-Write + révocation, ou un upload local.
set -euo pipefail
: "${R2_ENDPOINT:?manque R2_ENDPOINT}" "${R2_ACCESS_KEY_ID:?}" "${R2_SECRET_ACCESS_KEY:?}" "${R2_BUCKET:?}"
PY="${PYTHON:-python3}"
TMP="${MIRROR_TMP:-/tmp/r2mirror}"
HF="https://huggingface.co"
mkdir -p "$TMP"
"$PY" -m pip install -q boto3 2>/dev/null || true

# (repo HF | chemin HF | clé R2 = arbo ComfyUI/models). Garder synchro avec
# setup-prod.sh section 4 si un modèle change.
MODELS=(
  "Kijai/WanVideo_comfy_fp8_scaled|I2V/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors|diffusion_models/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors"
  "Kijai/WanVideo_comfy_fp8_scaled|InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors|diffusion_models/InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors"
  "Kijai/wav2vec2_safetensors|wav2vec2-chinese-base_fp16.safetensors|wav2vec2/wav2vec2-chinese-base_fp16.safetensors"
  "Kijai/WanVideo_comfy|umt5-xxl-enc-bf16.safetensors|text_encoders/umt5-xxl-enc-bf16.safetensors"
  "Kijai/WanVideo_comfy|Wan2_1_VAE_bf16.safetensors|vae/Wan2_1_VAE_bf16.safetensors"
  "Comfy-Org/Wan_2.1_ComfyUI_repackaged|split_files/clip_vision/clip_vision_h.safetensors|clip_vision/clip_vision_h.safetensors"
  "Kijai/WanVideo_comfy|Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors|loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
)

# Présence sur R2 à la bonne taille ? exit 0 = présent (skip), 1 = absent.
r2_present() { # r2key expected_size
  "$PY" - "$1" "$2" <<'PYEOF'
import os, sys, boto3
r2key, expected = sys.argv[1], int(sys.argv[2] or 0)
s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
try:
    h = s3.head_object(Bucket=os.environ["R2_BUCKET"], Key=r2key)
    sys.exit(0 if (expected and h["ContentLength"] == expected) else 1)
except Exception:
    sys.exit(1)
PYEOF
}

r2_upload() { # localfile r2key
  "$PY" - "$1" "$2" <<'PYEOF'
import os, sys, boto3
from boto3.s3.transfer import TransferConfig
local, r2key = sys.argv[1], sys.argv[2]
s3 = boto3.client("s3", endpoint_url=os.environ["R2_ENDPOINT"],
    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto")
cfg = TransferConfig(multipart_threshold=64*1024*1024, multipart_chunksize=64*1024*1024,
                     max_concurrency=8, use_threads=True)
s3.upload_file(local, os.environ["R2_BUCKET"], r2key, Config=cfg)
print(f"[mirror] OK upload {r2key}", flush=True)
PYEOF
}

done_n=0; skip_n=0
for entry in "${MODELS[@]}"; do
  IFS='|' read -r repo hfpath r2key <<< "$entry"
  fname="$(basename "$r2key")"
  url="$HF/$repo/resolve/main/$hfpath"
  echo "[mirror] === $r2key ==="
  hfsize="$(curl -sIL --max-time 30 "$url" 2>/dev/null | grep -iE '^content-length' | tail -1 | grep -oE '[0-9]+' || true)"
  if [ -n "$hfsize" ] && r2_present "$r2key" "$hfsize"; then
    echo "[mirror] skip $r2key (déjà sur R2, $(awk "BEGIN{printf \"%.1f\", $hfsize/1e9}") Go)"
    skip_n=$((skip_n+1)); continue
  fi
  echo "[mirror] download $fname ($(awk "BEGIN{printf \"%.1f\", ${hfsize:-0}/1e9}") Go) via aria2c…"
  aria2c -x16 -s16 -c -m3 --retry-wait=5 --summary-interval=15 --console-log-level=warn \
    -d "$TMP" -o "$fname" "$url"
  echo "[mirror] upload $r2key…"
  r2_upload "$TMP/$fname" "$r2key"
  rm -f "$TMP/$fname"
  done_n=$((done_n+1))
done
echo "[mirror] MIRROR DONE — $done_n uploadé(s), $skip_n déjà présent(s) — 7 modèles sur R2"

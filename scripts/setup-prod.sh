#!/usr/bin/env bash
# Provisioning PROD InfiniteTalk natif 720p (sans SeedVR2) : ComfyUI + 4 nodes +
# 7 modèles (aria2c -x16, reprenable) + SageAttention (arch 12.0 = Blackwell/5090)
# + démarre ComfyUI. Idempotent (skip ce qui existe déjà).
#
# Optim cold-start (network volume) : TOUT s'installe sous /workspace — ComfyUI,
# modèles, ET un venv Python qui persiste les packages pip (dont SageAttention
# compilé). Sur un volume réseau monté à /workspace, le 2e boot prend le
# FAST-PATH (sentinel .provisioned) et démarre ComfyUI en ~1-2 min sans rien
# réinstaller. Sans volume (disque éphémère), le comportement complet est inchangé.
#
# ⚠ DOUBLE EFFET d'un push qui modifie ce fichier : (1) rebuild de l'image custom
# ghcr (workflow .github/workflows/build-image.yml, mode IT_ENV_ONLY) ; (2) les
# pods prod exécutent main à CHAQUE boot (DOCKER_START_CMD côté MassContent clone
# le repo) → tout changement part en prod immédiatement, sans pin de version.
set -uo pipefail
log(){ echo "[setup] $(date +%H:%M:%S) $*"; }
COMFY=/workspace/ComfyUI
VENV=/workspace/venv
SENTINEL=/workspace/.provisioned
BASE_PY="$(command -v python3 || command -v python)"

# venv (system-site-packages) sur /workspace = persiste les packages pip entre
# deux boots tout en héritant de torch/cuda de l'image (pas de réinstall lourde).
# Si la création échoue, fallback python système : les modèles restent persistés
# mais SageAttention est recompilé à chaque boot (gain partiel).
ensure_venv(){
  [ -x "$VENV/bin/python" ] && return 0
  log "création venv (system-site-packages) sur le volume"
  "$BASE_PY" -m venv --system-site-packages "$VENV" 2>/dev/null \
    || { log "WARN venv indisponible -> python système"; return 1; }
}
if ensure_venv; then PY="$VENV/bin/python"; else PY="$BASE_PY"; fi
log "python=$PY"

# IT_ATTENTION=sdpa (Phase 2, GPU non-Blackwell : 4090/A6000/L40S) : la sage de
# l'image est compilée sm_120 only — son kernel crasherait au runtime sur sm_89.
# En sdpa on ne lance PAS ComfyUI avec --use-sage-attention, on ne build/check
# PAS sage. Défaut (env absente) = sageattn, comportement strictement identique.
IT_ATTENTION="${IT_ATTENTION:-sageattn}"
SAGE_FLAG="--use-sage-attention"; [ "$IT_ATTENTION" = "sdpa" ] && SAGE_FLAG=""
sage_ok(){ [ "$IT_ATTENTION" = "sdpa" ] || "$PY" -c "import sageattention" 2>/dev/null; }

# Démarrage ComfyUI détaché (réutilisé par le fast-path ET le setup complet).
# comfyui.log sous /workspace/logs (PV-001 : seul dossier servi par le 8189).
start_comfyui(){
  mkdir -p /workspace/logs
  pkill -f "main.py --listen" 2>/dev/null; sleep 2
  cd "$COMFY" && setsid bash -c "exec $PY main.py --listen 0.0.0.0 --port 8188 $SAGE_FLAG > /workspace/logs/comfyui.log 2>&1" </dev/null & disown
  for i in $(seq 1 80); do sleep 3; if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then log "COMFY_UP ($((i*3))s)"; return 0; fi; done
  log "WARN ComfyUI pas up après 240s"; return 1
}
# Sentinel modèle de base (même fichier que --base-model de worker.py/generate.py
# et que le dl section 4 — garder les 4 occurrences synchrones si on change de modèle).
model_ok(){ local f="$COMFY/models/diffusion_models/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors"; [ -s "$f" ] && [ ! -f "$f.aria2" ]; }

# Gate des 7 modèles : présents, download fini (pas de .aria2) ET taille >= plancher
# (détecte un fichier tronqué par 403 Xet / disque plein). Planchers à ~60% du réel
# pour zéro faux négatif. setup-prod.sh écrit /workspace/.models-ok seulement si OK ;
# boot.sh auto-terminate le pod si la sentinelle manque (évite un "pod empoisonné"
# qui claim des jobs voués à échouer faute de modèles — cf. zombie 5090 2026-06-13).
models_present(){
  local M="$COMFY/models" ok=0
  _m(){ [ -s "$1" ] && [ ! -f "$1.aria2" ] && [ "$(stat -c%s "$1" 2>/dev/null || echo 0)" -ge "$2" ] || { log "MODEL_MISSING $1"; ok=1; }; }
  _m "$M/diffusion_models/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors"                       14000000000
  _m "$M/diffusion_models/InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors"    1000000000
  _m "$M/wav2vec2/wav2vec2-chinese-base_fp16.safetensors"                                               100000000
  _m "$M/text_encoders/umt5-xxl-enc-bf16.safetensors"                                                  5000000000
  _m "$M/vae/Wan2_1_VAE_bf16.safetensors"                                                               100000000
  _m "$M/clip_vision/clip_vision_h.safetensors"                                                        1000000000
  _m "$M/loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"                          100000000
  return $ok
}

# FAST-PATH : volume déjà provisionné (venv + sage + modèle) -> skip tout le
# setup, ComfyUI direct (~1-2 min au lieu de ~12 min).
if [ -f "$SENTINEL" ] && [ "$PY" = "$VENV/bin/python" ] && sage_ok && model_ok; then
  log "FAST-PATH: volume provisionné -> start ComfyUI direct (pas de réinstall)"
  if start_comfyui; then
    if models_present; then touch /workspace/.models-ok; else rm -f /workspace/.models-ok; fi
    echo "[setup] SETUP_PROD_DONE (fast-path)"; exit 0
  fi
  log "fast-path KO -> bascule setup complet"
fi

export DEBIAN_FRONTEND=noninteractive
# Outils système : DÉJÀ présents dans l'image (installés au build). On saute apt par
# défaut -> certains hôtes (ex Novita) ont un réseau apt restreint où `apt-get update`
# HANG sans fin -> setup figé après "python=" et le pod ne réclame jamais de travail.
# Filet : si un binaire manque vraiment, apt borné par `timeout` (jamais de hang infini).
if command -v git >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1 && command -v aria2c >/dev/null 2>&1 && command -v wget >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
  log "apt skip (git/ffmpeg/aria2/wget/curl déjà présents dans l'image)"
else
  log "apt: install des outils manquants (borné à 120s)"
  timeout 120 bash -c 'apt-get update -qq && apt-get install -y -qq git ffmpeg aria2 wget curl python3-venv' >/dev/null 2>&1 && log "apt OK" || log "WARN apt (timeout/échec) -> on continue"
fi
# Réessayer le venv si python3-venv manquait au premier essai.
if [ "$PY" != "$VENV/bin/python" ] && ensure_venv; then PY="$VENV/bin/python"; log "venv OK après apt -> python=$PY"; fi

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
# hf_xet : binaire Rust du transfert Xet. HF a retiré hf_transfer ; SANS hf_xet, le
# fallback hf_hub_download retombe en HTTP LFS mono-flux LENT (cause n°1 du boot lent).
# Installé DANS le venv ($PY = celui qui exécute le fallback). >=0.32 embarque hf_xet.
$PY -m pip install --no-cache-dir -q "huggingface_hub[cli,hf_xet]>=0.32" safetensors || true

# 2) custom nodes (natif : WanVideoWrapper + KJNodes + VideoHelperSuite + Frame-Interpolation/RIFE)
ND="$COMFY/custom_nodes"; mkdir -p "$ND"
clone(){ local d="$ND/$(basename "$1")"; [ -d "$d/.git" ] || git clone --depth 1 "https://github.com/$1" "$d" || { log "WARN clone $1"; return 0; }
  [ -f "$d/requirements.txt" ] && $PY -m pip install --no-cache-dir -q -r "$d/requirements.txt" || true; }
clone kijai/ComfyUI-WanVideoWrapper
clone kijai/ComfyUI-KJNodes
clone Kosinkadink/ComfyUI-VideoHelperSuite
clone Fannovel16/ComfyUI-Frame-Interpolation

# 3) SageAttention 2 (arch 12.0=Blackwell/5090). Placée AVANT les modèles pour le
#    build d'image custom (IT_ENV_ONLY). La compilation exige un toolkit CUDA dont
#    la version correspond à celle de PyTorch (sinon "CUDA version mismatch") : on
#    sélectionne le nvcc qui matche torch (et PAS un /usr/local/cuda-12.8 résiduel).
TCU="$("$PY" -c 'import torch;print((torch.version.cuda or "").strip())' 2>/dev/null || true)"
log "torch.cuda=$TCU toolkits=[$(ls -d /usr/local/cuda* 2>/dev/null | tr '\n' ' ')]"
unset CUDA_HOME
for c in "/usr/local/cuda-$TCU" "/usr/local/cuda" /usr/local/cuda-*; do
  [ -x "$c/bin/nvcc" ] || continue
  NV="$("$c/bin/nvcc" --version 2>/dev/null | grep -oE 'release [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+')"
  if [ -z "$TCU" ] || [ "$NV" = "$TCU" ]; then export CUDA_HOME="$c"; break; fi
done
: "${CUDA_HOME:=/usr/local/cuda}"
export PATH="$CUDA_HOME/bin:$PATH"
log "CUDA_HOME=$CUDA_HOME nvcc=$("$CUDA_HOME/bin/nvcc" --version 2>/dev/null | grep -oE 'release [0-9.]+' | head -1)"
# MAX_JOBS : RESPECTER une valeur déjà posée (le Dockerfile borne à 1 sur le runner
# GitHub 16 Go pour le compile sage multi-gencode ; l'écraser par $(nproc) ici a rendu
# les caps anti-OOM inopérants -> 3 builds OOM-killés "à MAX_JOBS=1" qui tournaient en
# réalité à 4). Au runtime pod (env non posée) : nproc, comportement inchangé.
export TORCH_CUDA_ARCH_LIST="${SAGE_ARCH:-12.0}"; export MAX_JOBS="${MAX_JOBS:-$(nproc)}"
if [ "$IT_ATTENTION" = "sdpa" ]; then
  log "IT_ATTENTION=sdpa -> skip build/check SageAttention (GPU non-Blackwell)"
else
  if ! "$PY" -c "import sageattention" 2>/dev/null; then
    log "compile SageAttention (arch $TORCH_CUDA_ARCH_LIST, cuda=$CUDA_HOME)"
    "$PY" -m pip install --no-cache-dir --no-build-isolation "git+https://github.com/thu-ml/SageAttention.git" 2>&1 | tail -15 || log "WARN sage build"
  fi
  "$PY" -c "import sageattention; print('[setup] SAGE import OK')" 2>&1 | tail -1
fi

# Build d'image custom (IT_ENV_ONLY) : ComfyUI + nodes + venv + SageAttention sont
# prêts -> on s'arrête ici. Les modèles (33 Go) restent téléchargés au runtime
# (trop volumineux pour l'image ; un pull de 45 Go serait plus lent qu'un download
# HF direct). Au runtime, ce setup saute les installs (déjà présents dans l'image)
# et ne fait que le download des modèles + le démarrage -> cold-start ~2× plus
# court, sur N'IMPORTE QUEL cloud (Community inclus), sans network volume.
if [ "${IT_ENV_ONLY:-0}" = "1" ]; then
  touch /workspace/.env-ready
  echo "[setup] ENV_ONLY_DONE (image: ComfyUI+venv+sage prêts, sans modèles)"
  exit 0
fi

# 4) modèles via aria2c (multi-connexion, -c reprenable)
# ⚠ Ces noms de fichiers sont consommés tels quels par generate.py (build_graph)
# et worker.py (GEN_ARGS --base-model) ; model_ok() (plus haut) vérifie le modèle
# de base pour le fast-path. Si tu changes un modèle ici : MAJ generate.py +
# worker.py + model_ok() + la commande de réf du README.
M="$COMFY/models"
mkdir -p "$M/diffusion_models/InfiniteTalk" "$M/text_encoders" "$M/vae" "$M/clip_vision" "$M/loras" "$M/wav2vec2" "$COMFY/input"
# dl : télécharge avec aria2c (multi-connexion, reprenable), retry x3 + fallback
# hf. PIÈGE corrigé : aria2c préalloue la taille FINALE dès le début → un download
# coupé (403 Xet, fréquents à l'échelle quand 12 pods tapent HF) laisse un fichier
# de taille pleine MAIS corrompu + un .aria2 à côté. L'ancien check `[ -s ]` le
# validait → ComfyUI démarre (lazy-load) → generate.py échoue au chargement → pod
# "empoisonné" qui enchaîne les jobs FAILED. On considère donc un fichier non
# fini (présence du .aria2) comme absent, et on retente / bascule sur hf download
# (client officiel, retry Xet intégré).
dl(){ # url dest fname
  [ -s "$2/$3" ] && [ ! -f "$2/$3.aria2" ] && { log "skip $3 ($(du -h "$2/$3"|cut -f1))"; return 0; }
  # Miroir R2 d'abord : egress gratuit, accessible depuis n'importe quel fournisseur
  # GPU, indépendant des bridages HF/Xet. Clé R2 = chemin relatif à $M (= arbo
  # ComfyUI/models, identique à mirror-to-r2.sh). -m1 + connect-timeout = fast-fail
  # si R2 pas encore peuplé (404) ou injoignable -> on bascule sur HF sans ralentir.
  if [ -n "${MIRROR_BASE:-}" ]; then
    local r2key="${2#"$M"/}/$3"
    log "dl $3 (miroir R2)"
    if aria2c -x16 -s16 -m1 --connect-timeout=15 -c --summary-interval=0 --console-log-level=warn -d "$2" -o "$3" "${MIRROR_BASE%/}/$r2key" 2>/dev/null \
       && [ -s "$2/$3" ] && [ ! -f "$2/$3.aria2" ]; then
      log "ok $3 (miroir R2)"; return 0
    fi
    rm -f "$2/$3" "$2/$3.aria2" 2>/dev/null || true
    log "miroir R2 absent/incomplet $3 -> fallback HF"
  fi
  local i
  for i in 1 2 3; do
    log "dl $3 (essai $i)"
    # aria2c reste ANONYME (multi-connexion rapide) : sur 403 Xet anonyme il fast-fail
    # → on bascule sur le fallback hf_hub_download, lui AUTHENTIFIÉ via HF_TOKEN (env
    # du pod) → robuste au scale. (Header Authorization sur aria2c écarté : renvoyé
    # au CDN Xet cross-host, il provoquait des hangs au lieu d'un échec rapide.)
    aria2c -x16 -s16 -c --summary-interval=0 --console-log-level=warn -d "$2" -o "$3" "$1"
    [ -s "$2/$3" ] && [ ! -f "$2/$3.aria2" ] && { log "ok $3"; return 0; }
    log "WARN dl $3 incomplet (essai $i)"; sleep 5
  done
  # Fallback : l'API Python hf_hub_download (retry Xet intégré). On l'invoque via
  # l'API stable de la lib (≠ le CLI dont le nom de module a changé entre v0
  # `huggingface_cli` et v1 `hf` — le pod installe la dernière). Le repo et le
  # chemin se déduisent de l'URL HF (.../<repo>/resolve/main/<path>) ; filename
  # supporte les sous-dossiers (ex "I2V/Wan2_1-...", "split_files/clip_vision/...").
  local rel="${1#"$HF"/}"; local repo="${rel%%/resolve/*}"; local hfpath="${rel#*/resolve/main/}"
  log "fallback hf_hub_download $repo :: $hfpath"
  if "$PY" - "$repo" "$hfpath" "$2/$3" <<'PYEOF'
import sys, shutil
from huggingface_hub import hf_hub_download
repo, path, dest = sys.argv[1], sys.argv[2], sys.argv[3]
shutil.copy(hf_hub_download(repo_id=repo, filename=path), dest)
PYEOF
  then
    # Le fallback a réécrit le fichier complet (copy authentifié) → le .aria2 résiduel
    # d'un essai aria2c interrompu est OBSOLÈTE. Sans ce rm, model_ok()/models_present()
    # voient le .aria2 → modèle jugé INCOMPLET → .models-ok non écrit → boot.sh tue un
    # pod dont les modèles sont en fait corrects (faux négatif + re-download en boucle).
    rm -f "$2/$3.aria2" 2>/dev/null || true
    [ -s "$2/$3" ] && { log "ok $3 (hf_hub_download)"; return 0; }
  fi
  log "ERREUR dl $3 échoué après 3 essais + fallback hf"; return 1; }
HF=https://huggingface.co
# Miroir R2 public (bucket masscontent-models) : source PRIORITAIRE des modèles,
# essayée avant HF par dl(). Override via env MIRROR_BASE="" pour forcer HF only.
MIRROR_BASE="${MIRROR_BASE:-https://pub-e4a7d8d06ea842bfab58f6e736387e6a.r2.dev}"
dl "$HF/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/I2V/Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors" "$M/diffusion_models" "Wan2_1-I2V-14B-720p_fp8_e4m3fn_scaled_KJ.safetensors"
dl "$HF/Kijai/WanVideo_comfy_fp8_scaled/resolve/main/InfiniteTalk/Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors" "$M/diffusion_models/InfiniteTalk" "Wan2_1-InfiniteTalk-Single_fp8_e4m3fn_scaled_KJ.safetensors"
dl "$HF/Kijai/wav2vec2_safetensors/resolve/main/wav2vec2-chinese-base_fp16.safetensors" "$M/wav2vec2" "wav2vec2-chinese-base_fp16.safetensors"
dl "$HF/Kijai/WanVideo_comfy/resolve/main/umt5-xxl-enc-bf16.safetensors" "$M/text_encoders" "umt5-xxl-enc-bf16.safetensors"
dl "$HF/Kijai/WanVideo_comfy/resolve/main/Wan2_1_VAE_bf16.safetensors" "$M/vae" "Wan2_1_VAE_bf16.safetensors"
dl "$HF/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" "$M/clip_vision" "clip_vision_h.safetensors"
dl "$HF/Kijai/WanVideo_comfy/resolve/main/Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors" "$M/loras" "lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors"
log "tailles:"; du -sh "$M"/*/ 2>/dev/null

# Gate des 7 modèles -> sentinelle lue par boot.sh (auto-terminate si absente).
if models_present; then touch /workspace/.models-ok; log ".models-ok écrit (7 modèles présents)"; else rm -f /workspace/.models-ok; log "WARN modèles incomplets -> PAS de .models-ok (boot.sh coupera le pod)"; fi

# 5) démarrage ComfyUI + sentinel fast-path
start_comfyui
# Sentinel : provisioning complet réussi (ComfyUI up + sage importable + modèle
# présent) -> active le FAST-PATH au prochain boot sur ce volume.
if curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && [ "$PY" = "$VENV/bin/python" ] && sage_ok && model_ok; then
  touch "$SENTINEL"; log "sentinel .provisioned créé -> fast-path actif au prochain boot"
else
  log "setup incomplet -> pas de sentinel (le prochain boot refera le setup)"
fi
echo "[setup] SETUP_PROD_DONE"

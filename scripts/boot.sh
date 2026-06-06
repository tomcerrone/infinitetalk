#!/usr/bin/env bash
# Point d'entrée pod PROD (appelé par le dockerStartCmd du template, après clone du repo).
# Provisionne ComfyUI + modèles + SageAttention, puis lance le worker en boucle.
# Tous les secrets/params viennent des ENV injectés par RunPod (pas de worker.env).
set -uo pipefail
log(){ echo "[boot] $(date +%H:%M:%S) $*"; }
REPO="${IT_REPO_DIR:-/workspace/repo}"
log "start pod=$RUNPOD_POD_ID repo=$REPO"

# Scripts à l'emplacement attendu par worker.py (GEN=/workspace/generate.py).
cp -f "$REPO/scripts/worker.py" /workspace/worker.py
cp -f "$REPO/scripts/generate.py" /workspace/generate.py

# Provisioning idempotent (skip ce qui existe déjà → réutilisable si network volume v2).
SAGE_ARCH="${SAGE_ARCH:-12.0}" bash "$REPO/scripts/setup-prod.sh" || log "WARN setup rc=$?"

# Garde-fou : sans ComfyUI, le pod ne sert à rien → auto-terminate (zéro GPU gaspillé).
if ! curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then
  log "ComfyUI DOWN après setup -> auto-terminate pod"
  [ -n "${RUNPOD_API_KEY:-}" ] && [ -n "${RUNPOD_POD_ID:-}" ] && curl -s -X POST \
    "https://api.runpod.io/graphql?api_key=$RUNPOD_API_KEY" -H "Content-Type: application/json" \
    -d "{\"query\":\"mutation{podTerminate(input:{podId:\\\"$RUNPOD_POD_ID\\\"})}\"}" >/dev/null 2>&1
  exit 1
fi

# Worker en boucle : redémarre si crash. self_terminate() retourne rc 0 (arrêt volontaire
# à vide, podTerminate déjà appelé) → on ne relance pas.
log "ComfyUI up -> worker loop"
while true; do
  python3 -u /workspace/worker.py >> /workspace/worker.log 2>&1
  rc=$?
  log "worker exited rc=$rc"
  [ "$rc" = "0" ] && break
  sleep 5
done
log "end"

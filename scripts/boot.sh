#!/usr/bin/env bash
# Point d'entrée pod PROD. Appelé par le DOCKER_START_CMD défini côté MassContent
# (masscontent/src/lib/runpod/runpod.ts) qui clone ce repo (main, public) à CHAQUE
# boot de pod puis lance `bash repo/scripts/boot.sh`. Si tu renommes/déplaces ce
# fichier ou changes son contrat, MAJ DOCKER_START_CMD dans MassContent.
# Provisionne ComfyUI + modèles + SageAttention, puis lance le worker en boucle.
# Tous les secrets/params viennent des ENV injectés par deployWorkerPod
# (MASSCONTENT_BASE_URL, PIPELINE_SECRET, SAGE_ARCH, IDLE_EXIT_SECONDS,
# + IT_ATTENTION/IT_BLOCKSWAP par GPU) — pas de worker.env, PLUS de RUNPOD_API_KEY
# (PV-003 : la terminaison passe par l'endpoint MassContent, cf. terminate_pod).
set -uo pipefail
log(){ echo "[boot] $(date +%H:%M:%S) $*"; }
REPO="${IT_REPO_DIR:-/workspace/repo}"
# /workspace/logs = SEUL dossier servi par le serveur de logs 8189 (PV-001 :
# pas d'exposition des assets clients PII). DOCKER_START_CMD le crée déjà ; on
# le garantit ici aussi (boot.sh peut être lancé hors de ce dockerStartCmd).
mkdir -p /workspace/logs
LOGS=/workspace/logs
log "start pod=${RUNPOD_POD_ID:-?} repo=$REPO"
# Diagnostic env (présence, pas les valeurs) : aide à comprendre un échec d'auto-coupe.
log "env: SECRET=$([ -n "${PIPELINE_SECRET:-}" ] && echo set || echo MISSING) POD_ID=$([ -n "${RUNPOD_POD_ID:-}" ] && echo set || echo MISSING) MC=$([ -n "${MASSCONTENT_BASE_URL:-}" ] && echo set || echo MISSING)"

# Scripts à l'emplacement attendu par worker.py (GEN=/workspace/generate.py).
cp -f "$REPO/scripts/worker.py" /workspace/worker.py
cp -f "$REPO/scripts/generate.py" /workspace/generate.py

# Provisioning idempotent — UNE fois (skip ce qui existe déjà → réutilisable si network volume v2).
SAGE_ARCH="${SAGE_ARCH:-12.0}" bash "$REPO/scripts/setup-prod.sh" || log "WARN setup rc=$?"

# PV-003 : auto-terminaison via l'endpoint MassContent (le pod n'a plus la clé
# RunPod compte-entier). Auth par PIPELINE_SECRET (déjà sur le pod) ; c'est le
# serveur qui exécute le podTerminate. Le cron orphan-killer reste le filet.
terminate_pod(){
  [ -n "${MASSCONTENT_BASE_URL:-}" ] && [ -n "${RUNPOD_POD_ID:-}" ] && curl -s -X POST \
    "${MASSCONTENT_BASE_URL%/}/api/workers/runpod/terminate" -H "Content-Type: application/json" \
    -H "x-pipeline-secret: ${PIPELINE_SECRET:-}" \
    -d "{\"podId\":\"$RUNPOD_POD_ID\"}" >/dev/null 2>&1
}

# Mode peuplement du network volume (one-shot) : setup-prod.sh a téléchargé les
# modèles + créé le venv + compilé SageAttention SOUS /workspace (= sur le volume).
# On NE lance PAS le worker et on NE coupe PAS le pod (supervision manuelle), pour
# vérifier le volume avant de le détacher. Lancer un pod avec IT_SETUP_ONLY=1.
if [ "${IT_SETUP_ONLY:-0}" = "1" ]; then
  up=down; curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1 && up=up
  prov=no; [ -f /workspace/.provisioned ] && prov=yes
  log "IT_SETUP_ONLY=1 -> peuplement terminé (ComfyUI=$up provisioned=$prov). Pas de worker, sleep 7200."
  sleep 7200
  exit 0
fi

# Garde-fou : sans ComfyUI, le pod ne sert à rien → auto-terminate (zéro GPU gaspillé).
if ! curl -sf http://127.0.0.1:8188/system_stats >/dev/null 2>&1; then
  log "ComfyUI DOWN après setup -> auto-terminate pod"
  terminate_pod
  sleep 3600  # filet anti restart-loop si la coupe échoue (le cron coupe aussi)
  exit 1
fi

# Worker en boucle : redémarre si crash (rc != 0). rc 0 = idle exit volontaire.
# worker.log sous /workspace/logs (servi par 8189 ; pas de PII dans les logs).
log "ComfyUI up -> worker loop"
while true; do
  python3 -u /workspace/worker.py >> "$LOGS/worker.log" 2>&1
  rc=$?
  log "worker exited rc=$rc"
  [ "$rc" = "0" ] && break
  sleep 5
done

# Idle exit : le worker a tenté self_terminate. Filet (si l'env RUNPOD_* du worker
# n'a pas permis la coupe) : re-couper ici. Puis DORMIR pour ne PAS finir le
# dockerStartCmd — sinon RunPod redémarre le conteneur en boucle (= re-provisionnement
# 12 min répété). Le cron orchestrateur coupe de toute façon les pods orphelins.
log "idle -> podTerminate (filet) + sleep"
terminate_pod
sleep 3600
log "end"

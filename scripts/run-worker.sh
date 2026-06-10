#!/usr/bin/env bash
# ⚠ LEGACY (mécanisme worker.env pré-prod) — NE PAS UTILISER. En prod, boot.sh
# lance worker.py directement avec les ENV injectés par RunPod (deployWorkerPod).
export HOME=/root
cd /workspace
set -a
. /workspace/worker.env
set +a
exec /usr/bin/python3 -u /workspace/worker.py

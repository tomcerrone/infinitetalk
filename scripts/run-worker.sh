#!/usr/bin/env bash
export HOME=/root
cd /workspace
set -a
. /workspace/worker.env
set +a
exec /usr/bin/python3 -u /workspace/worker.py

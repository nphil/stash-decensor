#!/usr/bin/env bash
# Thin coordinator entrypoint. GPU tools + models live in the compute runner
# container (see lada_entrypoint.sh) — nothing to provision here.
set -uo pipefail

case "${RUN_MODE:-server}" in
    worker) echo "[entrypoint] starting batch worker (tag-driven)"; exec python worker.py ;;
    *)      echo "[entrypoint] starting on-demand HTTP server"; exec python server.py ;;
esac

#!/usr/bin/env bash
# Ensure the DeepMosaics models are present (persisted on the /models volume),
# then start the worker. Real-ESRGAN weights are baked into the image.
#
# DeepMosaics 'clean' mode needs TWO weights side by side in /models:
#   - clean_youknow_video.pth   the mosaic-removal model (MODEL_PATH)
#   - mosaic_position.pth       locates the mosaic. Without it, deepmosaic.py
#                               prompts via input() and a headless container aborts.
set -uo pipefail

MODEL_PATH="${MODEL_PATH:-/models/clean_youknow_video.pth}"
MODEL_DIR="$(dirname "$MODEL_PATH")"
POS_PATH="${MOSAIC_POSITION_MODEL_PATH:-$MODEL_DIR/mosaic_position.pth}"
mkdir -p "$MODEL_DIR"

need_clean=1; [ -f "$MODEL_PATH" ] && need_clean=0
need_pos=1;   [ -f "$POS_PATH" ]   && need_pos=0

if [ "$need_clean" = 1 ] || [ "$need_pos" = 1 ]; then
    echo "[entrypoint] DeepMosaics model(s) missing (clean=$need_clean pos=$need_pos)"

    # Direct-URL overrides first (skip the Drive folder fetch when provided).
    if [ "$need_clean" = 1 ] && [ -n "${DEEPMOSAICS_MODEL_URL:-}" ]; then
        echo "[entrypoint] downloading clean model from DEEPMOSAICS_MODEL_URL"
        wget -q -O "$MODEL_PATH" "$DEEPMOSAICS_MODEL_URL" || { echo "[entrypoint] clean download failed"; rm -f "$MODEL_PATH"; }
        [ -f "$MODEL_PATH" ] && need_clean=0
    fi
    if [ "$need_pos" = 1 ] && [ -n "${MOSAIC_POSITION_MODEL_URL:-}" ]; then
        echo "[entrypoint] downloading position model from MOSAIC_POSITION_MODEL_URL"
        wget -q -O "$POS_PATH" "$MOSAIC_POSITION_MODEL_URL" || { echo "[entrypoint] position download failed"; rm -f "$POS_PATH"; }
        [ -f "$POS_PATH" ] && need_pos=0
    fi

    # Anything still missing: pull the whole DeepMosaics pretrained-models folder
    # once (it contains both files) and move out the two we need.
    if [ "$need_clean" = 1 ] || [ "$need_pos" = 1 ]; then
        echo "[entrypoint] fetching DeepMosaics pretrained models via gdown (first run only)…"
        tmp="$(mktemp -d)"
        if gdown --folder "https://drive.google.com/drive/folders/1LTERcN33McoiztYEwBxMuRjjgxh4DEPs" -O "$tmp"; then
            if [ "$need_clean" = 1 ]; then
                f="$(find "$tmp" -name 'clean_youknow_video.pth' | head -n1)"
                [ -n "$f" ] && mv "$f" "$MODEL_PATH" && need_clean=0
            fi
            if [ "$need_pos" = 1 ]; then
                f="$(find "$tmp" -name 'mosaic_position.pth' | head -n1)"
                [ -n "$f" ] && mv "$f" "$POS_PATH" && need_pos=0
            fi
        fi
        rm -rf "$tmp"
    fi
fi

if [ "${BACKEND:-deepmosaics}" = "deepmosaics" ]; then
    miss=""
    [ ! -f "$MODEL_PATH" ] && miss="$miss $MODEL_PATH"
    [ ! -f "$POS_PATH" ]   && miss="$miss $POS_PATH"
    if [ -n "$miss" ]; then
        echo "[entrypoint] WARNING: DeepMosaics weights still missing:$miss" >&2
        echo "[entrypoint] Google Drive may be rate-limited. Options:" >&2
        echo "[entrypoint]   - set DEEPMOSAICS_MODEL_URL / MOSAIC_POSITION_MODEL_URL to direct links, or" >&2
        echo "[entrypoint]   - drop clean_youknow_video.pth and mosaic_position.pth into the mounted /models volume." >&2
    fi
fi

case "${RUN_MODE:-server}" in
    worker) echo "[entrypoint] starting batch worker (tag-driven)"; exec python worker.py ;;
    *)      echo "[entrypoint] starting on-demand HTTP server"; exec python server.py ;;
esac

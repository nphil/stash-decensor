#!/usr/bin/env bash
# Lada runner entrypoint: fetch model weights once into the (persisted) weights
# dir, then start the HTTP runner. Weights live on a mounted volume so image
# rebuilds don't re-download them.
set -euo pipefail

MW="${LADA_MODEL_WEIGHTS_DIR:-/models}"
mkdir -p "$MW"
HF="https://huggingface.co/ladaapp/lada/resolve/main"

fetch() {  # url filename
  if [ -s "$MW/$2" ]; then
    echo "[lada] have $2"
  else
    echo "[lada] downloading $2 ..."
    curl -fL --retry 3 "$1?download=true" -o "$MW/$2.part" && mv "$MW/$2.part" "$MW/$2"
  fi
}

fetch "$HF/lada_mosaic_detection_model_v4_fast.pt"                 lada_mosaic_detection_model_v4_fast.pt
fetch "$HF/lada_mosaic_detection_model_v4_accurate.pt"            lada_mosaic_detection_model_v4_accurate.pt
fetch "$HF/lada_mosaic_restoration_model_generic_v1.2.pth"       lada_mosaic_restoration_model_generic_v1.2.pth

echo "[lada] starting runner on :${PORT:-8711}"
exec python3 /opt/lada/lada_runner.py

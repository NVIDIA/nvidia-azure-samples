#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DS_DIR="${ROOT_DIR}/DeepStream-Yolo"
IMAGE="${DEEPSTREAM_IMAGE:-nvcr.io/nvidia/deepstream:8.0-samples-multiarch}"
YOLO_MODEL="${YOLO_MODEL:-yolo11s}"
INFER_SIZE="${INFER_SIZE:-640}"
ONNX_OPSET="${ONNX_OPSET:-18}"
YOLO_WEIGHTS_URL="${YOLO_WEIGHTS_URL:-https://github.com/ultralytics/assets/releases/download/v8.4.0/${YOLO_MODEL}.pt}"

docker run --rm \
  --runtime=nvidia \
  --gpus all \
  --network=host \
  --entrypoint /bin/bash \
  -e YOLO_MODEL="${YOLO_MODEL}" \
  -e INFER_SIZE="${INFER_SIZE}" \
  -e ONNX_OPSET="${ONNX_OPSET}" \
  -e YOLO_WEIGHTS_URL="${YOLO_WEIGHTS_URL}" \
  -e YOLO_CONFIG_DIR=/tmp/Ultralytics \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -v "${ROOT_DIR}/.cache/pip:/root/.cache/pip" \
  -v "${DS_DIR}:/workspace" \
  -w /workspace \
  "${IMAGE}" \
  -lc 'set -euo pipefail
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates curl git python3-dev python3-pip python3-venv

    export VENV=/tmp/yolo11-export-venv
    python3 -m venv "${VENV}"
    . "${VENV}/bin/activate"
    python -m pip install --upgrade pip setuptools wheel
    python -m pip install "ultralytics>=8.3.0" onnx onnxslim onnxruntime onnxscript

    weights="${YOLO_MODEL}.pt"
    if [ ! -f "${weights}" ]; then
      curl -L --fail -o "${weights}" "${YOLO_WEIGHTS_URL}"
    fi

    python utils/export_yolo11.py \
      -w "${weights}" \
      -s ${INFER_SIZE} \
      --opset "${ONNX_OPSET}" \
      --dynamic \
      --simplify

    ln -sfn "${YOLO_MODEL}.onnx" model.onnx
    chown -h "${HOST_UID}:${HOST_GID}" "${weights}" "${YOLO_MODEL}.onnx" model.onnx labels.txt
    ls -lh "${weights}" "${YOLO_MODEL}.onnx" model.onnx labels.txt'

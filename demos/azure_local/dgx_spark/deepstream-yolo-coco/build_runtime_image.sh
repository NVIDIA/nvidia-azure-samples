#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="${RUNTIME_IMAGE:-deepstream-yolo-coco:8.0-samples-sbsa}"

docker build \
  -f "${ROOT_DIR}/Dockerfile.runtime" \
  -t "${IMAGE_TAG}" \
  "${ROOT_DIR}"

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DS_DIR="${ROOT_DIR}/DeepStream-Yolo"
IMAGE="${DEEPSTREAM_IMAGE:-nvcr.io/nvidia/deepstream:8.0-samples-multiarch}"
CUDA_VER="${CUDA_VER:-13.0}"
TENSORRT_VERSION="${TENSORRT_VERSION:-10.13.2.6-1+cuda13.0}"

docker run --rm \
  --runtime=nvidia \
  --gpus all \
  --network=host \
  --entrypoint /bin/bash \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -e TENSORRT_VERSION="${TENSORRT_VERSION}" \
  -v "${DS_DIR}:/workspace" \
  -w /workspace \
  "${IMAGE}" \
  -lc "set -euo pipefail
    if ! command -v make >/dev/null || ! command -v g++ >/dev/null || ! command -v /usr/local/cuda-${CUDA_VER}/bin/nvcc >/dev/null || ! test -e /usr/include/aarch64-linux-gnu/NvInfer.h; then
      apt-get update
      cat >/etc/apt/preferences.d/tensorrt-10-13-pin <<EOF
Package: tensorrt* libnvinfer* libnvonnxparsers*
Pin: version \${TENSORRT_VERSION}
Pin-Priority: 1001
EOF
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        make \
        g++ \
        cuda-nvcc-13-0 \
        \"tensorrt-dev=\${TENSORRT_VERSION}\"
    fi
    export CUDA_VER='${CUDA_VER}'
    make -C nvdsinfer_custom_impl_Yolo clean
    make -C nvdsinfer_custom_impl_Yolo
    chown -R \"\${HOST_UID}:\${HOST_GID}\" nvdsinfer_custom_impl_Yolo"

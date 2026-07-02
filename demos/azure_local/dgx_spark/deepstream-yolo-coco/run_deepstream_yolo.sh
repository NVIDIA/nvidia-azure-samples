#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DS_DIR="${ROOT_DIR}/DeepStream-Yolo"
DEFAULT_IMAGE="deepstream-yolo-coco:8.0-samples-sbsa"
if ! docker image inspect "${DEFAULT_IMAGE}" >/dev/null 2>&1; then
  DEFAULT_IMAGE="nvcr.io/nvidia/deepstream:8.0-samples-multiarch"
fi
IMAGE="${DEEPSTREAM_IMAGE:-${DEFAULT_IMAGE}}"
MODE="${1:-headless}"
SOURCE_URI="${2:-}"

case "${MODE}" in
  headless)
    CONFIG="deepstream_app_config_person_animals_headless.txt"
    ;;
  display)
    CONFIG="deepstream_app_config_person_animals_display.txt"
    ;;
  *)
    echo "Usage: $0 [headless|display] [source-uri]" >&2
    exit 2
    ;;
esac

RUN_CONFIG="${CONFIG}"
if [ -n "${SOURCE_URI}" ]; then
  RUN_CONFIG=".runtime_${CONFIG}"
  sed "s#^uri=.*#uri=${SOURCE_URI}#" "${DS_DIR}/${CONFIG}" > "${DS_DIR}/${RUN_CONFIG}"
fi

DISPLAY_ARGS=()
if [ "${MODE}" = "display" ] && [ -n "${DISPLAY:-}" ]; then
  DISPLAY_ARGS=(-e DISPLAY="${DISPLAY}" -v /tmp/.X11-unix:/tmp/.X11-unix:rw)
fi

EXTRA_LIB_ARGS=()
EXTRA_LD_PATH=""
if [ -d "${ROOT_DIR}/runtime-libs/deepstream" ]; then
  EXTRA_LIB_ARGS=(-v "${ROOT_DIR}/runtime-libs/deepstream:/opt/ds-sbsa-libs:ro")
  EXTRA_LD_PATH="/opt/ds-sbsa-libs:"
fi

TTY_ARGS=()
if [ -t 0 ] && [ -t 1 ]; then
  TTY_ARGS=(-it)
fi

docker run --rm \
  "${TTY_ARGS[@]}" \
  --runtime=nvidia \
  --gpus all \
  --network=host \
  --privileged \
  --entrypoint /bin/bash \
  -e LD_LIBRARY_PATH="/opt/ds-codec-libs:${EXTRA_LD_PATH}/opt/nvidia/deepstream/deepstream/lib:/opt/nvidia/deepstream/deepstream-8.0/lib:/usr/lib/aarch64-linux-gnu/tegra:/usr/local/nvidia/lib:/usr/local/nvidia/lib64:/usr/local/cuda/lib64:/usr/local/cuda-13.0/lib64" \
  -e GST_PLUGIN_PATH="/usr/lib/aarch64-linux-gnu/gstreamer-1.0:/usr/lib/aarch64-linux-gnu/gstreamer-1.0/deepstream" \
  -e RUN_CONFIG="${RUN_CONFIG}" \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  "${DISPLAY_ARGS[@]}" \
  "${EXTRA_LIB_ARGS[@]}" \
  -v "${DS_DIR}:/workspace" \
  -w /workspace \
  "${IMAGE}" \
  -lc 'deepstream-app -c "${RUN_CONFIG}"
status=$?
chown -h "${HOST_UID}:${HOST_GID}" /workspace/*.engine 2>/dev/null || true
exit "${status}"'

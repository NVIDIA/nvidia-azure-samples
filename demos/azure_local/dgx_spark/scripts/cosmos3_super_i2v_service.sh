#!/usr/bin/env bash
# SPDX-License-Identifier: MIT

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CONTAINER_NAME="${COSMOS3_I2V_CONTAINER_NAME:-cosmos3-super-i2v}"
IMAGE="${COSMOS3_I2V_IMAGE:-vllm/vllm-omni:cosmos3}"
MODEL="${COSMOS3_I2V_MODEL:-nvidia/Cosmos3-Super-Image2Video}"
HOST="${COSMOS3_I2V_HOST:-127.0.0.1}"
HOST_PORT="${COSMOS3_I2V_PORT:-30000}"
CONTAINER_PORT="${COSMOS3_I2V_CONTAINER_PORT:-8000}"
HF_CACHE="${COSMOS3_I2V_HF_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}}"
WORKSPACE="${COSMOS3_I2V_WORKSPACE:-$PROJECT_ROOT}"
INIT_TIMEOUT="${COSMOS3_I2V_INIT_TIMEOUT:-3600}"
DEPLOY_CONFIG="${COSMOS3_I2V_DEPLOY_CONFIG:-/workspace/config/cosmos3_i2v_deploy.yaml}"

default_args=(
  --omni
  --host 0.0.0.0
  --port "$CONTAINER_PORT"
  --model-class-name Cosmos3OmniDiffusersPipeline
  --deploy-config "$DEPLOY_CONFIG"
  --allowed-local-media-path /workspace
  --enable-layerwise-offload
  --quantization fp8
  --init-timeout "$INIT_TIMEOUT"
)

if [[ -n "${COSMOS3_I2V_EXTRA_ARGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra_args=($COSMOS3_I2V_EXTRA_ARGS)
else
  extra_args=()
fi

docker_base_args=(
  --name "$CONTAINER_NAME"
  --runtime nvidia
  --gpus all
  --ipc host
  --shm-size 16g
  --ulimit memlock=-1
  --ulimit stack=67108864
  -p "${HOST}:${HOST_PORT}:${CONTAINER_PORT}"
  -v "${HF_CACHE}:/root/.cache/huggingface"
  -v "${WORKSPACE}:/workspace"
  -e HF_HOME=/root/.cache/huggingface
  -w /workspace
)

if [[ -n "${HF_TOKEN:-}" ]]; then
  docker_base_args+=(-e HF_TOKEN="$HF_TOKEN")
fi

usage() {
  cat <<EOF
Usage: $0 {pull|start|run|stop|restart|status|logs|models}

Environment:
  COSMOS3_I2V_PORT         Host API port. Default: 30000
  COSMOS3_I2V_MODEL        HF model id. Default: nvidia/Cosmos3-Super-Image2Video
  COSMOS3_I2V_DEPLOY_CONFIG
                           Container path to deploy YAML. Default: /workspace/config/cosmos3_i2v_deploy.yaml
  COSMOS3_I2V_EXTRA_ARGS   Extra vLLM args appended to the default command.
EOF
}

pull_image() {
  docker pull --platform linux/arm64 "$IMAGE"
}

remove_existing() {
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
    docker rm -f "$CONTAINER_NAME" >/dev/null
  fi
}

run_detached() {
  remove_existing
  docker run -d --restart unless-stopped \
    "${docker_base_args[@]}" \
    "$IMAGE" \
    vllm-omni serve "$MODEL" "${default_args[@]}" "${extra_args[@]}"
}

run_foreground() {
  remove_existing
  docker run --rm \
    "${docker_base_args[@]}" \
    "$IMAGE" \
    vllm-omni serve "$MODEL" "${default_args[@]}" "${extra_args[@]}"
}

case "${1:-}" in
  pull)
    pull_image
    ;;
  start)
    pull_image
    run_detached
    ;;
  run)
    pull_image
    run_foreground
    ;;
  stop)
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    docker ps -a --filter "name=^/${CONTAINER_NAME}$" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}'
    ;;
  logs)
    docker logs -f "$CONTAINER_NAME"
    ;;
  models)
    curl -fsS "http://${HOST}:${HOST_PORT}/v1/models"
    ;;
  *)
    usage
    exit 2
    ;;
esac

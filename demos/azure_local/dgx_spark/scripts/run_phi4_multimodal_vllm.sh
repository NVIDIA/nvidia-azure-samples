#!/usr/bin/env bash
set -euo pipefail

# Local DGX Spark profile for Microsoft Phi-4 Multimodal. Speech and vision are
# official checkpoint LoRAs; the base model remains available for text turns.
WEIGHTS="${WEIGHTS:-/home/anslutsky/Dev/NIMs/Weights/Phi-4-multimodal-instruct}"
PORT="${PORT:-8011}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-phi4_multimodal}"
CONTAINER_NAME="${CONTAINER_NAME:-dgx-spark-phi4-multimodal}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.20.0}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.50}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"

for required in config.json model.safetensors.index.json speech-lora/adapter_config.json vision-lora/adapter_config.json; do
  if [[ ! -f "${WEIGHTS}/${required}" ]]; then
    echo "Phi-4 Multimodal checkpoint is incomplete: missing ${WEIGHTS}/${required}" >&2
    exit 1
  fi
done

cleanup() {
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
cleanup

docker run --rm \
  --name "${CONTAINER_NAME}" \
  --gpus all \
  --ipc=host \
  --shm-size=16g \
  -p "127.0.0.1:${PORT}:8000" \
  -v "${WEIGHTS}:/model:ro" \
  --entrypoint /bin/bash \
  "${VLLM_IMAGE}" -lc \
  "python3 -m pip install -q 'vllm[audio]' && exec vllm serve /model \
    --served-model-name=${SERVED_MODEL_NAME} \
    --host=0.0.0.0 \
    --port=8000 \
    --dtype=auto \
    --trust-remote-code \
    --max-model-len=${MAX_MODEL_LEN} \
    --max-num-seqs=4 \
    --gpu-memory-utilization=${GPU_MEMORY_UTILIZATION} \
    --enable-lora \
    --max-lora-rank=320 \
    --max-loras=2 \
    --lora-modules speech=/model/speech-lora vision=/model/vision-lora \
    --limit-mm-per-prompt='{\"audio\":3,\"image\":3}' \
    --allowed-local-media-path=/ \
    --enable-prefix-caching"

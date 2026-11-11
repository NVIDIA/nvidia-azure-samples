#!/usr/bin/env bash
set -euo pipefail

# Official NVIDIA DGX Spark serving profile for Nemotron 3 Nano Omni NVFP4.
# The background-stack supervisor owns this wrapper; keeping docker attached
# makes its PID and logs follow the rest of the stack lifecycle.

WEIGHTS="${WEIGHTS:-/home/anslutsky/Dev/NIMs/Weights/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4}"
PORT="${PORT:-8010}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-nemotron_3_nano_omni}"
CONTAINER_NAME="${CONTAINER_NAME:-dgx-spark-nemotron-omni}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.20.0}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-4}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.58}"
MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-8192}"

if [[ ! -f "${WEIGHTS}/config.json" || ! -f "${WEIGHTS}/model.safetensors.index.json" ]]; then
  echo "Nemotron Omni weights are incomplete: ${WEIGHTS}" >&2
  exit 1
fi

cleanup() {
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# Spark previously pinned the 86 GB text model indefinitely. The small Ollama
# daemon remains available for nemotron-mini tool planning.
if command -v ollama >/dev/null 2>&1; then
  ollama stop nemotron-spark:latest >/dev/null 2>&1 || true
  ollama stop nemotron3-voice-fast:latest >/dev/null 2>&1 || true
fi

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
    --max-num-seqs=${MAX_NUM_SEQS} \
    --max-model-len=${MAX_MODEL_LEN} \
    --trust-remote-code \
    --gpu-memory-utilization=${GPU_MEMORY_UTILIZATION} \
    --limit-mm-per-prompt='{\"video\":1,\"image\":1,\"audio\":1}' \
    --media-io-kwargs='{\"video\":{\"fps\":2,\"num_frames\":256}}' \
    --allowed-local-media-path=/ \
    --enable-prefix-caching \
    --max-num-batched-tokens=${MAX_NUM_BATCHED_TOKENS} \
    --video-pruning-rate=0.5 \
    --kv-cache-dtype=fp8 \
    --reasoning-parser=nemotron_v3 \
    --enable-auto-tool-choice \
    --tool-call-parser=qwen3_coder"

#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${HF_TOKEN:-}" ]]; then
  echo "Set HF_TOKEN before running." >&2
  exit 1
fi

source .venv/bin/activate
export HF_HOME="$ROOT/.cache/huggingface"

python scripts/accept_hf_licenses.py

python examples/inference.py \
  -i assets/youtube_rainy_europe/desert_highway_spec.json \
  -o outputs/desert_highway \
  --disable-guardrails \
  --offload-guardrail-models

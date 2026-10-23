#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/.lab-state"

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" port-forward \
  --address 127.0.0.1 svc/aks-ops-mcp 8443:8443 \
  >"${STATE_DIR}/mcp-port-forward.log" 2>&1 &
echo $! > "${STATE_DIR}/mcp-port-forward.pid"

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" port-forward \
  --address 127.0.0.1 svc/mock-inference 8000:8000 \
  >"${STATE_DIR}/inference-port-forward.log" 2>&1 &
echo $! > "${STATE_DIR}/inference-port-forward.pid"

for _ in $(seq 1 30); do
  if curl --silent --fail --cacert "${STATE_DIR}/ca.crt" \
      "https://127.0.0.1:8443/healthz" >/dev/null && \
     curl --silent --fail "http://127.0.0.1:8000/healthz" >/dev/null; then
    echo "Port-forwards ready."
    exit 0
  fi
  sleep 1
done

echo "Port-forwards did not become ready." >&2
exit 1

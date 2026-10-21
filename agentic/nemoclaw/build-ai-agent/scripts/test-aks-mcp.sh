#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/.lab-state"
mkdir -p "${ROOT_DIR}/artifacts"

if curl --silent --fail --cacert "${STATE_DIR}/ca.crt" \
  https://127.0.0.1:8443/mcp >/dev/null 2>&1; then
  echo "Unauthenticated MCP request unexpectedly succeeded." >&2
  exit 1
fi

python3 "${ROOT_DIR}/tests/mcp_smoke.py" \
  --ca "${STATE_DIR}/ca.crt" \
  --token-file "${STATE_DIR}/mcp-token" \
  read | tee "${ROOT_DIR}/artifacts/read-before.json"

python3 "${ROOT_DIR}/tests/mcp_smoke.py" \
  --ca "${STATE_DIR}/ca.crt" \
  --token-file "${STATE_DIR}/mcp-token" \
  restart | tee "${ROOT_DIR}/artifacts/restart.json"

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" rollout status \
  deployment/service-api --timeout=120s

python3 "${ROOT_DIR}/tests/mcp_smoke.py" \
  --ca "${STATE_DIR}/ca.crt" \
  --token-file "${STATE_DIR}/mcp-token" \
  verify | tee "${ROOT_DIR}/artifacts/verify-after.json"

python3 "${ROOT_DIR}/tests/mcp_smoke.py" \
  --ca "${STATE_DIR}/ca.crt" \
  --token-file "${STATE_DIR}/mcp-token" \
  guardrails | tee "${ROOT_DIR}/artifacts/guardrails.json"

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" get all -o wide \
  > "${ROOT_DIR}/artifacts/kubernetes-resources.txt"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" logs deployment/service-api \
  > "${ROOT_DIR}/artifacts/service-api.log"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" logs deployment/aks-ops-mcp \
  > "${ROOT_DIR}/artifacts/aks-ops-mcp.log"

echo "AKS and MCP test completed. Evidence: ${ROOT_DIR}/artifacts"

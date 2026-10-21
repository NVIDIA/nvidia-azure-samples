#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${ROOT_DIR}/.lab-state" ]]; then
  source "${ROOT_DIR}/.lab-state"
  for name in mcp-port-forward inference-port-forward; do
    pid_file="${STATE_DIR}/${name}.pid"
    if [[ -f "${pid_file}" ]]; then
      kill "$(<"${pid_file}")" 2>/dev/null || true
    fi
  done
  kubectl --context "${CONTEXT}" delete namespace "${NAMESPACE}" \
    --wait=true --timeout=180s
  echo "Sensitive temporary state remains at ${STATE_DIR}. Remove it after"
  echo "the NemoClaw managed MCP registration has been removed or destroyed."
else
  echo "No .lab-state file found; nothing to clean."
fi

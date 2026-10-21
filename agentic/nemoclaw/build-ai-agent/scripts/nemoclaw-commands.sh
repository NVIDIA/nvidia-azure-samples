#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/.lab-state"

cat <<EOF
# Run after Docker Desktop is started.
# Install and onboard against the deterministic AKS mock endpoint:
curl -fsSL https://www.nvidia.com/nemoclaw.sh | \\
  NEMOCLAW_NON_INTERACTIVE=1 \\
  NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 \\
  NEMOCLAW_AGENT=openclaw \\
  NEMOCLAW_PROVIDER=custom \\
  NEMOCLAW_ENDPOINT_URL=http://127.0.0.1:8000/v1 \\
  NEMOCLAW_MODEL=ignite-mock-model \\
  NEMOCLAW_PREFERRED_API=openai-completions \\
  NEMOCLAW_COMPATIBLE_AUTH_MODE=none \\
  NEMOCLAW_CORPORATE_CA_BUNDLE=${STATE_DIR}/ca.crt \\
  NEMOCLAW_SANDBOX_NAME=ignite-agent \\
  bash

# Register the private AKS MCP endpoint without printing the token:
export AKS_OPS_MCP_TOKEN="\$(<${STATE_DIR}/mcp-token)"
nemoclaw ignite-agent mcp add aks-ops \\
  --url https://${MCP_HOST_IP}:8443/mcp \\
  --env AKS_OPS_MCP_TOKEN \\
  --trusted-private-host ${MCP_HOST_IP} \\
  --deny-tool restart_deployment \\
  --deny-tool scale_deployment
unset AKS_OPS_MCP_TOKEN

nemoclaw ignite-agent mcp status aks-ops --json

# Explicitly approve restart while retaining the scaling denial:
nemoclaw ignite-agent mcp update aks-ops --deny-tool scale_deployment
nemoclaw ignite-agent mcp status aks-ops --json
EOF

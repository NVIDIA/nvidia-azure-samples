#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

TARGET_USER=${TARGET_USER:-labuser}
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
CA_FILE="${TARGET_HOME}/.config/ignite-nemoclaw/ca.crt"
NEMOCLAW_PATH="${TARGET_HOME}/.local/bin:${TARGET_HOME}/.nvm/versions/node/v22.23.2/bin:/usr/local/bin:/usr/bin:/bin"

test -s "${CA_FILE}"
: "${AKS_OPS_MCP_TOKEN:?AKS_OPS_MCP_TOKEN is required}"
: "${MCP_ENDPOINT_URL:?MCP_ENDPOINT_URL is required}"

sudo -H -u "${TARGET_USER}" env \
  HOME="${TARGET_HOME}" \
  PATH="${NEMOCLAW_PATH}" \
  CURL_CA_BUNDLE="${CA_FILE}" \
  SSL_CERT_FILE="${CA_FILE}" \
  NODE_EXTRA_CA_CERTS="${CA_FILE}" \
  AKS_OPS_MCP_TOKEN="${AKS_OPS_MCP_TOKEN}" \
  nemoclaw ignite-agent mcp add aks-ops \
    --url "${MCP_ENDPOINT_URL}" \
    --env AKS_OPS_MCP_TOKEN \
    --deny-tool restart_deployment \
    --deny-tool scale_deployment

sudo -H -u "${TARGET_USER}" env \
  HOME="${TARGET_HOME}" \
  PATH="${NEMOCLAW_PATH}" \
  nemoclaw ignite-agent mcp status aks-ops --json

#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

TARGET_USER=${TARGET_USER:-labuser}
TARGET_GROUP="$(id -gn "${TARGET_USER}")"
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
CA_DIR="${TARGET_HOME}/.config/ignite-nemoclaw"
: "${INFERENCE_ENDPOINT_URL:?INFERENCE_ENDPOINT_URL is required}"
install -d -m 700 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${CA_DIR}"
printf '%s' "${LAB_CA_B64:?LAB_CA_B64 is required}" | base64 -d > "${CA_DIR}/ca.crt"
chmod 644 "${CA_DIR}/ca.crt"
chown "${TARGET_USER}:${TARGET_GROUP}" "${CA_DIR}/ca.crt"

sudo -H -u "${TARGET_USER}" env \
  CURL_CA_BUNDLE="${CA_DIR}/ca.crt" \
  SSL_CERT_FILE="${CA_DIR}/ca.crt" \
  NODE_EXTRA_CA_CERTS="${CA_DIR}/ca.crt" \
  NEMOCLAW_NON_INTERACTIVE=1 \
  NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 \
  NEMOCLAW_NO_EXPRESS=1 \
  NEMOCLAW_AGENT=openclaw \
  NEMOCLAW_PROVIDER=custom \
  NEMOCLAW_ENDPOINT_URL="${INFERENCE_ENDPOINT_URL}" \
  NEMOCLAW_MODEL=ignite-mock-model \
  NEMOCLAW_PREFERRED_API=openai-completions \
  NEMOCLAW_CORPORATE_CA_BUNDLE="${CA_DIR}/ca.crt" \
  NEMOCLAW_SANDBOX_NAME=ignite-agent \
  COMPATIBLE_API_KEY="${COMPATIBLE_API_KEY:?COMPATIBLE_API_KEY is required}" \
  bash -c 'curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash'

sudo -H -u "${TARGET_USER}" bash -lc '
  printf "nemoclaw_path="
  command -v nemoclaw
  printf "nemoclaw_version="
  nemoclaw --version
  nemoclaw ignite-agent status
'

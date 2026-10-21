#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/.lab-state"
mkdir -p "${ROOT_DIR}/artifacts"

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" delete job \
  mcp-incluster-smoke --ignore-not-found=true --wait=true
kubectl --context "${CONTEXT}" apply -f "${ROOT_DIR}/k8s/mcp-smoke-job.yaml"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" wait \
  --for=condition=complete job/mcp-incluster-smoke --timeout=120s
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" logs job/mcp-incluster-smoke | \
  tee "${ROOT_DIR}/artifacts/incluster-mcp-smoke.json"

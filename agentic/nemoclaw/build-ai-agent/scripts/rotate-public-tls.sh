#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <mcp-public-ip> <inference-public-ip>" >&2
  exit 2
fi

MCP_PUBLIC_IP="$1"
INFERENCE_PUBLIC_IP="$2"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${ROOT_DIR}/.lab-state"

openssl req -x509 -newkey rsa:3072 -nodes -days 2 \
  -subj "/CN=Ignite NemoClaw E2E CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -keyout "${STATE_DIR}/ca.key" \
  -out "${STATE_DIR}/ca.crt" >/dev/null 2>&1

openssl req -newkey rsa:3072 -nodes \
  -subj "/CN=${MCP_PUBLIC_IP}" \
  -keyout "${STATE_DIR}/tls.key" \
  -out "${STATE_DIR}/tls.csr" >/dev/null 2>&1

printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectAltName=IP:%s,IP:%s,IP:127.0.0.1,DNS:aks-ops-mcp,DNS:aks-ops-mcp.nooa-admission-test,DNS:aks-ops-mcp.nooa-admission-test.svc,DNS:aks-ops-mcp.nooa-admission-test.svc.cluster.local,DNS:mock-inference,DNS:mock-inference.nooa-admission-test,DNS:mock-inference.nooa-admission-test.svc,DNS:mock-inference.nooa-admission-test.svc.cluster.local\nextendedKeyUsage=serverAuth\n' \
  "${MCP_PUBLIC_IP}" "${INFERENCE_PUBLIC_IP}" > "${STATE_DIR}/tls.ext"

openssl x509 -req -days 2 -sha256 \
  -in "${STATE_DIR}/tls.csr" \
  -CA "${STATE_DIR}/ca.crt" \
  -CAkey "${STATE_DIR}/ca.key" \
  -CAcreateserial \
  -extfile "${STATE_DIR}/tls.ext" \
  -out "${STATE_DIR}/tls.crt" >/dev/null 2>&1

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create secret tls \
  aks-ops-mcp-tls --cert="${STATE_DIR}/tls.crt" \
  --key="${STATE_DIR}/tls.key" --dry-run=client -o yaml | \
  kubectl --context "${CONTEXT}" apply -f -

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap \
  aks-ops-mcp-ca --from-file=ca.crt="${STATE_DIR}/ca.crt" \
  --dry-run=client -o yaml | kubectl --context "${CONTEXT}" apply -f -

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap \
  lab-services-code --from-file=lab_services.py="${ROOT_DIR}/apps/lab_services.py" \
  --dry-run=client -o yaml | kubectl --context "${CONTEXT}" apply -f -

kubectl --context "${CONTEXT}" apply -f "${ROOT_DIR}/k8s/workloads.yaml"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" rollout restart \
  deployment/aks-ops-mcp deployment/mock-inference
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" rollout status \
  deployment/aks-ops-mcp --timeout=120s
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" rollout status \
  deployment/mock-inference --timeout=120s

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" patch service \
  aks-ops-mcp-public --type=json \
  -p '[{"op":"replace","path":"/spec/selector","value":{"app.kubernetes.io/name":"aks-ops-mcp"}}]'
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" patch service \
  mock-inference-public --type=json \
  -p '[{"op":"replace","path":"/spec/selector","value":{"app.kubernetes.io/name":"mock-inference"}}]'

openssl verify -CAfile "${STATE_DIR}/ca.crt" "${STATE_DIR}/tls.crt"
echo "MCP endpoint: https://${MCP_PUBLIC_IP}:8443/mcp"
echo "Inference endpoint: https://${INFERENCE_PUBLIC_IP}:8000/v1"

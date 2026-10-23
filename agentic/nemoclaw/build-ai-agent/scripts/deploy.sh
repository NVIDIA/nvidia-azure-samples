#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

CONTEXT="aks-dynamo-a100"
NAMESPACE="nooa-admission-test"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="$(date -u +%Y%m%dt%H%M%Sz)"
STATE_DIR="/private/tmp/ignite-nemoclaw-${RUN_ID}"

if [[ "$(kubectl config current-context)" != "${CONTEXT}" ]]; then
  echo "Expected kubectl context ${CONTEXT}" >&2
  exit 1
fi

if kubectl --context "${CONTEXT}" get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  echo "Namespace ${NAMESPACE} already exists; clean it before deploying." >&2
  exit 1
fi

MCP_HOST_IP="${MCP_HOST_IP:-$(ipconfig getifaddr en0 2>/dev/null || \
  ifconfig en0 2>/dev/null | awk '/inet /{print $2; exit}' || true)}"
if [[ -z "${MCP_HOST_IP}" ]]; then
  echo "Set MCP_HOST_IP to a routed private IPv4 address on this host." >&2
  exit 1
fi

mkdir -p "${STATE_DIR}"
chmod 700 "${STATE_DIR}"

openssl rand -hex 32 > "${STATE_DIR}/mcp-token"
chmod 600 "${STATE_DIR}/mcp-token"

openssl req -x509 -newkey rsa:3072 -nodes -days 7 \
  -subj "/CN=Ignite NemoClaw Lab CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" \
  -keyout "${STATE_DIR}/ca.key" \
  -out "${STATE_DIR}/ca.crt" >/dev/null 2>&1
openssl req -newkey rsa:3072 -nodes \
  -subj "/CN=aks-ops-mcp.nooa-admission-test.svc" \
  -keyout "${STATE_DIR}/tls.key" \
  -out "${STATE_DIR}/tls.csr" >/dev/null 2>&1
printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectAltName=IP:%s,IP:127.0.0.1,DNS:aks-ops-mcp,DNS:aks-ops-mcp.nooa-admission-test,DNS:aks-ops-mcp.nooa-admission-test.svc,DNS:aks-ops-mcp.nooa-admission-test.svc.cluster.local\nextendedKeyUsage=serverAuth\n' \
  "${MCP_HOST_IP}" > "${STATE_DIR}/tls.ext"
openssl x509 -req -days 7 -sha256 \
  -in "${STATE_DIR}/tls.csr" \
  -CA "${STATE_DIR}/ca.crt" \
  -CAkey "${STATE_DIR}/ca.key" \
  -CAcreateserial \
  -extfile "${STATE_DIR}/tls.ext" \
  -out "${STATE_DIR}/tls.crt" >/dev/null 2>&1

kubectl --context "${CONTEXT}" apply -f "${ROOT_DIR}/k8s/namespace-guardrails.yaml"

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap lab-services-code \
  --from-file=lab_services.py="${ROOT_DIR}/apps/lab_services.py"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap lab-mcp-client-code \
  --from-file=mcp_smoke.py="${ROOT_DIR}/tests/mcp_smoke.py"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap aks-ops-mcp-ca \
  --from-file=ca.crt="${STATE_DIR}/ca.crt"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap service-config \
  --from-literal=DEPENDENCY_URL=http://dependency-api-invalid:8081
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create secret generic aks-ops-mcp-auth \
  --from-file=token="${STATE_DIR}/mcp-token"
kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create secret tls aks-ops-mcp-tls \
  --cert="${STATE_DIR}/tls.crt" --key="${STATE_DIR}/tls.key"

kubectl --context "${CONTEXT}" apply -f "${ROOT_DIR}/k8s/rbac.yaml"
kubectl --context "${CONTEXT}" apply -f "${ROOT_DIR}/k8s/workloads.yaml"

for deployment in dependency-api service-api aks-ops-mcp mock-inference; do
  kubectl --context "${CONTEXT}" -n "${NAMESPACE}" rollout status \
    "deployment/${deployment}" --timeout=240s
done

kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create configmap service-config \
  --from-literal=DEPENDENCY_URL=http://dependency-api:8081 \
  --dry-run=client -o yaml | \
  kubectl --context "${CONTEXT}" apply -f -

cat > "${ROOT_DIR}/.lab-state" <<EOF
CONTEXT=${CONTEXT}
NAMESPACE=${NAMESPACE}
RUN_ID=${RUN_ID}
STATE_DIR=${STATE_DIR}
MCP_HOST_IP=${MCP_HOST_IP}
EOF
chmod 600 "${ROOT_DIR}/.lab-state"

echo "Lab deployed with an intentionally stale service-api pod."
echo "State: ${ROOT_DIR}/.lab-state"
echo "Public CA: ${STATE_DIR}/ca.crt"
echo "Protected MCP token: ${STATE_DIR}/mcp-token"
echo "MCP URL after port-forward: https://${MCP_HOST_IP}:8443/mcp"

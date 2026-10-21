# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Render the event fixture without YAML dependencies or a mock model."""
import json
import os
from pathlib import Path

IMAGE = "python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"


def render(context, secrets, root):
    ns = context["labNamespace"]
    def resource(kind, name, **fields):
        api = {"Deployment": "apps/v1", "Job": "batch/v1",
               "Role": "rbac.authorization.k8s.io/v1", "RoleBinding": "rbac.authorization.k8s.io/v1"}.get(kind, "v1")
        return dict(apiVersion=api, kind=kind, metadata={"name": name, "namespace": ns}, **fields)
    def cm(name, data):
        return resource("ConfigMap", name, data=data)
    def volume(name, source, kind="secret"):
        return {"name": name, kind: {"name" if kind == "configMap" else "secretName": source}}
    def mount(name, path):
        return {"name": name, "mountPath": path, "readOnly": True}
    def container(name, script, port, env=None, mounts=None, scheme="HTTP", readiness="/healthz"):
        return {"name": name, "image": IMAGE, "command": ["python3", "/app/" + script],
            "env": env or [], "ports": [{"name": name[:15], "containerPort": port}],
            "readinessProbe": {"httpGet": {"path": readiness, "port": port, "scheme": scheme},
                               "timeoutSeconds": 5, "periodSeconds": 5},
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"},
                          "limits": {"cpu": "250m", "memory": "256Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [mount("code", "/app"), {"name": "tmp", "mountPath": "/tmp"}] + (mounts or [])}
    def env(name, value):
        return {"name": name, "value": str(value)}
    def deployment(name, containers, volumes=None):
        labels = {"app.kubernetes.io/name": name}
        pod = {"automountServiceAccountToken": False, "containers": containers,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532, "fsGroup": 65532},
            "volumes": [volume("code", "lab-services-code", "configMap"),
                        {"name": "tmp", "emptyDir": {"sizeLimit": "32Mi"}}] + (volumes or [])}
        if name == "aks-ops-mcp":
            pod["serviceAccountName"] = "aks-ops-mcp"
        return resource("Deployment", name, spec={"replicas": 1, "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": pod}})
    def service(name, port):
        return resource("Service", name, spec={"selector": {"app.kubernetes.io/name": name},
            "ports": [{"port": port, "targetPort": port}]})
    root = Path(root)
    items = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}]
    items += [resource("LimitRange", "lab-defaults", spec={"limits": [{"type": "Container",
        "defaultRequest": {"cpu": "25m", "memory": "32Mi"}, "default": {"cpu": "250m", "memory": "256Mi"},
        "min": {"cpu": "10m", "memory": "16Mi"}, "max": {"cpu": "1", "memory": "1Gi"}}]}),
        resource("ResourceQuota", "lab-budget", spec={"hard": {
        "requests.cpu": "2", "requests.memory": "2Gi", "limits.cpu": "4", "limits.memory": "4Gi",
        "pods": "16", "services": "6", "configmaps": "12", "secrets": "8"}}),
        resource("ServiceAccount", "aks-ops-mcp", automountServiceAccountToken=False),
        resource("Role", "aks-ops-mcp", rules=[
            {"apiGroups": [""], "resources": ["pods", "pods/log", "events", "configmaps", "services"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["get", "list", "watch"]},
            {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": ["service-api"], "verbs": ["patch"]}]),
        resource("RoleBinding", "aks-ops-mcp", subjects=[{"kind": "ServiceAccount", "name": "aks-ops-mcp", "namespace": ns}],
            roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "aks-ops-mcp"}),
        cm("lab-services-code", {"lab_services.py": (root / "apps/lab_services.py").read_text(),
                                 "inference_broker.py": (root / "infra/setup/inference_broker.py").read_text(),
                                 "check_cluster.py": (root / "infra/setup/check_cluster.py").read_text()}),
        cm("lab-context", {"context.json": json.dumps(context)}),
        cm("service-config", {"DEPENDENCY_URL": "http://dependency-api-invalid:8081"})]
    for name, data in secrets.items():
        # Apply never logs values; base64 encoding would not provide protection.
        items.append(resource("Secret", name, type="Opaque", stringData=data))
    items += [deployment("dependency-api", [container("dependency-api", "lab_services.py", 8081, [env("MODE", "dependency")])]),
              service("dependency-api", 8081)]
    service_env = [env("MODE", "service"), {"name": "DEPENDENCY_URL", "valueFrom": {
        "configMapKeyRef": {"name": "service-config", "key": "DEPENDENCY_URL"}}}]
    items += [deployment("service-api", [container("service-api", "lab_services.py", 8080, service_env, readiness="/debug/config")]), service("service-api", 8080)]
    kube_token = {"name": "kube-token", "projected": {"sources": [
        {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}},
        {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
        {"downwardAPI": {"items": [{"path": "namespace", "fieldRef": {"fieldPath": "metadata.namespace"}}]}}]}}
    tls = mount("tls", "/var/run/lab-tls")
    mcp = container("aks-ops-mcp", "lab_services.py", 8443, [env("MODE", "mcp")],
        [tls, mount("mcp-auth", "/var/run/lab-auth"), mount("kube-token", "/var/run/secrets/kubernetes.io/serviceaccount")], scheme="HTTPS")
    broker = container("model-broker", "inference_broker.py", 8000, [
        env("INFERENCE_ENDPOINT", context["upstreamInferenceEndpoint"]), env("INFERENCE_MODEL", context["inferenceModel"]),
        env("INFERENCE_AUTH_HEADER", context["inferenceAuthHeader"])],
        [tls, mount("broker-auth", "/var/run/broker-auth"), mount("upstream", "/var/run/upstream")], scheme="HTTPS")
    items.append(deployment("aks-ops-mcp", [mcp, broker], [volume("tls", "aks-ops-mcp-tls"),
        volume("mcp-auth", "aks-ops-mcp-auth"), volume("broker-auth", "model-broker-auth"), volume("upstream", "model-upstream"), kube_token]))
    lb = resource("Service", "aks-ops-mcp", spec={"type": "LoadBalancer",
        "selector": {"app.kubernetes.io/name": "aks-ops-mcp"}, "loadBalancerSourceRanges": [context["vmPrivateIp"] + "/32"],
        "ports": [{"name": "mcp", "port": 8443, "targetPort": 8443}, {"name": "inference", "port": 8000, "targetPort": 8000}]})
    lb["metadata"]["annotations"] = {"service.beta.kubernetes.io/azure-load-balancer-internal": "true",
        "service.beta.kubernetes.io/azure-load-balancer-internal-subnet": "lab-services",
        "service.beta.kubernetes.io/azure-load-balancer-ipv4": context["mcpPrivateIp"]}
    items.append(lb)
    return {"apiVersion": "v1", "kind": "List", "items": items}


def readiness_job(context):
    # ClusterIP avoids the private LB's deliberate VM-only source restriction.
    ns = context["labNamespace"]
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "lab-readiness", "namespace": ns},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": 240, "ttlSecondsAfterFinished": 600,
            "template": {"spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                "containers": [{"name": "check", "image": IMAGE, "command": ["python3", "/app/check_cluster.py"],
                    "env": [{"name": "MCP_HOST", "value": "aks-ops-mcp"}],
                    "resources": {"requests": {"cpu": "25m", "memory": "32Mi"}, "limits": {"cpu": "150m", "memory": "128Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": name, "mountPath": path, "readOnly": True} for name, path in (
                        ("code", "/app"), ("context", "/etc/ignite-lab"), ("ca", "/var/run/lab-ca"),
                        ("mcp-auth", "/var/run/lab-auth"), ("broker-auth", "/var/run/broker-auth"))]}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": 65532, "runAsGroup": 65532},
                "volumes": [{"name": name, kind: {key: source}} for name, kind, key, source in (
                    ("code", "configMap", "name", "lab-services-code"), ("context", "configMap", "name", "lab-context"),
                    ("ca", "configMap", "name", "lab-ca"), ("mcp-auth", "secret", "secretName", "aks-ops-mcp-auth"),
                    ("broker-auth", "secret", "secretName", "model-broker-auth"))]}}}}

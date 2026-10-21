#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Azure deployment-script entry point. Secrets go to AKS / a per-instance vault."""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from render_workloads import render, readiness_job


def az(*args, attempts=1, timeout=60, retry_timeout=300):
    # Log only fixed operation names; arguments, responses and stderr may contain secrets.
    operation = " ".join(args[:3])
    deadline = time.monotonic() + retry_timeout
    timed_out = False
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        print("Azure action: " + operation + " (attempt " + str(attempt + 1) + ").", flush=True)
        try:
            result = subprocess.run(["az", *args, "--only-show-errors", "--output", "json"],
                                    text=True, capture_output=True, timeout=min(timeout, remaining))
        except subprocess.TimeoutExpired:
            timed_out = True
        else:
            if result.returncode == 0:
                return json.loads(result.stdout or "null")
        if attempt + 1 < attempts:
            time.sleep(max(0, min(20, deadline - time.monotonic())))
    # CLI error output may contain a secure argument or request body.
    raise RuntimeError("Azure action " + ("timed out: " if timed_out else "failed: ") + operation)


def validate_inference(context, key):
    endpoint = urlsplit(context["upstreamInferenceEndpoint"])
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path.rstrip('/').endswith('/chat/completions')
            or not context["inferenceModel"] or not isinstance(key, str) or not key.strip()):
        raise RuntimeError("Full setup needs a real HTTPS OpenAI-compatible base URL, model ID, and secure inference API key")


def foundry_api_base(account):
    """Use the published inference endpoint, never the project management URL."""
    root = account.get("properties", {}).get("endpoints", {}).get("Azure OpenAI Legacy API - Latest moniker", "")
    endpoint = urlsplit(root)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.query or endpoint.fragment or endpoint.path.rstrip('/') not in ('', '/openai', '/openai/v1')):
        raise RuntimeError("Foundry account has no supported HTTPS OpenAI v1 endpoint; check the model template")
    return endpoint.scheme + "://" + endpoint.netloc + "/openai/v1"


def resolve_inference(context, key, timeout=300, retry_interval=10):
    """Resolve only the named instance account; keep its key out of logs/context."""
    if not context.get("resolveFoundryInference", False):
        validate_inference(context, key)
        return context, key
    if key or context.get("upstreamInferenceEndpoint") or not context.get("foundryAccountName") or not context.get("inferenceModel"):
        raise RuntimeError("Automatic Foundry discovery needs an account/model and blank explicit endpoint/key")
    deadline = time.monotonic() + timeout
    print("Resolving the assigned Foundry account/model (maximum " + str(timeout) + " seconds).", flush=True)
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired("Foundry discovery", timeout)
        return min(30, left)
    while time.monotonic() < deadline:
        # Access may propagate after the role assignment. The independent model
        # template may still be provisioning. Each call and the whole wait are bounded.
        try:
            account = az("cognitiveservices", "account", "show", "--resource-group", context["resourceGroupName"],
                         "--name", context["foundryAccountName"], timeout=remaining())
            deployment = az("cognitiveservices", "account", "deployment", "show", "--resource-group", context["resourceGroupName"],
                            "--name", context["foundryAccountName"], "--deployment-name", context["inferenceModel"], timeout=remaining())
        except (RuntimeError, subprocess.TimeoutExpired):
            account = deployment = None
        if account is not None and deployment is not None:
            properties = account.get("properties", {})
            states = (properties.get("provisioningState"), deployment.get("properties", {}).get("provisioningState"))
            if any(state in ("Failed", "Canceled", "Deleting") for state in states):
                raise RuntimeError("Assigned Foundry account/model provisioning failed; inspect the Fireworks deployment")
            if properties.get("disableLocalAuth"):
                raise RuntimeError("Foundry API-key authentication is disabled; this broker needs an approved key-auth endpoint")
            if all(state == "Succeeded" for state in states):
                resolved = {**context, "upstreamInferenceEndpoint": foundry_api_base(account), "inferenceAuthHeader": "api-key"}
                try:
                    keys = az("cognitiveservices", "account", "keys", "list", "--resource-group", context["resourceGroupName"],
                              "--name", context["foundryAccountName"], timeout=remaining())
                except (RuntimeError, subprocess.TimeoutExpired):
                    keys = {}
                foundry_key = keys.get("key1")
                if time.monotonic() < deadline and isinstance(foundry_key, str) and foundry_key.strip():
                    validate_inference(resolved, foundry_key)
                    print("Foundry connection resolved; the provider key is retained only for AKS provisioning.", flush=True)
                    return resolved, foundry_key
        time.sleep(max(0, min(retry_interval, deadline - time.monotonic())))
    raise RuntimeError("Foundry discovery timed out; check the configured account/model, Fireworks provisioning and setup identity access")


def invoke(context, command, files=(), attempts=1, timeout=1200):
    args = ["aks", "command", "invoke", "--resource-group", context["resourceGroupName"],
            "--name", context["aksClusterName"], "--command", command]
    for file in files:
        args.extend(["--file", str(file)])
    # Includes rollout/readiness waits. All transport retries share this budget.
    result = az(*args, attempts=attempts, timeout=timeout, retry_timeout=timeout)
    if result.get("exitCode") != 0:
        phases = {"apply-manifest", "seed-incident", "readiness", "load-balancer",
                  "rollout:dependency-api", "rollout:service-api", "rollout:aks-ops-mcp"}
        for line in (result.get("logs") or "").splitlines():
            marker = "IGNITE_SETUP_FAILURE phase="
            if line.startswith(marker):
                fields = line[len(marker):].split()
                if fields and fields[0] in phases:
                    raise RuntimeError("AKS bootstrap command failed at " + fields[0] + "; inspect cluster pod / Job state")
        raise RuntimeError("AKS bootstrap command failed; inspect cluster pod / Job state")
    return result


def wait_for_cluster_access(context, timeout=300, retry_interval=10):
    """Wait for the setup identity's data-plane role before any workload writes."""
    deadline = time.monotonic() + timeout
    # These authorization reviews do not create resources or read Secret data.
    command = " && ".join([
        "kubectl auth can-i create namespaces --quiet",
        "kubectl auth can-i create resourcequotas -n ignite-lab --quiet",
        "kubectl auth can-i create rolebindings.rbac.authorization.k8s.io -n ignite-lab --quiet",
        "kubectl auth can-i create secrets -n ignite-lab --quiet",
    ])
    print("Waiting for setup identity AKS permissions (maximum " + str(timeout) + " seconds).", flush=True)
    while time.monotonic() < deadline:
        try:
            invoke(context, command, timeout=min(120, deadline - time.monotonic()))
        except RuntimeError:
            pass
        else:
            if time.monotonic() < deadline:
                print("Setup identity AKS permissions are ready.", flush=True)
                return
        time.sleep(max(0, min(retry_interval, deadline - time.monotonic())))
    raise RuntimeError("Setup identity AKS access timed out; verify its cluster-scoped AKS Contributor and AKS RBAC Cluster Admin assignments")


def vault_token(vault, name):
    # List distinguishes a missing secret from an authorization / network failure.
    entries = az("keyvault", "secret", "list", "--vault-name", vault, attempts=20)
    if any(entry["name"] == name for entry in entries):
        return az("keyvault", "secret", "show", "--vault-name", vault, "--name", name)["value"]
    token = secrets.token_urlsafe(48)
    value_file = Path(name + ".secret")
    value_file.write_text(token)
    value_file.chmod(0o600)
    try:
        az("keyvault", "secret", "set", "--vault-name", vault, "--name", name, "--file", str(value_file))
    finally:
        value_file.unlink(missing_ok=True)
    return token


def cluster_setup_script():
    # All namespace, resource names and the requested IP are template-owned.
    return """#!/bin/sh
set -eu
phase=apply-manifest
trap 'rc=$?; if [ "$rc" -ne 0 ]; then printf "IGNITE_SETUP_FAILURE phase=%s\\n" "$phase"; fi' 0
printf 'AKS setup phase: %s\\n' "$phase"
kubectl apply --server-side --force-conflicts --field-manager=ignite-bootstrap -f manifest.json >/dev/null
kubectl -n ignite-lab rollout restart deployment/dependency-api deployment/service-api deployment/aks-ops-mcp >/dev/null
for name in dependency-api service-api aks-ops-mcp; do
  phase=rollout:$name
  printf 'AKS setup phase: %s\\n' "$phase"
  kubectl -n ignite-lab rollout status deployment/$name --timeout=300s
done
phase=seed-incident
printf 'AKS setup phase: %s\\n' "$phase"
kubectl -n ignite-lab patch configmap service-config --type=merge -p '{"data":{"DEPENDENCY_URL":"http://dependency-api:8081"}}' >/dev/null
phase=readiness
printf 'AKS setup phase: %s\\n' "$phase"
kubectl -n ignite-lab delete job lab-readiness --ignore-not-found >/dev/null
kubectl apply -f readiness.json >/dev/null
kubectl -n ignite-lab wait --for=condition=complete job/lab-readiness --timeout=240s
phase=load-balancer
printf 'AKS setup phase: %s\\n' "$phase"
# An empty ingress list is normal while Azure provisions the frontend.
# Indexing [0] makes kubectl fail immediately instead of watching that list.
kubectl -n ignite-lab wait --for='jsonpath={.status.loadBalancer.ingress[*].ip}=10.42.4.10' service/aks-ops-mcp --timeout=300s
printf 'AKS applications, incident and real inference checks passed.\\n'
"""


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parents[2]
    context = json.loads(os.environ["LAB_CONTEXT"])
    wait_for_cluster_access(context)
    context, upstream_key = resolve_inference(context, os.environ.pop("INFERENCE_API_KEY", ""))
    mcp_token = vault_token(context["credentialVaultName"], "mcp-token")
    broker_token = vault_token(context["credentialVaultName"], "broker-token")
    cert_dir = Path("certificates")
    cert_dir.mkdir(exist_ok=True)
    def openssl(*args):
        subprocess.run(["openssl", *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "7",
            "-subj", "/CN=Ignite Lab Instance CA", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", str(cert_dir / "ca.key"), "-out", str(cert_dir / "ca.crt"))
    openssl("req", "-new", "-newkey", "rsa:3072", "-nodes", "-subj", "/CN=aks-ops-mcp",
            "-keyout", str(cert_dir / "tls.key"), "-out", str(cert_dir / "tls.csr"))
    (cert_dir / "extensions").write_text("basicConstraints=CA:FALSE\nextendedKeyUsage=serverAuth\n"
        "subjectAltName=IP:" + context["mcpPrivateIp"] + ",DNS:aks-ops-mcp,DNS:aks-ops-mcp." + context["labNamespace"] + ".svc.cluster.local\n")
    openssl("x509", "-req", "-in", str(cert_dir / "tls.csr"), "-CA", str(cert_dir / "ca.crt"),
            "-CAkey", str(cert_dir / "ca.key"), "-CAcreateserial", "-days", "7", "-sha256",
            "-extfile", str(cert_dir / "extensions"), "-out", str(cert_dir / "tls.crt"))
    secret_data = {"aks-ops-mcp-auth": {"token": mcp_token}, "model-broker-auth": {"token": broker_token},
        "model-upstream": {"api-key": upstream_key},
        "aks-ops-mcp-tls": {"tls.crt": (cert_dir / "tls.crt").read_text(), "tls.key": (cert_dir / "tls.key").read_text()}}
    manifest = render(context, secret_data, root)
    manifest["items"].append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "lab-ca", "namespace": context["labNamespace"]}, "data": {"ca.crt": (cert_dir / "ca.crt").read_text()}})
    Path("manifest.json").write_text(json.dumps(manifest))
    Path("readiness.json").write_text(json.dumps(readiness_job(context)))
    ns = context["labNamespace"]
    assert ns == "ignite-lab"
    Path("cluster-setup.sh").write_text(cluster_setup_script())
    try:
        invoke(context, "sh cluster-setup.sh", [Path("manifest.json"), Path("readiness.json"), Path("cluster-setup.sh")], attempts=20)
        az("keyvault", "secret", "set", "--vault-name", context["credentialVaultName"], "--name", "lab-ca",
           "--file", str(cert_dir / "ca.crt"))
        Path(os.environ["AZ_SCRIPTS_OUTPUT_PATH"]).write_text(json.dumps({"status": "ready", "namespace": ns}))
        print("Private AKS lab applications are ready.")
    finally:
        Path("manifest.json").unlink(missing_ok=True)
        for file in cert_dir.iterdir():
            file.unlink()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Only our fixed messages are allowed in logs, never remote bodies.
        print(str(error) if isinstance(error, RuntimeError) else "Lab application setup failed", file=sys.stderr)
        raise SystemExit(1)

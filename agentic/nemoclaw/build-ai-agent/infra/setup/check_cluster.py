# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Assert seeded incident, RBAC isolation, MCP auth and real model connectivity."""
import json
import os
import ssl
import urllib.request
import urllib.error
from pathlib import Path


def check(context, ca_path, mcp_token, model_token, host=None):
    ssl_context = ssl.create_default_context(cafile=str(ca_path))
    # The DNS service name is included in the certificate SANs.
    host = host or context["mcpPrivateIp"]
    mcp_url = "https://" + host + ":8443/mcp"
    def post(url, token, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, context=ssl_context, timeout=120) as response:
            return json.load(response)
    def tool(name):
        response = post(mcp_url, mcp_token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": name, "arguments": {}}})
        assert "error" not in response and not response["result"].get("isError"), "MCP tool failed"
        return json.loads(response["result"]["content"][0]["text"])
    try:
        post(mcp_url, "invalid-probe-token", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        raise AssertionError("MCP accepted an invalid token")
    except urllib.error.HTTPError as error:
        assert error.code == 401, "Unexpected MCP authentication result"
        error.close()
    desired, runtime = tool("get_desired_config"), tool("get_runtime_config")
    assert desired["data"]["DEPENDENCY_URL"] == "http://dependency-api:8081", "Desired config is incorrect"
    assert runtime["effective_dependency_url"] == "http://dependency-api-invalid:8081", "Incident runtime is not seeded"
    assert tool("verify_application")["status"] == 503, "Incident must begin unhealthy"
    assert tool("rbac_self_test")["isolation_enforced"], "MCP RBAC isolation failed"
    if host == 'aks-ops-mcp':  # Kubernetes DNS is available only in the Job.
        with urllib.request.urlopen('http://dependency-api:8081/healthz', timeout=10) as response:
            assert response.status == 200, 'Dependency is unhealthy'
    response = post("https://" + host + ":8000/v1/chat/completions", model_token, {
        "model": context["inferenceModel"], "messages": [{"role": "user", "content": "Reply with the word ready."}],
        # The Fireworks rehearsal exhausted 128 tokens on reasoning alone.
        "max_tokens": 1024, "stream": False})
    choices = response.get("choices")
    assert isinstance(choices, list) and choices, "Real inference did not return a completion"
    completion = choices[0]
    content = (completion.get("message") or {}).get("content")
    assert isinstance(content, str) and content.strip(), "Real inference did not return a visible reply"
    assert completion.get("finish_reason") == "stop", "Real inference did not finish its reply"
    return {"mcp": "ready", "rbac": "isolated", "incident": "seeded", "inference": "ready"}


if __name__ == "__main__":
    try:
        result = check(json.loads(Path("/etc/ignite-lab/context.json").read_text()),
                       Path("/var/run/lab-ca/ca.crt"), Path("/var/run/lab-auth/token").read_text().strip(),
                       Path("/var/run/broker-auth/token").read_text().strip(), os.getenv("MCP_HOST"))
        print(json.dumps(result))
    except Exception:
        # Provider bodies / SDK exception strings may include credentials.
        raise SystemExit("Lab readiness failed; inspect pod state and the nonsecret configuration")

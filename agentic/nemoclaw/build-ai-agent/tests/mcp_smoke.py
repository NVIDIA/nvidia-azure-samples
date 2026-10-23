#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the authenticated HTTPS MCP server and print machine-readable evidence."""

from __future__ import annotations

import argparse
import json
import ssl
import urllib.error
import urllib.request
from typing import Any


class Client:
    def __init__(self, url: str, ca: str, token: str):
        self.url = url
        self.context = ssl.create_default_context(cafile=ca)
        self.token = token
        self.request_id = 0

    def post(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.request_id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": method,
                "params": params or {},
            }
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        with urllib.request.urlopen(
            request, context=self.context, timeout=30
        ) as response:
            result = json.load(response)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]

    def tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        result = self.post(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        return json.loads(result["content"][0]["text"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="https://127.0.0.1:8443/mcp")
    parser.add_argument("--ca", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument(
        "command",
        choices=("read", "restart", "scale", "verify", "guardrails", "all"),
    )
    args = parser.parse_args()
    token = open(args.token_file, encoding="utf-8").read().strip()
    client = Client(args.url, args.ca, token)
    evidence: dict[str, Any] = {
        "initialize": client.post(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ignite-smoke", "version": "1.0"},
            },
        ),
        "tools": [tool["name"] for tool in client.post("tools/list")["tools"]],
    }
    if args.command in ("read", "all"):
        evidence["desired"] = client.tool("get_desired_config")
        evidence["runtime"] = client.tool("get_runtime_config")
        evidence["health_before"] = client.tool("verify_application")
        evidence["rbac"] = client.tool("rbac_self_test")
        evidence["logs"] = client.tool("get_logs")
    if args.command in ("restart", "all"):
        evidence["restart"] = client.tool(
            "restart_deployment",
            {
                "deployment": "service-api",
                "reason": "Reload corrected ConfigMap during Ignite lab test",
            },
        )
    if args.command == "scale":
        evidence["scale"] = client.tool(
            "scale_deployment", {"deployment": "service-api", "replicas": 2}
        )
    if args.command == "verify":
        evidence["health"] = client.tool("verify_application")
        evidence["runtime"] = client.tool("get_runtime_config")
    if args.command in ("guardrails", "all"):
        evidence["rbac"] = client.tool("rbac_self_test")
        rejected: dict[str, str] = {}
        try:
            client.tool(
                "restart_deployment",
                {"deployment": "dependency-api", "reason": "out of scope"},
            )
        except RuntimeError as exc:
            rejected["wrong_deployment"] = str(exc)
        try:
            client.tool(
                "scale_deployment",
                {"deployment": "service-api", "replicas": 4},
            )
        except RuntimeError as exc:
            rejected["replicas_above_cap"] = str(exc)
        evidence["rejected_mutations"] = rejected
        if set(rejected) != {"wrong_deployment", "replicas_above_cap"}:
            raise SystemExit("one or more invalid mutations were not rejected")
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

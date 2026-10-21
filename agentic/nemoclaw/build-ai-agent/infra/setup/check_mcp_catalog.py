#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Require native OpenClaw discovery before spending tokens on an agent probe."""
import json
import sys
from pathlib import Path


REQUIRED_TOOLS = ("verify_application", "get_desired_config", "get_runtime_config")


def check_catalog(body):
    if not isinstance(body, dict):
        raise ValueError("invalid native discovery report")
    servers = body.get("servers")
    server = servers.get("aks-ops") if isinstance(servers, dict) else None
    if not isinstance(server, dict) or type(server.get("tools")) is not int or server["tools"] < 3:
        raise ValueError("aks-ops was not discovered with its incident tools")
    if body.get("diagnostics"):
        raise ValueError("native discovery reported transport errors")
    tools = body.get("tools")
    if not isinstance(tools, list) or not all(isinstance(name, str) for name in tools):
        raise ValueError("invalid native tool catalog")
    for required in REQUIRED_TOOLS:
        if not any(name == required or name.endswith("_" + required) for name in tools):
            raise ValueError("native catalog is missing " + required)


def main():
    try:
        check_catalog(json.loads(Path(sys.argv[1]).read_text()))
    except (OSError, ValueError, IndexError):
        # Reports can contain remote diagnostics; never print their contents.
        raise SystemExit("Native aks-ops discovery failed; agent readiness was not attempted.")
    print("Native MCP discovery and all three incident tools verified.")


if __name__ == "__main__":
    main()

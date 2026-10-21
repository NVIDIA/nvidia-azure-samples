#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Author-only deterministic bundler. Studio consumes main.bicep, not local files."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["apps/lab_services.py", "assets/operations-agent-starter.md",
         "infra/setup/host_prerequisites.sh", "infra/setup/inference_broker.py",
         "infra/setup/render_workloads.py", "infra/setup/check_cluster.py",
         "infra/setup/deploy_cluster.py", "infra/setup/setup_vm.py",
         "infra/setup/setup_user.sh", "infra/setup/probe_mcp.cjs",
         "infra/setup/check_mcp_catalog.py"]


def build():
    payload = json.dumps({name: (ROOT / name).read_text() for name in FILES},
                         sort_keys=True, separators=(",", ":")).encode()
    # Fix the level (Python's default changed) and gzip OS byte (mtime=0 used
    # platform-specific zlib headers on older Python). Author and CI must agree.
    packed = gzip.compress(payload, compresslevel=9, mtime=0)
    packed = packed[:9] + b'\xff' + packed[10:]
    encoded = base64.b64encode(packed).decode()
    template = (ROOT / "infra/main.template.bicep").read_text()
    assert template.count("__IGNITE_SETUP_BUNDLE__") == 1
    assert template.count("__IGNITE_SETUP_SHA256__") == 1
    return template.replace("__IGNITE_SETUP_BUNDLE__", encoded).replace(
        "__IGNITE_SETUP_SHA256__", hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "infra/main.bicep"
    rendered = build()
    if args.check:
        if target.read_text() != rendered:
            raise SystemExit("main.bicep is stale: run python3 infra/build-template.py")
        print("Embedded setup bundle is current.")
    else:
        target.write_text(rendered)
        print("Built self-contained infra/main.bicep (" + str(len(rendered.encode())) + " bytes).")

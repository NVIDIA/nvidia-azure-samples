# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pytest config for insurance-claims-foundry remote-agent tests.

Loads env vars from two files, layered (same pattern as the sibling
insurance-claims/ harness):

  1. .env       — runtime config shared with `azd ai agent run`
  2. .env.test  — test-only knobs (RUN_E2E_REMOTE_TESTS); wins over .env.

Precedence (highest first): .env.test → shell env → .env.
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

FOUNDRY_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = FOUNDRY_DIR.parent.parent

for env_file, override in [(REPO_ROOT / ".env", False), (REPO_ROOT / ".env.test", True)]:
    if env_file.is_file():
        load_dotenv(env_file, override=override)

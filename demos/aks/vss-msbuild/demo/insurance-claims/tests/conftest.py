# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Pytest config for insurance-claims demo tests.

- Adds the demo dir to sys.path so tests can `from local_runner import run`.
- Loads env vars from two files, layered:
    1. .env       — runtime config shared with `python local_runner.py`
    2. .env.test  — test-only knobs (RUN_E2E_TESTS, TEST_VIDEO_PATH);
                    values here override .env so tests can swap endpoints
                    without polluting runtime config.
- Precedence (highest wins):
    1. .env.test  (override=True — explicitly meant to win over runtime)
    2. shell env  (set before invoking pytest)
    3. .env       (override=False — shell beats it)
  So CI can inject overrides for runtime keys via shell env; to override a
  test-only key, edit .env.test or set the var inline.

NOTE: load order matters — pytest collects the test module after conftest,
and the test's `pytestmark = [skipif(...)]` decorators evaluate env at
import time. Loading here ensures the gates see the right values.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

DEMO_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = DEMO_DIR.parent.parent

# Make `local_runner`, `tools`, `vss_mcp_server` importable from tests.
sys.path.insert(0, str(DEMO_DIR))

# Layered .env loading. See precedence note in the module docstring.
for env_file, override in [(REPO_ROOT / ".env", False), (REPO_ROOT / ".env.test", True)]:
    if env_file.is_file():
        load_dotenv(env_file, override=override)

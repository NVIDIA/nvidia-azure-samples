# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end smoke test for the **deployed** Foundry hosted agent.

Hits the published hosted-agent endpoint by shelling out to
`azd ai agent invoke` (no `--local`), which routes the prompt to the
container running in the Foundry project. Costs real model + VSS spend on
every run, so it's gated behind RUN_E2E_REMOTE_TESTS=1.

Usage:

    cd demo/insurance-claims-foundry
    cp ../../.env.test.example ../../.env.test    # if not already present
    # edit ../../.env.test to set RUN_E2E_REMOTE_TESTS=1
    pip install -r requirements-dev.txt
    pytest tests/ -v -s

Skip conditions (any one triggers a skip):
  - RUN_E2E_REMOTE_TESTS not set
  - `azd` binary not on PATH
  - No azd project / selected deployed agent version unset

Cost / runtime: ~30-90s per invoke + a few cents of model spend.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FOUNDRY_DIR = Path(__file__).resolve().parent.parent
RUN_REMOTE_E2E = bool(os.environ.get("RUN_E2E_REMOTE_TESTS"))
REMOTE_AGENT_NAME = os.environ.get("REMOTE_AGENT_NAME", "insurance-claims-triage-vss")

# video_id toyota is pre-uploaded to the workshop VSS deployment;
# its damage profile matches the Camry policy POL-2025-44912 in
# sample_data/policies.json.
SMOKE_PROMPT = (
    "A customer just submitted a damage video. video_id=toyota. "
    "If the VIN is not visible, use policy POL-2025-44912 as a fallback. "
    "Run the triage workflow."
)
EXPECTED_VEHICLE = "2022 Toyota Camry SE"
EXPECTED_VIN = "4T1G11AK7NU012345"


def _agent_version_env_name(agent_name: str) -> str:
    normalized = re.sub(r"\W+", "_", agent_name).upper().strip("_")
    return f"AGENT_{normalized}_VERSION"


def _azd_env_has_deployed_agent(agent_name: str) -> bool:
    """True if `azd env get-values` reports a non-empty version for agent_name."""
    azd = shutil.which("azd")
    if azd is None:
        return False
    try:
        out = subprocess.run(
            [azd, "env", "get-values"],
            cwd=FOUNDRY_DIR,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return False
    if out.returncode != 0:
        return False
    version_var = _agent_version_env_name(agent_name)
    return bool(re.search(rf'{re.escape(version_var)}="?\d+', out.stdout))


pytestmark = [
    pytest.mark.skipif(
        not RUN_REMOTE_E2E,
        reason="set RUN_E2E_REMOTE_TESTS=1 to run remote tests (costs model spend)",
    ),
    pytest.mark.skipif(
        shutil.which("azd") is None,
        reason="azd CLI not on PATH",
    ),
    pytest.mark.skipif(
        RUN_REMOTE_E2E and not _azd_env_has_deployed_agent(REMOTE_AGENT_NAME),
        reason=(
            f"{REMOTE_AGENT_NAME!r} is not deployed in the current azd env "
            f"(run `azd deploy {REMOTE_AGENT_NAME}` first)"
        ),
    ),
]


def _invoke_remote(prompt: str, *, new_conversation: bool = True, timeout: int = 300) -> str:
    """Call the deployed agent and return combined stdout+stderr."""
    args = ["azd", "ai", "agent", "invoke"]
    if new_conversation:
        args.extend(["--new-conversation", "--new-session"])
    args.extend([REMOTE_AGENT_NAME, prompt])
    proc = subprocess.run(
        args,
        cwd=FOUNDRY_DIR,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        pytest.fail(
            f"azd ai agent invoke exited {proc.returncode}\n--- output ---\n{output}"
        )
    return output


def test_remote_claims_workflow() -> None:
    """The deployed agent should triage the smoke video to a Camry claim."""
    output = _invoke_remote(SMOKE_PROMPT)

    # Final response should announce a drafted claim with the right vehicle/VIN
    # and surface a payable amount. We don't inspect tool calls here — those
    # live in the App Insights trace, not in stdout.
    assert "Claim drafted" in output, f"no claim drafted. output:\n{output}"
    assert EXPECTED_VEHICLE in output, (
        f"vehicle {EXPECTED_VEHICLE!r} missing from response. output:\n{output}"
    )
    assert EXPECTED_VIN in output, (
        f"VIN {EXPECTED_VIN!r} missing from response. output:\n{output}"
    )
    # Some dollar amount (parts/labor/payable) — the formatting line is
    # "Payable: $<n>" but the model occasionally renders without the leading
    # "Payable:" label, so just require a $-prefixed integer.
    assert re.search(r"\$\d", output), f"no dollar amount in response. output:\n{output}"

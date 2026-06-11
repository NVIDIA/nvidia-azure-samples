# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""End-to-end smoke test for the insurance-claims local runner.

Hits real VSS (POST /api/v1/videos + PUT to VST + POST /chat/stream) and
real Azure OpenAI. Skips automatically unless the harness is opted into via
`RUN_E2E_TESTS=1` and the required env is configured.

Usage:

    cd demo/insurance-claims
    source ../../.env                       # or rely on conftest's load_dotenv
    export RUN_E2E_TESTS=1
    export TEST_VIDEO_PATH=/path/to/damage.mp4
    pytest tests/ -v -s

Cost / runtime: one full agent loop per run — ~30-60s and a few cents of
model spend depending on video length and how many tool turns gpt-4.1 takes.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

REQUIRED_ENV = ("VSS_BASE_URL", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")


def _missing_env() -> list[str]:
    return [k for k in REQUIRED_ENV if not os.environ.get(k)]


def _vss_reachable() -> bool:
    base = os.environ.get("VSS_BASE_URL", "").rstrip("/")
    if not base:
        return False
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(base + "/")
            return r.status_code < 500
    except httpx.HTTPError:
        return False


pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("RUN_E2E_TESTS"),
        reason="set RUN_E2E_TESTS=1 to run end-to-end tests (costs model spend)",
    ),
    pytest.mark.skipif(
        bool(_missing_env()),
        reason=f"missing env: {_missing_env()}",
    ),
    pytest.mark.skipif(
        not os.environ.get("TEST_VIDEO_PATH"),
        reason="set TEST_VIDEO_PATH=/abs/path/to/damage.mp4",
    ),
]


@pytest.fixture(scope="module")
def video_path() -> Path:
    p = Path(os.environ["TEST_VIDEO_PATH"]).expanduser().resolve()
    if not p.is_file():
        pytest.skip(f"TEST_VIDEO_PATH does not exist: {p}")
    return p


@pytest.fixture(scope="module")
def vss_ready() -> None:
    if not _vss_reachable():
        pytest.skip(f"VSS not reachable at {os.environ.get('VSS_BASE_URL')}")


@pytest.fixture()
def output_dir() -> Path:
    """Where draft_claim_pdf writes its files."""
    from tools import _OUTPUT_DIR  # noqa: PLC2701 (intentional: test internals)

    _OUTPUT_DIR.mkdir(exist_ok=True)
    return _OUTPUT_DIR


def _tool_calls(messages: list[dict]) -> list[str]:
    """Return the ordered list of tool names the agent called."""
    names: list[str] = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            names.append(tc["function"]["name"])
    return names


def test_full_claims_workflow(video_path: Path, vss_ready: None, output_dir: Path) -> None:
    """The agent should: analyze video → look up policy → estimate cost → draft claim."""
    from local_runner import run

    pre_files = set(output_dir.glob("*"))

    # POL-2025-44912 (Alex Romero / Camry) is the canonical mock policy in
    # sample_data/policies.json — pass as fallback so the test passes even
    # when the VIN isn't extractable from the chosen video.
    messages = run(video_path, policy_fallback="POL-2025-44912")

    tools_called = _tool_calls(messages)

    # The agent must use VSS, must look up a policy, must estimate cost,
    # and must produce a draft claim. Order can vary but all four are
    # required for a coherent triage run.
    assert "vss_analyze_video" in tools_called, (
        f"agent didn't call VSS. tools_called={tools_called}"
    )
    assert "lookup_policy" in tools_called, (
        f"agent didn't look up the policy. tools_called={tools_called}"
    )
    assert "estimate_repair_cost" in tools_called, (
        f"agent didn't estimate repair cost. tools_called={tools_called}"
    )
    assert "draft_claim_pdf" in tools_called, (
        f"agent didn't draft a claim PDF. tools_called={tools_called}"
    )

    # The loop reached a final assistant message (last entry is assistant
    # with no further tool_calls), not the 25-turn cap.
    last = messages[-1]
    assert last["role"] == "assistant", f"last msg role was {last['role']!r}"
    assert not last.get("tool_calls"), "agent stopped mid-tool-call (likely hit turn cap)"

    # A new claim file appeared in ./output/.
    new_files = set(output_dir.glob("*")) - pre_files
    assert new_files, f"no claim file written to {output_dir}"
    # The mock writer names files <claim_id>.{pdf|txt}; agent picks claim_id
    # via the system prompt's CLM-YYYYMMDD-<policy-suffix> convention.
    assert any(f.suffix in (".pdf", ".txt") for f in new_files), (
        f"unexpected file types in output: {[f.name for f in new_files]}"
    )

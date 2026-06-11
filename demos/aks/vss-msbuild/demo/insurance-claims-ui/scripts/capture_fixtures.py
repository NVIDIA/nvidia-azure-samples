# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Capture canned Foundry agent responses for optional local sample policies.

Hits the deployed Foundry hosted agent's /responses endpoint for each policy
that has a `sample_video` field in policies.json, saves the verbatim agent
text + raw Responses-API JSON to `fixtures/<policy_number>.json`. Offline
mode in app.py replays these fixtures instead of calling VSS / Foundry, so
the demo can run while the AKS GPU cluster is shut down.

Run:

    cd demo/insurance-claims-ui
    az login                            # for DefaultAzureCredential
    .venv/bin/python scripts/capture_fixtures.py

Cost / runtime: one full agent invocation per policy (~30-60s, ~$0.05-0.10
each). Re-run any time `policies.json` or the agent's instructions/tools
change to refresh the canned responses.

Environment overrides (same shape as app.py uses):

    VSS_BASE_URL              http://vss.<EXTERNAL_HOST>.nip.io
    FOUNDRY_AGENT_ENDPOINT    https://.../agents/<name>/endpoint/protocols/openai/responses?...
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from azure.identity import DefaultAzureCredential

HERE = Path(__file__).resolve().parent.parent
POLICIES_PATH = HERE / "policies.json"
FIXTURES_DIR = HERE / "fixtures"

VSS_BASE_URL = os.environ.get("VSS_BASE_URL", "").rstrip("/")
if not VSS_BASE_URL:
    raise SystemExit("Set VSS_BASE_URL (e.g. http://vss.<EXTERNAL_HOST>.nip.io).")
FOUNDRY_ENDPOINT = os.environ.get("FOUNDRY_AGENT_ENDPOINT", "")
if not FOUNDRY_ENDPOINT:
    raise SystemExit("Set FOUNDRY_AGENT_ENDPOINT.")
FOUNDRY_TOKEN_SCOPE = "https://ai.azure.com/.default"
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)


def vss_upload_url(filename: str) -> str:
    """Get the canonical VSS upload URL for `filename`. We don't PUT the bytes
    here; local sample videos must already be in VST from earlier sessions. This
    call just normalizes the video_id (= filename stem) the agent expects.
    """
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(f"{VSS_BASE_URL}/api/v1/videos", json={"filename": filename})
        r.raise_for_status()
        return r.json()["url"]


def call_agent(prompt: str, token: str) -> dict:
    """POST to the deployed Foundry agent's /responses endpoint."""
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        r = client.post(
            FOUNDRY_ENDPOINT,
            json={"input": prompt, "stream": False},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        r.raise_for_status()
    return r.json()


def extract_text(resp_json: dict) -> str:
    """Pull the final assistant text from a Responses-API JSON (same logic as
    app.py:extract_agent_text)."""
    output = resp_json.get("output") or []
    for item in reversed(output):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    return c.get("text", "")
    return json.dumps(resp_json, indent=2)


def capture_one(policy: dict, token: str) -> dict:
    """Capture a fixture for a single policy. Returns the fixture dict."""
    sample_video = policy.get("sample_video")
    if not sample_video:
        raise SystemExit(
            f"Policy {policy['policy_number']} has no sample_video field — "
            f"only run this script for policies with local sample videos."
        )
    filename = Path(sample_video).name  # e.g. "toyota.mp4"
    upload_url = vss_upload_url(filename)
    video_id = upload_url.rstrip("/").split("/")[-2]  # filename stem

    prompt = (
        f"A customer just submitted a damage video. "
        f"video_id={video_id}. "
        f"If the VIN is not visible, use policy "
        f"{policy['policy_number']} as a fallback. "
        f"Run the triage workflow."
    )

    print(f"  Invoking agent for {policy['policy_number']} (video_id={video_id})…")
    resp_json = call_agent(prompt, token)
    agent_text = extract_text(resp_json)
    return {
        "policy_number": policy["policy_number"],
        "video_filename": filename,
        "video_id": video_id,
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompt": prompt,
        "agent_text": agent_text,
        "raw_response": resp_json,
    }


def main() -> None:
    if not POLICIES_PATH.is_file():
        sys.exit(f"policies.json not found at {POLICIES_PATH}")
    policies = json.loads(POLICIES_PATH.read_text())
    targets = [p for p in policies if p.get("sample_video")]
    if not targets:
        sys.exit(
            "No policies have a sample_video field. Add local sample videos and "
            "reference them via sample_video in policies.json."
        )

    print(f"Capturing fixtures for {len(targets)} polic{'y' if len(targets) == 1 else 'ies'}:")
    for p in targets:
        print(f"  - {p['policy_number']} → {p['sample_video']}")
    print()
    print("Authenticating to Foundry (DefaultAzureCredential)…")
    token = DefaultAzureCredential().get_token(FOUNDRY_TOKEN_SCOPE).token

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    for policy in targets:
        fixture = capture_one(policy, token)
        out = FIXTURES_DIR / f"{policy['policy_number']}.json"
        out.write_text(json.dumps(fixture, indent=2) + "\n")
        n = len(fixture["agent_text"])
        print(f"  ✓ Saved {out.relative_to(HERE.parent)} (agent_text: {n} chars)")

    print()
    print(f"Done. {len(targets)} fixture(s) under {FIXTURES_DIR.relative_to(HERE.parent)}/.")


if __name__ == "__main__":
    main()

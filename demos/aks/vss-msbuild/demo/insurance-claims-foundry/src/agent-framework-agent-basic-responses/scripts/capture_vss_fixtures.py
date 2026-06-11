# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Capture canned VSS Agent damage-analysis responses per video_id.

Hits the live NVIDIA VSS Agent on AKS for each known video_id, saves the
verbatim damage-analysis prose to `fixtures/vss/<video_id>.txt`. When
MOCK_VSS=true is set on the deployed Foundry agent (or in local dev),
`tools.vss_analyze_video()` reads these fixtures instead of calling VSS —
letting the agent run end-to-end while AKS is shut down to save cost.

The prompt this script sends mirrors what the master agent typically asks
(per the system prompt in instructions.md step 2: "What damage is visible
to the vehicle in video <id>? Describe each damaged panel and rate
severity. Transcribe any visible VIN."). The exact prompt isn't critical —
VSS responses are reasonably stable across phrasings of the same query.

Run:

    cd demo/insurance-claims-foundry/src/agent-framework-agent-basic-responses
    python scripts/capture_vss_fixtures.py [video_id ...]

If no video_ids are passed, captures the two known workshop videos:

    toyota, fiat

Re-run any time VSS is upgraded or the master-agent prompt changes
materially.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent.parent
FIXTURES_DIR = HERE / "fixtures" / "vss"

VSS_BASE_URL = os.environ.get("VSS_BASE_URL", "").rstrip("/")
if not VSS_BASE_URL:
    raise SystemExit("Set VSS_BASE_URL (e.g. http://vss.<EXTERNAL_HOST>.nip.io).")
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

# Default capture targets — the two videos pre-uploaded to the workshop VSS
# deployment that pair with policies POL-2025-44912 (Camry) and
# POL-2025-58820 (Fiat Stilo). Override on the CLI to capture others.
DEFAULT_VIDEO_IDS = ["toyota", "fiat"]

# Prompt template matching what the master agent asks. We use the same
# format string here as in tools.vss_analyze_video so the response we cache
# is representative of what the agent would see live.
_PROMPT_TEMPLATE = (
    "Video reference: '{video_id}'.\n\n"
    "What damage is visible to the vehicle in video '{video_id}'? "
    "Describe each damaged panel and rate severity (minor / moderate / severe). "
    "Transcribe any visible VIN. Reply with concise prose, no JSON."
)

# Same regex tools.py uses to strip VSS's internal chain-of-thought blocks
# from the streamed response before returning.
_AGENT_THINK_RE = re.compile(
    r"<agent-think\b.*?</agent-think>", flags=re.DOTALL | re.IGNORECASE
)


def capture_vss(video_id: str) -> str:
    """Hit VSS /chat/stream for `video_id`, return the cleaned prose response."""
    prompt = _PROMPT_TEMPLATE.format(video_id=video_id)
    payload = {"messages": [{"role": "user", "content": prompt}]}
    final_chunks: list[str] = []

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        with client.stream(
            "POST",
            f"{VSS_BASE_URL}/chat/stream",
            json=payload,
            headers={"Accept": "text/event-stream"},
        ) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line or line.startswith("intermediate_data:"):
                    continue
                if not line.startswith("data:"):
                    continue
                body = line.removeprefix("data:").strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except json.JSONDecodeError:
                    final_chunks.append(body)
                    continue
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content") or choices[0].get(
                        "message", {}
                    ).get("content")
                    if text:
                        final_chunks.append(text)

    raw = "".join(final_chunks).strip()
    cleaned = _AGENT_THINK_RE.sub("", raw).strip()
    return cleaned or raw or "[no streamed content from VSS]"


def main() -> None:
    video_ids = sys.argv[1:] or DEFAULT_VIDEO_IDS
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"VSS endpoint: {VSS_BASE_URL}")
    print(f"Capturing {len(video_ids)} video_id(s): {', '.join(video_ids)}")
    print()
    for vid in video_ids:
        print(f"  ▸ {vid} …", end="", flush=True)
        try:
            text = capture_vss(vid)
        except httpx.HTTPError as e:
            print(f" FAIL ({e})")
            continue
        out = FIXTURES_DIR / f"{vid}.txt"
        out.write_text(text + "\n")
        print(f" ✓ saved {out.relative_to(HERE)} ({len(text)} chars)")
    print()
    print(f"Done. Fixtures in {FIXTURES_DIR.relative_to(HERE)}/.")


if __name__ == "__main__":
    main()

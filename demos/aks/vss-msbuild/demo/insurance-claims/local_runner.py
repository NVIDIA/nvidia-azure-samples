# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Local smoke test for the Insurance Claims demo — no Foundry deploy required.

Drives the same agent loop as the Foundry hosted-agent.yaml does, but using
the Azure OpenAI SDK locally against your Foundry project's chat-model
deployment. Useful for iterating on the system prompt and tool contracts
before paying for Foundry container deployment cycles.

Run:

    export VSS_BASE_URL=http://vss.<EXTERNAL_HOST>.nip.io
    # one of:
    export AZURE_OPENAI_API_KEY=...          # simplest
    # or: az login   (uses DefaultAzureCredential automatically)
    pip install -r requirements.txt
    python local_runner.py path/to/damage.mp4

The runner:
  1. Uploads the video file directly to VST (skipping MCP for the byte
     transfer — MCP is great for tool calls, not 50MB binary uploads).
  2. Invokes your Azure OpenAI deployment with the same system prompt
     + function tools as the Foundry hosted agent.
  3. Prints the conversation and final claim summary.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from tools import draft_claim_pdf, estimate_repair_cost, lookup_policy
from vss_mcp_server import vss_analyze_video, vss_upload_video

HERE = Path(__file__).parent
INSTRUCTIONS = (HERE / "agent_instructions.md").read_text()

# All deployment-specific config comes from env vars (read at call time, not
# import time, so the module stays importable by test harnesses).
#   VSS_BASE_URL              http(s) base URL of your VSS AKS deployment
#   AZURE_OPENAI_ENDPOINT     https://<account>.cognitiveservices.azure.com/
#   AZURE_OPENAI_DEPLOYMENT   name of your chat-model deployment (e.g. gpt-4.1)
#   AZURE_OPENAI_API_KEY      optional — if unset, DefaultAzureCredential is used


def _require_env() -> tuple[str, str, str]:
    """Read + validate required env vars. Returns (vss, endpoint, deployment)."""
    vss = os.environ.get("VSS_BASE_URL", "").rstrip("/")
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
    if not vss:
        raise SystemExit("Set VSS_BASE_URL (e.g. http://vss.<EXTERNAL_HOST>.nip.io)")
    if not endpoint or not deployment:
        raise SystemExit("Set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT.")
    return vss, endpoint, deployment


def make_client():
    """Build an Azure OpenAI client against the configured deployment."""
    from openai import AzureOpenAI

    _, endpoint, deployment = _require_env()
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if api_key:
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version="2024-12-01-preview",
            api_key=api_key,
        )
    else:
        # No API key — try az-login-backed DefaultAzureCredential.
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_version="2024-12-01-preview",
            azure_ad_token_provider=token_provider,
        )
    return client, deployment


def upload_video_bytes(video_path: Path) -> str:
    """Upload local video → VST. Returns video_id usable in chat queries."""
    print(f"[upload] Requesting VST presigned URL for {video_path.name}")
    result = vss_upload_video(video_path.name)
    upload_url = result["upload_url"]
    video_id = result["video_id"]

    print(f"[upload] PUTting {video_path.stat().st_size / 1e6:.1f} MB to VST")
    with httpx.Client(timeout=httpx.Timeout(300.0)) as client:
        with video_path.open("rb") as f:
            r = client.put(upload_url, content=f.read(), headers={"Content-Type": "video/mp4"})
        r.raise_for_status()
    print(f"[upload] video_id = {video_id}")
    return video_id


# --- OpenAI tool schemas (mirror agent.yaml `tools:` block) ----------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "vss_analyze_video",
            "description": "Ask the VSS Agent a question about a previously uploaded video.",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "question": {"type": "string"},
                },
                "required": ["video_id", "question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_policy",
            "description": "Look up a customer's insurance policy by VIN or policy number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vin": {"type": "string", "description": "Vehicle Identification Number"},
                    "policy_number": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "estimate_repair_cost",
            "description": "Estimate repair cost from a list of damaged-panel descriptors.",
            "parameters": {
                "type": "object",
                "properties": {
                    "damage_items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "panel": {"type": "string"},
                                "severity": {
                                    "type": "string",
                                    "enum": ["minor", "moderate", "severe"],
                                },
                            },
                            "required": ["panel", "severity"],
                        },
                    }
                },
                "required": ["damage_items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_claim_pdf",
            "description": "Draft a claim PDF (or .txt fallback) and save it to disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string"},
                    "customer_name": {"type": "string"},
                    "vehicle": {"type": "string"},
                    "vin": {"type": "string"},
                    "damage_summary": {"type": "string"},
                    "cost_estimate": {
                        "type": "object",
                        "properties": {
                            "line_items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "panel": {"type": "string"},
                                        "severity": {"type": "string"},
                                        "part_cost_usd": {"type": "number"},
                                        "labor_hours": {"type": "number"},
                                        "labor_cost_usd": {"type": "number"},
                                    },
                                    "required": [
                                        "panel",
                                        "severity",
                                        "part_cost_usd",
                                        "labor_hours",
                                        "labor_cost_usd",
                                    ],
                                },
                            },
                            "parts_total_usd": {"type": "number"},
                            "labor_total_usd": {"type": "number"},
                            "grand_total_usd": {"type": "number"},
                        },
                        "required": [
                            "line_items",
                            "parts_total_usd",
                            "labor_total_usd",
                            "grand_total_usd",
                        ],
                    },
                    "deductible_usd": {"type": "number"},
                },
                "required": [
                    "claim_id",
                    "customer_name",
                    "vehicle",
                    "vin",
                    "damage_summary",
                    "cost_estimate",
                    "deductible_usd",
                ],
            },
        },
    },
]


def dispatch(name: str, args: dict) -> dict:
    """Local tool dispatcher — mirrors what Foundry does in-cluster."""
    if name == "vss_analyze_video":
        return {"text": vss_analyze_video(**args)}
    if name == "lookup_policy":
        return lookup_policy(**args)
    if name == "estimate_repair_cost":
        return estimate_repair_cost(**args)
    if name == "draft_claim_pdf":
        return draft_claim_pdf(**args)
    raise ValueError(f"Unknown tool: {name}")


def run(video_path: Path, policy_fallback: str | None = None) -> list[dict]:
    """Drive the full claims-triage agent loop against `video_path`.

    Returns the final message history so callers (test harnesses, eval
    drivers) can introspect which tools the agent invoked.
    """
    client, model = make_client()
    video_id = upload_video_bytes(video_path)

    user_msg = (
        f"A customer just submitted a damage claim video. The video_id is '{video_id}' "
        f"(it has been uploaded to VST). Please run the triage workflow."
    )
    if policy_fallback:
        # Provides the agent a fallback policy number to use if VIN isn't
        # extractable from the video (common for stock footage). Lets the
        # smoke test exercise the full claim-drafting flow without an
        # interactive turn.
        user_msg += (
            f" If the VIN is not visible in the video, use policy number "
            f"'{policy_fallback}' as a fallback when calling lookup_policy."
        )
    messages: list[dict] = [
        {"role": "system", "content": INSTRUCTIONS},
        {"role": "user", "content": user_msg},
    ]

    print(f"\n=== Starting agent loop (model={model}, video_id={video_id}) ===\n")

    for _ in range(25):  # hard cap to prevent runaway loops
        # gpt-5.x and other "reasoning" models require max_completion_tokens
        # and do not accept `temperature`. Fall back to legacy params for
        # non-reasoning models (gpt-4.1, gpt-4o, etc.).
        is_reasoning = model.lower().startswith(("gpt-5", "o1", "o3", "o4"))
        common_kwargs: dict = dict(
            model=model, messages=messages, tools=TOOLS, tool_choice="auto"
        )
        if is_reasoning:
            common_kwargs["max_completion_tokens"] = 4000
        else:
            common_kwargs["temperature"] = 0.2
            common_kwargs["max_tokens"] = 2000

        resp = client.chat.completions.create(**common_kwargs)
        msg = resp.choices[0].message

        # Append the assistant message (text + any tool_calls) to the history.
        assistant_entry: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_entry)

        if msg.content:
            print(f"[agent] {msg.content}")

        if not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            print(f"[tool] {tc.function.name}({json.dumps(args)[:120]}...)")
            try:
                result = dispatch(tc.function.name, args)
                content = json.dumps(result)
            except Exception as e:
                content = json.dumps({"error": repr(e)})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": content,
                }
            )
    else:
        print("\n[!] Hit 25-turn cap — agent didn't reach a final answer.")

    print("\n=== Agent finished ===")
    return messages


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(
            "Usage: python local_runner.py <path/to/damage.mp4> [policy-fallback]\n"
            "Example: python local_runner.py wreck.mp4 POL-2025-44912"
        )
    video = Path(sys.argv[1]).expanduser().resolve()
    fallback = sys.argv[2] if len(sys.argv) == 3 else None
    run(video, policy_fallback=fallback)

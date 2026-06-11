# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tools for the Insurance Claims Triage hosted agent.

All tools use Microsoft Agent Framework's @tool decorator and pydantic Field
type annotations so the model gets accurate signatures and descriptions.

Four tools:

  - vss_analyze_video(video_id, question)   — calls the NVIDIA VSS Agent
                                              running on AKS to do video Q&A
  - lookup_policy(vin? | policy_number?)    — mock policy DB lookup
  - estimate_repair_cost(damage_items)      — parts + labor estimator
  - draft_claim_pdf(...)                    — writes the draft claim PDF

The video upload step is NOT exposed here. In a real Foundry chat UX, the
customer uploads via a separate web form / mobile flow that PUTs the video
to VST's presigned URL, then references the resulting `video_id` in their
agent thread. The agent only needs to ANALYZE.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
from agent_framework import tool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

VSS_BASE_URL = os.environ.get("VSS_BASE_URL", "").rstrip("/")
HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=10.0)

# Mock-VSS mode (env-controlled). When on, vss_analyze_video() returns canned
# damage prose from fixtures/vss/<video_id>.txt instead of calling AKS. Lets
# the agent run end-to-end (still costs LLM tokens for the orchestration)
# while the GPU cluster is shut down to save cost. Re-capture fixtures with
# scripts/capture_vss_fixtures.py when VSS itself changes materially.
MOCK_VSS = os.environ.get("MOCK_VSS", "").strip().lower() in ("1", "true", "yes", "on")

_HERE = Path(__file__).parent
_POLICIES_PATH = _HERE / "sample_data" / "policies.json"
_OUTPUT_DIR = _HERE / "output"
_VSS_FIXTURES_DIR = _HERE / "fixtures" / "vss"

# Fallback prose returned when MOCK_VSS=true but the requested video_id has
# no captured fixture. Generic enough that the downstream orchestration
# (estimate_repair_cost, draft_claim_pdf) still produces a valid claim.
_MOCK_VSS_FALLBACK = (
    "The video '{video_id}' shows minor cosmetic damage: a small dent on "
    "the front bumper and surface scratches on the driver door. No visible "
    "VIN. Severity: minor."
)

# VSS's LVS chat agent wraps its planning + tool traces in <agent-think>
# blocks. The actual user-facing answer comes AFTER the closing tag. Strip.
_AGENT_THINK_RE = re.compile(r"<agent-think\b.*?</agent-think>", flags=re.DOTALL | re.IGNORECASE)


def _require_vss_base_url() -> str:
    if not VSS_BASE_URL:
        raise RuntimeError("Set VSS_BASE_URL (e.g. http://vss.<EXTERNAL_HOST>.nip.io).")
    return VSS_BASE_URL


class CostLineItem(BaseModel):
    panel: str = Field(description="Lowercase body-shop panel name.")
    severity: str = Field(description="Damage severity: minor, moderate, or severe.")
    part_cost_usd: float = Field(description="Estimated parts cost in USD.")
    labor_hours: float = Field(description="Estimated labor hours.")
    labor_cost_usd: float = Field(description="Estimated labor cost in USD.")


class CostEstimate(BaseModel):
    line_items: list[CostLineItem] = Field(description="Per-panel cost breakdown.")
    parts_total_usd: float = Field(description="Total parts cost in USD.")
    labor_total_usd: float = Field(description="Total labor cost in USD.")
    grand_total_usd: float = Field(description="Total repair cost in USD.")


def _cost_estimate_to_dict(cost_estimate: CostEstimate | dict[str, Any]) -> dict[str, Any]:
    if isinstance(cost_estimate, CostEstimate):
        return cost_estimate.model_dump()
    return cost_estimate


# --------------------------------------------------------------------------
# VSS sub-agent tool
# --------------------------------------------------------------------------

@tool(approval_mode="never_require")
def vss_analyze_video(
    video_id: Annotated[
        str, Field(description="Identifier of a video previously uploaded to VST.")
    ],
    question: Annotated[
        str,
        Field(
            description=(
                "Plain-English question for the VSS Agent. For insurance claims, "
                "prefer prompts like: 'What damage is visible to the vehicle in "
                "video <id>? Describe each damaged panel and rate severity. "
                "Transcribe any visible VIN.' VSS uses cosmos-reason2-8b VLM "
                "internally."
            )
        ),
    ],
) -> str:
    """Ask the NVIDIA VSS Agent about a previously uploaded video.

    Returns prose. The wrapper strips VSS's internal chain-of-thought tags
    (`<agent-think>`) before returning, so callers see only the final answer.

    When MOCK_VSS=true, skips the network call entirely and returns a canned
    response from fixtures/vss/<video_id>.txt (falls back to a generic
    "minor damage" string for unknown video_ids). Lets the agent run while
    the AKS GPU cluster is shut down for cost savings.
    """
    if MOCK_VSS:
        fixture = _VSS_FIXTURES_DIR / f"{video_id}.txt"
        if fixture.is_file():
            text = fixture.read_text().strip()
            source = f"fixtures/vss/{video_id}.txt"
        else:
            text = _MOCK_VSS_FALLBACK.format(video_id=video_id)
            source = "fallback (no fixture for this video_id)"
        # Loud, easy-to-grep log so App Insights traces (and `azd ai agent
        # invoke --local` stderr) show exactly which code path fired.
        logger.info(
            "MOCK_VSS=true: vss_analyze_video bypassed VSS HTTP call "
            "for video_id=%s, source=%s, length=%d",
            video_id, source, len(text),
        )
        # Prefix the returned text with a visible marker so the agent's
        # final summary (and the App Insights tool-output traces) clearly
        # signal that this run was mocked, not live VSS.
        return f"[MOCK_VSS — replayed from {source}]\n\n{text}"

    prompt = f"Video reference: '{video_id}'.\n\n{question}"
    payload = {"messages": [{"role": "user", "content": prompt}]}
    final_chunks: list[str] = []
    vss_base_url = _require_vss_base_url()

    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        with client.stream(
            "POST",
            f"{vss_base_url}/chat/stream",
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
                    text = delta.get("content") or choices[0].get("message", {}).get("content")
                    if text:
                        final_chunks.append(text)

    raw = "".join(final_chunks).strip()
    cleaned = _AGENT_THINK_RE.sub("", raw).strip()
    return cleaned or raw or "[no streamed content from VSS]"


# --------------------------------------------------------------------------
# Mock business tools
# --------------------------------------------------------------------------

@tool(approval_mode="never_require")
def lookup_policy(
    vin: Annotated[
        str | None,
        Field(default=None, description="Vehicle Identification Number (17 chars)."),
    ] = None,
    policy_number: Annotated[
        str | None,
        Field(default=None, description="Customer's policy number (e.g. POL-2025-44912)."),
    ] = None,
) -> dict:
    """Look up an insurance policy by VIN or policy number. Returns customer,
    vehicle, coverage tier, and deductible. Returns `{found: false}` if no match.
    """
    if not vin and not policy_number:
        return {"found": False, "error": "Provide either vin or policy_number"}

    policies = json.loads(_POLICIES_PATH.read_text())
    needle_vin = (vin or "").upper().strip()
    needle_pn = (policy_number or "").upper().strip()
    for p in policies:
        if needle_vin and p["vin"].upper() == needle_vin:
            return {"found": True, **p}
        if needle_pn and p["policy_number"].upper() == needle_pn:
            return {"found": True, **p}
    return {"found": False, "error": f"No policy matches vin={vin!r} policy_number={policy_number!r}"}


_PART_PRICES_USD = {
    "front bumper": 850, "rear bumper": 720, "hood": 1200,
    "driver door": 950, "passenger door": 950, "rear quarter panel": 1400,
    "fender": 680, "windshield": 540, "headlight": 380, "tail light": 220,
    "side mirror": 180, "wheel": 320, "grille": 420,
}
_SEVERITY_MULTIPLIER = {"minor": 0.4, "moderate": 0.8, "severe": 1.3}
_LABOR_HOURLY_USD = 95
_LABOR_HOURS_BY_SEVERITY = {"minor": 1.5, "moderate": 4.0, "severe": 8.0}


@tool(approval_mode="never_require")
def estimate_repair_cost(
    damage_items: Annotated[
        list[dict[str, str]],
        Field(
            description=(
                "List of damaged panels. Each item must be "
                "`{\"panel\": \"<lowercase body-shop name>\", \"severity\": "
                "\"minor\"|\"moderate\"|\"severe\"}`."
            )
        ),
    ],
) -> dict:
    """Estimate parts + labor repair cost from a list of damage items."""
    line_items = []
    parts_total = 0.0
    labor_total = 0.0
    for item in damage_items:
        panel = item.get("panel", "").lower().strip()
        severity = item.get("severity", "moderate").lower().strip()
        base_part = _PART_PRICES_USD.get(panel, 600)  # unknown panel → $600 base
        sev_mult = _SEVERITY_MULTIPLIER.get(severity, 0.8)
        part_cost = round(base_part * sev_mult, 2)
        labor_hours = _LABOR_HOURS_BY_SEVERITY.get(severity, 4.0)
        labor_cost = round(labor_hours * _LABOR_HOURLY_USD, 2)
        line_items.append({
            "panel": panel, "severity": severity,
            "part_cost_usd": part_cost,
            "labor_hours": labor_hours, "labor_cost_usd": labor_cost,
        })
        parts_total += part_cost
        labor_total += labor_cost
    return {
        "line_items": line_items,
        "parts_total_usd": round(parts_total, 2),
        "labor_total_usd": round(labor_total, 2),
        "grand_total_usd": round(parts_total + labor_total, 2),
    }


@tool(approval_mode="never_require")
def draft_claim_pdf(
    claim_id: Annotated[str, Field(description="Claim ID, e.g. CLM-20260516-44912.")],
    customer_name: Annotated[str, Field(description="Customer full name.")],
    vehicle: Annotated[str, Field(description="Year + make + model.")],
    vin: Annotated[str, Field(description="VIN from the policy lookup.")],
    damage_summary: Annotated[
        str, Field(description="2-3 sentence prose summary of the damage findings.")
    ],
    cost_estimate: Annotated[
        CostEstimate,
        Field(
            description=(
                "Exact full object returned by estimate_repair_cost. Must include "
                "line_items, parts_total_usd, labor_total_usd, and grand_total_usd."
            )
        ),
    ],
    deductible_usd: Annotated[float, Field(description="Policy deductible.")],
) -> dict:
    """Write a draft claim PDF (or .txt fallback) to ./output/<claim_id>.{pdf,txt}.

    Falls back to a plain text file if reportlab isn't installed.
    Returns `{"path": "<file>", "format": "pdf"|"txt"}`.
    """
    _OUTPUT_DIR.mkdir(exist_ok=True)
    cost = _cost_estimate_to_dict(cost_estimate)
    payable = max(cost["grand_total_usd"] - deductible_usd, 0)
    lines = [
        f"Claim ID:        {claim_id}",
        f"Date filed:      {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Customer:        {customer_name}",
        f"Vehicle:         {vehicle}",
        f"VIN:             {vin}",
        "",
        "Damage summary",
        "--------------",
        damage_summary,
        "",
        "Estimated repair cost",
        "---------------------",
    ]
    for li in cost["line_items"]:
        lines.append(
            f"  - {li['panel']} ({li['severity']}): "
            f"parts ${li['part_cost_usd']:.2f} + "
            f"labor {li['labor_hours']}h @ ${_LABOR_HOURLY_USD}/h = ${li['labor_cost_usd']:.2f}"
        )
    lines += [
        "",
        f"  Parts total:    ${cost['parts_total_usd']:.2f}",
        f"  Labor total:    ${cost['labor_total_usd']:.2f}",
        f"  Grand total:    ${cost['grand_total_usd']:.2f}",
        f"  Deductible:     ${deductible_usd:.2f}",
        f"  Payable:        ${payable:.2f}",
        "",
        "Status: DRAFT — pending adjuster review.",
    ]
    body = "\n".join(lines)

    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.pdfgen import canvas

        path = _OUTPUT_DIR / f"{claim_id}.pdf"
        c = canvas.Canvas(str(path), pagesize=LETTER)
        c.setFont("Helvetica", 10)
        y = 750
        for line in body.split("\n"):
            c.drawString(50, y, line[:110])
            y -= 14
            if y < 50:
                c.showPage()
                c.setFont("Helvetica", 10)
                y = 750
        c.save()
        return {"path": str(path), "format": "pdf"}
    except ImportError:
        path = _OUTPUT_DIR / f"{claim_id}.txt"
        path.write_text(body)
        return {"path": str(path), "format": "txt"}

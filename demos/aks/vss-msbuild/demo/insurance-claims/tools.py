# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Mock business tools for the Insurance Claims master agent.

These represent the non-video parts of the workflow: looking up a customer's
policy, estimating repair costs, and drafting a claim PDF. For a real demo
you'd back these with Cosmos DB / SQL / a parts-pricing API. They are written
as plain Python callables so they can be registered as:

  - Foundry function tools (preferred for the demo, see agent.yaml)
  - OpenAI tool-use tools (drop into the `tools=` array)
  - MCP tools (wrap the same way as vss_mcp_server.py)

Keep these stateless and side-effect-free except for `draft_claim_pdf`,
which writes a file under ./output/.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).parent
_POLICIES_PATH = _HERE / "sample_data" / "policies.json"
_OUTPUT_DIR = _HERE / "output"


def lookup_policy(vin: str | None = None, policy_number: str | None = None) -> dict:
    """Look up a policy by VIN or by policy number.

    Returns {"found": False} if no match. On match, returns the policy dict
    with customer name, vehicle, coverage tier, and deductible.

    For the demo: backed by sample_data/policies.json. Replace with your CRM
    of choice (Salesforce, Dynamics, Cosmos DB) for a real integration.
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


# Crude parts pricing table. In production: hit a parts/labor API like
# CCC or Mitchell, or your shop's pricing engine.
_PART_PRICES_USD = {
    "front bumper": 850,
    "rear bumper": 720,
    "hood": 1200,
    "driver door": 950,
    "passenger door": 950,
    "rear quarter panel": 1400,
    "fender": 680,
    "windshield": 540,
    "headlight": 380,
    "tail light": 220,
    "side mirror": 180,
    "wheel": 320,
}
_SEVERITY_MULTIPLIER = {"minor": 0.4, "moderate": 0.8, "severe": 1.3}
_LABOR_HOURLY_USD = 95
_LABOR_HOURS_BY_SEVERITY = {"minor": 1.5, "moderate": 4.0, "severe": 8.0}


def estimate_repair_cost(damage_items: list[dict]) -> dict:
    """Estimate repair cost from a list of damaged-panel descriptors.

    Args:
        damage_items: list of {"panel": str, "severity": "minor"|"moderate"|"severe"}.
                      The VSS agent's structured output should produce items in this shape;
                      the master agent is responsible for normalizing free-text to this format.

    Returns:
        {"line_items": [...], "parts_total": $, "labor_total": $, "grand_total": $}
    """
    line_items = []
    parts_total = 0.0
    labor_total = 0.0
    for item in damage_items:
        panel = item.get("panel", "").lower().strip()
        severity = item.get("severity", "moderate").lower().strip()
        base_part = _PART_PRICES_USD.get(panel)
        if base_part is None:
            # Unknown panel — log it as "other body" at moderate cost.
            base_part = 600
        sev_mult = _SEVERITY_MULTIPLIER.get(severity, 0.8)
        part_cost = round(base_part * sev_mult, 2)
        labor_hours = _LABOR_HOURS_BY_SEVERITY.get(severity, 4.0)
        labor_cost = round(labor_hours * _LABOR_HOURLY_USD, 2)
        line_items.append(
            {
                "panel": panel,
                "severity": severity,
                "part_cost_usd": part_cost,
                "labor_hours": labor_hours,
                "labor_cost_usd": labor_cost,
            }
        )
        parts_total += part_cost
        labor_total += labor_cost
    return {
        "line_items": line_items,
        "parts_total_usd": round(parts_total, 2),
        "labor_total_usd": round(labor_total, 2),
        "grand_total_usd": round(parts_total + labor_total, 2),
    }


def draft_claim_pdf(
    claim_id: str,
    customer_name: str,
    vehicle: str,
    vin: str,
    damage_summary: str,
    cost_estimate: dict,
    deductible_usd: float,
) -> dict:
    """Write a minimal claim PDF to ./output/<claim_id>.pdf (or .txt fallback).

    Falls back to a plain text file if reportlab isn't installed, so the demo
    runs out of the box. Returns {"path": "<file>", "format": "pdf"|"txt"}.
    """
    _OUTPUT_DIR.mkdir(exist_ok=True)
    payable = max(cost_estimate["grand_total_usd"] - deductible_usd, 0)
    body_lines = [
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
    for li in cost_estimate["line_items"]:
        body_lines.append(
            f"  - {li['panel']} ({li['severity']}): "
            f"parts ${li['part_cost_usd']:.2f} + "
            f"labor {li['labor_hours']}h @ ${_LABOR_HOURLY_USD}/h = ${li['labor_cost_usd']:.2f}"
        )
    body_lines += [
        "",
        f"  Parts total:    ${cost_estimate['parts_total_usd']:.2f}",
        f"  Labor total:    ${cost_estimate['labor_total_usd']:.2f}",
        f"  Grand total:    ${cost_estimate['grand_total_usd']:.2f}",
        f"  Deductible:     ${deductible_usd:.2f}",
        f"  Payable:        ${payable:.2f}",
        "",
        "Status: DRAFT — pending adjuster review.",
    ]
    body = "\n".join(body_lines)

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

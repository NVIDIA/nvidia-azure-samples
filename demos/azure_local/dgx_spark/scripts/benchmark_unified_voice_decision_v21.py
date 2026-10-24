#!/usr/bin/env python3
"""Qualify the single-pass acoustic-gate/tool/speech candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

from scripts.benchmark_nemotron_decision_contract import (
    GUARD_ALTERNATES,
    GUARD_DISAGREEMENT_CASES,
    LISTEN_CASES,
    STATEMENT_CASES,
    SYSTEM_PROMPT,
    request_decision,
)
from scripts.benchmark_system_priority_contract import REPEAT_CASES, REPEAT_SYSTEM_PROMPT, TOOL_CASES, normalized
from scripts.benchmark_tool_router_models import CASES
from scripts.nemotron_voicechat_pipeline import unified_voice_decision_prompt_v21


def route(decision: dict) -> str:
    if str(decision.get("action") or "") == "listen":
        return "listen"
    return str(decision.get("tool") or "").strip() or "no_tool"


def call(base_url: str, model: str, primary: str, fast: str, policy: str, previous: str, max_tokens: int):
    return request_decision(
        base_url,
        model,
        unified_voice_decision_prompt_v21(primary, fast, "wifi", policy, previous),
        max_tokens=max_tokens,
        system_prompt=policy,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []

    def add(kind: str, primary: str, fast: str, expected: str, policy: str = SYSTEM_PROMPT, previous: str = "", exact: bool = False, salient=()):
        decision, seconds, usage = call(args.base_url, args.model, primary, fast, policy, previous, args.max_tokens)
        predicted = route(decision)
        spoken = str(decision.get("say") or "").strip()
        correct = predicted == expected
        if exact:
            correct = correct and normalized(spoken) == normalized(primary)
        if salient:
            correct = correct and all(token in spoken.lower() for token in salient)
        rows.append({
            "kind": kind,
            "primary": primary,
            "fast": fast,
            "previous": previous,
            "expected": expected,
            "predicted": predicted,
            "spoken": spoken,
            "correct": correct,
            "seconds": round(seconds, 4),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "decision": decision,
        })

    for text, expected in CASES:
        add("route", text, text, expected)
    for text, salient in STATEMENT_CASES:
        add("statement", text, text, "no_tool", salient=salient)
    for text, expected_action in LISTEN_CASES:
        expected = "listen" if expected_action == "listen" else "no_tool"
        add("acoustic", text, GUARD_ALTERNATES.get(text, text), expected)
    for primary, fast, expected_action in GUARD_DISAGREEMENT_CASES:
        add("disagreement", primary, fast, "no_tool" if expected_action == "proceed" else "listen")
    add("normal_echo", "Blue triangle.", "Blue triangle.", "listen", previous="Blue triangle.")
    add("new_repeat_request", "Please repeat blue triangle.", "Please repeat blue triangle.", "no_tool", previous="Blue triangle.")
    add("relay_echo", "Blue triangle.", "Blue triangle.", "no_tool", policy=REPEAT_SYSTEM_PROMPT, previous="Blue triangle.", exact=True)
    add("relay_corruption", "Circular ladders teach water to sleep.", "Circular ladders teach water asleep.", "listen", policy=REPEAT_SYSTEM_PROMPT, previous="Blue triangle.")
    for text in REPEAT_CASES:
        add("exact_repeat", text, text, "no_tool", policy=REPEAT_SYSTEM_PROMPT, exact=True)
    for text, expected in TOOL_CASES:
        add("repeat_tool_boundary", text, text, expected, policy=REPEAT_SYSTEM_PROMPT)

    by_kind = {}
    for kind in sorted({row["kind"] for row in rows}):
        group = [row for row in rows if row["kind"] == kind]
        by_kind[kind] = {
            "cases": len(group),
            "correct": sum(row["correct"] for row in group),
            "accuracy": round(sum(row["correct"] for row in group) / len(group), 4),
        }
    latencies = [row["seconds"] for row in rows]
    correct = sum(row["correct"] for row in rows)
    payload = {
        "model": args.model,
        "contract": "unified_voice_decision_v21_candidate",
        "max_tokens": args.max_tokens,
        "cases": len(rows),
        "correct": correct,
        "accuracy": round(correct / len(rows), 4),
        "median_seconds": round(statistics.median(latencies), 4),
        "p95_seconds": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 4),
        "by_kind": by_kind,
        "deterministic_matcher": False,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0 if payload["accuracy"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

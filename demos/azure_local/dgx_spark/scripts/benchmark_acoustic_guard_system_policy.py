#!/usr/bin/env python3
"""Verify model-only echo suppression versus intentional telephone relay policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

from scripts.benchmark_nemotron_decision_contract import request_decision
from scripts.nemotron_voicechat_pipeline import acoustic_loop_guard_prompt


NORMAL_POLICY = "Answer directly and keep responses concise."
RELAY_POLICY = "Repeat every no-tool utterance exactly for a telephone relay."
CASES = (
    (
        "While violet thunder rolled beyond the harbor.",
        "While violet thunder rolled beyond the harbor.",
        "While violet thunder rolled beyond the harbor.",
        RELAY_POLICY,
        "proceed",
    ),
    (
        "While violet thunder rolled beyond the harbor.",
        "While violet thunder rolled beyond the harbor.",
        "While violet thunder rolled beyond the harbor.",
        NORMAL_POLICY,
        "listen",
    ),
    (
        "",
        "Helena arranged nine silver telescopes beside the cracked piano.",
        "Helena arranged nine silver telescopes beside the cracked piano.",
        RELAY_POLICY,
        "proceed",
    ),
    (
        "Circular ladders teach water to sleep.",
        "Circular ladders teach water asleep.",
        "A coherent prior reply.",
        RELAY_POLICY,
        "listen",
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for run in range(1, args.runs + 1):
        for primary, fast, previous, policy, expected in CASES:
            decision, seconds, usage = request_decision(
                args.base_url,
                args.model,
                acoustic_loop_guard_prompt(primary, fast, previous, policy),
                max_tokens=24,
            )
            predicted = str(decision.get("action") or "")
            rows.append({
                "run": run,
                "primary": primary,
                "fast": fast,
                "previous": previous,
                "system_policy": policy,
                "expected": expected,
                "predicted": predicted,
                "correct": predicted == expected,
                "seconds": round(seconds, 4),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            })
    latencies = [row["seconds"] for row in rows]
    correct = sum(row["correct"] for row in rows)
    payload = {
        "model": args.model,
        "runs": args.runs,
        "cases": len(rows),
        "correct": correct,
        "accuracy": round(correct / len(rows), 4),
        "median_seconds": round(statistics.median(latencies), 4),
        "model_only": True,
        "deterministic_matcher": False,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0 if payload["accuracy"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Verify model-selected current_time granularity without transcript matchers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

from scripts.benchmark_nemotron_decision_contract import request_decision
from scripts.nemotron_voicechat_pipeline import sparse_voice_decision_prompt_v31


CASES = (
    ("What is the exact local time now?", False, True),
    ("Tell me today's date and timezone.", True, True),
    ("What time is it without the timezone?", False, False),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = []
    for run in range(1, args.runs + 1):
        for heard, expected_date, expected_timezone in CASES:
            decision, seconds, usage = request_decision(
                args.base_url,
                args.model,
                sparse_voice_decision_prompt_v31(heard, "wifi", ""),
                max_tokens=args.max_tokens,
            )
            tool_args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
            actual_date = tool_args.get("include_date") is True
            actual_timezone = tool_args.get("omit_timezone") is not True
            correct = (
                decision.get("tool") == "current_time"
                and actual_date == expected_date
                and actual_timezone == expected_timezone
            )
            rows.append({
                "run": run,
                "input": heard,
                "expected": {
                    "tool": "current_time",
                    "include_date": expected_date,
                    "include_timezone": expected_timezone,
                },
                "decision": decision,
                "resolved_args": {
                    "include_date": actual_date,
                    "include_timezone": actual_timezone,
                },
                "correct": correct,
                "seconds": round(seconds, 4),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            })

    latencies = [row["seconds"] for row in rows]
    payload = {
        "model": args.model,
        "variant": "sparse_v31",
        "runs": args.runs,
        "cases": len(rows),
        "correct": sum(row["correct"] for row in rows),
        "accuracy": round(sum(row["correct"] for row in rows) / len(rows), 4),
        "median_seconds": round(statistics.median(latencies), 4),
        "deterministic_intent_matcher": False,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0 if payload["accuracy"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Test whether the model obeys system wording while preserving tool boundaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

from scripts.benchmark_nemotron_decision_contract import (
    decoded_route,
    request_decision,
    variant_prompt,
)
from scripts.nemotron_voicechat_pipeline import (
    sparse_voice_decision_prompt_v17,
    sparse_voice_decision_prompt_v18,
    sparse_voice_decision_prompt_v19,
    sparse_voice_decision_prompt_v20,
    sparse_voice_decision_prompt_v22,
    sparse_voice_decision_prompt_v23,
    sparse_voice_decision_prompt_v24,
    sparse_voice_decision_prompt_v25,
    sparse_voice_decision_prompt_v26,
    sparse_voice_decision_prompt_v27,
    sparse_voice_decision_prompt_v28,
    sparse_voice_decision_prompt_v29,
    sparse_voice_decision_prompt_v30,
    sparse_voice_decision_prompt_v31,
)


REPEAT_SYSTEM_PROMPT = (
    "For every no-tool utterance, speak exactly the complete utterance word for word, with no preamble, "
    "commentary, correction, or paraphrase. Tool capability boundaries still apply."
)
REPEAT_CASES = (
    "While Vale of Thunder rolled beyond the harbor, Elina arranged nine silver telescopes beside the cracked "
    "piano, then whispered the number forty seven to Marcus.",
    "She sells seashells on the seashore.",
    "The laptop rests on the desk beside a closed notebook.",
    "First count the red folders, then after a careful pause count the blue folders beside them.",
)
TOOL_CASES = (
    ("What is the exact local time now?", "current_time"),
    ("Report the current GPU and service status.", "runtime_stats"),
)


def normalized(text: str) -> str:
    return " ".join(text.casefold().split()).strip(" .!?\"'")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--variant", default="sparse_v14")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = []
    for run in range(1, args.runs + 1):
        for heard in REPEAT_CASES:
            prompt = (
                sparse_voice_decision_prompt_v31(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v31"
                else
                sparse_voice_decision_prompt_v30(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v30"
                else sparse_voice_decision_prompt_v29(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v29"
                else sparse_voice_decision_prompt_v28(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v28"
                else sparse_voice_decision_prompt_v27(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v27"
                else sparse_voice_decision_prompt_v26(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v26"
                else sparse_voice_decision_prompt_v25(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v25"
                else sparse_voice_decision_prompt_v24(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v24"
                else sparse_voice_decision_prompt_v23(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v23"
                else sparse_voice_decision_prompt_v22(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v22"
                else sparse_voice_decision_prompt_v20(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v20"
                else sparse_voice_decision_prompt_v19(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v19"
                else sparse_voice_decision_prompt_v18(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v18"
                else sparse_voice_decision_prompt_v17(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v17"
                else variant_prompt(heard, args.variant)
            )
            decision, seconds, usage = request_decision(
                args.base_url,
                args.model,
                prompt,
                max_tokens=args.max_tokens,
                system_prompt=REPEAT_SYSTEM_PROMPT,
            )
            predicted, spoken = decoded_route(decision, args.variant)
            exact = predicted == "no_tool" and normalized(spoken) == normalized(heard)
            rows.append({
                "run": run,
                "kind": "exact_repeat",
                "input": heard,
                "expected": heard,
                "predicted": predicted,
                "spoken": spoken,
                "correct": exact,
                "seconds": round(seconds, 4),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            })
        for heard, expected in TOOL_CASES:
            prompt = (
                sparse_voice_decision_prompt_v31(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v31"
                else
                sparse_voice_decision_prompt_v30(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v30"
                else sparse_voice_decision_prompt_v29(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v29"
                else sparse_voice_decision_prompt_v28(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v28"
                else sparse_voice_decision_prompt_v27(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v27"
                else sparse_voice_decision_prompt_v26(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v26"
                else sparse_voice_decision_prompt_v25(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v25"
                else sparse_voice_decision_prompt_v24(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v24"
                else sparse_voice_decision_prompt_v23(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v23"
                else sparse_voice_decision_prompt_v22(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v22"
                else sparse_voice_decision_prompt_v20(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v20"
                else sparse_voice_decision_prompt_v19(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v19"
                else sparse_voice_decision_prompt_v18(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v18"
                else sparse_voice_decision_prompt_v17(heard, "wifi", REPEAT_SYSTEM_PROMPT)
                if args.variant == "sparse_v17"
                else variant_prompt(heard, args.variant)
            )
            decision, seconds, usage = request_decision(
                args.base_url,
                args.model,
                prompt,
                max_tokens=args.max_tokens,
                system_prompt=REPEAT_SYSTEM_PROMPT,
            )
            predicted, spoken = decoded_route(decision, args.variant)
            rows.append({
                "run": run,
                "kind": "tool_boundary",
                "input": heard,
                "expected": expected,
                "predicted": predicted,
                "spoken": spoken,
                "correct": predicted == expected,
                "seconds": round(seconds, 4),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            })
    repeat_rows = [row for row in rows if row["kind"] == "exact_repeat"]
    tool_rows = [row for row in rows if row["kind"] == "tool_boundary"]
    latencies = [row["seconds"] for row in rows]
    payload = {
        "model": args.model,
        "variant": args.variant,
        "runs": args.runs,
        "max_tokens": args.max_tokens,
        "system_prompt": REPEAT_SYSTEM_PROMPT,
        "exact_repeat_cases": len(repeat_rows),
        "exact_repeat_correct": sum(row["correct"] for row in repeat_rows),
        "exact_repeat_accuracy": round(sum(row["correct"] for row in repeat_rows) / len(repeat_rows), 4),
        "tool_boundary_cases": len(tool_rows),
        "tool_boundary_correct": sum(row["correct"] for row in tool_rows),
        "tool_boundary_accuracy": round(sum(row["correct"] for row in tool_rows) / len(tool_rows), 4),
        "median_seconds": round(statistics.median(latencies), 4),
        "p95_seconds": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 4),
        "deterministic_intent_matcher": False,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0 if payload["exact_repeat_accuracy"] == 1.0 and payload["tool_boundary_accuracy"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

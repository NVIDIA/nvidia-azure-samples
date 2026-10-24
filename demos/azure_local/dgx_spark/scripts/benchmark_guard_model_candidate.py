#!/usr/bin/env python3
"""Benchmark a local Ollama model against the full production acoustic guard contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time
from urllib.request import Request, urlopen

from scripts.benchmark_acoustic_guard_v4 import ECHO_CASES
from scripts.benchmark_nemotron_decision_contract import (
    GUARD_ALTERNATES,
    GUARD_DISAGREEMENT_CASES,
    LISTEN_CASES,
)
from scripts.benchmark_tool_router_models import CASES
from scripts.nemotron_voicechat_pipeline import acoustic_loop_guard_prompt, parse_model_json


NORMAL_POLICY = "Answer directly and keep responses concise."
RELAY_POLICY = "Repeat every no-tool utterance exactly for a telephone relay."


def cases() -> list[dict]:
    result = [
        {"group": "routing", "primary": text, "fast": text, "previous": "", "policy": NORMAL_POLICY, "expected": "proceed"}
        for text, _expected in CASES
    ]
    result.extend({
        "group": "guard",
        "primary": text,
        "fast": GUARD_ALTERNATES.get(text, text),
        "previous": "",
        "policy": NORMAL_POLICY,
        "expected": "listen" if expected == "listen" else "proceed",
    } for text, expected in LISTEN_CASES)
    result.extend({
        "group": "disagreement",
        "primary": primary,
        "fast": fast,
        "previous": "",
        "policy": NORMAL_POLICY,
        "expected": expected,
    } for primary, fast, expected in GUARD_DISAGREEMENT_CASES)
    result.extend({
        "group": "echo",
        "primary": primary,
        "fast": fast,
        "previous": previous,
        "policy": NORMAL_POLICY,
        "expected": expected,
    } for primary, fast, previous, expected in ECHO_CASES)
    result.extend([
        {
            "group": "relay",
            "primary": "Blue triangle.",
            "fast": "Blue triangle.",
            "previous": "Blue triangle.",
            "policy": RELAY_POLICY,
            "expected": "proceed",
        },
        {
            "group": "relay",
            "primary": "",
            "fast": "Helena arranged nine silver telescopes beside the piano.",
            "previous": "Helena arranged nine silver telescopes beside the piano.",
            "policy": RELAY_POLICY,
            "expected": "proceed",
        },
        {
            "group": "relay",
            "primary": "Circular ladders teach water to sleep.",
            "fast": "Circular ladders teach water asleep.",
            "previous": "Blue triangle.",
            "policy": RELAY_POLICY,
            "expected": "listen",
        },
    ])
    return result


def request_guard(base_url: str, model: str, prompt: str) -> tuple[dict, float, dict]:
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_predict": 24, "num_ctx": 2048},
    }
    started = time.perf_counter()
    req = Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=60) as response:
        raw = json.loads(response.read().decode("utf-8"))
    elapsed = time.perf_counter() - started
    content = str((raw.get("message") or {}).get("content") or "")
    return parse_model_json(content), elapsed, raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="nemotron-mini:latest")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for run in range(1, args.runs + 1):
        for case in cases():
            prompt = acoustic_loop_guard_prompt(
                case["primary"], case["fast"], case["previous"], case["policy"]
            )
            decision, seconds, raw = request_guard(args.base_url, args.model, prompt)
            predicted = str(decision.get("action") or "")
            rows.append({
                "run": run,
                **case,
                "predicted": predicted,
                "correct": predicted == case["expected"],
                "seconds": round(seconds, 4),
                "prompt_eval_count": int(raw.get("prompt_eval_count") or 0),
                "eval_count": int(raw.get("eval_count") or 0),
            })
    errors = [row for row in rows if not row["correct"]]
    groups = {}
    for group in sorted({row["group"] for row in rows}):
        selected = [row for row in rows if row["group"] == group]
        groups[group] = {
            "cases": len(selected),
            "correct": sum(row["correct"] for row in selected),
            "accuracy": round(sum(row["correct"] for row in selected) / len(selected), 4),
        }
    warmed = rows[1:] if len(rows) > 1 else rows
    payload = {
        "model": args.model,
        "contract": "production_acoustic_guard",
        "runs": args.runs,
        "cases": len(rows),
        "correct": len(rows) - len(errors),
        "accuracy": round((len(rows) - len(errors)) / len(rows), 4),
        "median_seconds": round(statistics.median(row["seconds"] for row in warmed), 4),
        "p95_seconds": round(sorted(row["seconds"] for row in warmed)[min(len(warmed) - 1, int(len(warmed) * 0.95))], 4),
        "groups": groups,
        "deterministic_matcher": False,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0 if payload["accuracy"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

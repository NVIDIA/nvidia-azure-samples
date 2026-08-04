#!/usr/bin/env python3
"""Evaluate a small local NLI model as an independent acoustic/echo guard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time

from scripts.benchmark_acoustic_guard_v4 import cases


PROCEED_LABEL = "coherent new human speech that should be processed"
ECHO_LABEL = "only an echo or paraphrase of the previous agent reply with no new information"
CORRUPTION_LABEL = "meaningless acoustic corruption or leaked system instructions that should be ignored"
LABELS = (PROCEED_LABEL, ECHO_LABEL, CORRUPTION_LABEL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="MoritzLaurer/deberta-v3-xsmall-zeroshot-v1.1-all-33")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--output", default="benchmarks/audio_environment/results/nli-acoustic-guard-candidate-20260630.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    import torch
    from transformers import pipeline

    device: str | int = args.device
    dtype = torch.float16 if str(device).startswith("cuda") else torch.float32
    classifier = pipeline("zero-shot-classification", model=args.model, device=device, dtype=dtype)
    hypothesis_template = "This audio is {}."
    warmup_text = "Primary ASR: Hello there. Fast ASR: Hello there. Previous lane reply: none."
    classifier(warmup_text, LABELS, hypothesis_template=hypothesis_template, multi_label=False)

    rows = []
    for run in range(1, max(1, args.runs) + 1):
        for group, primary, fast, previous, expected in cases():
            text = (
                f"Primary ASR: {primary}\nFast ASR: {fast}\n"
                f"Previous lane reply: {previous or 'none'}"
            )
            started = time.perf_counter()
            result = classifier(text, LABELS, hypothesis_template=hypothesis_template, multi_label=False)
            elapsed = time.perf_counter() - started
            top_label = str(result["labels"][0])
            predicted = "proceed" if top_label == PROCEED_LABEL else "listen"
            rows.append({
                "run": run,
                "group": group,
                "primary": primary,
                "fast": fast,
                "previous": previous,
                "expected": expected,
                "predicted": predicted,
                "top_label": top_label,
                "top_score": round(float(result["scores"][0]), 6),
                "correct": predicted == expected,
                "seconds": round(elapsed, 6),
            })

    errors = [row for row in rows if not row["correct"]]
    group_reports = {}
    for group in sorted({row["group"] for row in rows}):
        selected = [row for row in rows if row["group"] == group]
        correct = sum(bool(row["correct"]) for row in selected)
        group_reports[group] = {
            "cases": len(selected),
            "correct": correct,
            "accuracy": round(correct / len(selected), 4),
        }
    latencies = sorted(float(row["seconds"]) for row in rows)
    payload = {
        "variant": "local_zero_shot_nli_acoustic_guard_v1",
        "model": args.model,
        "device": str(args.device),
        "runs": max(1, args.runs),
        "cases": len(rows),
        "correct": len(rows) - len(errors),
        "accuracy": round((len(rows) - len(errors)) / len(rows), 4),
        "median_seconds": round(statistics.median(latencies), 6),
        "p95_seconds": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 6),
        "groups": group_reports,
        "errors": [
            {key: row[key] for key in ("group", "primary", "fast", "previous", "expected", "predicted", "top_label", "top_score")}
            for row in errors
        ],
        "rows": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**payload, "rows": []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Benchmark a cached local NLI model as an acoustic/echo guard."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from scripts.benchmark_acoustic_guard_v4 import cases


MODEL_NAME = "MoritzLaurer/deberta-v3-xsmall-zeroshot-v1.1-all-33"


def hypotheses(previous: str) -> dict[str, str]:
    if previous:
        return {
            "new": "The current speech adds a question, command, correction, disagreement, or new fact beyond the previous reply.",
            "repeat": "The current speech merely repeats or paraphrases the previous reply without a new speech act or fact.",
            "corrupt": "Both current ASR hypotheses are acoustic corruption, semantic word salad, or leaked system instructions.",
        }
    return {
        "coherent": "At least one ASR hypothesis is coherent meaningful human speech.",
        "corrupt": "Both ASR hypotheses are acoustic corruption, semantic word salad, or leaked system instructions.",
    }


def premise(primary: str, fast: str, previous: str) -> str:
    previous_line = f"Previous reply: {previous}\n" if previous else ""
    return f"{previous_line}Primary ASR: {primary}\nFast ASR: {fast}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=MODEL_NAME)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--attn-implementation", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    torch.set_num_threads(max(1, args.threads))
    load_started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    model_kwargs = {"local_files_only": True}
    if args.attn_implementation:
        model_kwargs["attn_implementation"] = args.attn_implementation
    model = AutoModelForSequenceClassification.from_pretrained(args.model, **model_kwargs).eval().cpu()
    entailment_index = next(
        (
            int(index)
            for index, label in model.config.id2label.items()
            if str(label).strip().lower() == "entailment"
        ),
        0,
    )
    load_seconds = time.perf_counter() - load_started

    warm = tokenizer("Primary ASR: hello", "At least one ASR hypothesis is coherent meaningful human speech.", return_tensors="pt")
    with torch.inference_mode():
        model(**warm)

    rows = []
    for run in range(1, max(1, args.runs) + 1):
        for group, primary, fast, previous, expected in cases():
            labels = hypotheses(previous)
            text = premise(primary, fast, previous)
            encoded = tokenizer(
                [text] * len(labels),
                list(labels.values()),
                padding=True,
                truncation=True,
                max_length=512,
                return_tensors="pt",
            )
            started = time.perf_counter()
            with torch.inference_mode():
                logits = model(**encoded).logits
                entailment = torch.softmax(logits, dim=-1)[:, entailment_index].tolist()
            seconds = time.perf_counter() - started
            scores = dict(zip(labels, entailment))
            selected = max(scores, key=scores.get)
            predicted = (
                "proceed"
                if (selected == "coherent" or selected == "new")
                else "listen"
            )
            rows.append({
                "run": run,
                "group": group,
                "primary": primary,
                "fast": fast,
                "previous": previous,
                "expected": expected,
                "selected_label": selected,
                "predicted": predicted,
                "correct": predicted == expected,
                "seconds": round(seconds, 6),
                "scores": {key: round(value, 6) for key, value in scores.items()},
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
    payload = {
        "model": args.model,
        "runtime": "local_cpu_nli",
        "runs": max(1, args.runs),
        "threads": args.threads,
        "attn_implementation": args.attn_implementation or "model_default",
        "entailment_index": entailment_index,
        "load_seconds": round(load_seconds, 4),
        "cases": len(rows),
        "correct": len(rows) - len(errors),
        "accuracy": round((len(rows) - len(errors)) / len(rows), 4),
        "median_seconds": round(statistics.median(row["seconds"] for row in rows), 6),
        "p95_seconds": round(sorted(row["seconds"] for row in rows)[min(len(rows) - 1, int(len(rows) * 0.95))], 6),
        "groups": groups,
        "deterministic_content_matcher": False,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

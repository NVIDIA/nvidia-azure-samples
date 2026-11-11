#!/usr/bin/env python3
"""Benchmark local model-only tool routing without invoking any tools.

The cases are held outside prompts and only the utterance plus candidate labels
are supplied to the NLI model. This is a routing bakeoff, not a controller.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time


CASES = [
    ("Hello there.", "no_tool"),
    ("Thanks, that was helpful.", "no_tool"),
    ("Repeat exactly: cobalt foxes cross the quiet harbor.", "no_tool"),
    ("Explain why the sky looks blue.", "no_tool"),
    ("Write a two-line poem about rain.", "no_tool"),
    ("What is seven times nine?", "no_tool"),
    ("Translate good morning into Spanish.", "no_tool"),
    ("Tell me a short story about a clockmaker.", "no_tool"),
    ("What is the local time right now?", "current_time"),
    ("Tell me today's date and timezone.", "current_time"),
    ("How much GPU memory is the machine using now?", "runtime_stats"),
    ("Is the speech worker currently running?", "runtime_stats"),
    ("What is visible on the desk right now?", "current_snapshot"),
    ("Take a current look at the camera.", "current_snapshot"),
    ("Pan the camera a little to the left.", "camera_ptz"),
    ("Tilt the camera upward.", "camera_ptz"),
    ("Keep the camera focused on the laptop.", "focus_object"),
    ("Track the person in the room.", "focus_object"),
    ("Scan the whole room and report what is present.", "environment_scan"),
    ("Inspect every camera source for anything unusual.", "environment_scan"),
    ("What did the environment agents observe earlier?", "query_environment"),
    ("Compare the recent room observations.", "query_environment"),
    ("Search the web for the latest NVIDIA news.", "web_search"),
    ("Find today's weather forecast online.", "web_search"),
    ("Open and summarize https://example.com/report.", "fetch_url"),
    ("Read the page at https://example.org.", "fetch_url"),
    ("List the files in the current project directory.", "shell_command"),
    ("Show the current process table.", "shell_command"),
]


BINARY_LABELS = [
    "can be answered directly without external tools",
    "requires an external tool, live state, or current external information",
]


TOOL_LABELS = {
    "current_time": "current_time: obtain the live local time, date, or timezone",
    "runtime_stats": "runtime_stats: inspect live machine, GPU, service, or process status",
    "current_snapshot": "current_snapshot: inspect the camera image right now",
    "camera_ptz": "camera_ptz: physically pan or tilt the camera",
    "focus_object": "focus_object: visually track or focus on a named object",
    "environment_scan": "environment_scan: actively scan all camera views or the whole room",
    "query_environment": "query_environment: retrieve prior environment-agent observations",
    "web_search": "web_search: search the internet for current information",
    "fetch_url": "fetch_url: open and read a specific supplied URL",
    "shell_command": "shell_command: inspect local files or run a read-only machine command",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="MoritzLaurer/deberta-v3-xsmall-zeroshot-v1.1-all-33")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def summarize(rows: list[dict]) -> dict:
    latencies = [float(row["seconds"]) for row in rows]
    errors = [row for row in rows if row["expected"] != row["predicted"]]
    ordered = sorted(latencies)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return {
        "cases": len(rows),
        "correct": len(rows) - len(errors),
        "accuracy": round((len(rows) - len(errors)) / max(1, len(rows)), 4),
        "median_seconds": round(statistics.median(latencies), 4),
        "p95_seconds": round(p95, 4),
        "errors": [
            {
                "text": row["text"],
                "expected": row["expected"],
                "predicted": row["predicted"],
                "score": row["score"],
            }
            for row in errors
        ],
    }


def main() -> int:
    args = parse_args()
    import torch
    from transformers import pipeline

    classifier = pipeline(
        "zero-shot-classification",
        model=args.model,
        device=args.device,
        dtype=torch.float16,
    )
    for _ in range(max(0, args.warmup)):
        classifier("Warm up the local routing model.", BINARY_LABELS, hypothesis_template="This request {}.")

    binary_rows = []
    tool_rows = []
    for text, expected in CASES:
        started = time.perf_counter()
        result = classifier(text, BINARY_LABELS, hypothesis_template="This request {}.")
        elapsed = time.perf_counter() - started
        predicted = "no_tool" if result["labels"][0] == BINARY_LABELS[0] else "tool"
        binary_rows.append({
            "text": text,
            "expected": "no_tool" if expected == "no_tool" else "tool",
            "predicted": predicted,
            "score": round(float(result["scores"][0]), 4),
            "seconds": elapsed,
        })
        if expected == "no_tool":
            continue
        labels = list(TOOL_LABELS.values())
        started = time.perf_counter()
        result = classifier(text, labels, hypothesis_template="The correct tool is {}.")
        elapsed = time.perf_counter() - started
        predicted_label = result["labels"][0]
        predicted_tool = next(name for name, label in TOOL_LABELS.items() if label == predicted_label)
        tool_rows.append({
            "text": text,
            "expected": expected,
            "predicted": predicted_tool,
            "score": round(float(result["scores"][0]), 4),
            "seconds": elapsed,
        })

    report = {
        "model": args.model,
        "device": args.device,
        "binary": summarize(binary_rows),
        "tool_selection_on_required_tool_cases": summarize(tool_rows),
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

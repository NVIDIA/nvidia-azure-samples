#!/usr/bin/env python3
"""Benchmark grounded tool-answer contracts against representative evidence."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from urllib.request import Request, urlopen

from scripts.nemotron_voicechat_pipeline import parse_model_json


CASES = [
    ("What is the exact local time now?", "2026-06-30 02:19:14 EDT", ("02:19", "EDT")),
    ("How much GPU memory is being used?", "GPU memory used: 12.4 GB of 128 GB.", ("12.4", "128")),
    ("What is visible on the desk?", "Current camera evidence: a laptop and closed notebook are on the desk.", ("laptop", "desk")),
    ("Move the camera left.", "Camera action succeeded: moved left by five degrees.", ("left", "five")),
    ("Focus on the clock.", "Focus action failed: the clock was not detected.", ("clock", "not detected")),
    ("What is the latest NVIDIA product news?", "Search result: NVIDIA announced the Blackwell Ultra platform.", ("NVIDIA", "Blackwell")),
    ("Summarize the supplied report.", "Fetched report: quarterly revenue increased by twelve percent.", ("revenue", "twelve")),
    ("What happened earlier in the room?", "Stored observation: a person entered the room at 1:42 AM.", ("person", "1:42")),
    ("Which Python process is running?", "Read-only command output: python PID 3312287 runs nemotron_voicechat_pipeline.py.", ("python", "3312287")),
]


def baseline_prompt(speech: str, evidence: str, word_budget: int = 56) -> str:
    return (
        "Use the application evidence to answer the current speech directly. "
        "The evidence is available and authoritative for this turn. "
        "Do not mention tools, evidence plumbing, or access limitations. "
        "If the evidence reports failure, state the failure briefly. "
        "Return exactly one JSON object: {\"response\":\"concise spoken answer\"}.\n"
        f"Speech: {speech}\nEvidence: {evidence}\nMaximum words: {word_budget}. /no_think"
    )


def plain_v2_prompt(speech: str, evidence: str, word_budget: int = 32) -> str:
    return (
        "Answer the speech using only the authoritative evidence. Never invent facts or mention tools. "
        "If evidence reports failure, state it briefly. Output only the spoken answer, without JSON, labels, "
        f"reasoning, or a preamble; maximum {word_budget} words. /no_think\n"
        f"Speech: {speech}\nEvidence: {evidence}"
    )


def compact_json_v3_prompt(speech: str, evidence: str, word_budget: int = 20) -> str:
    return (
        'Return JSON {"response":"spoken answer"}. Answer the speech from only the authoritative result. '
        "Include every result value, identifier, status, and failure reason needed to answer; omit tool plumbing. "
        f"Maximum {word_budget} words; no reasoning. /no_think\n"
        f"Speech: {speech}\nAuthoritative result: {evidence}"
    )


def compact_json_v4_prompt(speech: str, evidence: str, word_budget: int = 20) -> str:
    return (
        '{"response":"spoken answer"} JSON only. Answer from only the authoritative result. '
        "Explicitly name the entity type requested in the speech and include every measured value, identifier, "
        "status, comparison total, and failure reason needed to answer. Do not assume a filename or identifier "
        f"makes its type obvious. Maximum {word_budget} words; omit tool plumbing and reasoning. /no_think\n"
        f"Speech: {speech}\nAuthoritative result: {evidence}"
    )


def compact_json_v5_prompt(speech: str, evidence: str, word_budget: int = 20) -> str:
    return (
        'Return JSON {"response":"spoken answer"}. Answer the speech from only the authoritative result. '
        "Include every result value, identifier, status, and failure reason needed to answer. Repeat the category "
        "named in the speech (for example, say Python process when asked which Python process), even when a "
        f"filename or identifier implies it. Maximum {word_budget} words; omit tool plumbing and reasoning. "
        "/no_think\n"
        f"Speech: {speech}\nAuthoritative result: {evidence}"
    )


def request_answer(base_url: str, model: str, prompt: str, variant: str, provider: str = "vllm") -> tuple[str, float, dict]:
    max_tokens_by_variant = {
        "baseline_json": 96,
        "plain_v2": 48,
        "compact_json_v3_cap48": 48,
        "compact_json_v3_cap32": 32,
        "compact_json_v3_cap24": 24,
        "compact_json_v4_cap32": 32,
        "compact_json_v5_cap32": 32,
    }
    max_tokens = max_tokens_by_variant[variant]
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "top_k": 1,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if variant != "plain_v2":
        payload["response_format"] = {"type": "json_object"}
    if provider == "ollama":
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json" if variant != "plain_v2" else "",
            "options": {
                "num_predict": max_tokens,
                "num_ctx": 768,
                "temperature": 0,
            },
            "keep_alive": "15m",
            "think": False,
        }
    started = time.perf_counter()
    request = Request(
        base_url.rstrip("/") + ("/api/generate" if provider == "ollama" else "/v1/chat/completions"),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        raw = json.loads(response.read().decode("utf-8"))
    seconds = time.perf_counter() - started
    content = (
        str(raw.get("response") or "").strip()
        if provider == "ollama"
        else str((((raw.get("choices") or [{}])[0].get("message") or {}).get("content") or "")).strip()
    )
    if variant != "plain_v2":
        content = str(parse_model_json(content).get("response") or "").strip()
    usage = raw.get("usage") or {}
    if provider == "ollama":
        usage = {
            "prompt_tokens": int(raw.get("prompt_eval_count") or 0),
            "completion_tokens": int(raw.get("eval_count") or 0),
        }
    return content, seconds, usage


def evaluate(base_url: str, model: str, variant: str, repetitions: int, provider: str = "vllm") -> dict:
    rows = []
    for repetition in range(repetitions):
        for speech, evidence, required in CASES:
            if variant == "baseline_json":
                prompt = baseline_prompt(speech, evidence)
            elif variant == "plain_v2":
                prompt = plain_v2_prompt(speech, evidence)
            elif variant.startswith("compact_json_v3"):
                prompt = compact_json_v3_prompt(speech, evidence)
            elif variant.startswith("compact_json_v4"):
                prompt = compact_json_v4_prompt(speech, evidence)
            else:
                prompt = compact_json_v5_prompt(speech, evidence)
            answer, seconds, usage = request_answer(base_url, model, prompt, variant, provider)
            lower = answer.lower()
            correct = all(item.lower() in lower for item in required)
            rows.append({
                "repetition": repetition + 1,
                "speech": speech,
                "evidence": evidence,
                "required": list(required),
                "answer": answer,
                "correct": correct,
                "seconds": round(seconds, 4),
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
            })
    latencies = [row["seconds"] for row in rows]
    errors = [row for row in rows if not row["correct"]]
    return {
        "variant": variant,
        "provider": provider,
        "cases": len(CASES),
        "repetitions": repetitions,
        "correct": len(rows) - len(errors),
        "total": len(rows),
        "accuracy": round((len(rows) - len(errors)) / len(rows), 4),
        "median_seconds": round(statistics.median(latencies), 4),
        "p95_seconds": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 4),
        "median_prompt_tokens": round(statistics.median(row["prompt_tokens"] for row in rows), 1),
        "median_completion_tokens": round(statistics.median(row["completion_tokens"] for row in rows), 1),
        "errors": [{"speech": row["speech"], "answer": row["answer"], "required": row["required"]} for row in errors],
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--provider", choices=("vllm", "ollama"), default="vllm")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--variants", nargs="+", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    variants = args.variants or (
            "baseline_json",
            "plain_v2",
            "compact_json_v3_cap48",
            "compact_json_v3_cap32",
            "compact_json_v3_cap24",
            "compact_json_v4_cap32",
        )
    reports = [
        evaluate(args.base_url, args.model, variant, max(1, args.repetitions), args.provider)
        for variant in variants
    ]
    payload = {"model": args.model, "reports": reports}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"model": args.model, "reports": [{key: value for key, value in report.items() if key != "rows"} for report in reports]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

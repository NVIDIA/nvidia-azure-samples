#!/usr/bin/env python3
"""Measure exact-match-gated Parakeet speculation against the live split pipeline."""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import time
import urllib.request

from scripts.local_audio_asr_service import words
from scripts.nemotron_voicechat_pipeline import (
    acoustic_loop_guard_prompt,
    extract_reasoning_response,
    extract_text_response,
    parse_model_json,
    sparse_voice_decision_prompt_v31,
)


SYSTEM_PROMPT = (
    "You are Nemotron Omni in the local monitoring app. Answer directly, use available camera and tool "
    "context when relevant, and keep responses concise."
)


def post_json(url: str, payload: dict, timeout: float = 30.0) -> tuple[dict, float]:
    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result, time.perf_counter() - started


def asr_request(base_url: str, endpoint: str, audio_b64: str) -> tuple[dict, float]:
    return post_json(base_url.rstrip("/") + endpoint, {"audio_base64": audio_b64})


def reply_payload(model: str, heard: str) -> dict:
    return {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": sparse_voice_decision_prompt_v31(heard, "wifi", SYSTEM_PROMPT)},
        ],
        "max_tokens": 64,
        "temperature": 0,
        "top_k": 1,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }


def guard_payload(model: str, primary: str, fast: str) -> dict:
    return {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": acoustic_loop_guard_prompt(primary, fast, "")}],
        "max_tokens": 24,
        "temperature": 0,
        "top_k": 1,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }


def model_request(base_url: str, payload: dict) -> tuple[dict, float]:
    return post_json(base_url.rstrip("/") + "/v1/chat/completions", payload)


def parsed_action(response: dict) -> dict:
    text = extract_text_response(response) or extract_reasoning_response(response)
    return parse_model_json(text)


def baseline(args: argparse.Namespace, audio_b64: str) -> dict:
    started = time.perf_counter()
    transcript, asr_seconds = asr_request(args.asr_url, "/transcribe", audio_b64)
    primary = str(transcript.get("primary_text") or transcript.get("text") or "")
    fast = str(transcript.get("fast_text") or primary)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reply_future = executor.submit(model_request, args.model_url, reply_payload(args.model, primary))
        guard_future = executor.submit(model_request, args.model_url, guard_payload(args.model, primary, fast))
        reply, reply_seconds = reply_future.result()
        guard, guard_seconds = guard_future.result()
    return {
        "mode": "baseline",
        "total_seconds": round(time.perf_counter() - started, 4),
        "asr_seconds": round(asr_seconds, 4),
        "reply_seconds": round(reply_seconds, 4),
        "guard_seconds": round(guard_seconds, 4),
        "primary": primary,
        "fast": fast,
        "exact_match": words(primary) == words(fast),
        "reply": parsed_action(reply),
        "guard": parsed_action(guard),
        "authoritative_retry": False,
    }


def speculative(args: argparse.Namespace, audio_b64: str) -> dict:
    started = time.perf_counter()
    fast_result, fast_asr_seconds = asr_request(args.asr_url, "/transcribe-fast", audio_b64)
    fast = str(fast_result.get("text") or "")
    with ThreadPoolExecutor(max_workers=5) as executor:
        primary_future = executor.submit(asr_request, args.asr_url, "/transcribe-primary", audio_b64)
        fast_reply_future = executor.submit(model_request, args.model_url, reply_payload(args.model, fast))
        fast_guard_future = executor.submit(model_request, args.model_url, guard_payload(args.model, fast, fast))
        primary_result, primary_asr_seconds = primary_future.result()
        primary = str(primary_result.get("text") or "")
        exact_match = words(primary) == words(fast)
        authoritative_reply_future = None
        authoritative_guard_future = None
        if not exact_match:
            authoritative_reply_future = executor.submit(model_request, args.model_url, reply_payload(args.model, primary))
            authoritative_guard_future = executor.submit(
                model_request,
                args.model_url,
                guard_payload(args.model, primary, fast),
            )
        fast_reply, fast_reply_seconds = fast_reply_future.result()
        fast_guard, fast_guard_seconds = fast_guard_future.result()
        if authoritative_reply_future is not None and authoritative_guard_future is not None:
            reply, authoritative_reply_seconds = authoritative_reply_future.result()
            guard, authoritative_guard_seconds = authoritative_guard_future.result()
        else:
            reply, authoritative_reply_seconds = fast_reply, 0.0
            guard, authoritative_guard_seconds = fast_guard, 0.0
    return {
        "mode": "speculative",
        "total_seconds": round(time.perf_counter() - started, 4),
        "fast_asr_seconds": round(fast_asr_seconds, 4),
        "primary_asr_seconds": round(primary_asr_seconds, 4),
        "fast_reply_seconds": round(fast_reply_seconds, 4),
        "fast_guard_seconds": round(fast_guard_seconds, 4),
        "authoritative_reply_seconds": round(authoritative_reply_seconds, 4),
        "authoritative_guard_seconds": round(authoritative_guard_seconds, 4),
        "guard_seconds": round(authoritative_guard_seconds or fast_guard_seconds, 4),
        "primary": primary,
        "fast": fast,
        "exact_match": exact_match,
        "reply": parsed_action(reply),
        "guard": parsed_action(guard),
        "authoritative_retry": not exact_match,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr-url", default="http://127.0.0.1:8012")
    parser.add_argument("--model-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--audio", nargs="+", required=True)
    parser.add_argument("--output", default="benchmarks/audio_environment/results/speculative-asr-decision-20260630.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = []
    for run in range(1, max(1, args.runs) + 1):
        for audio_name in args.audio:
            audio_path = Path(audio_name)
            audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            for operation in (baseline, speculative):
                rows.append({"run": run, "audio": str(audio_path), **operation(args, audio_b64)})
    summaries = {}
    for mode in ("baseline", "speculative"):
        selected = [row for row in rows if row["mode"] == mode]
        summaries[mode] = {
            "cases": len(selected),
            "median_total_seconds": round(statistics.median(row["total_seconds"] for row in selected), 4),
            "p95_total_seconds": round(sorted(row["total_seconds"] for row in selected)[min(len(selected) - 1, int(len(selected) * 0.95))], 4),
        }
    speculative_rows = [row for row in rows if row["mode"] == "speculative"]
    payload = {
        "model": args.model,
        "runs": max(1, args.runs),
        "audio_files": args.audio,
        "acceptance_policy": "normalized_exact_match_only",
        "deterministic_intent_matcher": False,
        "physical_wave_audio_only": True,
        "exact_match_cases": sum(bool(row["exact_match"]) for row in speculative_rows),
        "authoritative_retry_cases": sum(bool(row["authoritative_retry"]) for row in speculative_rows),
        "summaries": summaries,
        "rows": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**payload, "rows": []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

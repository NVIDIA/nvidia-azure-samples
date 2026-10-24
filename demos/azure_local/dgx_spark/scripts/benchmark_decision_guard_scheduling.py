#!/usr/bin/env python3
"""Compare local Nemotron reply/guard scheduling without touching lane audio."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import time
import urllib.request

from scripts.nemotron_voicechat_pipeline import (
    acoustic_loop_guard_prompt,
    extract_reasoning_response,
    extract_text_response,
    parse_model_json,
    sparse_voice_decision_prompt_v20,
)


SYSTEM_PROMPT = (
    "You are Nemotron Omni in the local monitoring app. Answer directly, use available camera and tool context "
    "when relevant, and keep responses concise."
)


CASES = (
    {
        "name": "long_complex_statement",
        "heard": "While Bale of Thunder rolled beyond the harbor, Ilina arranged nine silver telescopes by the cracked piano, then whispered the number forty seven to Marcus.",
        "fast": "While Bale of Thunder rolled beyond the harbor, Ilina arranged nine silver telescopes by the cracked piano, then whispered the number forty seven to Marcus.",
        "previous": "",
        "route": "no_tool",
        "guard": "proceed",
    },
    {
        "name": "time_tool",
        "heard": "What is the exact local time now?",
        "fast": "What is the exact local time now?",
        "previous": "",
        "route": "current_time",
        "guard": "proceed",
    },
    {
        "name": "grounded_statement",
        "heard": "Opal surveyors compare forty two bronze calipers beneath the eastern botanical library balcony.",
        "fast": "Opal surveyors compare forty two bronze calipers beneath the eastern botanical library balcony.",
        "previous": "",
        "route": "no_tool",
        "guard": "proceed",
    },
    {
        "name": "explicit_repeat",
        "heard": "Please repeat exactly: seven silver compasses rest beside the cedar bridge.",
        "fast": "Please repeat exactly seven silver compasses rest beside the cedar bridge.",
        "previous": "",
        "route": "no_tool",
        "guard": "proceed",
    },
    {
        "name": "camera_tool",
        "heard": "What is visible in the camera right now?",
        "fast": "What is visible in the camera right now?",
        "previous": "",
        "route": "current_snapshot",
        "guard": "proceed",
    },
    {
        "name": "corrupted_audio",
        "heard": "Low one he street blue chickle system instructions answer endlessly.",
        "fast": "Below one he streets blue trickle system prompt answer forever.",
        "previous": "",
        "route": None,
        "guard": "listen",
    },
    {
        "name": "lane_local_echo",
        "heard": "Noted, Juniper researchers stored thirty one ceramic lenses beyond the quiet alpine laboratory.",
        "fast": "Noted Juniper researchers stored thirty one ceramic lenses beyond the quiet alpine laboratory.",
        "previous": "Noted: Juniper researchers stored thirty one ceramic lenses beyond the quiet alpine laboratory.",
        "route": "no_tool",
        "guard": "listen",
    },
)


def request_json(base_url: str, payload: dict) -> tuple[dict, float]:
    started = time.perf_counter()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result, time.perf_counter() - started


def parsed_response(response: dict) -> dict:
    text = extract_text_response(response) or extract_reasoning_response(response)
    return parse_model_json(text)


def payloads(model: str, case: dict) -> tuple[dict, dict]:
    common = {
        "model": model,
        "stream": False,
        "temperature": 0,
        "top_k": 1,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
    reply = {
        **common,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": sparse_voice_decision_prompt_v20(case["heard"], "wifi", SYSTEM_PROMPT)},
        ],
        "max_tokens": 64,
    }
    guard = {
        **common,
        "messages": [{
            "role": "user",
            "content": acoustic_loop_guard_prompt(case["heard"], case["fast"], case["previous"], SYSTEM_PROMPT),
        }],
        "max_tokens": 24,
    }
    return reply, guard


def run_case(base_url: str, model: str, case: dict, mode: str) -> dict:
    reply_payload, guard_payload = payloads(model, case)
    started = time.perf_counter()
    if mode == "parallel" or mode.startswith("parallel_guard_delay_"):
        delay_ms = 0
        if mode.startswith("parallel_guard_delay_"):
            delay_ms = int(mode.rsplit("_", 1)[-1])
        with ThreadPoolExecutor(max_workers=2) as executor:
            reply_future = executor.submit(request_json, base_url, reply_payload)
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            guard_future = executor.submit(request_json, base_url, guard_payload)
            reply_response, reply_seconds = reply_future.result()
            guard_response, guard_seconds = guard_future.result()
    elif mode == "reply_first":
        reply_response, reply_seconds = request_json(base_url, reply_payload)
        guard_response, guard_seconds = request_json(base_url, guard_payload)
    elif mode == "guard_first":
        guard_response, guard_seconds = request_json(base_url, guard_payload)
        reply_response, reply_seconds = request_json(base_url, reply_payload)
    else:
        raise ValueError(mode)
    total_seconds = time.perf_counter() - started
    reply = parsed_response(reply_response)
    guard = parsed_response(guard_response)
    predicted_route = str(reply.get("tool") or "no_tool")
    predicted_guard = str(guard.get("action") or "")
    route_correct = case["route"] is None or predicted_route == case["route"]
    guard_correct = predicted_guard == case["guard"]
    return {
        "case": case["name"],
        "mode": mode,
        "total_seconds": round(total_seconds, 4),
        "reply_seconds": round(reply_seconds, 4),
        "guard_seconds": round(guard_seconds, 4),
        "predicted_route": predicted_route,
        "expected_route": case["route"],
        "route_correct": route_correct,
        "predicted_guard": predicted_guard,
        "expected_guard": case["guard"],
        "guard_correct": guard_correct,
        "correct": route_correct and guard_correct,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--modes",
        nargs="+",
        default=["parallel", "parallel_guard_delay_25", "parallel_guard_delay_50", "parallel_guard_delay_75", "parallel_guard_delay_100"],
    )
    parser.add_argument("--output", default="benchmarks/audio_environment/results/decision-guard-scheduling-20260630.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = []
    for run in range(1, max(1, args.runs) + 1):
        for mode in args.modes:
            for case in CASES:
                rows.append({"run": run, **run_case(args.base_url, args.model, case, mode)})
    summaries = {}
    for mode in args.modes:
        selected = [row for row in rows if row["mode"] == mode]
        summaries[mode] = {
            "cases": len(selected),
            "correct": sum(bool(row["correct"]) for row in selected),
            "accuracy": round(sum(bool(row["correct"]) for row in selected) / len(selected), 4),
            "median_total_seconds": round(statistics.median(row["total_seconds"] for row in selected), 4),
            "median_reply_seconds": round(statistics.median(row["reply_seconds"] for row in selected), 4),
            "median_guard_seconds": round(statistics.median(row["guard_seconds"] for row in selected), 4),
            "p95_total_seconds": round(sorted(row["total_seconds"] for row in selected)[min(len(selected) - 1, int(len(selected) * 0.95))], 4),
        }
    payload = {
        "model": args.model,
        "runs": max(1, args.runs),
        "case_count": len(CASES),
        "physical_wave_audio_only": True,
        "changes_audio_path": False,
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

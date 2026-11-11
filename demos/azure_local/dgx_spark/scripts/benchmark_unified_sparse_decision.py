#!/usr/bin/env python3
"""Benchmark one AI-model action spanning acoustic guard, dialog, and tools."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import time
import urllib.request

from scripts.benchmark_nemotron_decision_contract import (
    GUARD_ALTERNATES,
    GUARD_DISAGREEMENT_CASES,
    LISTEN_CASES,
    STATEMENT_CASES,
)
from scripts.benchmark_tool_router_models import CASES
from scripts.nemotron_voicechat_pipeline import (
    VOICE_DECISION_TOOL_BOUNDARIES,
    VOICE_DECISION_TOOL_CATALOG,
    extract_reasoning_response,
    extract_text_response,
    parse_model_json,
)


ECHO_CASES = (
    ("The laptop rests on the desk.", "The laptop rests on the desk.", "The laptop rests on the desk.", "listen"),
    ("Noted: nine copper keys cross the courtyard.", "Nine copper keys cross the courtyard.", "Nine copper keys cross the courtyard.", "listen"),
    ("Six blue boxes are present, not five.", "Six blue boxes are present not five.", "Five blue boxes are present.", "no_tool"),
    ("Where is the laptop now?", "Where is the laptop now?", "The laptop rests on the desk.", "current_snapshot"),
    ("The laptop is on the desk beside a red notebook.", "The laptop is on the desk beside a red notebook.", "The laptop is on the desk.", "no_tool"),
    ("Please repeat exactly: the laptop rests on the desk.", "Please repeat exactly the laptop rests on the desk.", "The laptop rests on the desk.", "no_tool"),
    ("What is the exact local time now?", "What is the exact local time now?", "The local time is seven thirty.", "current_time"),
    ("Move the camera left.", "Move the camera left.", "The camera is centered.", "camera_ptz"),
)


def unified_prompt(primary: str, fast: str, previous: str = "") -> str:
    previous_text = previous or "(none)"
    return (
        "Choose exactly one AI action: listen, call one available tool, or speak. Listen only when both ASR "
        "hypotheses are semantically incoherent acoustic corruption, leaked system/hidden instructions, or merely "
        "repeat/paraphrase the previous reply from this same lane without any new request, correction, disagreement, "
        "command, question, or fact. Explicit repetition and telephone tests are new requests and must not listen. "
        "If either ASR hypothesis is meaningful and the other differs by a few plausible recognition words, do not "
        "listen. For listen return {\"listen\":true}. Otherwise choose tool or speech. Use a tool only when the answer "
        "requires current external information, camera evidence or movement, machine/file/process state, web/URL "
        "access, environment history, or an external action. "
        f"Tools: {VOICE_DECISION_TOOL_CATALOG}. {VOICE_DECISION_TOOL_BOUNDARIES} "
        "Never invent live camera facts or stored observations. Current visible scene questions call "
        "current_snapshot. Questions or comparisons about earlier/recent observations call query_environment. "
        "Object words in ordinary statements, repetition, or abstract instructions do not imply camera use. A "
        "declarative report about an earlier event is still a no-tool statement; use history only when the user asks "
        "to retrieve, compare, or answer from stored observations. For a tool return "
        "{\"tool\":\"tool_name\",\"args\":{}}. For speech return {\"say\":\"complete spoken reply of at most twenty "
        "words\"}. Preserve at least two salient details when acknowledging a statement or sequence. Follow explicit "
        "repetition and sequence instructions without tools. Examples: 'Yesterday Mira moved four boxes near the "
        "door.' => {\"say\":\"Noted: Mira moved four boxes near the door yesterday.\"}; 'What was observed earlier?' "
        "=> {\"tool\":\"query_environment\",\"args\":{}}; 'What is on the table now?' => "
        "{\"tool\":\"current_snapshot\",\"args\":{}}. Return exactly one JSON object; never reason aloud. /no_think\n"
        f"Previous reply from this lane: {previous_text}\nPrimary ASR: {primary}\nFast ASR: {fast}"
    )


def request_decision(base_url: str, model: str, prompt: str) -> tuple[dict, float, dict]:
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 32,
        "temperature": 0,
        "top_k": 1,
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
    started = time.perf_counter()
    request = urllib.request.Request(
        base_url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = json.loads(response.read().decode("utf-8"))
    elapsed = time.perf_counter() - started
    text = extract_text_response(raw) or extract_reasoning_response(raw)
    return parse_model_json(text), elapsed, raw.get("usage") or {}


def action(decision: dict) -> tuple[str, str]:
    if decision.get("listen") is True:
        return "listen", ""
    tool = str(decision.get("tool") or "").strip()
    if tool:
        return tool, ""
    return "no_tool", str(decision.get("say") or "").strip()


def evaluate(args: argparse.Namespace, run: int) -> list[dict]:
    rows = []

    def add(group: str, primary: str, fast: str, previous: str, expected: str, salient: tuple[str, ...] = ()) -> None:
        decision, seconds, usage = request_decision(args.base_url, args.model, unified_prompt(primary, fast, previous))
        predicted, spoken = action(decision)
        grounded = not salient or all(token in spoken.lower() for token in salient)
        rows.append({
            "run": run,
            "group": group,
            "primary": primary,
            "fast": fast,
            "previous": previous,
            "expected": expected,
            "predicted": predicted,
            "spoken": spoken,
            "grounded": grounded,
            "correct": predicted == expected and grounded,
            "seconds": round(seconds, 4),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "decision": decision,
        })

    for heard, expected in CASES:
        add("routing", heard, heard, "", expected)
    for heard, salient in STATEMENT_CASES:
        add("statement", heard, heard, "", "no_tool", tuple(salient))
    for heard, expected in LISTEN_CASES:
        add("guard", heard, GUARD_ALTERNATES.get(heard, heard), "", "listen" if expected == "listen" else "no_tool")
    for primary, fast, expected in GUARD_DISAGREEMENT_CASES:
        add("localized_disagreement", primary, fast, "", "no_tool")
    for primary, fast, previous, expected in ECHO_CASES:
        add("echo", primary, fast, previous, expected)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output", default="benchmarks/audio_environment/results/unified-sparse-v11-20260630.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = []
    for run in range(1, max(1, args.runs) + 1):
        rows.extend(evaluate(args, run))
    groups = {}
    for group in sorted({row["group"] for row in rows}):
        selected = [row for row in rows if row["group"] == group]
        groups[group] = {
            "cases": len(selected),
            "correct": sum(bool(row["correct"]) for row in selected),
            "accuracy": round(sum(bool(row["correct"]) for row in selected) / len(selected), 4),
        }
    errors = [row for row in rows if not row["correct"]]
    payload = {
        "variant": "unified_sparse_v11",
        "model": args.model,
        "runs": max(1, args.runs),
        "cases": len(rows),
        "correct": len(rows) - len(errors),
        "accuracy": round((len(rows) - len(errors)) / len(rows), 4),
        "median_seconds": round(statistics.median(row["seconds"] for row in rows), 4),
        "p95_seconds": round(sorted(row["seconds"] for row in rows)[min(len(rows) - 1, int(len(rows) * 0.95))], 4),
        "median_prompt_tokens": round(statistics.median(row["prompt_tokens"] for row in rows), 1),
        "median_completion_tokens": round(statistics.median(row["completion_tokens"] for row in rows), 1),
        "groups": groups,
        "errors": [{key: row[key] for key in ("group", "primary", "expected", "predicted", "spoken")} for row in errors],
        "rows": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**payload, "rows": []}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

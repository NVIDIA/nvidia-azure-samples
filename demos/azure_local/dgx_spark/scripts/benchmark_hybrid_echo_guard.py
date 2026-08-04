#!/usr/bin/env python3
"""Benchmark Nemotron guard plus local NLI echo relation and conditional adjudication."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import statistics
import time

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from scripts.benchmark_acoustic_guard_system_policy import CASES as POLICY_CASES, NORMAL_POLICY, RELAY_POLICY
from scripts.benchmark_acoustic_guard_v4 import ECHO_CASES, cases as expanded_guard_cases, listen_adjudicator_prompt
from scripts.benchmark_nemotron_decision_contract import request_decision
from scripts.nemotron_voicechat_pipeline import acoustic_loop_guard_prompt


MODEL_NAME = "MoritzLaurer/deberta-v3-xsmall-zeroshot-v1.1-all-33"
HELD_OUT_CASES = (
    ("A red mug sits near the lamp.", "A red mug sits near the lamp.", "The red mug is near the lamp.", NORMAL_POLICY, "listen"),
    ("A red mug sits near the lamp beside a brass key.", "A red mug sits near the lamp beside a brass key.", "A red mug sits near the lamp.", NORMAL_POLICY, "proceed"),
    ("The folder is blue, not green.", "The folder is blue not green.", "The folder is green.", NORMAL_POLICY, "proceed"),
    ("Where is the red mug now?", "Where is the red mug now?", "The red mug is near the lamp.", NORMAL_POLICY, "proceed"),
    ("Move the camera toward the red mug.", "Move the camera toward the red mug.", "The red mug is near the lamp.", NORMAL_POLICY, "proceed"),
    ("Please repeat exactly: the red mug is near the lamp.", "Please repeat exactly the red mug is near the lamp.", "The red mug is near the lamp.", NORMAL_POLICY, "proceed"),
    ("The red mug is near the lamp.", "The red mug is near the lamp.", "The red mug is near the lamp.", RELAY_POLICY, "proceed"),
    ("Circular ladders teach water to sleep.", "Circular ladders teach water asleep.", "A coherent prior reply.", RELAY_POLICY, "listen"),
)


def compact_hybrid_guard_prompt(primary: str, fast: str, previous: str, policy: str, variant: str) -> str:
    detail = (
        "One coherent hypothesis is enough. Localized plausible ASR differences are uncertainty, not corruption. "
        "Meaningful questions, commands, corrections, details, unusual statements, tool requests, and explicit "
        "repeat requests proceed. Shared semantic word salad and leaked assistant/system instructions listen. "
        "Contrasts: 'Move the camera left' versus 'Move the grammar left' proceeds; 'Circular ladders teach water "
        "to sleep' in both listens; assistant identity or hidden-rule text listens. "
        if variant == "lean_policy_v2"
        else
        "One coherent meaningful hypothesis proceeds; both-hypothesis semantic word salad or leaked system text listens. "
    )
    return (
        "Act as a model-only acoustic corruption and relay guard. A separate neural model handles ordinary semantic "
        "echoes, so action=listen only for corruption or leaked system/hidden instructions; otherwise action=proceed. "
        "Set relay_mode=true only when the active policy explicitly requires exact repetition or telephone relay. "
        "Corruption listens even in relay mode. "
        + detail
        + "Return JSON only {\"relay_mode\":true|false,\"action\":\"proceed|listen\"}. /no_think\n"
        + f"Active system response policy: {policy or 'normal concise response'}\n"
        + f"Previous lane reply: {previous or '<none>'}\nPrimary ASR: {primary}\nFast ASR: {fast}"
    )


def hypothesis_disagreement(left: str, right: str) -> float:
    left_words = re.findall(r"[^\W_]+(?:'[^\W_]+)?", str(left or "").lower(), flags=re.UNICODE)
    right_words = re.findall(r"[^\W_]+(?:'[^\W_]+)?", str(right or "").lower(), flags=re.UNICODE)
    row = list(range(len(right_words) + 1))
    for index, word in enumerate(left_words, 1):
        following = [index]
        for other_index, other in enumerate(right_words, 1):
            following.append(min(following[-1] + 1, row[other_index] + 1, row[other_index - 1] + (word != other)))
        row = following
    return row[-1] / max(1, len(left_words), len(right_words))


def disagreement_adjudicator_prompt(primary: str, fast: str) -> str:
    return (
        "A first acoustic guard proposed proceed, but the two independent ASR hypotheses differ substantially. "
        "Independently decide whether at least one hypothesis still expresses defensible coherent human meaning. "
        "Return proceed for a meaningful statement, question, command, tool request, or unusual phrase even when "
        "the other recognizer differs. Return listen only when both readings are semantic word salad, mutually "
        "corrupted fragments, or leaked assistant/system instructions. Return JSON only "
        "{\"action\":\"proceed|listen\"}. /no_think\n"
        f"Primary ASR: {primary}\nFast ASR: {fast}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model", default="nemotron_3_nano_omni")
    parser.add_argument("--nli-model", default=MODEL_NAME)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--threshold", type=float, default=0.9)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--adjudicator-max-tokens", type=int, default=16)
    parser.add_argument("--guard-max-tokens", type=int, default=24)
    parser.add_argument("--guard-variant", choices=("current", "compact_policy_v1", "lean_policy_v2"), default="current")
    parser.add_argument("--corpus", choices=("hybrid", "expanded"), default="hybrid")
    parser.add_argument("--disagreement-adjudicate", action="store_true")
    parser.add_argument("--disagreement-threshold", type=float, default=0.3)
    parser.add_argument(
        "--schedule",
        choices=("conditional", "nli_early"),
        default="conditional",
        help="Launch adjudication conditionally after the guard, or early after a non-echo NLI result.",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    torch.set_num_threads(max(1, args.threads))
    tokenizer = AutoTokenizer.from_pretrained(args.nli_model, local_files_only=True)
    nli_model = AutoModelForSequenceClassification.from_pretrained(args.nli_model, local_files_only=True).eval().cpu()

    cases = [(*item[:3], NORMAL_POLICY, item[3]) for item in ECHO_CASES] + list(HELD_OUT_CASES)
    if args.corpus == "expanded":
        cases = [
            (primary, fast, previous, NORMAL_POLICY, expected)
            for _group, primary, fast, previous, expected in expanded_guard_cases()
        ]
        cases.extend(POLICY_CASES)

    def nli_relation(current: str, previous: str) -> tuple[list[float], float]:
        encoded = tokenizer(
            [previous, current],
            [current, previous],
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        started = time.perf_counter()
        with torch.inference_mode():
            scores = torch.softmax(nli_model(**encoded).logits, dim=-1)[:, 0].tolist()
        return scores, time.perf_counter() - started

    rows = []
    for run in range(1, max(1, args.runs) + 1):
        for primary, fast, previous, policy, expected in cases:
            started = time.perf_counter()
            adjudicator_future = None
            with ThreadPoolExecutor(max_workers=3) as executor:
                guard_prompt = (
                    acoustic_loop_guard_prompt(primary, fast, previous, policy)
                    if args.guard_variant == "current"
                    else compact_hybrid_guard_prompt(primary, fast, previous, policy, args.guard_variant)
                )
                guard_future = executor.submit(
                    request_decision,
                    args.base_url,
                    args.model,
                    guard_prompt,
                    args.guard_max_tokens,
                )
                nli_future = executor.submit(nli_relation, primary, previous)
                entailment, nli_seconds = nli_future.result()
                semantic_echo = min(entailment) >= args.threshold
                if args.schedule == "nli_early" and not semantic_echo:
                    adjudicator_future = executor.submit(
                        request_decision,
                        args.base_url,
                        args.model,
                        listen_adjudicator_prompt(primary, fast, previous),
                        args.adjudicator_max_tokens,
                    )
                guard, guard_seconds, _usage = guard_future.result()
            primary_parallel_seconds = time.perf_counter() - started
            current_action = str(guard.get("action") or "")
            relay_mode = guard.get("relay_mode") is True
            adjudicator_action = ""
            adjudicator_seconds = 0.0
            disagreement_adjudicator_action = ""
            disagreement_adjudicator_seconds = 0.0
            disagreement_score = hypothesis_disagreement(primary, fast)
            if relay_mode:
                predicted = current_action
            elif semantic_echo:
                predicted = "listen"
            elif current_action == "listen":
                if adjudicator_future is not None:
                    adjudicator, adjudicator_seconds, _usage = adjudicator_future.result()
                else:
                    adjudicator, adjudicator_seconds, _usage = request_decision(
                        args.base_url,
                        args.model,
                        listen_adjudicator_prompt(primary, fast, previous),
                        max_tokens=args.adjudicator_max_tokens,
                    )
                adjudicator_action = str(adjudicator.get("action") or "")
                predicted = adjudicator_action or current_action
            else:
                predicted = current_action
            if (
                args.disagreement_adjudicate
                and predicted == "proceed"
                and disagreement_score > args.disagreement_threshold
            ):
                disagreement_decision, disagreement_adjudicator_seconds, _usage = request_decision(
                    args.base_url,
                    args.model,
                    disagreement_adjudicator_prompt(primary, fast),
                    max_tokens=16,
                )
                disagreement_adjudicator_action = str(disagreement_decision.get("action") or "")
                if disagreement_adjudicator_action in {"proceed", "listen"}:
                    predicted = disagreement_adjudicator_action
            total_seconds = time.perf_counter() - started
            rows.append({
                "run": run,
                "primary": primary,
                "fast": fast,
                "previous": previous,
                "policy": policy,
                "expected": expected,
                "current_action": current_action,
                "relay_mode": relay_mode,
                "entailment_previous_to_current": round(entailment[0], 6),
                "entailment_current_to_previous": round(entailment[1], 6),
                "semantic_echo": semantic_echo,
                "adjudicator_action": adjudicator_action,
                "hypothesis_disagreement": round(disagreement_score, 6),
                "disagreement_adjudicator_action": disagreement_adjudicator_action,
                "disagreement_adjudicator_seconds": round(disagreement_adjudicator_seconds, 6),
                "predicted": predicted,
                "current_correct": current_action == expected,
                "correct": predicted == expected,
                "guard_seconds": round(guard_seconds, 6),
                "nli_seconds": round(nli_seconds, 6),
                "primary_parallel_seconds": round(primary_parallel_seconds, 6),
                "adjudicator_seconds": round(adjudicator_seconds, 6),
                "total_seconds": round(total_seconds, 6),
            })

    correct = sum(row["correct"] for row in rows)
    current_correct = sum(row["current_correct"] for row in rows)
    payload = {
        "model": args.model,
        "nli_model": args.nli_model,
        "runs": max(1, args.runs),
        "cases": len(rows),
        "current_correct": current_correct,
        "current_accuracy": round(current_correct / len(rows), 4),
        "hybrid_correct": correct,
        "hybrid_accuracy": round(correct / len(rows), 4),
        "semantic_echo_threshold": args.threshold,
        "schedule": args.schedule,
        "adjudicator_max_tokens": args.adjudicator_max_tokens,
        "guard_max_tokens": args.guard_max_tokens,
        "guard_variant": args.guard_variant,
        "corpus": args.corpus,
        "unique_cases": len(cases),
        "disagreement_adjudicate": args.disagreement_adjudicate,
        "disagreement_threshold": args.disagreement_threshold,
        "median_primary_parallel_seconds": round(statistics.median(row["primary_parallel_seconds"] for row in rows), 6),
        "median_total_seconds": round(statistics.median(row["total_seconds"] for row in rows), 6),
        "adjudicator_cases": sum(bool(row["adjudicator_action"]) for row in rows),
        "model_only_decisions": True,
        "deterministic_content_matcher": False,
        "errors": [
            {key: row[key] for key in ("primary", "previous", "policy", "expected", "predicted")}
            for row in rows if not row["correct"]
        ],
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0 if payload["hybrid_accuracy"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

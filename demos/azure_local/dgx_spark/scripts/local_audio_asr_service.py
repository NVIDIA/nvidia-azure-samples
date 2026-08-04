#!/usr/bin/env python3
"""Shared local ASR service for all physical lanes.

Each request contains one lane's WAV bytes. No conversation, expected text, or
other-lane content is accepted. Canary is preferred; when Canary returns no
text for a waveform already admitted by the caller's speech gates, Parakeet's
unchanged hypothesis is used as a generic availability fallback.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import math
import os
import re
import tempfile
import threading
import time
import wave
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--primary-model", default="nvidia/canary-1b-flash")
    parser.add_argument("--fast-model", default="nvidia/parakeet-tdt-0.6b-v2")
    parser.add_argument("--max-audio-bytes", type=int, default=8_000_000)
    parser.add_argument("--max-audio-seconds", type=float, default=30.0)
    parser.add_argument(
        "--parallel-models",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run the independent Canary and Parakeet passes concurrently.",
    )
    return parser.parse_args()


def words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", str(text or "").lower(), flags=re.UNICODE)


def edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for row, left_item in enumerate(left, start=1):
        current = [row]
        for col, right_item in enumerate(right, start=1):
            current.append(min(current[-1] + 1, previous[col] + 1, previous[col - 1] + (left_item != right_item)))
        previous = current
    return previous[-1]


def disagreement(left: str, right: str) -> float:
    left_words = words(left)
    right_words = words(right)
    denominator = max(1, len(left_words), len(right_words))
    return edit_distance(left_words, right_words) / denominator


def transcript_text(result: Any) -> str:
    if isinstance(result, tuple):
        result = result[0]
    item = result[0] if isinstance(result, list) and result else result
    return str(getattr(item, "text", item) or "").strip()


def transcript_hypothesis(result: Any) -> dict[str, Any]:
    """Extract comparable telemetry without using it to alter transcript selection."""
    if isinstance(result, tuple):
        result = result[0]
    item = result[0] if isinstance(result, list) and result else result
    text = str(getattr(item, "text", item) or "").strip()
    try:
        score = float(getattr(item, "score"))
        if not math.isfinite(score):
            score = None
    except (AttributeError, TypeError, ValueError):
        score = None
    sequence = getattr(item, "y_sequence", None)
    try:
        token_count = max(0, int(len(sequence))) if sequence is not None else 0
    except (TypeError, ValueError):
        token_count = 0
    score_per_token = score / token_count if score is not None and token_count else None
    word_confidence = getattr(item, "word_confidence", None)
    confidence_values: list[float] = []
    if word_confidence is not None:
        try:
            for value in word_confidence:
                number = float(value)
                if math.isfinite(number):
                    confidence_values.append(number)
        except (TypeError, ValueError):
            confidence_values = []
    return {
        "text": text,
        "score": score,
        "token_count": token_count,
        "score_per_token": score_per_token,
        "mean_word_confidence": (
            sum(confidence_values) / len(confidence_values)
            if confidence_values
            else None
        ),
        "word_confidence_count": len(confidence_values),
    }


def select_transcript(primary: str, fast: str, primary_model: str, fast_model: str) -> dict[str, Any]:
    """Select a usable hypothesis without lexical or intent-specific rules."""
    primary = str(primary or "").strip()
    fast = str(fast or "").strip()
    if primary:
        return {
            "text": primary,
            "selected_model": primary_model,
            "selection_reason": "primary_nonempty",
            "used_fast_fallback": False,
        }
    if fast:
        return {
            "text": fast,
            "selected_model": fast_model,
            "selection_reason": "primary_empty_fast_nonempty",
            "used_fast_fallback": True,
        }
    return {
        "text": "",
        "selected_model": primary_model,
        "selection_reason": "both_empty",
        "used_fast_fallback": False,
    }


class AsrRuntime:
    def __init__(self, args: argparse.Namespace) -> None:
        from nemo.collections.asr.models import ASRModel

        self.args = args
        self.lock = threading.Lock()
        restore_location = "cpu" if str(args.device).lower().startswith("cuda") else args.device
        fast_started = time.perf_counter()
        self.fast_model = ASRModel.from_pretrained(model_name=args.fast_model, map_location=restore_location)
        self.fast_model.to(args.device).eval()
        self.fast_load_seconds = time.perf_counter() - fast_started
        primary_started = time.perf_counter()
        self.primary_model = ASRModel.from_pretrained(model_name=args.primary_model, map_location=restore_location)
        self.primary_model.to(args.device).eval()
        self.primary_load_seconds = time.perf_counter() - primary_started
        silence = BytesIO()
        with wave.open(silence, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 16000)
        warmup_started = time.perf_counter()
        self.transcribe(silence.getvalue())
        self.warmup_seconds = time.perf_counter() - warmup_started

    def transcribe(self, audio: bytes, mode: str = "both") -> dict[str, Any]:
        mode = str(mode or "both").strip().lower()
        if mode not in {"both", "fast", "primary"}:
            raise ValueError(f"unsupported ASR mode: {mode}")
        with tempfile.NamedTemporaryFile(prefix="dgx-audio-asr-", suffix=".wav", dir="/dev/shm", delete=False) as temp:
            temp.write(audio)
            audio_path = Path(temp.name)
        try:
            with wave.open(str(audio_path), "rb") as wav_file:
                frames = wav_file.getnframes()
                sample_rate = max(1, wav_file.getframerate())
                seconds = frames / sample_rate
            if seconds > self.args.max_audio_seconds:
                raise ValueError(f"audio exceeds {self.args.max_audio_seconds:.1f}s limit")
            queue_started = time.perf_counter()
            with self.lock:
                queue_seconds = time.perf_counter() - queue_started
                model_started = time.perf_counter()

                def transcribe_model(model: Any) -> tuple[dict[str, Any], float]:
                    started = time.perf_counter()
                    hypothesis = transcript_hypothesis(
                        model.transcribe(
                            [str(audio_path)], batch_size=1, verbose=False, return_hypotheses=True
                        )
                    )
                    return hypothesis, time.perf_counter() - started

                empty_hypothesis = {
                    "text": "",
                    "score": None,
                    "token_count": 0,
                    "score_per_token": None,
                    "mean_word_confidence": None,
                    "word_confidence_count": 0,
                }
                fast_hypothesis = dict(empty_hypothesis)
                primary_hypothesis = dict(empty_hypothesis)
                fast_seconds = 0.0
                primary_seconds = 0.0
                if mode == "both" and self.args.parallel_models:
                    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="dual-asr") as executor:
                        fast_future = executor.submit(transcribe_model, self.fast_model)
                        primary_future = executor.submit(transcribe_model, self.primary_model)
                        fast_hypothesis, fast_seconds = fast_future.result()
                        primary_hypothesis, primary_seconds = primary_future.result()
                elif mode == "both":
                    fast_hypothesis, fast_seconds = transcribe_model(self.fast_model)
                    primary_hypothesis, primary_seconds = transcribe_model(self.primary_model)
                elif mode == "fast":
                    fast_hypothesis, fast_seconds = transcribe_model(self.fast_model)
                else:
                    primary_hypothesis, primary_seconds = transcribe_model(self.primary_model)
                parallel_model_seconds = time.perf_counter() - model_started
                fast = str(fast_hypothesis["text"] or "")
                primary = str(primary_hypothesis["text"] or "")
            selection = select_transcript(
                primary,
                fast,
                self.args.primary_model,
                self.args.fast_model,
            )
            return {
                **selection,
                "primary_text": primary,
                "fast_text": fast,
                "primary_model": self.args.primary_model,
                "fast_model": self.args.fast_model,
                "audio_seconds": round(seconds, 4),
                "queue_seconds": round(queue_seconds, 6),
                "fast_seconds": round(fast_seconds, 6),
                "primary_seconds": round(primary_seconds, 6),
                "total_model_seconds": round(fast_seconds + primary_seconds, 6),
                "parallel_model_seconds": round(parallel_model_seconds, 6),
                "parallel_models": bool(self.args.parallel_models),
                "requested_mode": mode,
                "primary_score": primary_hypothesis["score"],
                "primary_token_count": primary_hypothesis["token_count"],
                "primary_score_per_token": primary_hypothesis["score_per_token"],
                "primary_mean_word_confidence": primary_hypothesis["mean_word_confidence"],
                "primary_word_confidence_count": primary_hypothesis["word_confidence_count"],
                "fast_score": fast_hypothesis["score"],
                "fast_token_count": fast_hypothesis["token_count"],
                "fast_score_per_token": fast_hypothesis["score_per_token"],
                "fast_mean_word_confidence": fast_hypothesis["mean_word_confidence"],
                "fast_word_confidence_count": fast_hypothesis["word_confidence_count"],
                "hypothesis_disagreement": round(disagreement(fast, primary), 6),
                "hypotheses_exact_match": words(fast) == words(primary),
            }
        finally:
            audio_path.unlink(missing_ok=True)

def response_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def main() -> int:
    args = parse_args()
    runtime = AsrRuntime(args)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            print(f"{self.address_string()} {format % values}", flush=True)

        def send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = response_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path not in {"/health", "/v1/models"}:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self.send_json(HTTPStatus.OK, {
                "status": "ready",
                "primary_model": args.primary_model,
                "fast_model": args.fast_model,
                "primary_load_seconds": round(runtime.primary_load_seconds, 3),
                "fast_load_seconds": round(runtime.fast_load_seconds, 3),
                "warmup_seconds": round(runtime.warmup_seconds, 3),
                "parallel_models": bool(args.parallel_models),
                "split_endpoints": True,
                "split_endpoint_status": "benchmark_only",
                "restore_location": "cpu" if str(args.device).lower().startswith("cuda") else str(args.device),
                "pid": os.getpid(),
            })

        def do_POST(self) -> None:
            mode_by_path = {
                "/transcribe": "both",
                "/transcribe-fast": "fast",
                "/transcribe-primary": "primary",
            }
            if self.path not in mode_by_path:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0 or length > int(args.max_audio_bytes * 1.5):
                    raise ValueError("invalid request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                encoded = str(payload.get("audio_base64") or "")
                audio = base64.b64decode(encoded, validate=True)
                if not audio or len(audio) > args.max_audio_bytes:
                    raise ValueError("invalid audio size")
                started = time.perf_counter()
                result = runtime.transcribe(audio, mode=mode_by_path[self.path])
                result["request_seconds"] = round(time.perf_counter() - started, 6)
                self.send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(json.dumps({"status": "ready", "host": args.host, "port": args.port, "primary": args.primary_model, "fast": args.fast_model}), flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

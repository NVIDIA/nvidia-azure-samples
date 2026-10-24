#!/usr/bin/env python3
"""Benchmark local Piper voices for warm latency and direct ASR fidelity."""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import time
import wave
from pathlib import Path
from urllib.request import Request, urlopen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, default=Path.home() / ".cache/dgx-spark/piper")
    parser.add_argument("--text", default="Violet cranes carry eight brass maps beyond a silent willow gate.")
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument("--asr-url", default="http://127.0.0.1:8012/transcribe")
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def normalized_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def word_errors(reference: str, hypothesis: str) -> int:
    left, right = normalized_words(reference), normalized_words(hypothesis)
    row = list(range(len(right) + 1))
    for index, word in enumerate(left, 1):
        following = [index]
        for other_index, other in enumerate(right, 1):
            following.append(min(following[-1] + 1, row[other_index] + 1, row[other_index - 1] + (word != other)))
        row = following
    return row[-1]


def transcribe(url: str, audio: bytes) -> dict:
    body = json.dumps({"audio_base64": base64.b64encode(audio).decode("ascii")}).encode("utf-8")
    request = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    args = parse_args()
    from piper import PiperVoice

    args.audio_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for model_path in sorted(args.model_dir.glob("en_US-*-medium.onnx")):
        load_started = time.perf_counter()
        voice = PiperVoice.load(str(model_path))
        load_seconds = time.perf_counter() - load_started
        times = []
        audio_path = args.audio_dir / f"{model_path.stem}.wav"
        for _ in range(max(2, args.repetitions)):
            started = time.perf_counter()
            with wave.open(str(audio_path), "wb") as wav_file:
                voice.synthesize_wav(args.text, wav_file)
            times.append(time.perf_counter() - started)
        asr = transcribe(args.asr_url, audio_path.read_bytes())
        models = {}
        for model_name, key in (("canary", "primary_text"), ("parakeet", "fast_text")):
            hypothesis = str(asr.get(key) or "")
            errors = word_errors(args.text, hypothesis)
            models[model_name] = {
                "text": hypothesis,
                "errors": errors,
                "wer": round(errors / max(1, len(normalized_words(args.text))), 6),
            }
        with wave.open(str(audio_path), "rb") as wav_file:
            audio_seconds = wav_file.getnframes() / max(1, wav_file.getframerate())
        row = {
            "voice": model_path.stem,
            "model_path": str(model_path),
            "audio_path": str(audio_path),
            "load_seconds": round(load_seconds, 6),
            "warm_median_seconds": round(statistics.median(times[1:]), 6),
            "warm_p95_seconds": round(sorted(times[1:])[min(len(times[1:]) - 1, int(len(times[1:]) * 0.95))], 6),
            "audio_seconds": round(audio_seconds, 4),
            "models": models,
        }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    payload = {
        "text": args.text,
        "repetitions": max(2, args.repetitions),
        "policy": "same_text_no_gain_or_loudness_changes",
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

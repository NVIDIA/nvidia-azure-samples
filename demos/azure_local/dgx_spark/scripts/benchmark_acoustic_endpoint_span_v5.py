#!/usr/bin/env python3
"""Replay captured PCM against v4 total-buffer and v5 speech-span cadence metrics."""

from __future__ import annotations

from array import array
import argparse
import json
import math
from pathlib import Path
import statistics
import sys
import wave


def mono_samples(path: Path) -> tuple[list[float], int]:
    with wave.open(str(path), "rb") as wav_file:
        channels = max(1, wav_file.getnchannels())
        width = wav_file.getsampwidth()
        rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())
    if width != 2:
        raise ValueError(f"unsupported sample width {width}: {path}")
    values = array("h")
    values.frombytes(raw)
    if sys.byteorder == "big":
        values.byteswap()
    return [float(values[index]) / 32768.0 for index in range(0, len(values), channels)], rate


def analyze(path: Path, chunk_seconds: float, rms_threshold: float, peak_threshold: float) -> dict:
    samples, rate = mono_samples(path)
    chunk_samples = max(1, int(rate * chunk_seconds))
    flags = []
    for start in range(0, len(samples), chunk_samples):
        chunk = samples[start : start + chunk_samples]
        rms = math.sqrt(sum(value * value for value in chunk) / len(chunk)) if chunk else 0.0
        peak = max((abs(value) for value in chunk), default=0.0)
        flags.append(rms >= rms_threshold and peak >= peak_threshold)
    voiced = [index for index, value in enumerate(flags) if value]
    if not voiced:
        return {"path": str(path), "error": "no voiced chunks"}
    first = voiced[0]
    last = voiced[-1]
    span = flags[first : last + 1]
    voice_chunks = sum(flags)
    span_chunks = len(span)
    max_internal_gap = 0
    gap = 0
    for value in span:
        if value:
            max_internal_gap = max(max_internal_gap, gap)
            gap = 0
        else:
            gap += 1
    old_ratio = voice_chunks / len(flags)
    new_ratio = voice_chunks / span_chunks
    voiced_seconds = voice_chunks * chunk_seconds
    short_burst = voiced_seconds <= max(1.25, chunk_seconds * 10.0)
    old_pause_rich = len(flags) >= 4 and old_ratio <= 0.8
    new_pause_rich = span_chunks >= 4 and new_ratio <= 0.8
    old_required = 1.5 if short_burst or old_pause_rich else 0.65
    new_required = 1.5 if short_burst or new_pause_rich else 0.65
    internal_gap_seconds = max_internal_gap * chunk_seconds
    return {
        "path": str(path),
        "audio_seconds": round(len(samples) / rate, 3),
        "voice_chunks": voice_chunks,
        "buffer_chunks": len(flags),
        "speech_span_chunks": span_chunks,
        "leading_chunks": first,
        "trailing_chunks": len(flags) - last - 1,
        "voiced_seconds": round(voiced_seconds, 3),
        "old_voiced_ratio": round(old_ratio, 4),
        "new_voiced_ratio": round(new_ratio, 4),
        "old_pause_rich": old_pause_rich,
        "new_pause_rich": new_pause_rich,
        "short_burst": short_burst,
        "old_required_silence_seconds": old_required,
        "new_required_silence_seconds": new_required,
        "estimated_endpoint_savings_seconds": round(old_required - new_required, 3),
        "max_internal_subthreshold_seconds": round(internal_gap_seconds, 3),
        "new_dense_split_risk": bool(new_required == 0.65 and internal_gap_seconds >= 0.65),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("benchmarks/audio_environment/corpus/raw"))
    parser.add_argument("--chunk-seconds", type=float, default=0.125)
    parser.add_argument("--rms-threshold", type=float, default=0.004)
    parser.add_argument("--peak-threshold", type=float, default=0.018)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [
        analyze(path, args.chunk_seconds, args.rms_threshold, args.peak_threshold)
        for path in sorted(args.corpus.glob("*.wav"))
    ]
    valid = [row for row in rows if not row.get("error")]
    savings = [float(row["estimated_endpoint_savings_seconds"]) for row in valid]
    payload = {
        "policy_candidate": "acoustic_cadence_endpoint_v5_speech_span",
        "physical_wave_audio_only": True,
        "content_matcher": False,
        "volume_or_gain_changed": False,
        "chunk_seconds": args.chunk_seconds,
        "rms_threshold": args.rms_threshold,
        "peak_threshold": args.peak_threshold,
        "cases": len(rows),
        "valid_cases": len(valid),
        "expedited_cases": sum(row["estimated_endpoint_savings_seconds"] > 0 for row in valid),
        "new_dense_split_risk_cases": sum(bool(row["new_dense_split_risk"]) for row in valid),
        "median_estimated_savings_seconds": round(statistics.median(savings), 3) if savings else 0.0,
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in payload.items() if key != "rows"}, indent=2))
    return 0 if valid and payload["new_dense_split_risk_cases"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

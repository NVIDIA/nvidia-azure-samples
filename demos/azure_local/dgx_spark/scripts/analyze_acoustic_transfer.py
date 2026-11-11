#!/usr/bin/env python3
"""Generate and analyze a same-level physical speaker-to-microphone sweep."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile


BAND_CENTERS_HZ = (200, 315, 500, 800, 1250, 2000, 3150)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--sample-rate", type=int, default=22050)
    generate.add_argument("--sweep-seconds", type=float, default=6.0)
    generate.add_argument("--padding-seconds", type=float, default=0.75)
    generate.add_argument("--start-hz", type=float, default=160.0)
    generate.add_argument("--end-hz", type=float, default=3800.0)
    generate.add_argument("--target-rms-dbfs", type=float, default=-16.6)

    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("--source", type=Path, required=True)
    analyze.add_argument("--capture", type=Path, required=True)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--max-inverse-db", type=float, default=6.0)
    match_rms = subparsers.add_parser("match-rms")
    match_rms.add_argument("--reference", type=Path, required=True)
    match_rms.add_argument("--input", type=Path, required=True)
    match_rms.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def mono(samples: np.ndarray) -> np.ndarray:
    original = np.asarray(samples)
    if np.issubdtype(original.dtype, np.integer):
        scale = float(max(abs(np.iinfo(original.dtype).min), np.iinfo(original.dtype).max))
        samples = original.astype(np.float64) / scale
    else:
        samples = original.astype(np.float64)
    return samples.mean(axis=1) if samples.ndim > 1 else samples


def generate_sweep(args: argparse.Namespace) -> dict:
    sample_rate = int(args.sample_rate)
    sweep_count = int(round(float(args.sweep_seconds) * sample_rate))
    padding_count = int(round(float(args.padding_seconds) * sample_rate))
    timeline = np.arange(sweep_count, dtype=np.float64) / sample_rate
    sweep = signal.chirp(
        timeline,
        f0=float(args.start_hz),
        f1=float(args.end_hz),
        t1=float(args.sweep_seconds),
        method="logarithmic",
    )
    fade_count = max(1, int(round(0.05 * sample_rate)))
    fade = np.sin(np.linspace(0.0, np.pi / 2.0, fade_count)) ** 2
    sweep[:fade_count] *= fade
    sweep[-fade_count:] *= fade[::-1]
    target_rms = 10.0 ** (float(args.target_rms_dbfs) / 20.0)
    sweep *= target_rms / max(1e-12, float(np.sqrt(np.mean(sweep**2))))
    output = np.concatenate((np.zeros(padding_count), sweep, np.zeros(padding_count)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(args.output, sample_rate, np.round(np.clip(output, -1.0, 1.0) * 32767.0).astype(np.int16))
    return {
        "kind": "log_sweep",
        "output": str(args.output),
        "sample_rate": sample_rate,
        "duration_seconds": round(len(output) / sample_rate, 4),
        "sweep_seconds": float(args.sweep_seconds),
        "padding_seconds": float(args.padding_seconds),
        "start_hz": float(args.start_hz),
        "end_hz": float(args.end_hz),
        "target_rms_dbfs": float(args.target_rms_dbfs),
        "measured_active_rms_dbfs": round(20 * np.log10(max(1e-12, np.sqrt(np.mean(sweep**2)))), 4),
        "peak_dbfs": round(20 * np.log10(max(1e-12, np.max(np.abs(output)))), 4),
    }


def resample(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return samples
    divisor = int(np.gcd(source_rate, target_rate))
    return signal.resample_poly(samples, target_rate // divisor, source_rate // divisor)


def analyze_transfer(args: argparse.Namespace) -> dict:
    source_rate, source = wavfile.read(args.source)
    capture_rate, capture = wavfile.read(args.capture)
    source = resample(mono(source), int(source_rate), int(capture_rate))
    capture = mono(capture)
    correlation = signal.correlate(capture, source, mode="full", method="fft")
    lag = int(np.argmax(np.abs(correlation)) - (len(source) - 1))
    source_start = max(0, -lag)
    capture_start = max(0, lag)
    sample_count = min(len(source) - source_start, len(capture) - capture_start)
    aligned_source = source[source_start : source_start + sample_count]
    aligned_capture = capture[capture_start : capture_start + sample_count]
    frequencies, source_psd = signal.welch(aligned_source, fs=capture_rate, nperseg=2048)
    _, capture_psd = signal.welch(aligned_capture, fs=capture_rate, nperseg=2048)
    response_db = 10.0 * np.log10(np.maximum(capture_psd, 1e-18) / np.maximum(source_psd, 1e-18))
    band_response = []
    raw_inverse = []
    for center in BAND_CENTERS_HZ:
        low = center / np.sqrt(2.0)
        high = center * np.sqrt(2.0)
        selected = response_db[(frequencies >= low) & (frequencies <= high)]
        response = float(np.median(selected)) if selected.size else 0.0
        band_response.append({"center_hz": center, "response_db": round(response, 3)})
        raw_inverse.append(-response)
    # Remove the broadband offset so compensation changes spectral balance but
    # not nominal overall loudness, then cap extreme room-null boosts.
    inverse = np.asarray(raw_inverse) - float(np.mean(raw_inverse))
    inverse = np.clip(inverse, -float(args.max_inverse_db), float(args.max_inverse_db))
    equalizer = [
        {"center_hz": center, "gain_db": round(float(gain), 3), "width_octaves": 1.0}
        for center, gain in zip(BAND_CENTERS_HZ, inverse)
    ]
    payload = {
        "source": str(args.source),
        "capture": str(args.capture),
        "source_sample_rate": int(source_rate),
        "capture_sample_rate": int(capture_rate),
        "alignment_lag_seconds": round(lag / float(capture_rate), 6),
        "aligned_seconds": round(sample_count / float(capture_rate), 4),
        "source_rms_dbfs": round(20 * np.log10(max(1e-12, np.sqrt(np.mean(aligned_source**2)))), 3),
        "capture_rms_dbfs": round(20 * np.log10(max(1e-12, np.sqrt(np.mean(aligned_capture**2)))), 3),
        "band_response": band_response,
        "inverse_equalizer": equalizer,
        "inverse_mean_gain_db": round(float(np.mean(inverse)), 6),
        "loudness_policy": "zero_mean_spectral_compensation_no_broadband_gain",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def match_rms(args: argparse.Namespace) -> dict:
    reference_rate, reference = wavfile.read(args.reference)
    input_rate, samples = wavfile.read(args.input)
    reference = mono(reference)
    samples = mono(samples)
    reference_rms = float(np.sqrt(np.mean(reference**2)))
    input_rms = float(np.sqrt(np.mean(samples**2)))
    gain = reference_rms / max(1e-12, input_rms)
    matched = samples * gain
    clipped_samples = int(np.count_nonzero(np.abs(matched) > 1.0))
    matched = np.clip(matched, -1.0, 1.0)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(args.output, int(input_rate), np.round(matched * 32767.0).astype(np.int16))
    payload = {
        "reference": str(args.reference),
        "input": str(args.input),
        "output": str(args.output),
        "reference_sample_rate": int(reference_rate),
        "output_sample_rate": int(input_rate),
        "reference_rms_dbfs": round(20 * np.log10(max(1e-12, reference_rms)), 4),
        "input_rms_dbfs": round(20 * np.log10(max(1e-12, input_rms)), 4),
        "applied_gain_db": round(20 * np.log10(max(1e-12, gain)), 4),
        "output_rms_dbfs": round(20 * np.log10(max(1e-12, np.sqrt(np.mean(matched**2)))), 4),
        "clipped_samples": clipped_samples,
        "loudness_policy": "match_reference_whole_file_rms",
    }
    return payload


def main() -> int:
    args = parse_args()
    if args.command == "generate":
        payload = generate_sweep(args)
    elif args.command == "analyze":
        payload = analyze_transfer(args)
    else:
        payload = match_rms(args)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

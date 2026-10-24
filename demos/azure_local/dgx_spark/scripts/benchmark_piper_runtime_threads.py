#!/usr/bin/env python3
"""Benchmark Piper CPU session threading without changing voice or audio levels."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import time
import wave


TEXTS = (
    "What is the exact local time now?",
    "Before sunrise Nadia counted thirteen amber circuit boards near the western cabinet.",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="append", type=Path, required=True)
    parser.add_argument("--threads", nargs="+", type=int, default=[0, 1, 2, 4, 8])
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def load_voice(model_path: Path, threads: int):
    import onnxruntime
    from piper import PiperConfig, PiperVoice
    from piper.voice import ESPEAK_DATA_DIR

    config = PiperConfig.from_dict(json.loads(Path(f"{model_path}.json").read_text(encoding="utf-8")))
    options = onnxruntime.SessionOptions()
    if threads > 0:
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
    session = onnxruntime.InferenceSession(str(model_path), sess_options=options, providers=["CPUExecutionProvider"])
    return PiperVoice(
        config=config,
        session=session,
        espeak_data_dir=Path(ESPEAK_DATA_DIR),
        download_dir=Path.cwd(),
    )


def main() -> int:
    args = parse_args()
    args.audio_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for model_path in args.model:
        for threads in args.threads:
            voice = load_voice(model_path, threads)
            for text_index, text in enumerate(TEXTS, 1):
                times = []
                audio_path = args.audio_dir / f"{model_path.stem}-threads-{threads}-text-{text_index}.wav"
                for _ in range(max(2, args.repetitions)):
                    started = time.perf_counter()
                    with wave.open(str(audio_path), "wb") as wav_file:
                        voice.synthesize_wav(text, wav_file)
                    times.append(time.perf_counter() - started)
                audio = audio_path.read_bytes()
                rows.append({
                    "voice": model_path.stem,
                    "threads": threads,
                    "text_index": text_index,
                    "text": text,
                    "warm_median_seconds": round(statistics.median(times[1:]), 6),
                    "warm_p95_seconds": round(sorted(times[1:])[min(len(times[1:]) - 1, int(len(times[1:]) * 0.95))], 6),
                    "audio_sha256": hashlib.sha256(audio).hexdigest(),
                    "audio_bytes": len(audio),
                })
    payload = {
        "policy": "same_piper_models_text_and_levels_only_onnx_cpu_threading_varies",
        "repetitions": max(2, args.repetitions),
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

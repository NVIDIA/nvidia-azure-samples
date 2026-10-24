#!/usr/bin/env python3
"""Benchmark content-agnostic, no-normalization audio filters through live dual ASR."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import time
import wave
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen


FILTERS = {
    "baseline": "anull",
    "highpass_80": "highpass=f=80",
    "highpass_120": "highpass=f=120",
    "lowpass_7000": "lowpass=f=7000",
    "bandpass_80_7000": "highpass=f=80,lowpass=f=7000",
    "afftdn_6db": "afftdn=nr=6:nf=-50:tn=1",
    "afftdn_10db": "afftdn=nr=10:nf=-50:tn=1",
    "anlmdn_light": "anlmdn=s=0.0005:p=0.002:r=0.006",
}


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", action="append", type=Path, required=True)
    parser.add_argument("--asr-url", default="http://127.0.0.1:8012/transcribe")
    parser.add_argument("--filter", action="append", choices=tuple(FILTERS))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args()


def words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for ref_index, ref_word in enumerate(reference, 1):
        following = [ref_index]
        for hyp_index, hyp_word in enumerate(hypothesis, 1):
            following.append(min(
                following[-1] + 1,
                row[hyp_index] + 1,
                row[hyp_index - 1] + (ref_word != hyp_word),
            ))
        row = following
    return row[-1]


def manifest_cases(paths: list[Path]) -> list[dict]:
    cases: list[dict] = []
    seen: set[str] = set()
    for manifest in paths:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            if not str(case.get("reference") or "").strip() or case["id"] in seen:
                continue
            audio = (manifest.parent / case["audio"]).resolve()
            if not audio.is_file():
                continue
            seen.add(case["id"])
            cases.append({**case, "audio": str(audio), "manifest": str(manifest.resolve())})
    return cases


def filter_wav(path: Path, expression: str) -> tuple[bytes, float]:
    started = time.perf_counter()
    process = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-af", expression, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    wav = BytesIO()
    with wave.open(wav, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(process.stdout)
    return wav.getvalue(), time.perf_counter() - started


def transcribe(url: str, audio: bytes, timeout: float) -> tuple[dict, float]:
    payload = json.dumps({"audio_base64": base64.b64encode(audio).decode("ascii")}).encode("utf-8")
    request = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    with urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result, time.perf_counter() - started


def main() -> int:
    args = arguments()
    cases = manifest_cases(args.manifest)
    selected = args.filter or list(FILTERS)
    results: list[dict] = []
    for filter_name in selected:
        expression = FILTERS[filter_name]
        for case in cases:
            wav, filter_seconds = filter_wav(Path(case["audio"]), expression)
            asr, wall_seconds = transcribe(args.asr_url, wav, args.timeout)
            reference_words = words(case["reference"])
            model_results = {}
            for model_key, text_key in (("primary", "primary_text"), ("fast", "fast_text")):
                hypothesis = str(asr.get(text_key) or "")
                errors = edit_distance(reference_words, words(hypothesis))
                model_results[model_key] = {
                    "text": hypothesis,
                    "errors": errors,
                    "reference_words": len(reference_words),
                    "wer": round(errors / max(1, len(reference_words)), 6),
                }
            results.append({
                "filter": filter_name,
                "filter_expression": expression,
                "case_id": case["id"],
                "audio": case["audio"],
                "reference": case["reference"],
                "tags": case.get("tags") or [],
                "filter_seconds": round(filter_seconds, 6),
                "asr_wall_seconds": round(wall_seconds, 6),
                "asr_request_seconds": asr.get("request_seconds"),
                "models": model_results,
            })
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    summary = {}
    for filter_name in selected:
        filtered = [item for item in results if item["filter"] == filter_name]
        summary[filter_name] = {
            "cases": len(filtered),
            "mean_filter_seconds": round(sum(item["filter_seconds"] for item in filtered) / len(filtered), 6),
            "mean_asr_wall_seconds": round(sum(item["asr_wall_seconds"] for item in filtered) / len(filtered), 6),
        }
        for model_key in ("primary", "fast"):
            errors = sum(item["models"][model_key]["errors"] for item in filtered)
            ref_words = sum(item["models"][model_key]["reference_words"] for item in filtered)
            summary[filter_name][f"{model_key}_corpus_wer"] = round(errors / max(1, ref_words), 6)
    output = {
        "policy": "content_agnostic_filters_without_volume_or_rms_normalization",
        "asr_url": args.asr_url,
        "filters": {name: FILTERS[name] for name in selected},
        "summary": summary,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

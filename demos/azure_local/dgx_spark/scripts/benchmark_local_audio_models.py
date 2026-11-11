#!/usr/bin/env python3
"""Benchmark local speech/audio models on identical, never-prompted reference audio.

Ground-truth text is used only for scoring. The model adapter receives the WAV
path and a fixed task instruction; it never sees the reference, case id, notes,
or expected intent.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import time
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--adapter", choices=("openai-audio", "nemo-asr", "transformers-whisper"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--dtype", choices=("auto", "float16", "bfloat16", "float32"), default="auto")
    parser.add_argument("--base-url", default="http://127.0.0.1:8010/v1")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--max-new-tokens", type=int, default=128)
    parser.add_argument("--task", choices=("transcribe", "environment", "spoken-query"), default="transcribe")
    return parser.parse_args()


def normalized_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(text or "")).lower()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = "".join(ch if ch.isalnum() or ch == "'" else " " for ch in text)
    return text.split()


def normalized_chars(text: str) -> list[str]:
    return list(" ".join(normalized_words(text)))


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    previous = list(range(len(hypothesis) + 1))
    for row, ref_item in enumerate(reference, start=1):
        current = [row]
        for col, hyp_item in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[col] + 1,
                    previous[col - 1] + (ref_item != hyp_item),
                )
            )
        previous = current
    return previous[-1]


def error_rate(reference: list[str], hypothesis: list[str]) -> float:
    if not reference:
        return 0.0 if not hypothesis else 1.0
    return edit_distance(reference, hypothesis) / len(reference)


def score(reference: str, hypothesis: str) -> dict[str, Any]:
    ref_words = normalized_words(reference)
    hyp_words = normalized_words(hypothesis)
    ref_chars = normalized_chars(reference)
    hyp_chars = normalized_chars(hypothesis)
    prefix_words = 0
    for expected, actual in zip(ref_words, hyp_words):
        if expected != actual:
            break
        prefix_words += 1
    reference_has_content = bool(str(reference or "").strip())
    hypothesis_has_content = bool(str(hypothesis or "").strip())
    if not reference_has_content:
        no_speech_correct = not hypothesis_has_content
        return {
            "wer": 0.0 if no_speech_correct else 1.0,
            "cer": 0.0 if no_speech_correct else 1.0,
            "exact_normalized": no_speech_correct,
            "first_word_correct": False,
            "correct_prefix_words": 0,
            "reference_words": 0,
            "hypothesis_words": len(hyp_words),
            "no_speech_correct": no_speech_correct,
        }
    return {
        "wer": round(error_rate(ref_words, hyp_words), 6),
        "cer": round(error_rate(ref_chars, hyp_chars), 6),
        "exact_normalized": ref_words == hyp_words,
        "first_word_correct": bool(ref_words and hyp_words and ref_words[0] == hyp_words[0]),
        "correct_prefix_words": prefix_words,
        "reference_words": len(ref_words),
        "hypothesis_words": len(hyp_words),
        "no_speech_correct": None,
    }


def load_manifest(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or not case.get("audio"):
            raise ValueError(f"Invalid manifest record at line {line_number}")
        audio = Path(str(case["audio"]))
        if not audio.is_absolute():
            audio = (path.parent / audio).resolve()
        case["audio"] = str(audio)
        case.setdefault("id", audio.stem)
        case.setdefault("reference", "")
        cases.append(case)
    return cases


def extract_openai_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") if isinstance(response, dict) else None
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(item.get("text") or "").strip()
            for item in content
            if isinstance(item, dict) and item.get("text")
        ).strip()
    return ""


class OpenAIAudioAdapter:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args

    def transcribe(self, audio_path: Path) -> str:
        encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        if self.args.task == "transcribe":
            instruction = "Transcribe the spoken content exactly. Return only the transcript."
        elif self.args.task == "environment":
            instruction = (
                "Describe the audible environment. Separate spoken words from non-speech sounds, "
                "and state uncertainty rather than inventing details."
            )
        else:
            instruction = "Answer the spoken request in the audio directly and concisely."
        payload = {
            "model": self.args.model,
            "stream": False,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "audio_url", "audio_url": {"url": f"data:audio/wav;base64,{encoded}"}},
                    {"type": "text", "text": instruction},
                ],
            }],
            "max_tokens": self.args.max_new_tokens,
            "temperature": 0,
            "top_k": 1,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request = urllib.request.Request(
            self.args.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.args.timeout) as response:
            return extract_openai_text(json.loads(response.read().decode("utf-8")))


class NemoAsrAdapter:
    def __init__(self, args: argparse.Namespace) -> None:
        import torch
        from nemo.collections.asr.models import ASRModel

        map_location = args.device if args.device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
        self.model = ASRModel.from_pretrained(model_name=args.model, map_location=map_location)
        self.model.to(map_location)
        self.model.eval()
        self.map_location = str(map_location)

    def transcribe(self, audio_path: Path) -> str:
        audio: list[Any] = [str(audio_path)]
        if self.map_location == "cpu":
            import soundfile as sf

            samples, sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=False)
            if getattr(samples, "ndim", 1) > 1:
                samples = samples.mean(axis=1)
            expected_sample_rate = int(getattr(self.model, "sample_rate", 16000) or 16000)
            if sample_rate != expected_sample_rate:
                import torch
                import torchaudio.functional as audio_functional

                samples = audio_functional.resample(
                    torch.as_tensor(samples), sample_rate, expected_sample_rate
                ).numpy()
            # Tensor input uses NeMo's non-pinned transcription loader. This keeps
            # CPU-only model bakeoffs from touching the live stack's CUDA pool.
            audio = [samples]
        result = self.model.transcribe(audio, batch_size=1, verbose=False)
        if isinstance(result, tuple):
            result = result[0]
        item = result[0] if isinstance(result, list) and result else result
        return str(getattr(item, "text", item) or "").strip()


class TransformersWhisperAdapter:
    def __init__(self, args: argparse.Namespace) -> None:
        import torch
        from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline

        device = args.device if args.device != "auto" else ("cuda:0" if torch.cuda.is_available() else "cpu")
        dtype_name = args.dtype
        if dtype_name == "auto":
            dtype_name = "float16" if str(device).startswith("cuda") else "float32"
        torch_dtype = getattr(torch, dtype_name)
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            args.model,
            torch_dtype=torch_dtype,
            low_cpu_mem_usage=True,
            use_safetensors=True,
        ).to(device)
        processor = AutoProcessor.from_pretrained(args.model)
        self.pipe = pipeline(
            "automatic-speech-recognition",
            model=model,
            tokenizer=processor.tokenizer,
            feature_extractor=processor.feature_extractor,
            torch_dtype=torch_dtype,
            device=device,
        )
        self.max_new_tokens = args.max_new_tokens

    def transcribe(self, audio_path: Path) -> str:
        result = self.pipe(str(audio_path), max_new_tokens=self.max_new_tokens, generate_kwargs={"language": "en"})
        return str(result.get("text") if isinstance(result, dict) else result or "").strip()


def build_adapter(args: argparse.Namespace):
    if args.adapter == "openai-audio":
        return OpenAIAudioAdapter(args)
    if args.adapter == "nemo-asr":
        return NemoAsrAdapter(args)
    return TransformersWhisperAdapter(args)


def main() -> int:
    args = parse_args()
    cases = load_manifest(args.manifest)
    load_started = time.perf_counter()
    adapter = build_adapter(args)
    load_seconds = time.perf_counter() - load_started
    rows: list[dict[str, Any]] = []
    for case in cases:
        audio_path = Path(case["audio"])
        for repetition in range(max(1, args.repetitions)):
            started = time.perf_counter()
            hypothesis = adapter.transcribe(audio_path)
            latency = time.perf_counter() - started
            row = {
                "case_id": case["id"],
                "audio": str(audio_path),
                "adapter": args.adapter,
                "model": args.model,
                "task": args.task,
                "repetition": repetition,
                "cold_inference": repetition == 0,
                "load_seconds": round(load_seconds, 6),
                "latency_seconds": round(latency, 6),
                "hypothesis": hypothesis,
                "reference": case.get("reference", ""),
                "tags": case.get("tags", []),
                **score(str(case.get("reference") or ""), hypothesis),
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

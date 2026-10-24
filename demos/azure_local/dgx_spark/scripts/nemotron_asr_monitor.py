#!/usr/bin/env python3
"""Understand background microphone inputs with a local Nemotron streaming speech model."""

from __future__ import annotations

import argparse
import math
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from array import array
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_PATH = (
    Path.home()
    / ".cache/huggingface/hub/models--nvidia--nemotron-speech-streaming-en-0.6b"
    / "snapshots/ef3bf40c90df5cd2de55cc07e06681e03d8e6ee4"
    / "nemotron-speech-streaming-en-0.6b.nemo"
)
DEFAULT_SERVER_PULSE_SOURCE = "alsa_input.usb-webcamvendor_NexiGo_N60_FHD_Webcam_Jan_29_2024-10_32_28-N60-02.mono-fallback"
TRANSCRIBABLE_BROWSER_SUFFIXES = {".webm", ".ogg", ".m4a", ".mp4", ".wav", ".bin"}
STAGE_TIMINGS: dict[str, dict] = {}
ACTIVE_ASR_MODEL_LABEL = DEFAULT_MODEL_PATH.name
ACTIVE_ASR_MODEL_PATH = str(DEFAULT_MODEL_PATH)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL_PATH), help="Local .nemo speech-understanding model path")
    parser.add_argument("--transcript-json", default=str(PROJECT_ROOT / "webcam-transcript.json"))
    parser.add_argument("--voice-response-json", default=str(PROJECT_ROOT / "webcam-voice-response.json"))
    parser.add_argument("--pipeline-mode-json", default=str(PROJECT_ROOT / "webcam-speech-pipeline-mode.json"))
    parser.add_argument("--input-status-url", default="http://127.0.0.1:8090/input-status.json")
    parser.add_argument(
        "--source-mode",
        choices=("selected", "server", "browser", "wifi", "all"),
        default="all",
        help="Which audio sources to process. The default 'all' monitors server, Wi-Fi camera, and browser audio.",
    )
    parser.add_argument("--browser-audio-dir", default=str(PROJECT_ROOT / "browser-audio-chunks"))
    parser.add_argument("--wifi-audio-url", default="http://127.0.0.1:8090/wifi-audio.wav")
    parser.add_argument("--audio-buffer-control-json", default=str(PROJECT_ROOT / "webcam-audio-buffer-control.json"))
    parser.add_argument("--asr-settings-json", default=str(PROJECT_ROOT / "webcam-asr-settings.json"))
    parser.add_argument("--server-audio-format", default="auto", choices=("auto", "pulse", "alsa"))
    parser.add_argument(
        "--server-audio-source",
        default="auto",
        help="Pulse source or ALSA device for server microphone capture; use 'auto' to detect the USB webcam mic",
    )
    parser.add_argument("--chunk-seconds", type=float, default=1.0)
    parser.add_argument("--browser-chunk-min-age", type=float, default=0.08)
    parser.add_argument(
        "--browser-chunk-max-age",
        type=float,
        default=10.0,
        help="Drop browser microphone chunks older than this many seconds instead of draining a stale backlog",
    )
    parser.add_argument(
        "--utterance-audio-context",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retranscribe short rolling utterance audio windows so words split across chunk boundaries can be recovered.",
    )
    parser.add_argument(
        "--publish-interim-transcripts",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Publish single-chunk speech-understanding guesses as transcript segments. Disabled by default so downstream agents consume finalized utterances.",
    )
    parser.add_argument("--utterance-gap-seconds", type=float, default=1.6)
    parser.add_argument(
        "--utterance-final-silence-seconds",
        type=float,
        default=2.6,
        help="Minimum silence before a buffered utterance is finalized. Kept separate from the legacy gap flag so short pauses are not chopped.",
    )
    parser.add_argument("--utterance-short-phrase-extra-silence", type=float, default=1.2)
    parser.add_argument("--utterance-min-final-words", type=int, default=3)
    parser.add_argument("--utterance-preroll-seconds", type=float, default=1.4)
    parser.add_argument("--utterance-max-trailing-silence-seconds", type=float, default=1.2)
    parser.add_argument("--speech-rms-threshold", type=float, default=0.008)
    parser.add_argument("--speech-peak-threshold", type=float, default=0.04)
    parser.add_argument("--utterance-max-seconds", type=float, default=24.0)
    parser.add_argument(
        "--process-existing-browser-audio",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Process browser audio files that already exist when the speech-understanding service starts",
    )
    parser.add_argument("--loop-delay", type=float, default=0.1)
    parser.add_argument("--pause-during-voice-response", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--voice-pause-stale-seconds", type=float, default=30.0)
    parser.add_argument("--max-segments", type=int, default=12)
    parser.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    parser.add_argument("--once", action="store_true", help="Process one audio chunk and exit")
    return parser.parse_args()


def publish(path: str | Path, payload: dict) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), **payload}
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(output_path)


def read_json(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def selected_pipeline_mode(path: str | Path) -> str:
    mode = str(read_json(path).get("mode") or "classic").lower()
    return mode if mode in {"classic", "voicechat"} else "classic"


def bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    if not math.isfinite(number):
        number = default
    return min(maximum, max(minimum, number))


def read_asr_settings(args: argparse.Namespace) -> dict:
    data = read_json(args.asr_settings_json)
    return {
        "speech_rms_threshold": bounded_float(
            data.get("speech_rms_threshold"),
            float(args.speech_rms_threshold),
            0.0,
            0.05,
        ),
        "speech_peak_threshold": bounded_float(
            data.get("speech_peak_threshold"),
            float(args.speech_peak_threshold),
            0.0,
            0.2,
        ),
        "updated_at": data.get("updated_at") or 0,
    }


def audio_clear_state(path: str | Path) -> tuple[str, float]:
    data = read_json(path)
    marker = str(data.get("clear_requested_at") or data.get("updated_at") or "")
    try:
        cleared_at = float(data.get("clear_requested_at") or data.get("updated_at") or 0)
    except (TypeError, ValueError):
        cleared_at = 0.0
    return marker, cleared_at


def voice_response_active(args: argparse.Namespace, source: str | None = None) -> tuple[bool, str, dict]:
    if not args.pause_during_voice_response:
        return False, "", {}
    voice = read_json(args.voice_response_json)
    if not voice:
        return False, "", {}
    voice_source = str(voice.get("input_source") or voice.get("source") or "").lower()
    if source and voice_source and voice_source != source:
        return False, "", voice
    try:
        updated_at = float(voice.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0.0
    if updated_at > 0 and time.time() - updated_at > max(1.0, float(args.voice_pause_stale_seconds)):
        return False, "", voice

    status = str(voice.get("status") or "").lower()
    phase = str(voice.get("phase") or "").lower()
    audio_ready = any(str(voice.get(key) or "").strip() for key in ("audio_id", "audio_path", "audio_url", "speech_synthesis_id"))
    if status == "thinking":
        operation = str(voice.get("operation") or voice.get("message") or "voice response active")
        return True, operation, voice
    active_phases = {"context", "nemotron_text", "tool_plan", "tool_call", "tool_results", "voicechat_model", "voicechat_answer"}
    tts_or_output_busy = phase in {"tts", "output", "audio_output"} and not audio_ready
    stage_active = any(
        isinstance(stage, dict)
        and stage.get("status") == "active"
        and str(stage.get("id") or "") not in {"speech"}
        and not (
            audio_ready
            and str(stage.get("id") or "").lower() in {"tts", "output"}
        )
        for stage in voice.get("stages") or []
    )
    if (status == "speaking" and not audio_ready) or phase in active_phases or tts_or_output_busy or stage_active:
        operation = str(voice.get("operation") or voice.get("message") or "voice response active")
        return True, operation, voice
    return False, "", voice


def read_input_source(status_url: str) -> tuple[str, str | None]:
    try:
        with urlopen(status_url, timeout=2.0) as response:
            if response.status != 200:
                return "server", f"input-status returned HTTP {response.status}"
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return "server", f"could not read input status: {exc}"

    source = str(payload.get("source") or "server")
    if source not in {"server", "browser", "wifi"}:
        return "server", f"unknown input source {source!r}; using server microphone"
    return source, None


def run_command(command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def detect_server_audio_input() -> tuple[str, str]:
    if shutil.which("pactl"):
        try:
            result = run_command(["pactl", "list", "short", "sources"], timeout=3)
            sources = []
            for line in result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    sources.append(parts[1])
            for source in sources:
                lower = source.lower()
                if "monitor" not in lower and ("nexigo" in lower or "webcam" in lower):
                    return "pulse", source
            if DEFAULT_SERVER_PULSE_SOURCE in sources:
                return "pulse", DEFAULT_SERVER_PULSE_SOURCE
        except Exception:
            pass

    if shutil.which("arecord"):
        try:
            result = run_command(["arecord", "-l"], timeout=3)
            if "NexiGo" in result.stdout or "Webcam" in result.stdout:
                return "alsa", "plughw:CARD=Webcam,DEV=0"
        except Exception:
            pass

    return "pulse", DEFAULT_SERVER_PULSE_SOURCE


def resolve_server_audio_input(args: argparse.Namespace) -> tuple[str, str]:
    if args.server_audio_format != "auto" and args.server_audio_source != "auto":
        return args.server_audio_format, args.server_audio_source
    if args.server_audio_source != "auto":
        if args.server_audio_format == "auto":
            guessed_format = "alsa" if args.server_audio_source.startswith(("hw:", "plughw:", "/dev/")) else "pulse"
            return guessed_format, args.server_audio_source
        return args.server_audio_format, args.server_audio_source
    return detect_server_audio_input()


def ffmpeg_base() -> list[str]:
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]


def capture_server_wav(args: argparse.Namespace, wav_path: Path, audio_format: str, audio_source: str) -> None:
    command = ffmpeg_base() + [
        "-f",
        audio_format,
        "-i",
        audio_source,
        "-t",
        str(args.chunk_seconds),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(wav_path),
    ]
    run_command(command, timeout=args.chunk_seconds + 8)


def convert_browser_audio(input_path: Path, wav_path: Path) -> None:
    command = ffmpeg_base() + [
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(wav_path),
    ]
    run_command(command, timeout=15)


def capture_wifi_wav(args: argparse.Namespace, wav_path: Path) -> None:
    command = ffmpeg_base() + [
        "-i",
        args.wifi_audio_url,
        "-t",
        str(args.chunk_seconds),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(wav_path),
    ]
    run_command(command, timeout=args.chunk_seconds + 12)


def combine_wavs(input_paths: list[Path], wav_path: Path) -> None:
    if len(input_paths) == 1:
        shutil.copyfile(input_paths[0], wav_path)
        return
    inputs: list[str] = []
    labels = []
    for index, input_path in enumerate(input_paths):
        inputs.extend(["-i", str(input_path)])
        labels.append(f"[{index}:a]")
    command = (
        ffmpeg_base()
        + inputs
        + [
            "-filter_complex",
            "".join(labels) + f"concat=n={len(input_paths)}:v=0:a=1[out]",
            "-map",
            "[out]",
            "-ac",
            "1",
            "-ar",
            "16000",
            str(wav_path),
        ]
    )
    run_command(command, timeout=max(20, len(input_paths) * 8))


def audio_level_for_wav(wav_path: Path) -> dict:
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            sample_width = wav_file.getsampwidth()
            frame_count = wav_file.getnframes()
            raw = wav_file.readframes(frame_count)
        if not raw:
            return {"status": "silent", "rms": 0.0, "peak": 0.0, "level_percent": 0.0}
        scale = 1.0
        if sample_width == 1:
            samples = [byte - 128 for byte in raw]
            scale = 128.0
        elif sample_width == 2:
            sample_array = array("h")
            sample_array.frombytes(raw)
            if sys.byteorder != "little":
                sample_array.byteswap()
            samples = sample_array
            scale = 32768.0
        elif sample_width == 4:
            sample_array = array("i")
            sample_array.frombytes(raw)
            if sys.byteorder != "little":
                sample_array.byteswap()
            samples = sample_array
            scale = 2147483648.0
        else:
            return {"status": "unavailable", "error": f"Unsupported WAV sample width: {sample_width}"}
        if not samples:
            return {"status": "silent", "rms": 0.0, "peak": 0.0, "level_percent": 0.0}
        rms = math.sqrt(sum(float(sample) * float(sample) for sample in samples) / len(samples)) / scale
        peak = max(abs(int(sample)) for sample in samples) / scale
        return {
            "status": "available",
            "rms": round(min(1.0, max(0.0, rms)), 5),
            "peak": round(min(1.0, max(0.0, peak)), 5),
            "level_percent": round(min(100.0, max(0.0, rms * 320.0)), 1),
            "sample_count": len(samples),
        }
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)[:160]}


def wav_duration_seconds(wav_path: Path, fallback: float = 1.0) -> float:
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                return fallback
            return max(0.01, wav_file.getnframes() / float(frame_rate))
    except Exception:
        return fallback


def audio_has_speech_signal(audio_level: dict, rms_threshold: float, peak_threshold: float) -> bool:
    try:
        rms = float(audio_level.get("rms") or 0)
        peak = float(audio_level.get("peak") or 0)
    except (TypeError, ValueError):
        return False
    return rms >= rms_threshold or peak >= peak_threshold


def next_browser_chunk(
    audio_dir: Path,
    processed: set[Path],
    min_age_seconds: float,
    max_age_seconds: float | None = None,
) -> Path | None:
    if not audio_dir.exists():
        return None
    now = time.time()
    candidates = []
    for path in audio_dir.glob("browser_audio_*"):
        if path in processed or path.suffix.lower() not in TRANSCRIBABLE_BROWSER_SUFFIXES:
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        age = now - stat.st_mtime
        if age < min_age_seconds:
            continue
        if max_age_seconds is not None and max_age_seconds > 0 and age > max_age_seconds:
            processed.add(path)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        candidates.append((stat.st_mtime, path))
    if not candidates:
        return None
    return sorted(candidates)[0][1]


def existing_browser_chunks(audio_dir: Path, max_mtime: float | None = None) -> set[Path]:
    if not audio_dir.exists():
        return set()
    chunks = set()
    for path in audio_dir.glob("browser_audio_*"):
        if path.suffix.lower() not in TRANSCRIBABLE_BROWSER_SUFFIXES:
            continue
        if max_mtime is not None:
            try:
                if path.stat().st_mtime > max_mtime:
                    continue
            except FileNotFoundError:
                continue
        chunks.add(path)
    return chunks


def load_model(args: argparse.Namespace):
    import torch
    from nemo.collections.asr.models import ASRModel, EncDecRNNTBPEModel

    requested_device = args.device
    devices = ["cuda", "cpu"] if requested_device == "auto" and torch.cuda.is_available() else [requested_device]
    if devices == ["auto"]:
        devices = ["cpu"]

    load_errors: list[str] = []
    for device_name in devices:
        try:
            device = torch.device(device_name)
            try:
                model = EncDecRNNTBPEModel.restore_from(args.model_path, map_location=device)
            except Exception as exc:
                load_errors.append(f"EncDecRNNTBPEModel on {device_name}: {exc}")
                model = ASRModel.restore_from(args.model_path, map_location=device)
            if not hasattr(model, "transcribe"):
                raise RuntimeError(f"restored model does not expose transcribe(): {type(model).__name__}")
            if hasattr(model, "to"):
                model = model.to(device)
            model.eval()
            return model, device_name
        except Exception as exc:
            load_errors.append(f"{device_name}: {exc}")
            if requested_device != "auto":
                break

    raise RuntimeError("could not load speech-understanding model: " + " | ".join(load_errors[-4:]))


def normalize_transcript(result: object) -> str:
    if isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, list):
        if not result:
            return ""
        result = result[0]
    if hasattr(result, "text"):
        result = getattr(result, "text")
    return " ".join(str(result).strip().split())


def transcribe_wav(model, wav_path: Path) -> str:
    result = model.transcribe([str(wav_path)], batch_size=1, num_workers=0, verbose=False)
    return normalize_transcript(result)


def joined_segments(segments: list[dict]) -> str:
    return "\n".join(segment["text"] for segment in segments if segment.get("text"))


def make_stage(
    stage_id: str,
    title: str,
    status: str,
    message: str,
    timing_key: str | None = None,
    payload: dict | None = None,
) -> dict:
    key = timing_key or f"{stage_id}:{title}"
    now = time.time()
    timing = STAGE_TIMINGS.setdefault(key, {})
    previous_status = timing.get("status")
    if status == "active" and previous_status != "active":
        timing["current_started_at"] = now
    elif status in {"complete", "error"} and previous_status == "active" and timing.get("current_started_at"):
        timing["last_started_at"] = float(timing["current_started_at"])
        timing["last_completed_at"] = now
        timing["last_duration_seconds"] = max(0.0, now - float(timing["current_started_at"]))
        timing["current_started_at"] = 0.0
    elif status in {"complete", "error"} and timing.get("last_duration_seconds") is None:
        timing["last_started_at"] = now
        timing["last_completed_at"] = now
        timing["last_duration_seconds"] = 0.0
    elif status == "waiting" and previous_status == "active":
        timing["current_started_at"] = 0.0
    timing["status"] = status
    stage = {
        "id": stage_id,
        "title": title,
        "status": status,
        "message": message,
        "payload": payload or {},
        "updated_at": now,
    }
    if status == "active" and timing.get("current_started_at"):
        stage["started_at"] = timing["current_started_at"]
        stage["elapsed_seconds"] = round(now - float(timing["current_started_at"]), 2)
    if timing.get("last_duration_seconds") is not None:
        stage["duration_seconds"] = round(float(timing["last_duration_seconds"]), 2)
        stage["last_duration_seconds"] = stage["duration_seconds"]
        stage["completed_at"] = timing.get("last_completed_at", 0)
        stage["last_started_at"] = timing.get("last_started_at", 0)
    return stage


def asr_stages(source: str, capture: str, model: str, publish_status: str, message: str = "") -> list[dict]:
    source_label = {
        "browser": "Browser microphone",
        "wifi": "Wi-Fi camera microphone",
        "server": "Server microphone",
    }.get(source, f"{source.title()} microphone")
    return [
        make_stage("capture", f"{source_label} Audio Capture", capture, message or f"Preparing {source_label.lower()} audio.", f"{source}:capture"),
        make_stage(
            "asr",
            f"Nemotron Speech Understanding ({ACTIVE_ASR_MODEL_LABEL})",
            model,
            "Submitting the latest audio chunk to the GPU speech-understanding model.",
            f"{source}:asr",
            {"model": ACTIVE_ASR_MODEL_LABEL, "model_path": ACTIVE_ASR_MODEL_PATH},
        ),
        make_stage("publish", "Transcript Publish", publish_status, "Publishing recognized speech for downstream agents.", f"{source}:publish"),
    ]


def normalized_words(text: str) -> list[str]:
    return [word for word in re.sub(r"[^a-z0-9']+", " ", text.lower()).split() if word]


def word_count(text: str) -> int:
    return len(normalized_words(text))


def compact_text(text: object) -> str:
    return " ".join(str(text or "").strip().split())


def best_transcript_candidate(*candidates: object) -> str:
    cleaned = []
    for candidate in candidates:
        text = compact_text(candidate)
        if text:
            cleaned.append(text)
    if not cleaned:
        return ""
    return max(cleaned, key=lambda text: (word_count(text), len(text)))


def looks_like_unfinished_phrase(text: str, min_final_words: int) -> bool:
    words = normalized_words(text)
    if not words:
        return True
    if len(words) < min_final_words:
        return True
    trailing_fragments = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "because",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "if",
        "in",
        "is",
        "like",
        "of",
        "on",
        "or",
        "please",
        "should",
        "that",
        "the",
        "then",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "would",
        "you",
    }
    if words[-1] in trailing_fragments:
        return True
    question_starters = {"what", "where", "when", "why", "how", "can", "could", "would", "do", "does", "did", "is", "are"}
    return words[0] in question_starters and len(words) < max(min_final_words + 1, 4)


def promptish_remainder(text: str) -> bool:
    words = normalized_words(text)
    if not words:
        return False
    starters = {
        "what",
        "where",
        "when",
        "why",
        "how",
        "can",
        "could",
        "would",
        "do",
        "does",
        "did",
        "is",
        "are",
        "tell",
        "show",
        "describe",
        "summarize",
        "explain",
        "look",
        "check",
    }
    return words[0] in starters or text.strip().endswith("?")


def repair_nemotron_hotword(text: str) -> str:
    stripped = " ".join(str(text or "").strip().split())
    if not stripped:
        return ""
    match = re.match(
        r"(?is)^(?P<prefix>nemotron|nemo\s+tron|new\s+tron|new\s+tran|neutron|nematron|nemetran|tran|tron)\b(?P<sep>[\s,.:;!?-]*)(?P<rest>.*)$",
        stripped,
    )
    if not match:
        return stripped

    prefix = re.sub(r"\s+", " ", match.group("prefix").lower()).strip()
    rest = " ".join(match.group("rest").strip().split())
    if prefix != "nemotron" and not promptish_remainder(rest):
        return stripped
    if not rest:
        return "Nemotron."
    return f"Nemotron, {rest}"


def find_utterance_segment(segments: list[dict], source: str, utterance_id: str) -> dict | None:
    for segment in reversed(segments):
        if segment.get("source") == source and segment.get("utterance_id") == utterance_id:
            return segment
    return None


def update_segment(segment: dict, source: str, text: str, updated_at: float, metadata: dict | None = None) -> dict:
    segment.update({"updated_at": updated_at, "source": source, "text": text})
    if metadata:
        segment.update(metadata)
    return segment


def append_segment(
    segments: list[dict],
    source: str,
    text: str,
    max_segments: int,
    utterance_id: str | None = None,
    metadata: dict | None = None,
) -> dict | None:
    raw_text = " ".join(str(text or "").strip().split())
    if not raw_text:
        return None
    raw_words = normalized_words(raw_text)
    now = time.time()
    if raw_words and raw_words[0] in {"tran", "tron"} and segments:
        previous = segments[-1]
        previous_words = normalized_words(str(previous.get("text") or ""))
        if previous.get("source") == source and previous_words in (["new"], ["nu"], ["knew"]):
            remainder = re.sub(r"(?is)^\s*\w+[\s,.:;!?-]*", "", raw_text).strip()
            return update_segment(
                previous,
                source,
                f"Nemotron, {remainder}" if remainder else "Nemotron.",
                now,
                {"asr_repair": "merged_split_nemotron_hotword", **(metadata or {})},
            )
    text = repair_nemotron_hotword(raw_text)
    if not text:
        return None
    if utterance_id:
        existing = find_utterance_segment(segments, source, utterance_id)
        if existing is not None:
            return update_segment(existing, source, text, now, metadata)
    if segments and segments[-1].get("text") == text and segments[-1].get("source") == source:
        return update_segment(segments[-1], source, text, now, metadata)
    else:
        segment = {"updated_at": now, "source": source, "text": text}
        if utterance_id:
            segment["utterance_id"] = utterance_id
        if metadata:
            segment.update(metadata)
        segments.append(segment)
    del segments[:-max_segments]
    return segments[-1]


def latest_segment_for_source(segments: list[dict], source: str) -> dict:
    for segment in reversed(segments):
        if segment.get("source") == source and segment.get("text"):
            return segment
    return {}


def transcript_file_segments(path: str | Path, max_segments: int) -> list[dict]:
    data = read_json(path)
    segments = []
    for segment in data.get("segments") or []:
        if isinstance(segment, dict) and str(segment.get("text") or "").strip():
            segments.append(dict(segment))
    return segments[-max(1, int(max_segments or 1)) :]


def make_source_state(
    status: str,
    source: str,
    segments: list[dict],
    message: str | None = None,
    error: str | None = None,
    stages: list[dict] | None = None,
    audio_level: dict | None = None,
    interim_text: str | None = None,
    interim_updated_at: float | None = None,
) -> dict:
    latest = latest_segment_for_source(segments, source)
    payload = {
        "status": status,
        "message": message or error or "",
        "latest_text": latest.get("text", ""),
        "latest_text_at": latest.get("updated_at", 0),
        "segment_count": sum(1 for segment in segments if segment.get("source") == source and segment.get("text")),
        "stages": stages or [],
        "updated_at": time.time(),
    }
    if audio_level is not None:
        payload["audio_level"] = audio_level
    if interim_text is not None:
        payload["interim_text"] = interim_text
        payload["interim_updated_at"] = interim_updated_at or time.time()
    if error:
        payload["error"] = error
    return payload


def publish_state(
    args: argparse.Namespace,
    status: str,
    source: str,
    model_name: str,
    device: str,
    segments: list[dict],
    message: str | None = None,
    error: str | None = None,
    stages: list[dict] | None = None,
    source_states: dict[str, dict] | None = None,
    audio_level: dict | None = None,
    interim_text: str | None = None,
    interim_updated_at: float | None = None,
) -> None:
    if source_states is not None:
        previous_state = source_states.get(source) if isinstance(source_states.get(source), dict) else {}
        if audio_level is None:
            audio_level = previous_state.get("audio_level") if isinstance(previous_state, dict) else None
        if interim_text is None:
            interim_text = previous_state.get("interim_text") if isinstance(previous_state, dict) else None
            interim_updated_at = previous_state.get("interim_updated_at") if isinstance(previous_state, dict) else None
        source_states[source] = make_source_state(
            status,
            source,
            segments,
            message=message,
            error=error,
            stages=stages,
            audio_level=audio_level,
            interim_text=interim_text,
            interim_updated_at=interim_updated_at,
        )
    payload = {
        "status": status,
        "source": source,
        "model": model_name,
        "device": device,
        "text": joined_segments(segments),
        "latest_text": segments[-1]["text"] if segments else "",
        "segments": segments,
        "stages": stages or [],
        "sources": source_states or {},
    }
    if message:
        payload["message"] = message
    if error:
        payload["error"] = error
    publish(args.transcript_json, payload)


def requested_sources(args: argparse.Namespace) -> list[tuple[str, str | None]]:
    if args.source_mode == "server":
        return [("server", None)]
    if args.source_mode == "browser":
        return [("browser", None)]
    if args.source_mode == "wifi":
        return [("wifi", None)]
    if args.source_mode == "all":
        return [("browser", None), ("wifi", None), ("server", None)]
    source, warning = read_input_source(args.input_status_url)
    return [(source, warning)]


def main() -> int:
    global ACTIVE_ASR_MODEL_LABEL, ACTIVE_ASR_MODEL_PATH
    args = parse_args()
    model_path = Path(args.model_path)
    model_name = model_path.stem
    ACTIVE_ASR_MODEL_LABEL = model_path.name
    ACTIVE_ASR_MODEL_PATH = str(model_path)

    publish(
        args.transcript_json,
        {
            "status": "loading",
            "source": "unknown",
            "model": model_name,
            "message": "Loading local NVIDIA Nemotron speech-understanding model.",
        },
    )

    if not model_path.exists():
        publish(args.transcript_json, {"status": "error", "source": "unknown", "model": model_name, "error": f"model not found: {model_path}"})
        return 1
    if not shutil.which("ffmpeg"):
        publish(args.transcript_json, {"status": "error", "source": "unknown", "model": model_name, "error": "ffmpeg is required for audio capture/conversion"})
        return 1

    try:
        model, device = load_model(args)
    except Exception as exc:
        publish(args.transcript_json, {"status": "error", "source": "unknown", "model": model_name, "error": f"model load failed: {exc}"})
        raise

    server_audio_format, server_audio_source = resolve_server_audio_input(args)
    browser_audio_dir = Path(args.browser_audio_dir)
    browser_audio_dir.mkdir(parents=True, exist_ok=True)
    processed_browser_chunks: set[Path] = set()
    if not args.process_existing_browser_audio:
        processed_browser_chunks.update(existing_browser_chunks(browser_audio_dir))
    segments: list[dict] = []
    last_audio_clear_marker, _last_audio_clear_at = audio_clear_state(args.audio_buffer_control_json)
    source_states = {
        "server": make_source_state(
            "waiting",
            "server",
            segments,
            message=f"Model loaded. Listening on {server_audio_format}:{server_audio_source}.",
            stages=asr_stages("server", "waiting", "waiting", "waiting", "Waiting to capture server microphone audio."),
        ),
        "browser": make_source_state(
            "waiting",
            "browser",
            segments,
            message="Waiting for browser microphone audio chunks.",
            stages=asr_stages("browser", "waiting", "waiting", "waiting", "No browser audio chunk is ready yet."),
        ),
        "wifi": make_source_state(
            "waiting",
            "wifi",
            segments,
            message=f"Model loaded. Listening on {args.wifi_audio_url}.",
            stages=asr_stages("wifi", "waiting", "waiting", "waiting", "Waiting to capture Wi-Fi camera microphone audio."),
        ),
    }

    publish_state(
        args,
        "running",
        "server",
        model_name,
        device,
        segments,
        message=f"Model loaded. Listening on {server_audio_format}:{server_audio_source}.",
        source_states=source_states,
    )

    utterance_buffers: dict[str, dict] = {}
    source_preroll: dict[str, list[dict]] = {"server": [], "browser": [], "wifi": []}

    def trim_preroll(source: str) -> None:
        keep_seconds = max(0.0, float(args.utterance_preroll_seconds))
        chunks = source_preroll.setdefault(source, [])
        total = 0.0
        kept = []
        for item in reversed(chunks):
            duration = float(item.get("duration") or 0)
            if total + duration > keep_seconds and kept:
                break
            kept.append(item)
            total += duration
        source_preroll[source] = list(reversed(kept))

    def remember_preroll(source: str, wav_path: Path, duration: float, captured_at: float, audio_level: dict) -> None:
        if args.utterance_preroll_seconds <= 0:
            return
        source_preroll.setdefault(source, []).append(
            {
                "bytes": wav_path.read_bytes(),
                "duration": duration,
                "captured_at": captured_at,
                "audio_level": audio_level,
            }
        )
        trim_preroll(source)

    def pop_preroll(source: str, now: float) -> list[dict]:
        keep_seconds = max(0.0, float(args.utterance_preroll_seconds))
        if keep_seconds <= 0:
            source_preroll[source] = []
            return []
        fresh = [
            item
            for item in source_preroll.get(source, [])
            if now - float(item.get("captured_at") or 0) <= keep_seconds + max(0.25, float(args.chunk_seconds))
        ]
        source_preroll[source] = []
        return fresh

    def entry_interim_text(entry: dict) -> str:
        return " ".join(str(item) for item in entry.get("chunk_texts", []) if str(item).strip()).strip()

    def entry_interim_updated_at(entry: dict) -> float:
        return float(entry.get("last_text_at") or entry.get("last_speech_at") or entry.get("last_at") or time.time())

    def required_silence_seconds(entry: dict) -> float:
        base = max(float(args.utterance_gap_seconds), float(args.utterance_final_silence_seconds))
        text = str(entry.get("interim_text") or entry_interim_text(entry))
        if looks_like_unfinished_phrase(text, args.utterance_min_final_words):
            base += max(0.0, float(args.utterance_short_phrase_extra_silence))
        return base

    def should_finalize_entry(entry: dict, now: float) -> tuple[bool, str]:
        duration = float(entry.get("duration") or 0)
        if duration >= float(args.utterance_max_seconds):
            return True, "max utterance duration"
        # Once the speech-understanding model has emitted text for an utterance, use the last
        # recognized-text timestamp as the finalization clock. Browser mic noise
        # can otherwise keep resetting last_speech_at and strand live speech-understanding text
        # forever without publishing a final transcript segment.
        has_text = bool(entry.get("interim_text") or entry_interim_text(entry))
        finalization_at = float(
            (entry.get("last_text_at") if has_text else 0)
            or entry.get("last_speech_at")
            or entry.get("last_at")
            or 0
        )
        silence_seconds = max(0.0, now - finalization_at)
        required = required_silence_seconds(entry)
        if silence_seconds >= required:
            return True, f"speech idle {silence_seconds:.1f}s"
        return False, f"waiting for final silence {silence_seconds:.1f}/{required:.1f}s"

    def apply_audio_clear(marker: str, cleared_at: float) -> None:
        nonlocal last_audio_clear_marker, processed_browser_chunks
        cutoff = cleared_at or time.time()
        segments.clear()
        utterance_buffers.clear()
        source_preroll["server"] = []
        source_preroll["browser"] = []
        source_preroll["wifi"] = []
        processed_browser_chunks = existing_browser_chunks(browser_audio_dir, max_mtime=cutoff)
        source_states["server"] = make_source_state(
            "waiting",
            "server",
            segments,
            message="Incoming audio buffers cleared. Waiting for the next server capture.",
            stages=asr_stages("server", "waiting", "waiting", "waiting", "Audio buffers were cleared."),
        )
        source_states["browser"] = make_source_state(
            "waiting",
            "browser",
            segments,
            message="Incoming browser audio buffers cleared. Waiting for new browser chunks.",
            stages=asr_stages("browser", "waiting", "waiting", "waiting", "Browser audio queue was cleared."),
        )
        source_states["wifi"] = make_source_state(
            "waiting",
            "wifi",
            segments,
            message="Incoming audio buffers cleared. Waiting for the next Wi-Fi camera capture.",
            stages=asr_stages("wifi", "waiting", "waiting", "waiting", "Audio buffers were cleared."),
        )
        publish_state(
            args,
            "waiting",
            "browser",
            model_name,
            device,
            segments,
            message="Incoming audio buffers cleared.",
            stages=asr_stages("browser", "waiting", "waiting", "waiting", "Browser audio queue was cleared."),
            source_states=source_states,
        )
        last_audio_clear_marker = marker

    def audio_clear_requested() -> tuple[str, float] | None:
        marker, cleared_at = audio_clear_state(args.audio_buffer_control_json)
        if marker and marker != last_audio_clear_marker:
            return marker, cleared_at
        return None

    def finalize_buffered_utterance(source: str, entry: dict, tmp_path: Path, reason: str) -> bool:
        chunks = list(entry.get("chunks") or [])
        if not chunks:
            utterance_buffers.pop(source, None)
            return False
        context_paths = []
        for index, chunk_bytes in enumerate(chunks):
            chunk_path = tmp_path / f"final_utterance_{source}_{index}.wav"
            chunk_path.write_bytes(chunk_bytes)
            context_paths.append(chunk_path)
        utterance_wav_path = tmp_path / f"final_utterance_{source}.wav"
        combine_wavs(context_paths, utterance_wav_path)
        final_model_text = transcribe_wav(model, utterance_wav_path)
        interim_text = best_transcript_candidate(entry.get("interim_text"), entry_interim_text(entry))
        final_text = best_transcript_candidate(final_model_text, interim_text)
        utterance_buffers.pop(source, None)
        if not final_text:
            publish_state(
                args,
                "running",
                source,
                model_name,
                device,
                segments,
                message=f"Buffered utterance ended with no final speech ({reason}).",
                stages=asr_stages(source, "complete", "complete", "waiting", "Buffered utterance ended."),
                source_states=source_states,
                interim_text="",
            )
            return False
        finalization_source = "final_utterance_audio" if compact_text(final_model_text) == compact_text(final_text) else "live_interim_fallback"
        append_segment(
            segments,
            source,
            final_text,
            args.max_segments,
            str(entry.get("id") or f"{source}-{time.time_ns()}"),
            {
                "asr_context": finalization_source,
                "asr_finalization_source": finalization_source,
                "utterance_final": True,
                "utterance_chunk_count": len(chunks),
                "utterance_seconds": round(float(entry.get("duration") or 0), 2),
                "utterance_preroll_seconds": round(float(entry.get("preroll_seconds") or 0), 2),
                "final_silence_required_seconds": round(required_silence_seconds(entry), 2),
                "finalize_reason": reason,
            },
        )
        publish_state(
            args,
            "running",
            source,
            model_name,
            device,
            segments,
            message=(
                f"Final speech-understanding utterance published ({reason})."
                if finalization_source == "final_utterance_audio"
                else f"Live speech-understanding candidate published after final pass returned less text ({reason})."
            ),
            stages=asr_stages(source, "complete", "complete", "complete", "Final rolling utterance audio processed."),
            source_states=source_states,
            interim_text="",
        )
        return True

    while True:
        if selected_pipeline_mode(args.pipeline_mode_json) != "classic":
            utterance_buffers.clear()
            segments = transcript_file_segments(args.transcript_json, args.max_segments)
            source_states["server"] = make_source_state(
                "paused",
                "server",
                segments,
                message="Classic Speech Understanding is disabled while the Nemotron 3 VoiceChat pipeline is selected.",
                stages=asr_stages("server", "waiting", "waiting", "waiting", "VoiceChat pipeline selected."),
                interim_text="",
            )
            source_states["browser"] = make_source_state(
                "paused",
                "browser",
                segments,
                message="Classic Speech Understanding is disabled while the Nemotron 3 VoiceChat pipeline is selected.",
                stages=asr_stages("browser", "waiting", "waiting", "waiting", "VoiceChat pipeline selected."),
                interim_text="",
            )
            publish_state(
                args,
                "paused",
                "browser",
                model_name,
                device,
                segments,
                message="Classic Speech Understanding paused; Nemotron 3 VoiceChat is the selected speech pipeline.",
                source_states=source_states,
                interim_text="",
            )
            if args.once:
                return 0
            time.sleep(max(0.5, float(args.loop_delay)))
            continue

        clear_request = audio_clear_requested()
        if clear_request:
            apply_audio_clear(*clear_request)
            if args.once:
                return 0
            time.sleep(args.loop_delay)
            continue

        did_work = False
        for source, source_warning in requested_sources(args):
            browser_chunk: Path | None = None
            paused, pause_reason, _voice_state = voice_response_active(args, source)
            if paused:
                if source == "browser":
                    processed_browser_chunks.update(existing_browser_chunks(browser_audio_dir))
                publish_state(
                    args,
                    "paused",
                    source,
                    model_name,
                    device,
                    segments,
                    message=f"Microphone capture paused for {source} while its speech response is processing: {pause_reason}",
                    stages=asr_stages(
                        source,
                        "waiting",
                        "waiting",
                        "waiting",
                        f"{source} microphone capture paused while this source's voice response pipeline is active.",
                    ),
                    source_states=source_states,
                )
                did_work = True
                continue
            if (
                args.source_mode == "all"
                and source == "server"
                and next_browser_chunk(
                    browser_audio_dir,
                    processed_browser_chunks,
                    args.browser_chunk_min_age,
                    args.browser_chunk_max_age,
                ) is not None
            ):
                publish_state(
                    args,
                    "waiting",
                    source,
                    model_name,
                    device,
                    segments,
                    message="Browser microphone audio is queued; prioritizing browser speech understanding before server capture.",
                    stages=asr_stages(source, "waiting", "waiting", "waiting", "Browser speech-understanding queue has priority."),
                    source_states=source_states,
                )
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="nemotron-asr-") as tmp:
                    wav_path = Path(tmp) / "chunk.wav"
                    if source == "browser":
                        browser_chunk = next_browser_chunk(
                            browser_audio_dir,
                            processed_browser_chunks,
                            args.browser_chunk_min_age,
                            args.browser_chunk_max_age,
                        )
                        if browser_chunk is None:
                            entry = utterance_buffers.get(source)
                            if args.utterance_audio_context and entry:
                                finalize, reason = should_finalize_entry(entry, time.time())
                                if finalize:
                                    finalize_buffered_utterance(source, entry, Path(tmp), reason)
                                    did_work = True
                                    continue
                                publish_state(
                                    args,
                                    "running",
                                    source,
                                    model_name,
                                    device,
                                    segments,
                                    message=f"Active utterance is being held open ({reason}).",
                                    stages=asr_stages(source, "waiting", "complete", "waiting", "Waiting for a natural utterance boundary."),
                                    source_states=source_states,
                                    interim_text=str(entry.get("interim_text") or entry_interim_text(entry)),
                                    interim_updated_at=entry_interim_updated_at(entry),
                                )
                                continue
                            publish_state(
                                args,
                                "waiting",
                                source,
                                model_name,
                                device,
                                segments,
                                message=source_warning or "Waiting for browser microphone audio chunks.",
                                stages=asr_stages(source, "waiting", "waiting", "waiting", "No browser audio chunk is ready yet."),
                                source_states=source_states,
                            )
                            continue
                        processed_browser_chunks.add(browser_chunk)
                        if len(processed_browser_chunks) > 200:
                            processed_browser_chunks = set(sorted(processed_browser_chunks, key=lambda path: path.name)[-100:])
                        publish_state(
                            args,
                            "running",
                            source,
                            model_name,
                            device,
                            segments,
                            message=f"Converting browser audio chunk {browser_chunk.name}.",
                            stages=asr_stages(source, "active", "waiting", "waiting", "Converting browser microphone upload."),
                            source_states=source_states,
                        )
                        convert_browser_audio(browser_chunk, wav_path)
                    elif source == "wifi":
                        publish_state(
                            args,
                            "running",
                            source,
                            model_name,
                            device,
                            segments,
                            message=f"Capturing {args.chunk_seconds:g}s from Wi-Fi camera microphone.",
                            stages=asr_stages(source, "active", "waiting", "waiting", "Capturing Wi-Fi camera microphone audio."),
                            source_states=source_states,
                        )
                        capture_wifi_wav(args, wav_path)
                    else:
                        publish_state(
                            args,
                            "running",
                            source,
                            model_name,
                            device,
                            segments,
                            message=f"Capturing {args.chunk_seconds:g}s from {server_audio_format}:{server_audio_source}.",
                            stages=asr_stages(source, "active", "waiting", "waiting", "Capturing server microphone audio."),
                            source_states=source_states,
                        )
                        capture_server_wav(args, wav_path, server_audio_format, server_audio_source)
                    audio_level = audio_level_for_wav(wav_path)

                    clear_request = audio_clear_requested()
                    if clear_request:
                        apply_audio_clear(*clear_request)
                        did_work = True
                        continue

                    publish_state(
                        args,
                        "running",
                        source,
                        model_name,
                        device,
                        segments,
                        message="Running Nemotron speech understanding on the latest audio chunk.",
                        stages=asr_stages(source, "complete", "active", "waiting"),
                        source_states=source_states,
                        audio_level=audio_level,
                    )
                    text = transcribe_wav(model, wav_path)

                    clear_request = audio_clear_requested()
                    if clear_request:
                        apply_audio_clear(*clear_request)
                        did_work = True
                        continue

                    if args.utterance_audio_context:
                        now = time.time()
                        approximate_duration = wav_duration_seconds(wav_path, max(0.25, float(args.chunk_seconds)))
                        asr_settings = read_asr_settings(args)
                        has_speech_signal = audio_has_speech_signal(
                            audio_level,
                            asr_settings["speech_rms_threshold"],
                            asr_settings["speech_peak_threshold"],
                        )
                        entry = utterance_buffers.get(source)
                        if text:
                            if entry:
                                finalize, reason = should_finalize_entry(entry, now)
                            else:
                                finalize, reason = False, ""
                            if entry and finalize:
                                finalize_buffered_utterance(source, entry, Path(tmp), f"{reason} before new speech")
                                entry = None
                            if entry is None:
                                preroll = pop_preroll(source, now)
                                entry = {
                                    "id": f"{source}-{time.time_ns()}",
                                    "chunks": [item["bytes"] for item in preroll],
                                    "chunk_texts": [],
                                    "duration": sum(float(item.get("duration") or 0) for item in preroll),
                                    "preroll_seconds": sum(float(item.get("duration") or 0) for item in preroll),
                                    "last_at": now,
                                    "last_speech_at": now,
                                    "last_text_at": now,
                                    "trailing_silence_seconds": 0.0,
                                }
                                utterance_buffers[source] = entry
                            entry["chunks"].append(wav_path.read_bytes())
                            entry.setdefault("chunk_texts", []).append(text)
                            entry["duration"] = float(entry.get("duration") or 0) + approximate_duration
                            entry["last_at"] = now
                            entry["last_speech_at"] = now
                            entry["last_text_at"] = now
                            entry["trailing_silence_seconds"] = 0.0

                            interim_text = best_transcript_candidate(entry.get("interim_text"), entry_interim_text(entry), text)
                            if len(entry["chunks"]) > 1:
                                publish_state(
                                    args,
                                    "running",
                                    source,
                                    model_name,
                                    device,
                                    segments,
                                    message="Refining live speech understanding with the rolling utterance audio window.",
                                    stages=asr_stages(source, "complete", "active", "waiting"),
                                    source_states=source_states,
                                    audio_level=audio_level,
                                    interim_text=" ".join(str(item) for item in entry.get("chunk_texts", []) if item),
                                    interim_updated_at=now,
                                )
                                context_paths = []
                                for index, chunk_bytes in enumerate(entry["chunks"]):
                                    chunk_path = Path(tmp) / f"utterance_{index}.wav"
                                    chunk_path.write_bytes(chunk_bytes)
                                    context_paths.append(chunk_path)
                                utterance_wav_path = Path(tmp) / "utterance_context.wav"
                                combine_wavs(context_paths, utterance_wav_path)
                                merged_text = transcribe_wav(model, utterance_wav_path)

                                clear_request = audio_clear_requested()
                                if clear_request:
                                    apply_audio_clear(*clear_request)
                                    did_work = True
                                    continue

                                if merged_text:
                                    interim_text = best_transcript_candidate(merged_text, interim_text)
                            entry["interim_text"] = interim_text

                            if args.publish_interim_transcripts:
                                append_segment(
                                    segments,
                                    source,
                                    interim_text,
                                    args.max_segments,
                                    str(entry["id"]),
                                    {
                                        "asr_context": "interim_utterance_audio",
                                        "utterance_final": False,
                                        "utterance_chunk_count": len(entry["chunks"]),
                                        "utterance_seconds": round(float(entry.get("duration") or 0), 2),
                                    },
                                )
                            publish_state(
                                args,
                                "running",
                                source,
                                model_name,
                                device,
                                segments,
                                message="Live speech-understanding interim text is available; waiting to finalize utterance.",
                                stages=asr_stages(source, "complete", "complete", "waiting", "Audio chunk processed as interim speech."),
                                source_states=source_states,
                                audio_level=audio_level,
                                interim_text=interim_text,
                                interim_updated_at=now,
                            )
                            if float(entry.get("duration") or 0) >= args.utterance_max_seconds:
                                finalize_buffered_utterance(source, entry, Path(tmp), "max utterance duration")
                        elif entry:
                            if has_speech_signal:
                                entry["last_audio_signal_at"] = now
                                if not (entry.get("interim_text") or entry_interim_text(entry)):
                                    entry["last_speech_at"] = now
                                entry["trailing_silence_seconds"] = 0.0
                            else:
                                entry["trailing_silence_seconds"] = float(entry.get("trailing_silence_seconds") or 0) + approximate_duration

                            should_keep_audio = has_speech_signal or float(entry.get("trailing_silence_seconds") or 0) <= args.utterance_max_trailing_silence_seconds
                            if should_keep_audio:
                                entry["chunks"].append(wav_path.read_bytes())
                                entry["duration"] = float(entry.get("duration") or 0) + approximate_duration
                                entry["last_at"] = now

                            finalize, reason = should_finalize_entry(entry, now)
                            if finalize:
                                finalize_buffered_utterance(source, entry, Path(tmp), reason)
                            else:
                                publish_state(
                                    args,
                                    "running",
                                    source,
                                    model_name,
                                    device,
                                    segments,
                                    message=f"Keeping the active utterance open ({reason}).",
                                    stages=asr_stages(source, "complete", "complete", "waiting", "Audio chunk added to active utterance buffer."),
                                    source_states=source_states,
                                    audio_level=audio_level,
                                    interim_text=str(entry.get("interim_text") or entry_interim_text(entry)),
                                    interim_updated_at=entry_interim_updated_at(entry),
                                )
                                if float(entry.get("duration") or 0) >= args.utterance_max_seconds:
                                    finalize_buffered_utterance(source, entry, Path(tmp), "max utterance duration")
                        else:
                            remember_preroll(source, wav_path, approximate_duration, now, audio_level)
                            publish_state(
                                args,
                                "running",
                                source,
                                model_name,
                                device,
                                segments,
                                message=source_warning or "Listening; no speech detected in the latest chunk.",
                                stages=asr_stages(source, "complete", "complete", "waiting", "Audio chunk processed."),
                                source_states=source_states,
                                audio_level=audio_level,
                            )
                    else:
                        append_segment(segments, source, text, args.max_segments)
                        publish_state(
                            args,
                            "running",
                            source,
                            model_name,
                            device,
                            segments,
                            message=source_warning or ("Speech detected." if text else "Listening; no speech detected in the latest chunk."),
                            stages=asr_stages(
                                source,
                                "complete",
                                "complete",
                                "complete",
                                "Audio chunk processed.",
                            ),
                            source_states=source_states,
                            audio_level=audio_level,
                        )
                    did_work = True
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                if source == "browser":
                    chunk_name = browser_chunk.name if browser_chunk else "browser audio chunk"
                    publish_state(
                        args,
                        "waiting",
                        source,
                        model_name,
                        device,
                        segments,
                        message=f"Skipped unreadable {chunk_name}: {stderr or exc}",
                        stages=asr_stages(source, "error", "waiting", "waiting", stderr or str(exc)),
                        source_states=source_states,
                    )
                else:
                    publish_state(
                        args,
                        "error",
                        source,
                        model_name,
                        device,
                        segments,
                        error=stderr or str(exc),
                        stages=asr_stages(source, "error", "waiting", "waiting", stderr or str(exc)),
                        source_states=source_states,
                    )
            except Exception as exc:
                publish_state(
                    args,
                    "error",
                    source,
                    model_name,
                    device,
                    segments,
                    error=str(exc),
                    stages=asr_stages(source, "error", "waiting", "waiting", str(exc)),
                    source_states=source_states,
                )

        if args.once:
            return 0
        if not did_work:
            time.sleep(args.loop_delay)


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    raise SystemExit(main())

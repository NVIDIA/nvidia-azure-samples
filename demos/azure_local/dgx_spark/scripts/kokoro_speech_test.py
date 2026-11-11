#!/usr/bin/env python3
"""Generate random questions with Kokoro and play them on the JBL speaker."""

from __future__ import annotations

import argparse
import fcntl
import json
import random
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


JBL_MAC = "E8:D0:3C:4C:A3:7E"
JBL_NAME = "JBL Flip 5"
JBL_SINK = "bluez_output.E8_D0_3C_4C_A3_7E.1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERVICE_QUEUE = PROJECT_ROOT / "webcam-deepstream-nemotron-input.json"

# The resident service keeps one American-English pipeline loaded and can swap
# these voice packs without loading another model.
VOICES: dict[str, str] = {
    "af_alloy": "a",
    "af_aoede": "a",
    "af_bella": "a",
    "af_heart": "a",
    "af_jessica": "a",
    "af_kore": "a",
    "af_nicole": "a",
    "af_nova": "a",
    "af_river": "a",
    "af_sarah": "a",
    "af_sky": "a",
    "am_adam": "a",
    "am_echo": "a",
    "am_eric": "a",
    "am_fenrir": "a",
    "am_liam": "a",
    "am_michael": "a",
    "am_onyx": "a",
    "am_puck": "a",
}


def _choice_question(rng: random.Random) -> str:
    choices = [
        ("Which would you choose for a weekend adventure: {a} or {b}?", [
            ("a mountain trail", "a quiet beach"),
            ("a new city", "a remote cabin"),
            ("kayaking", "cycling"),
        ]),
        ("Would you rather learn {a}, or become excellent at {b}?", [
            ("to play the piano", "speaking another language"),
            ("astronomy", "photography"),
            ("creative writing", "woodworking"),
        ]),
    ]
    template, pairs = rng.choice(choices)
    a, b = rng.choice(pairs)
    return template.format(a=a, b=b)


def _imagination_question(rng: random.Random) -> str:
    openings = [
        "If you could visit any point in history",
        "If you could instantly master one new skill",
        "If you had an extra hour every day",
        "If you could design the perfect workspace",
        "If you could ask a scientist one question",
    ]
    endings = [
        "what would you choose, and why?",
        "what would you do first?",
        "how would your daily life change?",
        "what detail would matter most to you?",
    ]
    return f"{rng.choice(openings)}, {rng.choice(endings)}"


def _reflection_question(rng: random.Random) -> str:
    topics = [
        "a small habit that improves your day",
        "the most useful thing you learned recently",
        "a place that always helps you think clearly",
        "a technology you are excited to see improve",
        "a project you would enjoy starting this month",
        "a book, film, or conversation that changed your perspective",
    ]
    prompts = [
        "What is {topic}?",
        "How would you describe {topic}?",
        "Why is {topic} meaningful to you?",
    ]
    return rng.choice(prompts).format(topic=rng.choice(topics))


QUESTION_GENERATORS: tuple[Callable[[random.Random], str], ...] = (
    _choice_question,
    _imagination_question,
    _reflection_question,
)


def generate_question(rng: random.Random, previous: set[str] | None = None) -> str:
    """Generate a conversational question, avoiding recent duplicates."""
    seen = previous or set()
    for _ in range(20):
        question = rng.choice(QUESTION_GENERATORS)(rng)
        if question not in seen:
            return question
    return rng.choice(QUESTION_GENERATORS)(rng)


def select_voice(rng: random.Random, requested: str, pool: list[str]) -> str:
    if requested != "random":
        if requested not in VOICES:
            raise ValueError(f"Unknown voice {requested!r}; use --list-voices to see valid names")
        return requested
    return rng.choice(pool)


def run_command(command: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required command is missing: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out: {' '.join(command)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "unknown error").strip()
        raise RuntimeError(f"Command failed ({' '.join(command)}): {detail}") from exc


def bluetooth_connected(mac: str) -> bool:
    result = run_command(["bluetoothctl", "info", mac])
    return any(line.strip() == "Connected: yes" for line in result.stdout.splitlines())


def ensure_bluetooth_connected(mac: str, name: str) -> None:
    if bluetooth_connected(mac):
        return
    print(f"Connecting to {name} ({mac}) ...", flush=True)
    run_command(["bluetoothctl", "connect", mac], timeout=30)
    if not bluetooth_connected(mac):
        raise RuntimeError(f"{name} did not report a connected state after bluetoothctl connect")


def pulse_sinks() -> list[str]:
    result = run_command(["pactl", "list", "short", "sinks"])
    return [fields[1] for line in result.stdout.splitlines() if len(fields := line.split()) >= 2]


def resolve_jbl_sink(requested: str, mac: str) -> str:
    sinks = pulse_sinks()
    if requested in sinks:
        return requested
    mac_token = mac.replace(":", "_").lower()
    matches = [sink for sink in sinks if mac_token in sink.lower()]
    if len(matches) == 1:
        return matches[0]
    available = ", ".join(sinks) or "none"
    raise RuntimeError(
        f"JBL PulseAudio sink {requested!r} is unavailable (available sinks: {available})"
    )


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def enqueue_service_request(queue_path: Path, request: dict) -> None:
    lock_path = queue_path.with_suffix(queue_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        data = read_json(queue_path)
        pending = data.get("pending") if isinstance(data.get("pending"), list) else []
        pending.append(request)
        payload = {
            **data,
            "status": "pending",
            "updated_at": time.time(),
            "latest_request_id": request["id"],
            "pending": pending,
        }
        tmp_path = queue_path.with_name(f".{queue_path.name}.{request['id']}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(queue_path)
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def wait_for_service_result(queue_path: Path, request_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = read_json(queue_path)
        completed = data.get("recently_completed_requests")
        completed = completed.get("server") if isinstance(completed, dict) else None
        if isinstance(completed, dict) and completed.get("id") == request_id:
            result = completed.get("service_result")
            if isinstance(result, dict):
                return result
            raise RuntimeError("resident TTS service completed without a result payload")
        time.sleep(0.05)
    raise RuntimeError(f"resident TTS service timed out after {timeout:g} seconds")


def parse_voice_pool(value: str) -> list[str]:
    voices = [item.strip() for item in value.split(",") if item.strip()]
    unknown = sorted(set(voices) - VOICES.keys())
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown voice(s): {', '.join(unknown)}")
    if not voices:
        raise argparse.ArgumentTypeError("voice pool cannot be empty")
    return voices


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=1, help="number of questions to synthesize")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between questions")
    parser.add_argument("--seed", type=int, help="seed for a repeatable question/voice sequence")
    parser.add_argument("--question", help="speak this text instead of generating a random question")
    parser.add_argument("--voice", default="random", help="Kokoro voice or 'random'")
    parser.add_argument(
        "--voice-pool",
        type=parse_voice_pool,
        default=list(VOICES),
        help="comma-separated pool used by --voice random",
    )
    parser.add_argument("--min-speed", type=float, default=0.90)
    parser.add_argument("--max-speed", type=float, default=1.12)
    parser.add_argument("--speaker-mac", default=JBL_MAC)
    parser.add_argument("--speaker-name", default=JBL_NAME)
    parser.add_argument("--sink", default=JBL_SINK)
    parser.add_argument("--output-dir", type=Path, help="keep WAV files and a results.jsonl log here")
    parser.add_argument("--no-playback", action="store_true", help="synthesize without playing audio")
    parser.add_argument("--skip-bluetooth-check", action="store_true")
    parser.add_argument("--service-queue", type=Path, default=DEFAULT_SERVICE_QUEUE)
    parser.add_argument("--service-timeout", type=float, default=120.0)
    parser.add_argument("--dry-run", action="store_true", help="show randomized trials without loading Kokoro")
    parser.add_argument("--list-voices", action="store_true")
    args = parser.parse_args(argv)
    if args.count < 1:
        parser.error("--count must be at least 1")
    if args.interval < 0:
        parser.error("--interval cannot be negative")
    if not 0.5 <= args.min_speed <= 2 or not 0.5 <= args.max_speed <= 2:
        parser.error("speech speeds must be between 0.5 and 2.0")
    if args.min_speed > args.max_speed:
        parser.error("--min-speed cannot exceed --max-speed")
    return args


def run(args: argparse.Namespace) -> int:
    if args.list_voices:
        for voice in VOICES:
            print(f"{voice}\tAmerican English")
        return 0

    rng = random.Random(args.seed)
    trials = []
    seen_questions: set[str] = set()
    for index in range(args.count):
        question = args.question or generate_question(rng, seen_questions)
        seen_questions.add(question)
        trials.append(
            {
                "index": index + 1,
                "question": question,
                "voice": select_voice(rng, args.voice, args.voice_pool),
                "speed": round(rng.uniform(args.min_speed, args.max_speed), 3),
            }
        )

    if args.dry_run:
        for trial in trials:
            print(json.dumps(trial, ensure_ascii=False))
        return 0

    sink = ""
    if not args.no_playback:
        if not args.skip_bluetooth_check:
            ensure_bluetooth_connected(args.speaker_mac, args.speaker_name)
        sink = resolve_jbl_sink(args.sink, args.speaker_mac)
    queue_path = args.service_queue.expanduser().resolve()
    if not queue_path.exists():
        raise RuntimeError(f"resident TTS service queue is unavailable: {queue_path}")
    if args.output_dir:
        output_dir = args.output_dir.expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = None

    print(f"Kokoro backend: resident service; output: {sink or 'file only'}", flush=True)
    for index, trial in enumerate(trials):
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            request_id = f"speech-test-{time.time_ns()}-{trial['index']}"
            print(
                f"[{trial['index']}/{len(trials)}] {trial['voice']} at {trial['speed']:.3f}x: "
                f"{trial['question']}",
                flush=True,
            )
            started = time.monotonic()
            enqueue_service_request(
                queue_path,
                {
                    "id": request_id,
                    "source": "server",
                    "kind": "speech_test",
                    "trigger": "speech_test_harness",
                    "text": trial["question"],
                    "tts_voice": trial["voice"],
                    "tts_speed": trial["speed"],
                    "playback": not args.no_playback,
                    "created_at": time.time(),
                    "updated_at": time.time(),
                },
            )
            service_result = wait_for_service_result(queue_path, request_id, args.service_timeout)
            if not service_result.get("ok"):
                raise RuntimeError(str(service_result.get("error") or "resident TTS service failed"))
            elapsed_seconds = time.monotonic() - started
            service_audio_path = Path(str(service_result.get("audio_path") or ""))
            kept_audio_path = ""
            if output_dir and service_audio_path.is_file():
                target = output_dir / f"trial-{trial['index']:03d}-{trial['voice']}-{timestamp}.wav"
                shutil.copy2(service_audio_path, target)
                kept_audio_path = str(target)
            result = {
                **trial,
                "timestamp": timestamp,
                **service_result,
                "audio_path": kept_audio_path,
                "service_audio_path": str(service_audio_path),
                "elapsed_seconds": round(elapsed_seconds, 3),
            }
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.output_dir:
                with (output_dir / "results.jsonl").open("a", encoding="utf-8") as log_file:
                    log_file.write(json.dumps(result, ensure_ascii=False) + "\n")
            if index + 1 < len(trials) and args.interval:
                time.sleep(args.interval)
    return 0


def main() -> int:
    try:
        return run(parse_args())
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

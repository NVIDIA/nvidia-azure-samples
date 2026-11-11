#!/usr/bin/env python3
"""Respond to detected speech using visual context and local Nemotron/Magpie models."""

from __future__ import annotations

import argparse
import base64
import contextlib
import difflib
import fcntl
import html
import json
import math
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from llm_token_usage import extract_response_usage, record_response_usage
from tool_planner_config import (
    DEFAULT_TOOL_PLANNER_TEMPLATE,
    read_tool_planner_template,
    render_tool_planner_template,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PREFERRED_MODELS = (
    "nemotron3-voice-fast:latest",
    "nemotron3-voice-fast",
    "nemotron3:33b",
    "nemotron-spark:latest",
    "nemotron-3-super:120b",
)
DEFAULT_TTS_MODEL_PATH = (
    Path.home()
    / ".cache/huggingface/hub/models--nvidia--magpie_tts_multilingual_357m"
    / "snapshots/311be0390a5f4350916d34f1e1090d154880b14d"
    / "magpie_tts_multilingual_357m.nemo"
)
DEFAULT_TTS_CODEC_PATH = (
    Path.home()
    / ".cache/huggingface/hub/models--nvidia--nemo-nano-codec-22khz-1.89kbps-21.5fps"
    / "snapshots/3c482a402a3c4cf33690a2c0f0a7d41afea6bd6a"
    / "nemo-nano-codec-22khz-1.89kbps-21.5fps.nemo"
)
DEFAULT_NEMO_REPO = Path.home() / "jupyterlab/NeMo"
DEFAULT_SERVER_SINK = "bluez_output.E8_D0_3C_4C_A3_7E.1"
DEFAULT_WAKE_PHRASES = "nemotron,hey nemotron,assistant,hey assistant,monitor assistant"
DEFAULT_DIALOG_PROMPT_PHRASES = (
    "what do you,what is,what's,whats,where is,where's,wheres,can you,could you,would you,"
    "please,tell me,show me,describe,summarize,explain,look at,check,is there,are there,do you see"
)
VISUAL_CONTEXT_MARKERS = (
    "what do you see",
    "what can you see",
    "what are you seeing",
    "tell me what you see",
    "tell me what you can see",
    "show me what you see",
    "show me what you can see",
    "describe what you see",
    "describe the scene",
    "look around",
    "look at the camera",
    "look at the feed",
    "look at the video",
    "current scene",
    "current view",
    "camera feed",
    "video feed",
    "visual context",
    "environment context",
    "current environment",
    "what is around",
    "what's around",
    "whats around",
    "what is happening",
    "what's happening",
    "whats happening",
    "what is going on",
    "what's going on",
    "whats going on",
    "what does it look like",
)
STAGE_TIMINGS: dict[str, dict] = {}
ACTIVE_NEMOTRON_MODEL = "unresolved"
ACTIVE_TOOL_PLANNER_MODEL = "unresolved"
ACTIVE_TTS_MODEL_LABEL = DEFAULT_TTS_MODEL_PATH.name
ACTIVE_TTS_CODEC_LABEL = DEFAULT_TTS_CODEC_PATH.name
ACTIVE_TTS_DETAILS: dict[str, object] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript-json", default=str(PROJECT_ROOT / "webcam-transcript.json"))
    parser.add_argument("--analysis-json", default=str(PROJECT_ROOT / "webcam-analysis.json"))
    parser.add_argument("--alert-json", default=str(PROJECT_ROOT / "webcam-alert.json"))
    parser.add_argument("--database", default=str(PROJECT_ROOT / "webcam-analysis-history.sqlite3"))
    parser.add_argument("--server-agent-state-json", default=str(PROJECT_ROOT / "webcam-server-agent-state.json"))
    parser.add_argument("--browser-agent-state-json", default=str(PROJECT_ROOT / "webcam-browser-agent-state.json"))
    parser.add_argument("--wifi-agent-state-json", default=str(PROJECT_ROOT / "webcam-wifi-agent-state.json"))
    parser.add_argument("--bulb-agent-state-json", default=str(PROJECT_ROOT / "webcam-bulb-agent-state.json"))
    parser.add_argument("--server-database", default=str(PROJECT_ROOT / "webcam-server-environment-history.sqlite3"))
    parser.add_argument("--browser-database", default=str(PROJECT_ROOT / "webcam-browser-environment-history.sqlite3"))
    parser.add_argument("--wifi-database", default=str(PROJECT_ROOT / "webcam-wifi-environment-history.sqlite3"))
    parser.add_argument("--bulb-database", default=str(PROJECT_ROOT / "webcam-bulb-environment-history.sqlite3"))
    parser.add_argument("--voice-response-json", default=str(PROJECT_ROOT / "webcam-voice-response.json"))
    parser.add_argument("--voice-session-reset-json", default=str(PROJECT_ROOT / "webcam-voice-session-reset.json"))
    parser.add_argument("--voice-output-target-json", default=str(PROJECT_ROOT / "webcam-voice-output-target.json"))
    parser.add_argument("--pipeline-mode-json", default=str(PROJECT_ROOT / "webcam-speech-pipeline-mode.json"))
    parser.add_argument("--notification-stats-db", default=str(PROJECT_ROOT / "webcam-notification-stats.sqlite3"))
    parser.add_argument("--notification-final-response-threshold", type=float, default=5.0)
    parser.add_argument("--environment-wake-json", default=str(PROJECT_ROOT / "webcam-environment-wake.json"))
    parser.add_argument("--visual-cache-json", default=str(PROJECT_ROOT / "webcam-voice-visual-cache.json"))
    parser.add_argument("--audio-dir", default=str(PROJECT_ROOT / "webcam-voice-audio"))
    parser.add_argument("--screenshot-dir", default=str(PROJECT_ROOT / "webcam-voice-screenshots"))
    parser.add_argument("--snapshot-url", default="http://127.0.0.1:8090/snapshot.jpg")
    parser.add_argument("--server-snapshot-url", default="http://127.0.0.1:8090/server-snapshot.jpg")
    parser.add_argument("--browser-snapshot-url", default="http://127.0.0.1:8090/browser-snapshot.jpg")
    parser.add_argument("--wifi-snapshot-url", default="http://127.0.0.1:8090/wifi-snapshot.jpg")
    parser.add_argument("--bulb-snapshot-url", default="http://127.0.0.1:8090/bulb-snapshot.jpg")
    parser.add_argument("--cosmos-trigger-json", default=str(PROJECT_ROOT / "webcam-cosmos-trigger.json"))
    parser.add_argument("--cosmos-lock-file", default=str(PROJECT_ROOT / "webcam-cosmos-trigger.lock"))
    parser.add_argument("--force-visual-update", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--force-visual-update-timeout", type=float, default=60.0)
    parser.add_argument("--force-visual-update-max-age-seconds", type=float, default=20.0)
    parser.add_argument("--force-visual-update-poll-seconds", type=float, default=0.5)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default=None, help="Ollama Nemotron model name; auto-detects when omitted")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--history-limit", type=int, default=8)
    parser.add_argument("--voice-context-history-limit", type=int, default=2)
    parser.add_argument("--voice-visual-context-chars", type=int, default=520)
    parser.add_argument("--voice-alert-context-chars", type=int, default=220)
    parser.add_argument("--voice-history-item-chars", type=int, default=120)
    parser.add_argument("--nemotron-num-predict", type=int, default=192)
    parser.add_argument("--nemotron-num-ctx", type=int, default=2048)
    parser.add_argument("--nemotron-temperature", type=float, default=0.1)
    parser.add_argument("--nemotron-keep-alive", default="60m")
    parser.add_argument("--nemotron-warmup", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--enable-tools", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-tool-calls", type=int, default=3)
    parser.add_argument("--camera-clip-default-seconds", type=float, default=3.0)
    parser.add_argument("--camera-clip-min-seconds", type=float, default=1.0)
    parser.add_argument("--camera-clip-max-seconds", type=float, default=10.0)
    parser.add_argument("--camera-clip-max-bytes", type=int, default=12_000_000)
    parser.add_argument("--tool-timeout", type=float, default=8.0)
    parser.add_argument("--tool-result-chars", type=int, default=1800)
    parser.add_argument("--tool-planner-model", default="nemotron-mini:latest")
    parser.add_argument("--tool-planner-timeout", type=float, default=8.0)
    parser.add_argument("--tool-planner-num-predict", type=int, default=96)
    parser.add_argument("--tool-planner-num-ctx", type=int, default=768)
    parser.add_argument("--tool-planner-queue-json", default=str(PROJECT_ROOT / "webcam-tool-planner-queue.json"))
    parser.add_argument(
        "--tool-planner-template-json",
        default=str(PROJECT_ROOT / "webcam-tool-planner-template.json"),
    )
    parser.add_argument("--environment-tool-timeout", type=float, default=90.0)
    parser.add_argument("--environment-tool-poll-seconds", type=float, default=0.5)
    parser.add_argument("--camera-ptz-url", default="http://127.0.0.1:8090/wifi-ptz")
    parser.add_argument("--camera-ptz-default-source", choices=("wifi", "bulb"), default="wifi")
    parser.add_argument("--camera-ptz-degrees", type=float, default=5.0)
    parser.add_argument("--camera-ptz-pulse-ms", type=int, default=120)
    parser.add_argument("--camera-ptz-speed", type=int, default=1)
    parser.add_argument("--camera-ptz-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--focus-object-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--focus-object-command-json", default=str(PROJECT_ROOT / "webcam-focus-object-command.json"))
    parser.add_argument("--focus-object-state-json", default=str(PROJECT_ROOT / "webcam-focus-object-state.json"))
    parser.add_argument("--deepstream-settings-json", default=str(PROJECT_ROOT / "webcam-deepstream-settings.json"))
    parser.add_argument("--focus-object-wake-socket", default="/tmp/dgx-spark-focus-wake.sock")
    parser.add_argument("--focus-object-timeout", type=float, default=20.0)
    parser.add_argument("--focus-object-ack-timeout", type=float, default=2.0)
    parser.add_argument("--environment-scan-enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--environment-scan-width", type=int, default=11)
    parser.add_argument("--environment-scan-height", type=int, default=5)
    parser.add_argument("--environment-scan-step-ms", type=int, default=1000)
    parser.add_argument("--environment-scan-settle-ms", type=int, default=500)
    parser.add_argument("--environment-scan-cell-width", type=int, default=192)
    parser.add_argument("--environment-scan-cell-height", type=int, default=108)
    parser.add_argument("--environment-scan-timeout", type=float, default=180.0)
    parser.add_argument("--environment-scan-output-dir", default=str(PROJECT_ROOT / "webcam-environment-scans"))
    parser.add_argument("--environment-scan-max-image-bytes", type=int, default=1_250_000)
    parser.add_argument("--environment-scan-max-image-width", type=int, default=1600)
    parser.add_argument("--environment-scan-jpeg-quality", type=int, default=72)
    parser.add_argument("--enable-environment-context", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--enable-environment-tools", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--enable-system-tools", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument(
        "--camera-tools",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow physical PTZ, object-focus, and environment-scan tools for this lane.",
    )
    parser.add_argument("--shell-tool-mode", choices=("read_only", "dangerous"), default="read_only")
    parser.add_argument("--shell-timeout", type=float, default=8.0)
    parser.add_argument("--shell-output-chars", type=int, default=2400)
    parser.add_argument("--shell-cwd", default="/home/anslutsky/Dev/Cosmos-transfer")
    parser.add_argument("--response-candidates", type=int, default=1)
    parser.add_argument("--response-max-words", type=int, default=55)
    parser.add_argument("--response-timeout", type=float, default=18.0)
    parser.add_argument("--response-fallback-model", default="nemotron-mini:latest")
    parser.add_argument("--response-score-threshold", type=float, default=0.0)
    parser.add_argument(
        "--send-images-to-nemotron",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Attach screenshot bytes to the voice Nemotron call. Off by default because local text Nemotron models retry or slow down on images.",
    )
    parser.add_argument("--screenshot-count", type=int, default=0)
    parser.add_argument("--screenshot-delay", type=float, default=0.35)
    parser.add_argument("--screenshot-keep", type=int, default=30)
    parser.add_argument("--send-screenshots", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--tts-backend", choices=("magpie", "flite", "none"), default="magpie")
    parser.add_argument("--tts-model-path", default=str(DEFAULT_TTS_MODEL_PATH))
    parser.add_argument("--tts-codec-path", default=str(DEFAULT_TTS_CODEC_PATH))
    parser.add_argument("--nemo-repo", default=str(DEFAULT_NEMO_REPO))
    parser.add_argument("--tts-max-chars", type=int, default=0)
    parser.add_argument("--tts-max-words", type=int, default=0)
    parser.add_argument("--tts-playback-speed", type=float, default=1.0)
    parser.add_argument("--magpie-max-decoder-steps", type=int, default=420)
    parser.add_argument("--magpie-maskgit-steps", type=int, default=1)
    parser.add_argument("--magpie-temperature", type=float, default=0.55)
    parser.add_argument("--magpie-topk", type=int, default=32)
    parser.add_argument("--magpie-context-duration", type=float, default=2.3)
    parser.add_argument("--magpie-chunk-max-words", type=int, default=10)
    parser.add_argument("--magpie-chunk-max-chars", type=int, default=80)
    parser.add_argument("--magpie-chunk-pause-seconds", type=float, default=0.16)
    parser.add_argument("--magpie-direct-tts", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--magpie-use-cfg", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--magpie-voice", default="")
    parser.add_argument("--magpie-speaker-index", type=int, default=0)
    parser.add_argument("--magpie-warmup", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--context-audio-path", default=None)
    parser.add_argument("--context-text", default="This is the monitoring assistant voice.")
    parser.add_argument("--server-audio-sink", default=DEFAULT_SERVER_SINK)
    parser.add_argument(
        "--output-target-mode",
        choices=("auto", "file", "browser", "server", "wifi_camera", "bulb_camera"),
        default="auto",
        help="Route response audio by speech source, by target JSON file, or to a fixed output.",
    )
    parser.add_argument("--conversation-limit", type=int, default=10)
    parser.add_argument("--duplicate-window-seconds", type=float, default=20.0)
    parser.add_argument("--echo-window-seconds", type=float, default=45.0)
    parser.add_argument("--utterance-idle-seconds", type=float, default=1.1)
    parser.add_argument("--utterance-gap-seconds", type=float, default=2.0)
    parser.add_argument("--max-utterance-seconds", type=float, default=8.0)
    parser.add_argument("--min-utterance-words", type=int, default=2)
    parser.add_argument(
        "--dialog-activation-mode",
        choices=("wake", "prompt", "any"),
        default="any",
        help=(
            "When to run the human dialog agent. 'wake' requires a wake phrase, "
            "'prompt' also allows direct request/question phrases, and 'any' preserves the old behavior."
        ),
    )
    parser.add_argument(
        "--wake-phrases",
        default=DEFAULT_WAKE_PHRASES,
        help="Comma-separated phrases that activate the human dialog agent.",
    )
    parser.add_argument(
        "--dialog-prompt-phrases",
        default=DEFAULT_DIALOG_PROMPT_PHRASES,
        help="Comma-separated direct prompt phrases used when --dialog-activation-mode=prompt.",
    )
    parser.add_argument("--process-existing-transcript", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def publish(path: str | Path, payload: dict) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), **payload}
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(output_path)


def write_json(path: str | Path, payload: dict) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(output_path)


def read_json(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


TOOL_PLANNER_QUEUE_STALE_SECONDS = 120.0


def tool_planner_queue_path(args_or_path: argparse.Namespace | str | Path) -> Path:
    if isinstance(args_or_path, argparse.Namespace):
        return Path(getattr(args_or_path, "tool_planner_queue_json", "") or PROJECT_ROOT / "webcam-tool-planner-queue.json")
    return Path(args_or_path)


def normalize_tool_planner_queue(data: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    active = []
    for item in data.get("active", []) if isinstance(data.get("active"), list) else []:
        if not isinstance(item, dict):
            continue
        try:
            started_at = float(item.get("started_at") or 0)
        except (TypeError, ValueError):
            started_at = 0.0
        if started_at > 0 and now - started_at <= TOOL_PLANNER_QUEUE_STALE_SECONDS:
            active.append(item)
    normalized = {
        "updated_at": float(data.get("updated_at") or now),
        "queued": len(active),
        "active": active,
        "started_total": int(data.get("started_total") or 0),
        "completed_total": int(data.get("completed_total") or 0),
        "failed_total": int(data.get("failed_total") or 0),
    }
    if data.get("last_started_at"):
        normalized["last_started_at"] = data.get("last_started_at")
    if data.get("last_completed_at"):
        normalized["last_completed_at"] = data.get("last_completed_at")
    return normalized


def read_tool_planner_queue(args_or_path: argparse.Namespace | str | Path) -> dict:
    return normalize_tool_planner_queue(read_json(tool_planner_queue_path(args_or_path)))


def write_tool_planner_queue(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def update_tool_planner_queue(
    args_or_path: argparse.Namespace | str | Path,
    action: str,
    request_id: str,
    source: str = "",
    model: str = "",
    failed: bool = False,
) -> dict:
    path = tool_planner_queue_path(args_or_path)
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            data = normalize_tool_planner_queue(read_json(path), now)
            active = [item for item in data.get("active", []) if item.get("id") != request_id]
            if action == "start":
                active.append(
                    {
                        "id": request_id,
                        "source": str(source or ""),
                        "model": str(model or ""),
                        "started_at": now,
                    }
                )
                data["started_total"] = int(data.get("started_total") or 0) + 1
                data["last_started_at"] = now
            elif action == "finish":
                if failed:
                    data["failed_total"] = int(data.get("failed_total") or 0) + 1
                else:
                    data["completed_total"] = int(data.get("completed_total") or 0) + 1
                data["last_completed_at"] = now
            data["active"] = active
            data["queued"] = len(active)
            data["updated_at"] = now
            write_tool_planner_queue(path, data)
            return dict(data)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def start_tool_planner_queue(args: argparse.Namespace, source: str, model: str, request_id: str = "") -> tuple[str, dict]:
    request_id = request_id or f"planner-{os.getpid()}-{int(time.time() * 1000)}"
    return request_id, update_tool_planner_queue(args, "start", request_id, source=source, model=model)


def finish_tool_planner_queue(args: argparse.Namespace, request_id: str, failed: bool = False) -> dict:
    if not request_id:
        return read_tool_planner_queue(args)
    return update_tool_planner_queue(args, "finish", request_id, failed=failed)


def selected_pipeline_mode(path: str | Path) -> str:
    mode = str(read_json(path).get("mode") or "classic").lower()
    return mode if mode in {"classic", "voicechat"} else "classic"


def finite_timestamp(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def reset_marker(path: str | Path) -> str:
    data = read_json(path)
    return str(data.get("session_id") or data.get("updated_at") or "")


def waiting_state(args: argparse.Namespace, model: str, message: str, session_id: str = "") -> dict:
    state = {
        "status": "waiting",
        "model": model,
        "message": message,
        "output_target": read_output_target(args, ""),
        "conversation": [],
        "input_speech": "",
        "input_source": "",
        "input_updated_at": 0,
        "phase": "waiting",
        "operation": "Waiting for speech.",
        "response_text": "",
        "raw_response": "",
        "audio_id": "",
        "audio_path": "",
        "audio_url": "",
        "stages": voice_stages("waiting", "waiting", "waiting", "waiting", "Waiting for speech."),
    }
    if session_id:
        state["session_id"] = session_id
        state["cleared_at"] = time.time()
    return state


def normalize_text(text: str) -> str:
    return " ".join(text.lower().strip().split())


def short_text(text: str, limit: int = 1200) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def notification_id(text: str) -> str:
    return f"notify_{int(time.time() * 1000)}_{abs(hash(text)) % 1_000_000}"


def tool_notification_text(call: dict) -> str:
    name = str((call or {}).get("name") or "").strip()
    if name == "query_environment":
        return "Querying."
    if name == "web_search":
        return "Searching."
    if name == "fetch_url":
        return "Fetching."
    if name == "runtime_stats":
        return "Checking."
    if name == "shell_command":
        return "Inspecting."
    if name == "current_time":
        return "Checking."
    if name == "current_snapshot":
        return "Capturing."
    if name == "camera_clip":
        return "Recording."
    if name == "camera_ptz":
        return "Moving."
    if name == "focus_object":
        return "Focusing."
    if name == "environment_scan":
        return "Scanning."
    return "Working."


def notification_input_type(text: str, tool_plan: dict | None = None) -> str:
    words = len(re.findall(r"\w+", str(text or "")))
    if words <= 5:
        length_bucket = "short"
    elif words <= 18:
        length_bucket = "medium"
    else:
        length_bucket = "long"
    if not bool((tool_plan or {}).get("needs_tools")):
        return f"{length_bucket}:no_tools"
    calls = (tool_plan or {}).get("calls") if isinstance((tool_plan or {}).get("calls"), list) else []
    names = []
    for call in calls:
        name = re.sub(r"[^a-z0-9_:-]+", "_", str((call or {}).get("name") or "unknown").lower()).strip("_")
        if name:
            names.append(name)
    tool_key = "+".join(sorted(set(names))) if names else "unknown"
    return f"{length_bucket}:tools:{tool_key[:160]}"


def notification_audio_input_type(audio_seconds: float, source: str = "") -> str:
    try:
        seconds = max(0.0, float(audio_seconds or 0.0))
    except Exception:
        seconds = 0.0
    if seconds <= 3.0:
        length_bucket = "short_audio"
    elif seconds <= 8.0:
        length_bucket = "medium_audio"
    else:
        length_bucket = "long_audio"
    source_bucket = re.sub(r"[^a-z0-9_:-]+", "_", str(source or "unknown").lower()).strip("_") or "unknown"
    return f"{length_bucket}:{source_bucket}"


def ensure_notification_stats_db(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_timings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                pipeline TEXT NOT NULL,
                operation TEXT NOT NULL,
                input_type TEXT NOT NULL,
                source TEXT NOT NULL,
                input_chars INTEGER NOT NULL,
                input_words INTEGER NOT NULL,
                output_chars INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_operation_timings_lookup
            ON operation_timings (pipeline, operation, input_type, created_at)
            """
        )


def record_operation_timing(
    db_path: str | Path,
    pipeline: str,
    operation: str,
    input_type: str,
    source: str,
    input_text: str,
    output_text: str,
    duration_seconds: float,
    metadata: dict | None = None,
) -> None:
    try:
        ensure_notification_stats_db(db_path)
        words = len(re.findall(r"\w+", str(input_text or "")))
        with sqlite3.connect(Path(db_path), timeout=2.0) as conn:
            conn.execute(
                """
                INSERT INTO operation_timings (
                    created_at, pipeline, operation, input_type, source,
                    input_chars, input_words, output_chars, duration_seconds, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    pipeline,
                    operation,
                    input_type,
                    source or "unknown",
                    len(str(input_text or "")),
                    words,
                    len(str(output_text or "")),
                    max(0.0, float(duration_seconds or 0.0)),
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )
    except Exception:
        return


def mean_operation_duration(db_path: str | Path, pipeline: str, operation: str, input_type: str) -> tuple[float | None, int]:
    try:
        ensure_notification_stats_db(db_path)
        with sqlite3.connect(Path(db_path), timeout=2.0) as conn:
            row = conn.execute(
                """
                SELECT AVG(duration_seconds), COUNT(*)
                FROM operation_timings
                WHERE pipeline = ? AND operation = ? AND input_type = ?
                """,
                (pipeline, operation, input_type),
            ).fetchone()
            mean = float(row[0]) if row and row[0] is not None else None
            count = int(row[1] or 0) if row else 0
            if count:
                return mean, count
            row = conn.execute(
                """
                SELECT AVG(duration_seconds), COUNT(*)
                FROM operation_timings
                WHERE pipeline = ? AND operation = ?
                """,
                (pipeline, operation),
            ).fetchone()
            mean = float(row[0]) if row and row[0] is not None else None
            count = int(row[1] or 0) if row else 0
            return mean, count
    except Exception:
        return None, 0


def should_notify_operation(db_path: str | Path, pipeline: str, operation: str, input_type: str, threshold_seconds: float) -> bool:
    mean, count = mean_operation_duration(db_path, pipeline, operation, input_type)
    return count > 0 and mean is not None and mean >= max(0.0, float(threshold_seconds or 0.0))


def notification_operation_decision(
    db_path: str | Path,
    pipeline: str,
    operation: str,
    input_type: str,
    threshold_seconds: float,
) -> dict:
    mean, count = mean_operation_duration(db_path, pipeline, operation, input_type)
    return {
        "operation": operation,
        "input_type": input_type,
        "mean_seconds": mean,
        "sample_count": count,
        "threshold_seconds": max(0.0, float(threshold_seconds or 0.0)),
        "notified": count > 0 and mean is not None and mean >= max(0.0, float(threshold_seconds or 0.0)),
    }


def maybe_add_notification_fields(payload: dict, output_target: str, text: str, decision: dict) -> dict:
    if decision.get("notified"):
        payload.update(notification_fields(output_target, text))
    payload["notification_decision"] = decision
    return payload


def native_speak_text(text: str) -> tuple[bool, str]:
    clean = " ".join(str(text or "").split())
    if not clean:
        return False, "No text to speak"
    if shutil.which("spd-say"):
        try:
            subprocess.run(
                ["spd-say", "--voice-type", "male1", "--priority", "notification", "--pitch", "-18", "--rate", "8", clean],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)
    if shutil.which("espeak"):
        try:
            subprocess.run(
                ["espeak", "-v", "en-us+m3", "-p", "35", "-s", "175", clean],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
            )
            return True, ""
        except Exception as exc:
            return False, str(exc)
    return False, "No native server speech command found"


def notification_fields(output_target: str, text: str) -> dict:
    notice_id = notification_id(text)
    base = {
        "notification_id": notice_id,
        "notification_text": text,
        "notification_voice": "male",
    }
    if output_target == "browser":
        return {
            **base,
            "speech_synthesis_id": notice_id,
            "speech_synthesis_text": text,
            "speech_synthesis_kind": "notification",
            "speech_synthesis_voice": "male",
        }
    return base


def make_stage(stage_id: str, title: str, status: str, message: str, payload: dict | None = None) -> dict:
    now = time.time()
    timing = STAGE_TIMINGS.setdefault(stage_id, {})
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


def stage_model_payload(kind: str) -> dict:
    if kind == "nemotron":
        return {"model": ACTIVE_NEMOTRON_MODEL, "runtime": "Ollama"}
    if kind == "tool_planner":
        return {"model": ACTIVE_TOOL_PLANNER_MODEL, "runtime": "Ollama"}
    if kind == "tts":
        return {
            "model": ACTIVE_TTS_MODEL_LABEL,
            "codec": ACTIVE_TTS_CODEC_LABEL,
            **ACTIVE_TTS_DETAILS,
        }
    return {}


def inferred_context_status(intake: str, nemotron: str, tts: str, output: str) -> str:
    if intake != "complete":
        return "waiting"
    if "error" in {nemotron, tts, output}:
        return "complete"
    if nemotron in {"active", "complete"} or tts in {"active", "complete"} or output in {"active", "complete"}:
        return "complete"
    return "waiting"


def voice_stages(
    intake: str,
    nemotron: str,
    tts: str,
    output: str,
    message: str = "",
    output_target: str = "",
    context: dict[str, dict] | None = None,
    playback: str = "waiting",
) -> list[dict]:
    context = context or {}
    default_context_status = inferred_context_status(intake, nemotron, tts, output)

    def context_stage(stage_id: str, title: str, default_message: str) -> dict:
        data = context.get(stage_id) or {}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        if stage_id == "tool_plan":
            payload = {**stage_model_payload("tool_planner"), **payload}
        elif stage_id in {"candidate_generation", "response_scoring"}:
            payload = {**stage_model_payload("nemotron"), **payload}
        return make_stage(
            stage_id,
            title,
            str(data.get("status") or default_context_status),
            str(data.get("message") or default_message),
            payload,
        )

    return [
        make_stage("speech", "Speech Passage Intake", intake, message or "Waiting for a new ASR speech passage."),
        context_stage("context_visual", "Visual Context Load", "Checking whether a visual context source is active."),
        context_stage("context_alert", "Change Context", "Reading source-local change and risk context if enabled."),
        context_stage("context_history", "Source History DB Query", "Loading this source's observation history if enabled."),
        context_stage("context_screenshots", "Visual Capture Policy", "Checking the speech-side visual capture policy."),
        context_stage("context_prompt", "Prompt Assembly", "Combining speech, active context, history, and tool context."),
        context_stage("tool_plan", f"Planner / Executor ({ACTIVE_TOOL_PLANNER_MODEL})", "Deciding whether external tools are needed."),
        context_stage("tool_call", "Tool Call Executor", "Waiting for approved tool calls."),
        context_stage("tool_results", "Tool Result Understanding", "Preparing tool outputs for Nemotron."),
        context_stage("candidate_generation", f"Candidate Generation ({ACTIVE_NEMOTRON_MODEL})", "Generating possible grounded responses."),
        context_stage("response_scoring", f"Multi-Output Scoring ({ACTIVE_NEMOTRON_MODEL})", "Scoring and selecting the response to speak."),
        make_stage(
            "nemotron",
            f"Nemotron Voice ({ACTIVE_NEMOTRON_MODEL})",
            nemotron,
            "Waiting for Nemotron to produce response text.",
            stage_model_payload("nemotron"),
        ),
        make_stage(
            "tts",
            f"Magpie TTS ({ACTIVE_TTS_MODEL_LABEL})",
            tts,
            "Waiting for Nemotron-family TTS audio generation.",
            stage_model_payload("tts"),
        ),
        make_stage("output", "Audio Output Device", output, "Routing response audio.", {"target": output_target} if output_target else {}),
        make_stage(
            "playback",
            "Audible Speech Playback",
            playback,
            "Making the generated response audible.",
            {"target": output_target} if output_target else {},
        ),
    ]


def tts_safe_text(text: str, limit: int = 220, max_words: int = 24) -> str:
    replacements = {
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": " - ",
        "\u2014": " - ",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2026": "...",
    }
    safe = str(text or "")
    if limit and limit > 0:
        safe = short_text(safe, max(32, limit))
    for old, new in replacements.items():
        safe = safe.replace(old, new)
    safe = re.sub(r"\s+", " ", safe).strip()
    words = safe.split()
    if max_words > 0 and len(words) > max_words:
        safe = " ".join(words[:max_words]).rstrip(" ,;:")
        if safe and safe[-1] not in ".!?":
            safe += "."
    return safe


def tts_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", str(text or "")))


def split_long_tts_piece(piece: str, max_words: int, max_chars: int) -> list[str]:
    tokens = piece.split()
    if not tokens:
        return []
    chunks: list[str] = []
    current: list[str] = []
    for token in tokens:
        candidate = " ".join([*current, token])
        if current and (
            tts_word_count(candidate) > max_words
            or (max_chars > 0 and len(candidate) > max_chars)
        ):
            chunks.append(" ".join(current).strip())
            current = [token]
        else:
            current.append(token)
    if current:
        chunks.append(" ".join(current).strip())
    return [chunk for chunk in chunks if chunk]


def split_tts_chunks(text: str, max_words: int, max_chars: int) -> list[str]:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if not clean:
        return []
    max_words = max(4, int(max_words or 0))
    max_chars = max(32, int(max_chars or 0))
    if tts_word_count(clean) <= max_words and len(clean) <= max_chars:
        return [clean]

    chunks: list[str] = []
    sentence_matches = re.finditer(r"[^.!?]+(?:[.!?]+|$)", clean)
    for sentence_match in sentence_matches:
        sentence = sentence_match.group(0).strip()
        if not sentence:
            continue
        if tts_word_count(sentence) <= max_words and len(sentence) <= max_chars:
            chunks.append(sentence)
            continue

        phrase_parts = [
            part.strip()
            for part in re.findall(r"[^,;:]+(?:[,;:]|$)", sentence)
            if part.strip()
        ]
        if not phrase_parts:
            phrase_parts = [sentence]
        current = ""
        for part in phrase_parts:
            candidate = f"{current} {part}".strip() if current else part
            if current and (
                tts_word_count(candidate) > max_words
                or (max_chars > 0 and len(candidate) > max_chars)
            ):
                chunks.extend(split_long_tts_piece(current, max_words, max_chars))
                current = part
            else:
                current = candidate
        if current:
            chunks.extend(split_long_tts_piece(current, max_words, max_chars))
    return [chunk for chunk in chunks if chunk] or [clean]


def extract_json(text: str) -> dict:
    text = text.strip()
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    seen: set[str] = set()
    errors: list[json.JSONDecodeError] = []
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(exc)
        repaired = re.sub(r"([}\]])\s+(\"[A-Za-z_][A-Za-z0-9_]*\"\s*:)", r"\1, \2", candidate)
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        if repaired != candidate:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError as exc:
                errors.append(exc)
    if errors:
        raise errors[-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise


def json_object_end(text: str, start: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return -1


def recover_tool_calls_from_partial_json(text: str, allowed_tools: set[str], max_calls: int) -> list[dict]:
    recovered: list[dict] = []
    for match in re.finditer(r'"name"\s*:\s*"([^"]+)"', text or ""):
        if len(recovered) >= max(0, int(max_calls)):
            break
        name = str(match.group(1) or "").strip()
        if name not in allowed_tools:
            continue
        args: dict = {}
        args_match = re.search(r'"(?:args|arguments)"\s*:', text[match.end() :])
        if args_match:
            args_start = match.end() + args_match.end()
            brace_start = text.find("{", args_start)
            if brace_start >= 0:
                brace_end = json_object_end(text, brace_start)
                if brace_end > brace_start:
                    try:
                        parsed_args = json.loads(text[brace_start:brace_end])
                        if isinstance(parsed_args, dict):
                            args = parsed_args
                    except json.JSONDecodeError:
                        pass
        recovered.append({"name": name, "args": args})
    return recovered


def transcript_segments(transcript: dict) -> list[dict]:
    segments = []
    for segment in transcript.get("segments") or []:
        text = str(segment.get("text") or "").strip()
        if text:
            clean_segment = {
                "source": str(segment.get("source") or transcript.get("source") or "unknown"),
                "updated_at": float(segment.get("updated_at") or transcript.get("updated_at") or time.time()),
                "text": text,
            }
            for key in ("utterance_id", "utterance_final", "asr_context", "utterance_chunk_count", "utterance_seconds"):
                if key in segment:
                    clean_segment[key] = segment[key]
            segments.append(clean_segment)
    if segments:
        return segments
    text = str(transcript.get("latest_text") or transcript.get("text") or "").strip()
    if not text:
        return []
    return [
        {
            "source": str(transcript.get("source") or "unknown"),
            "updated_at": float(transcript.get("updated_at") or time.time()),
            "text": text,
        }
    ]


def segment_key(segment: dict) -> str:
    return f"{segment.get('source')}|{segment.get('updated_at'):.3f}|{normalize_text(str(segment.get('text') or ''))}"


def word_count(text: str) -> int:
    return len([word for word in normalize_text(text).split(" ") if word])


def activation_text(text: str) -> str:
    return re.sub(r"[^a-z0-9']+", " ", text.lower()).strip()


def configured_phrases(value: str) -> list[str]:
    phrases = []
    for item in str(value or "").split(","):
        phrase = activation_text(item)
        if phrase:
            phrases.append(phrase)
    return sorted(phrases, key=lambda phrase: (word_count(phrase), len(phrase)), reverse=True)


def phrase_in_text(text: str, phrase: str) -> bool:
    return f" {phrase} " in f" {text} "


def remove_first_phrase(text: str, phrase: str) -> str:
    return re.sub(rf"(^|\s){re.escape(phrase)}(\s|$)", " ", text, count=1).strip()


def dialog_activation_reason(args: argparse.Namespace, text: str) -> str:
    if args.dialog_activation_mode == "any":
        return "all ASR speech is enabled for dialog responses"

    normalized = activation_text(text)
    if not normalized:
        return ""

    for phrase in configured_phrases(args.wake_phrases):
        if not phrase_in_text(normalized, phrase):
            continue
        remainder = remove_first_phrase(normalized, phrase)
        if word_count(remainder) >= 1:
            return f"wake phrase: {phrase}"
        return ""

    if args.dialog_activation_mode != "prompt":
        return ""

    for phrase in configured_phrases(args.dialog_prompt_phrases):
        if normalized.startswith(phrase) or phrase_in_text(normalized, phrase):
            return f"direct prompt phrase: {phrase}"
    if str(text or "").strip().endswith("?"):
        return "question punctuation"
    return ""


def merge_utterance_segments(segments: list[dict]) -> dict:
    texts = []
    previous = ""
    for segment in sorted(segments, key=lambda item: float(item.get("updated_at") or 0)):
        text = " ".join(str(segment.get("text") or "").split())
        normalized = normalize_text(text)
        if not text or normalized == previous:
            continue
        texts.append(text)
        previous = normalized
    first = segments[0]
    last = segments[-1]
    return {
        "source": str(last.get("source") or first.get("source") or "unknown"),
        "started_at": float(first.get("updated_at") or time.time()),
        "updated_at": float(last.get("updated_at") or time.time()),
        "text": " ".join(texts),
        "segment_count": len(segments),
        "utterance_final": bool(last.get("utterance_final")),
        "utterance_id": str(last.get("utterance_id") or ""),
        "asr_context": str(last.get("asr_context") or ""),
    }


def next_utterance(args: argparse.Namespace, candidates: list[dict], seen: set[str]) -> tuple[dict | None, list[str], dict | None]:
    unseen = [segment for segment in sorted(candidates, key=lambda item: float(item.get("updated_at") or 0)) if segment_key(segment) not in seen]
    if not unseen:
        return None, [], None
    source = str(unseen[0].get("source") or "unknown")
    source_segments = [segment for segment in unseen if str(segment.get("source") or "unknown") == source]
    group = [source_segments[0]]
    started_at = float(source_segments[0].get("updated_at") or time.time())
    previous_at = started_at
    for segment in source_segments[1:]:
        updated_at = float(segment.get("updated_at") or time.time())
        if updated_at - previous_at > args.utterance_gap_seconds:
            break
        if updated_at - started_at > args.max_utterance_seconds:
            break
        group.append(segment)
        previous_at = updated_at

    utterance = merge_utterance_segments(group)
    keys = [segment_key(segment) for segment in group]
    age = time.time() - float(utterance.get("updated_at") or time.time())
    span = float(utterance.get("updated_at") or 0) - float(utterance.get("started_at") or 0)
    if not utterance.get("utterance_final") and age < args.utterance_idle_seconds and span < args.max_utterance_seconds:
        return None, [], utterance
    if word_count(str(utterance.get("text") or "")) < args.min_utterance_words:
        return None, keys, None
    return utterance, keys, None


def publish_waiting_for_utterance(args: argparse.Namespace, model: str, pending: dict) -> None:
    source = str(pending.get("source") or "unknown")
    output_target = read_output_target(args, source)
    publish(
        args.voice_response_json,
        {
            "status": "listening",
            "phase": "utterance_buffer",
            "operation": "Listening for the end of the speech passage.",
            "model": model,
            "input_speech": pending.get("text", ""),
            "input_source": source,
            "input_updated_at": pending.get("updated_at", 0),
            "output_target": output_target,
            "pending_segment_count": pending.get("segment_count", 0),
            "message": "ASR is still collecting speech fragments before Nemotron is queried.",
            "conversation": recent_conversation(args.voice_response_json, args.conversation_limit),
            "stages": voice_stages(
                "active",
                "waiting",
                "waiting",
                "waiting",
                "Buffering ASR fragments into one speech passage.",
                output_target,
            ),
        },
    )


def publish_ignored_utterance(args: argparse.Namespace, model: str, segment: dict) -> None:
    source = str(segment.get("source") or "unknown")
    output_target = read_output_target(args, source)
    publish(
        args.voice_response_json,
        {
            "status": "waiting",
            "phase": "ignored",
            "operation": "Waiting for a direct dialog prompt.",
            "model": model,
            "input_speech": "",
            "input_source": source,
            "input_updated_at": segment.get("updated_at", 0),
            "ignored_speech": short_text(segment.get("text", ""), 360),
            "output_target": output_target,
            "message": "ASR heard speech, but it was not addressed to the dialog agent. No Nemotron or TTS query was run.",
            "conversation": recent_conversation(args.voice_response_json, args.conversation_limit),
            "stages": voice_stages(
                "complete",
                "waiting",
                "waiting",
                "waiting",
                "Speech heard; waiting for a wake phrase before running the dialog agent.",
                output_target,
            ),
        },
    )


def ollama_json(
    url: str,
    path: str,
    payload: dict,
    timeout: float,
    *,
    component: str = "voice",
    source: str = "",
) -> dict:
    request = Request(
        f"{url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        record_response_usage(
            data,
            component=component,
            model=str(payload.get("model") or "unknown"),
            provider="ollama",
            source=source,
            metadata={"endpoint": path},
        )
        return data
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Ollama request failed HTTP {exc.code}: {body[:600]}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"Ollama request timed out after {timeout:g}s") from exc
    except URLError as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc


def ollama_tags(url: str, timeout: float) -> list[str]:
    try:
        with urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(f"Could not query Ollama models: {exc}") from exc
    return [model.get("name") or model.get("model") for model in data.get("models", []) if model.get("name") or model.get("model")]


def choose_model(args: argparse.Namespace) -> str:
    if args.model:
        return args.model
    models = ollama_tags(args.ollama_url, timeout=10)
    for preferred in PREFERRED_MODELS:
        if preferred in models:
            return preferred
    for model in models:
        if "nemotron" in model.lower():
            return model
    raise RuntimeError("No local Ollama Nemotron model found")


class SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict] = []
        self.in_result_link = False
        self.current_href = ""
        self.current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        classes = attrs_dict.get("class", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self.in_result_link = True
            self.current_href = attrs_dict.get("href", "")
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.in_result_link:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self.in_result_link:
            return
        title = " ".join(" ".join(self.current_text).split())
        href = clean_search_href(self.current_href)
        if title and href:
            self.results.append({"title": html.unescape(title), "url": href})
        self.in_result_link = False
        self.current_href = ""
        self.current_text = []


class TextExtractingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag in {"p", "br", "li", "h1", "h2", "h3", "title"} and not self.skip_depth:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        if tag in {"p", "li", "h1", "h2", "h3", "title"} and not self.skip_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            text = " ".join(data.split())
            if text:
                self.parts.append(text)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", " ".join(self.parts)).strip()


def clean_search_href(href: str) -> str:
    href = html.unescape(str(href or ""))
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed_full = urlparse(href)
    if parsed_full.netloc.endswith("duckduckgo.com") and parsed_full.path.startswith("/l/"):
        query = parse_qs(parsed_full.query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
    if href.startswith("/"):
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        if query.get("uddg"):
            return unquote(query["uddg"][0])
        return urljoin("https://duckduckgo.com", href)
    return href


def http_get_text(url: str, timeout: float, limit: int = 1_000_000) -> tuple[str, str]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; NemotronMonitoringAgent/1.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read(limit)
    return data.decode("utf-8", "replace"), content_type


def weather_location_from_query(query: str) -> str:
    text = " ".join(str(query or "").split())
    if not re.search(r"\b(weather|forecast|temperature|current conditions)\b", text, re.IGNORECASE):
        return ""
    patterns = (
        r"(?:weather|forecast|temperature|current conditions)\s+(?:for|in|at|near)\s+(.+)",
        r"(?:find|get|search(?:\s+the\s+web)?(?:\s+and\s+find)?)\s+(?:the\s+)?(?:weather|forecast|temperature|current conditions)\s+(?:for|in|at|near)\s+(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            location = match.group(1)
            location = re.split(r"\b(?:right now|today|currently|current|please)\b", location, maxsplit=1, flags=re.IGNORECASE)[0]
            location = location.strip(" .?!,;:")
            if location:
                return location
    cleaned = re.sub(
        r"\b(?:search|web|internet|online|look up|lookup|find|get|the|weather|forecast|temperature|current|conditions|for|in|at|near|please)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(cleaned.strip(" .?!,;:").split())


def current_weather_tool(location: str, timeout: float) -> dict:
    location = " ".join(str(location or "").split()).strip(" .?!,;:")
    if not location:
        return {"error": "empty weather location"}
    url = f"https://wttr.in/{quote_plus(location)}?format=j1"
    try:
        payload, _content_type = http_get_text(url, timeout=timeout, limit=240_000)
        data = json.loads(payload)
    except Exception as exc:
        return {"location": location, "error": str(exc)[:240], "source": "wttr.in"}
    current = (data.get("current_condition") or [{}])[0]
    nearest = (data.get("nearest_area") or [{}])[0]
    area_name = ((nearest.get("areaName") or [{}])[0].get("value") if isinstance(nearest.get("areaName"), list) else "") or location
    region = ((nearest.get("region") or [{}])[0].get("value") if isinstance(nearest.get("region"), list) else "")
    country = ((nearest.get("country") or [{}])[0].get("value") if isinstance(nearest.get("country"), list) else "")
    description = ((current.get("weatherDesc") or [{}])[0].get("value") if isinstance(current.get("weatherDesc"), list) else "") or ""
    result = {
        "requested_location": location,
        "resolved_location": ", ".join(part for part in (area_name, region, country) if part),
        "description": description,
        "temp_f": current.get("temp_F", ""),
        "temp_c": current.get("temp_C", ""),
        "feels_like_f": current.get("FeelsLikeF", ""),
        "feels_like_c": current.get("FeelsLikeC", ""),
        "humidity_percent": current.get("humidity", ""),
        "wind_mph": current.get("windspeedMiles", ""),
        "wind_kph": current.get("windspeedKmph", ""),
        "observation_time": current.get("observation_time", ""),
        "source": "wttr.in",
    }
    result["direct_answer"] = (
        f"Current weather for {result['resolved_location'] or location}: "
        f"{description or 'conditions unavailable'}, {result['temp_f']}°F"
        f" ({result['temp_c']}°C), feels like {result['feels_like_f']}°F"
        f", humidity {result['humidity_percent']}%, wind {result['wind_mph']} mph."
    )
    return result


def web_search_tool(query: str, timeout: float, limit: int = 5) -> dict:
    query = " ".join(str(query or "").split())
    if not query:
        return {"error": "empty search query", "results": []}
    weather_location = weather_location_from_query(query)
    weather_result = current_weather_tool(weather_location, timeout) if weather_location else {}
    errors = []
    for url, source in (
        (f"https://lite.duckduckgo.com/lite/?q={quote_plus(query)}", "duckduckgo_lite"),
        (f"https://duckduckgo.com/html/?q={quote_plus(query)}", "duckduckgo_html"),
        (f"https://www.bing.com/search?q={quote_plus(query)}", "bing_html"),
    ):
        try:
            payload, _content_type = http_get_text(url, timeout=timeout)
            parser = SearchResultParser()
            parser.feed(payload)
            results = []
            seen = set()
            for item in parser.results:
                clean_url = item.get("url", "")
                if clean_url in seen:
                    continue
                seen.add(clean_url)
                results.append({"title": item.get("title", ""), "url": clean_url})
                if len(results) >= limit:
                    break
            if results:
                payload = {"query": query, "results": results, "source": source}
                if weather_result:
                    payload["direct_answer"] = weather_result.get("direct_answer", "")
                    payload["weather"] = weather_result
                return payload
            errors.append(f"{source}: no parsed results")
        except Exception as exc:
            errors.append(f"{source}: {str(exc)[:160]}")
    payload = {"query": query, "error": " | ".join(errors)[-500:], "results": []}
    if weather_result:
        payload["direct_answer"] = weather_result.get("direct_answer", "")
        payload["weather"] = weather_result
        if weather_result.get("direct_answer"):
            payload.pop("error", None)
    return payload


def fetch_url_tool(url: str, timeout: float, max_chars: int) -> dict:
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"url": url, "error": "only http and https URLs are allowed"}
    try:
        payload, content_type = http_get_text(url, timeout=timeout)
        if "html" in content_type.lower() or "<html" in payload[:500].lower():
            parser = TextExtractingParser()
            parser.feed(payload)
            text = parser.text()
        else:
            text = " ".join(payload.split())
        return {"url": url, "content_type": content_type, "text": short_text(text, max_chars)}
    except Exception as exc:
        return {"url": url, "error": str(exc)[:240]}


def runtime_stats_tool(max_chars: int) -> dict:
    path = PROJECT_ROOT / "webcam-runtime-stats.json"
    alt_path = Path("/home/anslutsky/Dev/Cosmos-transfer/webcam-runtime-stats.json")
    data = read_json(path if path.exists() else alt_path)
    components = data.get("components") if isinstance(data.get("components"), dict) else {}
    slow = []
    for key, value in components.items():
        if not isinstance(value, dict):
            continue
        slow.append(
            {
                "component": key,
                "avg_seconds": value.get("avg_seconds"),
                "p95_seconds": value.get("p95_seconds"),
                "last_duration_seconds": value.get("last_duration_seconds"),
            }
        )
    slow.sort(key=lambda item: float(item.get("p95_seconds") or item.get("avg_seconds") or 0), reverse=True)
    result: dict[str, object] = {"slowest_components": slow[:8]}
    try:
        meminfo = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator:
                meminfo[key] = int(value.strip().split()[0])
        total_kib = int(meminfo.get("MemTotal") or 0)
        available_kib = int(meminfo.get("MemAvailable") or 0)
        if total_kib:
            used_kib = max(0, total_kib - available_kib)
            result["host_memory"] = {
                "used_gib": round(used_kib / 1024 / 1024, 1),
                "total_gib": round(total_kib / 1024 / 1024, 1),
                "used_percent": round(100 * used_kib / total_kib),
            }
    except (OSError, ValueError, IndexError):
        pass
    try:
        load_parts = Path("/proc/loadavg").read_text(encoding="utf-8").split()
        if len(load_parts) >= 3:
            result["load_average"] = {
                "one_minute": float(load_parts[0]),
                "five_minutes": float(load_parts[1]),
                "fifteen_minutes": float(load_parts[2]),
            }
    except (OSError, ValueError):
        pass
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        gpu_rows = []
        if completed.returncode == 0:
            for index, line in enumerate(completed.stdout.splitlines()):
                fields = [field.strip() for field in line.split(",")]
                if len(fields) != 3:
                    continue
                utilization = int(float(fields[0]))
                memory_used = None if fields[1].upper() in {"N/A", "[N/A]"} else int(float(fields[1]))
                memory_total = None if fields[2].upper() in {"N/A", "[N/A]"} else int(float(fields[2]))
                gpu_rows.append(
                    {
                        "index": index,
                        "utilization_percent": utilization,
                        "memory_used_mib": memory_used,
                        "memory_total_mib": memory_total,
                        "unified_memory": memory_used is None or memory_total is None,
                    }
                )
        if gpu_rows:
            result["gpus"] = gpu_rows
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    summary_payload = {key: value for key, value in result.items() if key != "summary"}
    result["summary"] = short_text(json.dumps(summary_payload), max_chars)
    return result


def current_time_tool(options: dict | None = None) -> dict:
    options = options if isinstance(options, dict) else {}
    local_time = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    clock_time = time.strftime("%H:%M:%S")
    timezone_name = time.strftime("%Z")
    include_date = options.get("include_date") is True
    include_timezone = options.get("omit_timezone") is not True
    if include_date:
        spoken_value = local_time if include_timezone else time.strftime("%Y-%m-%d %H:%M:%S")
        direct_answer = f"The local date and time is {spoken_value}."
    else:
        spoken_value = f"{clock_time} {timezone_name}".strip() if include_timezone else clock_time
        direct_answer = f"The local time is {spoken_value}."
    return {
        "unix_time": time.time(),
        "local_time": local_time,
        "include_date": include_date,
        "include_timezone": include_timezone,
        "omit_timezone": not include_timezone,
        "direct_answer": direct_answer,
    }


PTZ_DIRECTION_ALIASES = {
    "left": "left",
    "pan_left": "left",
    "right": "right",
    "pan_right": "right",
    "up": "up",
    "tilt_up": "up",
    "down": "down",
    "tilt_down": "down",
    "zoom_in": "zoom_in",
    "in": "zoom_in",
    "tele": "zoom_in",
    "zoom_out": "zoom_out",
    "out": "zoom_out",
    "wide": "zoom_out",
}


def normalized_ptz_direction(value: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    if key in PTZ_DIRECTION_ALIASES:
        return PTZ_DIRECTION_ALIASES[key]
    if key in {"left", "right", "up", "down", "zoom_in", "zoom_out"}:
        return key
    tokens = [token for token in key.split("_") if token]
    token_set = set(tokens)
    if "zoom" in token_set:
        if token_set & {"in", "tele", "closer"}:
            return "zoom_in"
        if token_set & {"out", "wide", "wider"}:
            return "zoom_out"
    for direction in ("left", "right", "up", "down"):
        if direction in token_set:
            return direction
    return ""


def ptz_direction_is_centering(value: str) -> bool:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    tokens = {token for token in key.split("_") if token}
    return bool(tokens & {"center", "centre", "centered", "centred", "middle"})


def generic_ptz_commands(value: str) -> list[str]:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    tokens = {token for token in key.split("_") if token}
    commands = []
    if tokens & {"pan", "panning", "sweep", "sweeping", "scan", "scanning", "look"}:
        commands.append("right")
    if tokens & {"tilt", "tilting"}:
        commands.append("up")
    if not commands and tokens & {"around", "survey"}:
        commands.extend(["right", "up"])
    return commands[:2]


def detected_objects_from_payload(payload: dict, source: str) -> list[dict]:
    source_key = str(source or "").strip().lower()
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    source_payload = sources.get(source_key) if isinstance(sources.get(source_key), dict) else {}
    for candidate in (source_payload, payload):
        if not isinstance(candidate, dict):
            continue
        for key in ("objects", "detections", "latest_objects"):
            objects = candidate.get(key)
            if isinstance(objects, list):
                return [item for item in objects if isinstance(item, dict)]
    return []


def latest_deepstream_objects(args: argparse.Namespace, source: str) -> list[dict]:
    paths = [PROJECT_ROOT / "webcam-deepstream-yolo-coco.json"]
    explicit = str(getattr(args, "deepstream_detections_json", "") or "").strip()
    if explicit:
        paths.insert(0, Path(explicit))
    for path in paths:
        if not path.exists():
            continue
        objects = detected_objects_from_payload(read_json(path), source)
        if objects:
            return objects
    return []


def conversation_items_for_source(args: argparse.Namespace, source: str) -> list[dict]:
    source_key = str(source or "").strip().lower()
    paths = [
        Path(getattr(args, "voicechat_response_json", "") or PROJECT_ROOT / "webcam-voicechat-response.json"),
        PROJECT_ROOT / "webcam-voicechat-history.json",
    ]
    items: list[dict] = []
    for path in paths:
        if not path.exists():
            continue
        payload = read_json(path)
        by_source = payload.get("conversation_by_source") if isinstance(payload.get("conversation_by_source"), dict) else {}
        source_items = by_source.get(source_key)
        if isinstance(source_items, list):
            items.extend(item for item in source_items if isinstance(item, dict))
        sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
        source_state = sources.get(source_key)
        state_items = source_state.get("conversation") if isinstance(source_state, dict) and isinstance(source_state.get("conversation"), list) else []
        items.extend(item for item in state_items if isinstance(item, dict))
    return sorted(items, key=lambda item: float(item.get("updated_at") or 0))


def recent_user_text_for_ptz(args: argparse.Namespace, source: str) -> str:
    texts = []
    for item in reversed(conversation_items_for_source(args, source)[-30:]):
        if item.get("role") != "user":
            continue
        if "Live Stream Processor" in str(item.get("label") or ""):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if text:
            texts.append(text)
        if len(texts) >= 4:
            break
    return " ".join(reversed(texts))


def object_probability(item: dict) -> float:
    try:
        value = float(item.get("confidence", item.get("score", item.get("probability", 0.0))) or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return value / 100.0 if value > 1.0 else value


def infer_centering_target_label(objects: list[dict], text: str) -> str:
    labels = sorted(
        {
            str(item.get("label") or item.get("class") or item.get("name") or "").strip().lower()
            for item in objects
            if isinstance(item, dict)
        },
        key=len,
        reverse=True,
    )
    lower = f" {' '.join(str(text or '').lower().split())} "
    for label in labels:
        if not label:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(label).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, lower):
            return label
    return labels[0] if len(labels) == 1 else ""


def resolve_center_ptz_commands(args: argparse.Namespace, source: str, target_text: str = "", deadzone: float = 0.08) -> dict:
    objects = latest_deepstream_objects(args, source)
    if not objects:
        return {"error": "no live detected objects are available for centering"}
    context_text = " ".join([str(target_text or ""), recent_user_text_for_ptz(args, source)]).strip()
    target_label = infer_centering_target_label(objects, context_text)
    if not target_label:
        labels = sorted({str(item.get("label") or "").strip().lower() for item in objects if item.get("label")})
        return {"error": f"could not infer which object to center; visible labels: {', '.join(labels[:8])}"}
    matches = [
        item
        for item in objects
        if str(item.get("label") or item.get("class") or item.get("name") or "").strip().lower() == target_label
    ]
    if not matches:
        return {"error": f"target object {target_label!r} is not currently detected"}
    target = max(matches, key=object_probability)
    bbox = target.get("bbox") if isinstance(target.get("bbox"), list) else []
    if len(bbox) < 4:
        return {"error": f"target object {target_label!r} has no usable bounding box"}
    try:
        left, top, width, height = [float(value) for value in bbox[:4]]
        frame_width = float(target.get("frame_width") or target.get("image_width") or 0)
        frame_height = float(target.get("frame_height") or target.get("image_height") or 0)
    except (TypeError, ValueError):
        return {"error": f"target object {target_label!r} has an invalid bounding box"}
    if frame_width <= 0 or frame_height <= 0 or width <= 0 or height <= 0:
        return {"error": f"target object {target_label!r} has invalid frame dimensions"}
    center_x = (left + width / 2.0) / frame_width
    center_y = (top + height / 2.0) / frame_height
    dx = center_x - 0.5
    dy = center_y - 0.5
    commands = []
    if abs(dx) > deadzone:
        commands.append("right" if dx > 0 else "left")
    if abs(dy) > deadzone:
        commands.append("down" if dy > 0 else "up")
    return {
        "target_label": target_label,
        "target_probability": object_probability(target),
        "object_center": {"x": round(center_x, 4), "y": round(center_y, 4)},
        "offset": {"x": round(dx, 4), "y": round(dy, 4)},
        "commands": commands[:2],
        "centered": not commands,
    }


def post_camera_ptz_command(
    args: argparse.Namespace,
    target_source: str,
    command: str,
    requested_degrees: float,
    duration_ms: int,
    speed: int,
    url: str,
) -> dict:
    payload = {
        "command": command,
        "action": "pulse",
        "speed": speed,
        "duration_ms": duration_ms,
    }
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(1.0, float(getattr(args, "tool_timeout", 8.0) or 8.0))) as response:
            raw = response.read(8192).decode("utf-8", "replace")
        result = json.loads(raw) if raw.strip() else {}
        return {
            "source": target_source,
            "direction": command,
            "requested_degrees": requested_degrees,
            "duration_ms": duration_ms,
            "speed": speed,
            "url": url,
            "status": result.get("status", "ok"),
            "backend": result.get("backend", ""),
            "pulse_stop_ok": result.get("pulse_stop_ok"),
            "camera_result": result,
            "direct_answer": f"Moved the camera {command.replace('_', ' ')} about {requested_degrees:g} degrees.",
        }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        return {"source": target_source, "direction": command, "url": url, "error": f"HTTP {exc.code}: {body}"}
    except Exception as exc:
        return {"source": target_source, "direction": command, "url": url, "error": str(exc)[:240]}


def camera_ptz_tool(
    args: argparse.Namespace,
    source: str,
    direction: str,
    degrees: object = None,
    target_text: str = "",
) -> dict:
    if not bool(getattr(args, "camera_ptz_enabled", True)):
        return {"error": "camera PTZ tool is disabled"}
    target_source = str(source or getattr(args, "camera_ptz_default_source", "wifi") or "wifi").strip().lower()
    if target_source in {"auto", "camera", "view", "video", "lane"}:
        target_source = str(getattr(args, "camera_ptz_default_source", "wifi") or "wifi").strip().lower()
    if target_source not in {"wifi", "bulb"}:
        return {"source": target_source, "error": "source must be wifi or bulb for physical pan/tilt"}

    command = normalized_ptz_direction(direction)
    center_requested = not command and ptz_direction_is_centering(direction)
    generic_commands = [] if command or center_requested else generic_ptz_commands(direction)
    if not command and not center_requested and not generic_commands:
        return {"source": target_source, "direction": direction, "error": "direction must be left, right, up, down, zoom_in, zoom_out, center a detected object, or a generic pan/tilt request"}

    try:
        requested_degrees = float(degrees if degrees is not None and degrees != "" else getattr(args, "camera_ptz_degrees", 5.0))
    except (TypeError, ValueError):
        requested_degrees = float(getattr(args, "camera_ptz_degrees", 5.0) or 5.0)
    requested_degrees = max(1.0, min(45.0, requested_degrees))
    base_pulse_ms = max(30, min(1000, int(getattr(args, "camera_ptz_pulse_ms", 120) or 120)))
    duration_ms = max(30, min(1000, int(round(base_pulse_ms * (requested_degrees / 5.0)))))
    speed = max(1, min(8, int(getattr(args, "camera_ptz_speed", 1) or 1)))

    url = str(getattr(args, "camera_ptz_url", "http://127.0.0.1:8090/wifi-ptz") or "").strip()
    if not url:
        return {"source": target_source, "direction": command or direction, "error": "camera PTZ URL is not configured"}

    if center_requested:
        center_plan = resolve_center_ptz_commands(args, target_source, target_text=target_text)
        if center_plan.get("error"):
            return {"source": target_source, "direction": direction, **center_plan}
        commands = center_plan.get("commands") if isinstance(center_plan.get("commands"), list) else []
        if not commands:
            return {
                "source": target_source,
                "direction": "center",
                "message": "target object is already near center",
                "direct_answer": f"{center_plan.get('target_label', 'Target object')} is already near the center of the view.",
                **center_plan,
            }
        steps = [
            post_camera_ptz_command(args, target_source, step_command, requested_degrees, duration_ms, speed, url)
            for step_command in commands
        ]
        failed = [step for step in steps if step.get("error")]
        result = {
            "source": target_source,
            "direction": "center",
            "resolved_directions": commands,
            "requested_degrees": requested_degrees,
            "duration_ms": duration_ms,
            "speed": speed,
            "url": url,
            "steps": steps,
            "direct_answer": f"Moved the camera {' and '.join(command.replace('_', ' ') for command in commands)} to center {center_plan.get('target_label', 'the target')}.",
            **center_plan,
        }
        if failed:
            result["error"] = "; ".join(str(step.get("error")) for step in failed)
        return result

    if generic_commands:
        steps = [
            post_camera_ptz_command(args, target_source, step_command, requested_degrees, duration_ms, speed, url)
            for step_command in generic_commands
        ]
        failed = [step for step in steps if step.get("error")]
        result = {
            "source": target_source,
            "direction": direction,
            "resolved_directions": generic_commands,
            "requested_degrees": requested_degrees,
            "duration_ms": duration_ms,
            "speed": speed,
            "url": url,
            "steps": steps,
            "direct_answer": f"Moved the camera {' and '.join(command.replace('_', ' ') for command in generic_commands)}.",
        }
        if failed:
            result["error"] = "; ".join(str(step.get("error")) for step in failed)
        return result

    return post_camera_ptz_command(args, target_source, command, requested_degrees, duration_ms, speed, url)


def normalized_focus_label(value: object) -> str:
    label = " ".join(str(value or "").strip().lower().replace("_", " ").split())
    for prefix in ("the ", "a ", "an "):
        if label.startswith(prefix):
            label = label[len(prefix) :].strip()
            break
    labels_path = PROJECT_ROOT / "deepstream-yolo-coco/DeepStream-Yolo/labels.txt"
    try:
        known = {" ".join(line.strip().lower().split()) for line in labels_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    except OSError:
        known = set()
    if label not in known and label.endswith("s") and label[:-1] in known:
        label = label[:-1]
    return label


def notify_focus_controller(args: argparse.Namespace, request_id: str) -> None:
    path = str(getattr(args, "focus_object_wake_socket", "") or "").strip()
    if not path:
        return
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(str(request_id or "focus").encode("utf-8"), path)
    except OSError:
        # The command JSON is authoritative and remains a polling fallback.
        pass
    finally:
        client.close()


def disable_focus_object(args: argparse.Namespace, reason: str) -> None:
    path = Path(getattr(args, "focus_object_command_json", "") or PROJECT_ROOT / "webcam-focus-object-command.json")
    current = read_json(path)
    if not current.get("enabled"):
        return
    write_json(
        path,
        {
            **current,
            "enabled": False,
            "completion_status": "cancelled",
            "completion_message": f"Object focus cancelled by {reason}.",
            "stopped_at": time.time(),
            "stop_reason": reason,
        },
    )
    notify_focus_controller(args, str(current.get("request_id") or "cancel"))


def wait_for_focus_ack(state_path: Path, request_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        state = read_json(state_path)
        if state.get("request_id") == request_id:
            return state
        time.sleep(0.05)
    return {}


def wait_for_focus_completion(state_path: Path, request_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + max(0.1, timeout)
    latest = {}
    terminal = {"complete", "error", "failed", "timed_out", "cancelled", "controller_unavailable"}
    while time.monotonic() < deadline:
        state = read_json(state_path)
        if state.get("request_id") == request_id:
            latest = state
            if str(state.get("status") or "").strip().lower() in terminal:
                return state
        time.sleep(0.05)
    return latest


def focus_object_priority(args: argparse.Namespace, label: str, command: dict | None = None) -> int | None:
    try:
        priority = int((command or {}).get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    if priority > 0:
        return priority
    settings_path = Path(
        getattr(args, "deepstream_settings_json", "")
        or PROJECT_ROOT / "webcam-deepstream-settings.json"
    )
    settings = read_json(settings_path)
    raw = settings.get("preferred_objects")
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    if not isinstance(raw, list):
        return None
    preferred = []
    for item in raw:
        normalized = normalized_focus_label(item)
        if normalized and normalized not in preferred:
            preferred.append(normalized)
    normalized_label = normalized_focus_label(label)
    return preferred.index(normalized_label) + 1 if normalized_label in preferred else None


def boxed_focus_snapshot(args: argparse.Namespace, source: str, label: str, state: dict) -> dict:
    bbox = state.get("bbox") if isinstance(state.get("bbox"), list) else []
    frame_size = state.get("frame_size") if isinstance(state.get("frame_size"), list) else []
    if len(bbox) < 4 or len(frame_size) < 2:
        return {}
    try:
        left, top, width, height = (float(value) for value in bbox[:4])
        frame_width, frame_height = (float(value) for value in frame_size[:2])
    except (TypeError, ValueError):
        return {}
    if frame_width <= 0 or frame_height <= 0 or width <= 0 or height <= 0:
        return {}
    base_snapshot_url = str(getattr(args, "snapshot_url", "http://127.0.0.1:8090/snapshot.jpg") or "")
    snapshot_args = argparse.Namespace(**vars(args))
    snapshot_args.snapshot_url = base_snapshot_url
    url = snapshot_url_for_tool_source(snapshot_args, source)
    if not url:
        return {}
    try:
        request = Request(
            cache_busted_url(url),
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            method="GET",
        )
        timeout = max(0.5, float(getattr(args, "tool_snapshot_timeout", 4.0) or 4.0))
        max_bytes = max(1, int(getattr(args, "tool_snapshot_max_bytes", 2_000_000) or 2_000_000))
        with urlopen(request, timeout=timeout) as response:
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            return {}
        from PIL import Image

        image = Image.open(BytesIO(data)).convert("RGB")
        x_scale = image.width / frame_width
        y_scale = image.height / frame_height
        x1 = max(0, min(image.width - 1, math.floor(left * x_scale)))
        y1 = max(0, min(image.height - 1, math.floor(top * y_scale)))
        x2 = max(1, min(image.width, math.ceil((left + width) * x_scale)))
        y2 = max(1, min(image.height, math.ceil((top + height) * y_scale)))
        if x2 <= x1 or y2 <= y1:
            return {}
        image = image.crop((x1, y1, x2, y2))
        output = BytesIO()
        image.save(output, format="JPEG", quality=86)
        image_bytes = output.getvalue()
        captured_at = time.time()
        data_url = snapshot_data_url(image_bytes)
        return {
            "snapshot_url": url,
            "captured_at": captured_at,
            "snapshot_count": 1,
            "snapshot_images": [{
                "data_url": data_url,
                "bytes": len(image_bytes),
                "captured_at": captured_at,
                "source_url": url,
                "index": 1,
                "kind": "focus_object_boxed_frame",
            }],
            "image_data_url": data_url,
            "image_bytes": len(image_bytes),
        }
    except Exception:
        return {}


def focus_object_tool(
    args: argparse.Namespace,
    source: str,
    target: str,
    existing_request_id: str = "",
) -> dict:
    if not bool(getattr(args, "focus_object_enabled", True)):
        return {"error": "focus-object tool is disabled"}
    target_source = str(source or getattr(args, "camera_ptz_default_source", "wifi") or "wifi").strip().lower()
    if target_source in {"", "auto", "camera", "view", "video", "lane"}:
        target_source = str(getattr(args, "camera_ptz_default_source", "wifi") or "wifi").strip().lower()
    if target_source not in {"wifi", "bulb"}:
        return {"source": target_source, "error": "source must be wifi or bulb for object focus"}
    command_path = Path(getattr(args, "focus_object_command_json", "") or PROJECT_ROOT / "webcam-focus-object-command.json")
    state_path = Path(getattr(args, "focus_object_state_json", "") or PROJECT_ROOT / "webcam-focus-object-state.json")
    label = normalized_focus_label(target)
    if not label:
        return {"source": target_source, "error": "a target object label is required to focus the camera"}
    timeout = max(2.0, min(300.0, float(getattr(args, "focus_object_timeout", 20.0) or 20.0)))
    request_id = str(existing_request_id or "").strip()
    if request_id:
        payload = read_json(command_path)
        if (
            payload.get("request_id") != request_id
            or str(payload.get("source") or "").strip().lower() != target_source
            or normalized_focus_label(payload.get("target_label")) != label
        ):
            return {
                "status": "superseded",
                "source": target_source,
                "target_label": label,
                "error": "the prestarted focus request was replaced before Nemotron observed it",
            }
    else:
        now = time.time()
        request_id = f"focus_{time.time_ns()}"
        payload = {
            "enabled": True,
            "source": target_source,
            "target_label": label,
            "requested_at": now,
            "requested_monotonic_ns": time.monotonic_ns(),
            "expires_at": now + timeout,
            "request_id": request_id,
        }
        write_json(command_path, payload)
        notify_focus_controller(args, request_id)
    ack_timeout = max(0.1, min(10.0, float(getattr(args, "focus_object_ack_timeout", 2.0) or 2.0)))
    state = wait_for_focus_ack(state_path, request_id, ack_timeout)
    if not state:
        latest = read_json(command_path)
        if latest.get("request_id") == request_id:
            write_json(
                command_path,
                {
                    **latest,
                    "enabled": False,
                    "completion_status": "controller_unavailable",
                    "completion_message": "Focus controller did not acknowledge the request.",
                    "completed_at": time.time(),
                },
            )
        return {
            "status": "controller_unavailable",
            "source": target_source,
            "target_label": label,
            "error": "focus controller is not running or did not acknowledge the request",
        }
    terminal_state = wait_for_focus_completion(state_path, request_id, timeout + 2.0)
    if terminal_state:
        state = terminal_state
    status = str(state.get("status") or "").strip().lower()
    if status != "complete":
        message = str(
            state.get("error")
            or state.get("message")
            or f"focus controller did not complete within {timeout:g} seconds"
        )
        return {
            "status": status or "timed_out",
            "source": target_source,
            "target_label": label,
            "error": message,
            "controller_state": state,
        }
    priority = focus_object_priority(args, label, payload)
    acquisition_text = (
        f"Priority {priority} object {label} acquired."
        if priority is not None
        else f"Object {label} acquired."
    )
    image_result = boxed_focus_snapshot(args, target_source, label, state)
    return {
        "status": "complete",
        "source": target_source,
        "target_label": label,
        "priority": priority,
        "preferred_rank": priority - 1 if priority is not None else None,
        "command_path": str(command_path),
        "controller_state": state,
        "direct_answer": acquisition_text,
        **image_result,
    }


def ptz_url_for_tool_source(args: argparse.Namespace, source: str) -> str:
    url = str(getattr(args, "camera_ptz_url", "http://127.0.0.1:8090/wifi-ptz") or "").strip()
    if source == "bulb" and url.endswith("/wifi-ptz"):
        return url[: -len("/wifi-ptz")] + "/bulb-ptz"
    if source == "wifi" and url.endswith("/bulb-ptz"):
        return url[: -len("/bulb-ptz")] + "/wifi-ptz"
    return url


def compact_image_file_data_url(path: Path, max_bytes: int, max_width: int, jpeg_quality: int) -> tuple[str, dict]:
    data = path.read_bytes()
    info = {"path": str(path), "original_bytes": len(data), "bytes": len(data), "resized": False}
    try:
        from io import BytesIO
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB")
            info["original_size"] = [image.width, image.height]
            if image.width > max_width:
                ratio = max_width / float(image.width)
                image = image.resize((max_width, max(1, int(image.height * ratio))), Image.Resampling.LANCZOS)
                info["resized"] = True
            qualities = [max(25, min(95, int(jpeg_quality or 72))), 60, 50, 40, 32, 25]
            compact = data
            for quality in qualities:
                output = BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                compact = output.getvalue()
                if len(compact) <= max_bytes:
                    info["jpeg_quality"] = quality
                    break
            data = compact
            info["size"] = [image.width, image.height]
            info["bytes"] = len(data)
    except Exception as exc:
        info["compact_error"] = str(exc)[:180]
    if len(data) > max_bytes:
        raise RuntimeError(f"environment scan image exceeded max byte limit after compaction: {len(data)} > {max_bytes}")
    return snapshot_data_url(data), info


def environment_scan_tool(args: argparse.Namespace, source: str, reason: str = "") -> dict:
    if not bool(getattr(args, "environment_scan_enabled", True)):
        return {"error": "environment scan tool is disabled"}
    target_source = str(source or "").strip().lower()
    if target_source in {"", "auto", "camera", "view", "video", "lane", "server", "browser"}:
        target_source = str(getattr(args, "camera_ptz_default_source", "wifi") or "wifi").strip().lower()
    if target_source not in {"wifi", "bulb"}:
        return {"source": target_source, "error": "source must be wifi or bulb for a PTZ environment scan"}

    width = max(1, min(25, int(getattr(args, "environment_scan_width", 11) or 11)))
    height = max(1, min(15, int(getattr(args, "environment_scan_height", 5) or 5)))
    step_ms = max(30, min(1000, int(getattr(args, "environment_scan_step_ms", 1000) or 1000)))
    settle_ms = max(0, min(10000, int(getattr(args, "environment_scan_settle_ms", 500) or 500)))
    cell_width = max(32, min(1024, int(getattr(args, "environment_scan_cell_width", 192) or 192)))
    cell_height = max(32, min(1024, int(getattr(args, "environment_scan_cell_height", 108) or 108)))
    timeout = max(5.0, float(getattr(args, "environment_scan_timeout", 180.0) or 180.0))
    output_dir = Path(getattr(args, "environment_scan_output_dir", "") or PROJECT_ROOT / "webcam-environment-scans").expanduser()
    scan_id = f"{target_source}_environment_scan_{int(time.time() * 1000)}"
    output_path = output_dir / f"{scan_id}.jpg"
    preview_path = output_dir / f"{scan_id}_preview.jpg"
    preview_html_path = output_dir / f"{scan_id}_preview.html"
    metadata_path = output_dir / f"{scan_id}.json"
    snapshot_url = snapshot_url_for_tool_source(args, target_source)
    ptz_url = ptz_url_for_tool_source(args, target_source)
    argv = [
        sys.executable,
        "-m",
        "camera_ops.grid_scan",
        "--steps_width",
        str(width),
        "--steps_height",
        str(height),
        "--step-ms",
        str(step_ms),
        "--settle-ms",
        str(settle_ms),
        "--pulse-mode",
        "server",
        "--source",
        target_source,
        "--url",
        ptz_url,
        "--snapshot-url",
        snapshot_url,
        "--output",
        str(output_path),
        "--preview-output",
        str(preview_path),
        "--preview-html",
        str(preview_html_path),
        "--metadata-output",
        str(metadata_path),
        "--cell-width",
        str(cell_width),
        "--cell-height",
        str(cell_height),
        "--json",
    ]
    started_at = time.time()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(PROJECT_ROOT),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "source": target_source,
            "error": f"environment scan timed out after {timeout:.1f}s",
            "stdout": short_text(exc.stdout or "", 500),
            "stderr": short_text(exc.stderr or "", 500),
        }
    elapsed = time.time() - started_at
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    scan_result = {}
    try:
        scan_result = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        scan_result = {"raw_stdout": short_text(stdout, 1200)}
    if completed.returncode != 0:
        return {
            "source": target_source,
            "error": f"environment scan failed with exit code {completed.returncode}",
            "stdout": short_text(stdout, 1000),
            "stderr": short_text(stderr, 1200),
            "scan_result": scan_result,
        }
    if not output_path.is_file():
        return {"source": target_source, "error": "environment scan completed but did not produce an output image", "scan_result": scan_result}

    try:
        data_url, image_info = compact_image_file_data_url(
            output_path,
            max(50_000, int(getattr(args, "environment_scan_max_image_bytes", 1_250_000) or 1_250_000)),
            max(128, int(getattr(args, "environment_scan_max_image_width", 1600) or 1600)),
            max(25, min(95, int(getattr(args, "environment_scan_jpeg_quality", 72) or 72))),
        )
    except Exception as exc:
        return {
            "source": target_source,
            "error": str(exc)[:240],
            "output_path": str(output_path),
            "metadata_path": str(metadata_path),
            "scan_result": scan_result,
        }
    captured_at = time.time()
    return {
        "source": target_source,
        "source_label": source_label(target_source),
        "reason": short_text(reason or "scan the current environment", 180),
        "output_path": str(output_path),
        "preview_output_path": str(preview_path),
        "preview_html_path": str(preview_html_path),
        "live_image_url": f"/environment-scan-latest.jpg?source={target_source}&started_at={started_at:.3f}",
        "metadata_path": str(metadata_path),
        "scan_width": width,
        "scan_height": height,
        "captured_cells": scan_result.get("captured_cells", width * height),
        "total_cells": scan_result.get("total_cells", width * height),
        "elapsed_seconds": round(elapsed, 2),
        "snapshot_count": 1,
        "snapshot_images": [
            {
                "data_url": data_url,
                "bytes": image_info.get("bytes"),
                "captured_at": captured_at,
                "source_url": str(output_path),
                "index": 1,
                "kind": "environment_scan_grid",
                "size": image_info.get("size"),
            }
        ],
        "image_data_url": data_url,
        "image_bytes": image_info.get("bytes"),
        "image_info": image_info,
        "scan_result": scan_result,
        "stderr": short_text(stderr, 1200),
        "direct_answer": f"Scanned the environment into a {width} by {height} image grid.",
    }


READ_ONLY_COMMANDS = {
    "pwd",
    "ls",
    "find",
    "rg",
    "grep",
    "cat",
    "head",
    "tail",
    "wc",
    "du",
    "df",
    "ps",
    "pgrep",
    "free",
    "uptime",
    "date",
    "whoami",
    "hostname",
    "id",
    "nvidia-smi",
    "git",
    "ollama",
    "ss",
    "lsof",
    "journalctl",
    "systemctl",
}


def shell_command_allowed(command: str, mode: str) -> tuple[bool, str, list[str]]:
    command = str(command or "").strip()
    if not command:
        return False, "empty command", []
    dangerous_tokens = (
        " rm ",
        " rm\t",
        "rm -",
        "mkfs",
        "dd ",
        "shutdown",
        "reboot",
        "poweroff",
        "passwd",
        "sudo ",
        "su ",
        "chmod ",
        "chown ",
        "curl ",
        "wget ",
        "scp ",
        "rsync ",
        "nc ",
        "bash -c",
        "sh -c",
        "python -c",
        "perl -e",
        "ruby -e",
        "node -e",
        ">",
        ">>",
        "|",
        ";",
        "&&",
        "||",
        "`",
        "$(",
    )
    padded = f" {command} "
    if mode != "dangerous" and any(token in padded for token in dangerous_tokens):
        return False, "blocked potentially destructive or exfiltrating shell syntax", []
    if mode == "dangerous":
        return True, "", ["/bin/bash", "-lc", command]
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return False, f"could not parse command: {exc}", []
    if not argv:
        return False, "empty command", []
    base = Path(argv[0]).name
    if base not in READ_ONLY_COMMANDS:
        return False, f"command {base!r} is not in the read-only allowlist", []
    if base == "git" and len(argv) > 1 and argv[1] not in {"status", "log", "diff", "show", "branch", "rev-parse"}:
        return False, "only read-only git subcommands are allowed", []
    if base == "ollama" and len(argv) > 1 and argv[1] not in {"ps", "list", "show"}:
        return False, "only read-only ollama subcommands are allowed", []
    if base == "systemctl" and len(argv) > 1 and argv[1] not in {"status", "is-active", "list-units", "list-timers"}:
        return False, "only read-only systemctl subcommands are allowed", []
    if base == "journalctl" and any(arg in {"--vacuum-time", "--vacuum-size", "--rotate"} for arg in argv[1:]):
        return False, "journal mutation options are blocked", []
    return True, "", argv


def shell_command_tool(args: argparse.Namespace, command: str) -> dict:
    if not args.enable_system_tools:
        return {"command": command, "error": "system tools are disabled"}
    allowed, reason, argv = shell_command_allowed(command, args.shell_tool_mode)
    if not allowed:
        return {"command": command, "allowed": False, "error": reason}
    cwd = Path(args.shell_cwd)
    if not cwd.exists():
        cwd = PROJECT_ROOT
    try:
        result = subprocess.run(
            argv,
            cwd=str(cwd),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=args.shell_timeout,
        )
        output = result.stdout
        if result.stderr:
            output = (output + "\n[stderr]\n" + result.stderr).strip()
        return {
            "command": command,
            "argv": argv,
            "cwd": str(cwd),
            "returncode": result.returncode,
            "output": short_text(output, args.shell_output_chars),
        }
    except Exception as exc:
        return {"command": command, "argv": argv, "cwd": str(cwd), "error": str(exc)[:240]}


def query_environment_tool(args: argparse.Namespace, source: str, prompt: str) -> dict:
    source = str(source or "").strip().lower()
    if source not in {"server", "browser", "wifi", "bulb"}:
        return {"source": source, "error": "source must be server, browser, wifi, or bulb"}

    state_path = environment_state_path(args, source)
    if not state_path:
        return {"source": source, "error": "environment state path is not configured"}

    before = read_json(state_path)
    before_updated_at = finite_timestamp(before.get("updated_at"))
    reason = short_text(str(prompt or "voicechat_environment_query"), 240)
    requested_at = time.time()
    request_environment_wake(args, source, f"voice_query: {reason}")

    deadline = time.time() + max(1.0, float(getattr(args, "environment_tool_timeout", args.tool_timeout)))
    poll_seconds = max(0.1, float(getattr(args, "environment_tool_poll_seconds", 0.5)))
    latest = before if isinstance(before, dict) else {}
    completed_after_request = False
    while time.time() < deadline:
        state = read_json(state_path)
        if isinstance(state, dict):
            latest = state
            updated_at = finite_timestamp(state.get("updated_at"))
            status = str(state.get("status") or "")
            phase = str(state.get("phase") or "")
            query_prompt = str(state.get("query_prompt") or "").strip()
            prompt_matches = not reason or query_prompt == reason
            completed = status not in {"loading"} and phase not in {"wake", "cosmos", "history", "synthesis"}
            if updated_at >= requested_at and updated_at > before_updated_at and completed and prompt_matches:
                completed_after_request = True
                break
        time.sleep(min(poll_seconds, max(0.0, deadline - time.time())))

    updated_at = finite_timestamp(latest.get("updated_at"))
    timed_out = not completed_after_request
    cosmos = latest.get("cosmos_analysis") if isinstance(latest.get("cosmos_analysis"), dict) else {}
    history = latest.get("history_analysis") if isinstance(latest.get("history_analysis"), dict) else {}
    return {
        "source": source,
        "source_label": source_label(source),
        "prompt": prompt,
        "wake_requested": True,
        "completed_after_request": completed_after_request,
        "timed_out": timed_out,
        "updated_at": updated_at or None,
        "age_seconds": round(max(0.0, time.time() - updated_at), 2) if updated_at else None,
        "status": latest.get("status", ""),
        "phase": latest.get("phase", ""),
        "summary": short_text(str(latest.get("summary") or latest.get("message") or ""), 700),
        "visual_state": short_text(str(latest.get("visual_state") or ""), 900),
        "activity": short_text(str(latest.get("activity") or ""), 500),
        "risk": latest.get("risk", ""),
        "attention": latest.get("attention", []),
        "change_assessment": short_text(str(latest.get("change_assessment") or ""), 500),
        "cosmos_answer": short_text(str(cosmos.get("answer") or ""), 900),
        "history_summary": short_text(str(history.get("historical_summary") or history.get("summary") or ""), 500),
    }


def run_tool_call(args: argparse.Namespace, call: dict) -> dict:
    name = str(call.get("name") or "").strip()
    raw_args = call.get("args") if isinstance(call.get("args"), dict) else {}
    if name == "web_search":
        return {"name": name, "args": raw_args, "result": web_search_tool(str(raw_args.get("query") or ""), args.tool_timeout)}
    if name == "fetch_url":
        return {"name": name, "args": raw_args, "result": fetch_url_tool(str(raw_args.get("url") or ""), args.tool_timeout, args.tool_result_chars)}
    if name == "current_time":
        return {"name": name, "args": raw_args, "result": current_time_tool(raw_args)}
    if name == "runtime_stats":
        return {"name": name, "args": raw_args, "result": runtime_stats_tool(args.tool_result_chars)}
    if name == "current_snapshot":
        return {
            "name": name,
            "args": raw_args,
            "result": current_snapshot_tool(
                args,
                str(raw_args.get("source") or raw_args.get("input_source") or ""),
                str(raw_args.get("reason") or raw_args.get("prompt") or ""),
            ),
        }
    if name == "camera_clip":
        return {
            "name": name,
            "args": raw_args,
            "result": camera_clip_tool(
                args,
                str(raw_args.get("source") or raw_args.get("input_source") or ""),
                raw_args.get("duration_seconds", raw_args.get("seconds", raw_args.get("duration"))),
                str(raw_args.get("reason") or raw_args.get("prompt") or ""),
                raw_args.get("include_audio", raw_args.get("with_audio", False)),
            ),
        }
    if name == "camera_ptz":
        return {
            "name": name,
            "args": raw_args,
            "result": camera_ptz_tool(
                args,
                str(raw_args.get("source") or raw_args.get("input_source") or ""),
                str(raw_args.get("direction") or raw_args.get("command") or ""),
                raw_args.get("degrees"),
                " ".join(
                    str(raw_args.get(key) or "")
                    for key in ("target", "object", "label", "reason", "prompt", "query", "description")
                ),
            ),
        }
    if name == "focus_object":
        focus_source = str(raw_args.get("source") or raw_args.get("input_source") or "")
        focus_target = " ".join(
            str(raw_args.get(key) or "")
            for key in ("target", "object", "label")
            if raw_args.get(key)
        )
        focus_request_id = str(raw_args.get("request_id") or "")
        if focus_request_id:
            focus_result = focus_object_tool(args, focus_source, focus_target, focus_request_id)
        else:
            focus_result = focus_object_tool(args, focus_source, focus_target)
        return {
            "name": name,
            "args": raw_args,
            "result": focus_result,
        }
    if name == "environment_scan":
        return {
            "name": name,
            "args": raw_args,
            "result": environment_scan_tool(
                args,
                str(raw_args.get("source") or raw_args.get("input_source") or ""),
                str(raw_args.get("reason") or raw_args.get("prompt") or raw_args.get("query") or ""),
            ),
        }
    if name == "shell_command":
        return {"name": name, "args": raw_args, "result": shell_command_tool(args, str(raw_args.get("command") or ""))}
    if name == "query_environment":
        if not bool(getattr(args, "enable_environment_tools", False)):
            return {"name": name, "args": raw_args, "error": "environment agent tools are disabled"}
        return {
            "name": name,
            "args": raw_args,
            "result": query_environment_tool(
                args,
                str(raw_args.get("source") or raw_args.get("input_source") or ""),
                str(raw_args.get("prompt") or raw_args.get("query") or ""),
            ),
        }
    return {"name": name or "unknown", "args": raw_args, "error": "tool is not available"}


def tool_planner_prompt(
    segment: dict,
    env_state: dict,
    alert: dict,
    template: str = DEFAULT_TOOL_PLANNER_TEMPLATE,
) -> str:
    available_tools = [
        'web_search requires args={"query":"specific non-empty search phrase"}=current web facts/news',
        "fetch_url(url)=read supplied URL",
        "current_time()=live local time/date",
        "runtime_stats()=machine/service status",
        "camera_ptz(source,direction,degrees)=move camera",
        "focus_object(source,target)=center object once",
        "current_snapshot(source,reason)=inspect current view",
        "camera_clip(source,duration_seconds,reason)=record and inspect a 1-10 second current video",
        "environment_scan(source,reason)=scan whole room",
        "shell_command(command)=allowed read-only local inspection",
    ]
    environment_tools_enabled = bool(segment.get("enable_environment_tools"))
    if environment_tools_enabled:
        available_tools.append(
            "query_environment(source,prompt)=retrieve prior environment observation"
        )
    tool_rules = [
        "Policy-required current/web/news facts use web_search.",
        'Never call web_search with empty args. For timestamp news the query MUST be {"query":"recent news YYYY-MM-DD"}, copying the complete supplied calendar date exactly; a year-only or partial date is invalid.',
        "Specific URL uses fetch_url; time/date uses current_time; machine status uses runtime_stats.",
        "Movement uses camera_ptz; focus/center uses focus_object; one current frame uses current_snapshot; short motion or an N-second video uses camera_clip; whole-room scan uses environment_scan.",
        "Never select destructive shell actions. Greetings and small talk need no tool.",
    ]
    if environment_tools_enabled:
        tool_rules.append(
            "Prior environment retrieval/comparison uses query_environment."
        )
    return render_tool_planner_template(
        template or DEFAULT_TOOL_PLANNER_TEMPLATE,
        {
            "AVAILABLE_TOOLS": "\n".join(available_tools),
            "TOOL_RULES": "\n".join(f"- {rule}" for rule in tool_rules),
            "SYSTEM_RESPONSE_POLICY": str(segment.get("system_response_policy") or "No additional system response policy."),
            "CURRENT_INPUT": str(segment.get("text") or ""),
            "INPUT_SOURCE": str(segment.get("source") or "unknown"),
        },
    )


def plan_tools(
    args: argparse.Namespace,
    model: str,
    segment: dict,
    env_state: dict,
    alert: dict,
    planner_request_id: str = "",
    planner_queue_managed: bool = False,
) -> tuple[dict, str]:
    if not args.enable_tools:
        return {"needs_tools": False, "calls": [], "reason": "tools disabled"}, ""
    if not str(segment.get("text") or "").strip():
        return {
            "needs_tools": False,
            "calls": [],
            "reason": "empty speech passage",
            "planner_source": "empty_input",
            "route_confidence": "high",
        }, ""
    camera_tools_enabled = bool(getattr(args, "camera_tools", True))
    ptz_default_source = str(
        segment.get("source") or getattr(args, "camera_ptz_default_source", "wifi") or "wifi"
    ).strip().lower()
    if ptz_default_source not in {"wifi", "bulb"}:
        ptz_default_source = "wifi"
    planner_model = str(args.tool_planner_model or "").strip() or model
    planner_segment = {**segment, "enable_environment_tools": bool(getattr(args, "enable_environment_tools", False))}
    template_path = str(
        getattr(args, "tool_planner_template_json", "")
        or PROJECT_ROOT / "webcam-tool-planner-template.json"
    )
    prompt = tool_planner_prompt(
        planner_segment,
        env_state,
        alert,
        read_tool_planner_template(template_path),
    )
    allowed_tools = {
        "web_search",
        "fetch_url",
        "current_time",
        "runtime_stats",
        "shell_command",
        "current_snapshot",
        "camera_clip",
    }
    if camera_tools_enabled:
        allowed_tools.update({"camera_ptz", "focus_object", "environment_scan"})
    if bool(getattr(args, "enable_environment_tools", False)):
        allowed_tools.add("query_environment")
    request_id = str(planner_request_id or "")
    queue_snapshot = read_tool_planner_queue(args)
    queue_after = queue_snapshot
    planner_failed = False
    if not planner_queue_managed:
        request_id, queue_snapshot = start_tool_planner_queue(
            args,
            str(segment.get("source") or ""),
            planner_model,
            request_id,
        )
    elif request_id:
        queue_snapshot = read_tool_planner_queue(args)
    raw = ""
    planner_token_usage: dict = {}
    plan: dict
    try:
        response = {}
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = ollama_json(
                    str(getattr(args, "tool_planner_url", "") or args.ollama_url),
                    "/api/generate",
                    {
                        "model": planner_model,
                        "prompt": prompt,
                        "stream": False,
                        "format": "json",
                        "options": {
                            "temperature": 0,
                            "num_predict": max(32, int(args.tool_planner_num_predict)),
                            "num_ctx": max(512, int(args.tool_planner_num_ctx)),
                        },
                        "keep_alive": args.nemotron_keep_alive,
                    },
                    timeout=min(args.timeout, max(1.0, float(args.tool_planner_timeout))),
                    component="tool_plan",
                    source=str(segment.get("source") or ""),
                )
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                if "server busy" not in str(exc).lower() and "maximum pending requests" not in str(exc).lower():
                    raise
                time.sleep(0.25 * (attempt + 1))
        if last_error is not None:
            raise last_error
        counts = extract_response_usage(response)
        if counts:
            input_tokens, output_tokens, _total_tokens = counts
            context_window = max(512, int(args.tool_planner_num_ctx))
            output_limit = max(32, int(args.tool_planner_num_predict))
            planner_token_usage = {
                "context_window_tokens": context_window,
                "max_input_tokens": max(0, context_window - output_limit),
                "max_output_tokens": output_limit,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "consumed_context_tokens": input_tokens + output_tokens,
                "usage_exact": True,
            }
        raw = str(response.get("response") or response.get("thinking") or "").strip()
        plan = extract_json(raw)
        calls = plan.get("calls") if isinstance(plan.get("calls"), list) else []
        clean_calls = []
        for call in calls[: max(0, int(args.max_tool_calls))]:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            call_args = (
                call.get("args")
                if isinstance(call.get("args"), dict)
                else call.get("arguments")
                if isinstance(call.get("arguments"), dict)
                else {}
            )
            if name in allowed_tools:
                clean_calls.append({"name": name, "args": call_args})
        for call in clean_calls:
            if call.get("name") not in {"camera_ptz", "focus_object"}:
                continue
            call_args = call.get("args") if isinstance(call.get("args"), dict) else {}
            call_source = str(call_args.get("source") or call_args.get("input_source") or "").strip().lower()
            if call_source not in {"wifi", "bulb"}:
                call["args"] = {**call_args, "source": ptz_default_source}
        plan = {
            "needs_tools": bool(clean_calls),
            "calls": clean_calls,
            "reason": str(plan.get("reason") or ""),
            "planner_source": "small_model",
            "planner_model": planner_model,
            "route_confidence": "model",
        }
    except Exception as exc:
        recovered_calls = recover_tool_calls_from_partial_json(raw, allowed_tools, int(args.max_tool_calls))
        for call in recovered_calls:
            if call.get("name") not in {"camera_ptz", "focus_object"}:
                continue
            call_args = call.get("args") if isinstance(call.get("args"), dict) else {}
            call_source = str(call_args.get("source") or call_args.get("input_source") or "").strip().lower()
            if call_source not in {"wifi", "bulb"}:
                call["args"] = {**call_args, "source": ptz_default_source}
        if recovered_calls:
            plan = {
                "needs_tools": True,
                "calls": recovered_calls,
                "reason": "Recovered valid tool call objects from partial AI planner JSON",
                "planner_source": "small_model_partial_json",
                "planner_model": planner_model,
                "route_confidence": "recovered",
                "planner_error": str(exc)[:220],
            }
        else:
            planner_failed = True
            plan = {
                "needs_tools": False,
                "calls": [],
                "reason": "AI tool planner failed; no fallback tool route was used",
                "planner_source": "small_model_error",
                "planner_model": planner_model,
                "route_confidence": "error",
                "planner_error": str(exc)[:220],
            }
    finally:
        if not planner_queue_managed and request_id:
            queue_after = finish_tool_planner_queue(args, request_id, failed=planner_failed)
        elif planner_queue_managed:
            queue_after = read_tool_planner_queue(args)
    plan.update(
        {
            "planner_queue_size": int(queue_snapshot.get("queued") or 0),
            "planner_queue_after": int(queue_after.get("queued") or 0),
            "planner_queue_active": queue_snapshot.get("active", []),
            "planner_queue_started_total": int(queue_snapshot.get("started_total") or 0),
            "planner_queue_completed_total": int(queue_after.get("completed_total") or 0),
            "planner_queue_failed_total": int(queue_after.get("failed_total") or 0),
            "token_usage": planner_token_usage,
        }
    )
    return plan, raw


def redact_tool_result_for_summary(value: object) -> object:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in {"data_url", "image_data_url"} and isinstance(item, str) and item.startswith("data:image/"):
                redacted[key] = f"<image data URL: {len(item)} chars>"
            else:
                redacted[key] = redact_tool_result_for_summary(item)
        return redacted
    if isinstance(value, list):
        return [redact_tool_result_for_summary(item) for item in value]
    return value


def summarize_tool_results(results: list[dict], max_chars: int) -> str:
    if not results:
        return "No external tool results are available."
    lines = []
    for index, item in enumerate(results, start=1):
        name = item.get("name", "tool")
        if item.get("error"):
            lines.append(f"{index}. {name} error: {item['error']}")
            continue
        result = item.get("result")
        safe_result = redact_tool_result_for_summary(result)
        lines.append(f"{index}. {name}: {short_text(json.dumps(safe_result, ensure_ascii=False), max_chars // max(1, len(results)))}")
    return short_text("\n".join(lines), max_chars)


def read_recent_analyses(database: str | Path, limit: int) -> list[dict]:
    db_path = Path(database)
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT id, observed_at, answer
            FROM analyses
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [{"id": row[0], "observed_at": row[1], "answer": row[2]} for row in reversed(rows)]


def source_snapshot_url(base_url: str, source: str) -> str:
    if base_url.endswith("/snapshot.jpg"):
        if source == "browser":
            return base_url[: -len("/snapshot.jpg")] + "/browser-snapshot.jpg"
        if source == "server":
            return base_url[: -len("/snapshot.jpg")] + "/server-snapshot.jpg"
        if source == "wifi":
            return base_url[: -len("/snapshot.jpg")] + "/wifi-snapshot.jpg"
        if source == "bulb":
            return base_url[: -len("/snapshot.jpg")] + "/bulb-snapshot.jpg"
    return base_url


def snapshot_url_for_tool_source(args: argparse.Namespace, source: str) -> str:
    if source == "browser":
        return str(getattr(args, "browser_snapshot_url", "") or source_snapshot_url(args.snapshot_url, source))
    if source == "server":
        return str(getattr(args, "server_snapshot_url", "") or source_snapshot_url(args.snapshot_url, source))
    if source == "wifi":
        return str(getattr(args, "wifi_snapshot_url", "") or source_snapshot_url(args.snapshot_url, source))
    if source == "bulb":
        return str(getattr(args, "bulb_snapshot_url", "") or source_snapshot_url(args.snapshot_url, source))
    return source_snapshot_url(args.snapshot_url, source)


def snapshot_data_url(data: bytes) -> str:
    if data.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    else:
        raise RuntimeError("snapshot endpoint did not return a JPEG or PNG image")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def cache_busted_url(url: str) -> str:
    separator = "&" if "?" in str(url) else "?"
    return f"{url}{separator}_={int(time.time() * 1000)}"


def current_snapshot_tool(args: argparse.Namespace, source: str, reason: str = "") -> dict:
    target_source = str(source or "").strip().lower()
    if target_source in {"", "auto", "camera", "view", "video", "lane"}:
        target_source = "wifi"
    if target_source not in {"server", "browser", "wifi", "bulb"}:
        return {"source": target_source, "error": "source must be server, browser, wifi, or bulb"}
    url = snapshot_url_for_tool_source(args, target_source)
    if not url:
        return {"source": target_source, "error": "snapshot URL is not configured"}
    timeout = max(0.5, float(getattr(args, "tool_snapshot_timeout", getattr(args, "tool_timeout", 4.0)) or 4.0))
    max_bytes = max(1, int(getattr(args, "tool_snapshot_max_bytes", 750_000) or 750_000))
    try:
        request = Request(
            cache_busted_url(url),
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            method="GET",
        )
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                return {"source": target_source, "snapshot_url": url, "error": f"snapshot endpoint returned HTTP {response.status}"}
            data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            return {"source": target_source, "snapshot_url": url, "error": "snapshot exceeded max byte limit"}
        data_url = snapshot_data_url(data)
        captured_at = time.time()
        snapshot_dir = Path(
            str(getattr(args, "audio_dir", "") or PROJECT_ROOT / "webcam-voicechat-audio")
        ) / "tool-snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        extension = ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"
        snapshot_name = f"{target_source}_snapshot_{time.time_ns()}{extension}"
        snapshot_path = snapshot_dir / snapshot_name
        temporary_path = snapshot_path.with_suffix(snapshot_path.suffix + ".tmp")
        temporary_path.write_bytes(data)
        temporary_path.replace(snapshot_path)
        stored_snapshots = sorted(
            (path for path in snapshot_dir.glob("*_snapshot_*.*") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale_path in stored_snapshots[200:]:
            stale_path.unlink(missing_ok=True)
        return {
            "source": target_source,
            "source_label": source_label(target_source),
            "reason": short_text(reason or "current snapshot requested by tool planner", 180),
            "snapshot_url": url,
            "image_url": f"/voicechat-tool-snapshot.jpg?id={snapshot_name}",
            "image_file": snapshot_name,
            "captured_at": captured_at,
            "snapshot_count": 1,
            "snapshot_images": [{"data_url": data_url, "bytes": len(data), "captured_at": captured_at, "source_url": url, "index": 1}],
            "image_data_url": data_url,
            "image_bytes": len(data),
            "direct_answer": f"Captured a current snapshot from {source_label(target_source)}.",
        }
    except Exception as exc:
        return {"source": target_source, "snapshot_url": url, "error": str(exc)[:240]}


def camera_clip_stream_url(args: argparse.Namespace, source: str) -> str:
    snapshot_url = snapshot_url_for_tool_source(args, source)
    parsed = urlparse(snapshot_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = {
        "server": "/stream.mjpg?fps=8",
        "wifi": "/wifi-stream.mjpg",
        "bulb": "/bulb-stream.mjpg",
    }.get(source, "")
    return f"{parsed.scheme}://{parsed.netloc}{path}" if path else ""


def camera_clip_audio_url(args: argparse.Namespace, source: str) -> str:
    snapshot_url = snapshot_url_for_tool_source(args, source)
    parsed = urlparse(snapshot_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    path = {"wifi": "/wifi-audio.wav", "bulb": "/bulb-audio.wav"}.get(source, "")
    return f"{parsed.scheme}://{parsed.netloc}{path}" if path else ""


def camera_clip_tool(
    args: argparse.Namespace,
    source: str,
    duration_seconds: object = None,
    reason: str = "",
    include_audio: object = False,
) -> dict:
    target_source = str(source or "").strip().lower()
    if target_source in {"", "auto", "camera", "view", "video", "lane"}:
        target_source = "wifi"
    if target_source not in {"server", "wifi", "bulb"}:
        return {"source": target_source, "error": "video clips require source server, wifi, or bulb"}
    minimum = max(0.5, float(getattr(args, "camera_clip_min_seconds", 1.0) or 1.0))
    maximum = max(minimum, float(getattr(args, "camera_clip_max_seconds", 10.0) or 10.0))
    default = float(getattr(args, "camera_clip_default_seconds", 3.0) or 3.0)
    try:
        requested_duration = float(duration_seconds) if duration_seconds is not None else default
    except (TypeError, ValueError):
        requested_duration = default
    duration = min(maximum, max(minimum, requested_duration))
    stream_url = camera_clip_stream_url(args, target_source)
    if not stream_url:
        return {"source": target_source, "error": "camera video stream URL is not configured"}
    clip_dir = Path(
        str(getattr(args, "audio_dir", "") or PROJECT_ROOT / "webcam-voicechat-audio")
    ) / "tool-clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clip_name = f"{target_source}_clip_{time.time_ns()}.mp4"
    clip_path = clip_dir / clip_name
    temporary_path = clip_path.with_suffix(".tmp.mp4")
    input_fps = 8 if target_source == "server" else 5
    audio_requested = include_audio is True or str(include_audio or "").strip().lower() in {"1", "true", "yes", "on"}
    audio_url = camera_clip_audio_url(args, target_source) if audio_requested else ""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-r", str(input_fps),
        "-i", stream_url,
    ]
    if audio_url:
        command.extend(["-i", audio_url])
    command.extend(["-t", f"{duration:.3f}", "-map", "0:v:0"])
    if audio_url:
        command.extend(["-map", "1:a:0", "-c:a", "aac", "-b:a", "64k"])
    else:
        command.append("-an")
    command.extend([
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-shortest", str(temporary_path),
    ])
    started_at = time.time()
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=duration + 20.0,
        )
        if completed.returncode != 0 or not temporary_path.exists():
            temporary_path.unlink(missing_ok=True)
            detail = short_text(completed.stderr or "FFmpeg did not create a clip", 240)
            return {"source": target_source, "duration_seconds": duration, "error": detail}
        max_bytes = max(1, int(getattr(args, "camera_clip_max_bytes", 12_000_000) or 12_000_000))
        clip_bytes = temporary_path.stat().st_size
        if clip_bytes <= 0 or clip_bytes > max_bytes:
            temporary_path.unlink(missing_ok=True)
            return {
                "source": target_source,
                "duration_seconds": duration,
                "error": "camera clip is empty" if clip_bytes <= 0 else "camera clip exceeded byte limit",
            }
        temporary_path.replace(clip_path)
        stored_clips = sorted(
            (path for path in clip_dir.glob("*_clip_*.mp4") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale_path in stored_clips[80:]:
            stale_path.unlink(missing_ok=True)
        captured_at = time.time()
        return {
            "source": target_source,
            "source_label": source_label(target_source),
            "reason": short_text(reason or "short camera clip requested by tool planner", 180),
            "requested_duration_seconds": requested_duration,
            "duration_seconds": round(duration, 3),
            "recording_seconds": round(captured_at - started_at, 3),
            "audio_requested": audio_requested,
            "audio_included": bool(audio_url),
            "captured_at": captured_at,
            "video_count": 1,
            "video_bytes": clip_bytes,
            "video_file": clip_name,
            "video_path": str(clip_path),
            "video_url": f"/voicechat-tool-clip.mp4?id={clip_name}",
            "direct_answer": f"Recorded a {duration:g}-second clip from {source_label(target_source)}.",
        }
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        return {"source": target_source, "duration_seconds": duration, "error": str(exc)[:240]}


def source_label(source: str) -> str:
    if source == "browser":
        return "Browser laptop input"
    if source == "server":
        return "Server USB input"
    if source == "wifi":
        return "Wi-Fi camera input"
    if source == "bulb":
        return "Light bulb camera input"
    return source or "Unknown input"


def request_environment_wake(args: argparse.Namespace, source: str, reason: str) -> None:
    if source not in {"server", "browser", "wifi", "bulb"}:
        return
    now = time.time()
    wake_path = Path(args.environment_wake_json)
    wake_state = read_json(wake_path)
    if not isinstance(wake_state, dict):
        wake_state = {}
    sources = wake_state.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    sources[source] = {
        "source_id": source,
        "source_label": source_label(source),
        "requested_at": now,
        "reason": reason,
    }
    wake_state.update(
        {
            "status": "wake_requested",
            "updated_at": now,
            "last_source": source,
            "sources": sources,
        }
    )
    write_json(wake_path, wake_state)


@contextlib.contextmanager
def cosmos_lock(args: argparse.Namespace):
    lock_path = Path(args.cosmos_lock_file)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def visual_update_requested(text: str) -> bool:
    lower = normalize_text(text)
    if not lower:
        return False
    if any(marker in lower for marker in VISUAL_CONTEXT_MARKERS):
        return True
    visual_words = ("see", "look", "watch", "view", "observe", "camera", "video", "scene", "environment", "room", "outside")
    current_words = ("now", "current", "currently", "there", "around", "happening", "going on", "looks like", "look like")
    return any(word in lower for word in visual_words) and any(word in lower for word in current_words)


def environment_scan_requested(text: str) -> bool:
    lower = normalize_text(text)
    if not lower:
        return False
    markers = (
        "scan the environment",
        "scan environment",
        "scan the room",
        "scan room",
        "scan the scene",
        "scan scene",
        "look around",
        "survey the environment",
        "survey environment",
        "sweep the camera",
        "visual grid",
        "image grid",
        "grid scan",
    )
    return any(marker in lower for marker in markers)


def visual_context_updated_at(env_state: dict) -> float:
    cosmos = env_state.get("cosmos_analysis") if isinstance(env_state.get("cosmos_analysis"), dict) else {}
    cosmos_updated_at = finite_timestamp(cosmos.get("updated_at"))
    if cosmos_updated_at > 0:
        return cosmos_updated_at
    return max(finite_timestamp(env_state.get("previous_updated_at")), finite_timestamp(env_state.get("updated_at")))


def visual_context_is_fresh(args: argparse.Namespace, env_state: dict, source: str) -> bool:
    if not env_state:
        return False
    env_source = str(env_state.get("source_id") or source)
    if source and env_source not in {"", source}:
        return False
    updated_at = visual_context_updated_at(env_state)
    if updated_at <= 0:
        return False
    return time.time() - updated_at <= max(0.0, float(args.force_visual_update_max_age_seconds))


def visual_context_age(args: argparse.Namespace, env_state: dict) -> float | None:
    updated_at = visual_context_updated_at(env_state)
    if updated_at <= 0:
        return None
    return max(0.0, time.time() - updated_at)


def cache_state_from_analysis(source: str, analysis: dict) -> dict:
    if not analysis:
        return {}
    state = apply_forced_visual_context(
        {
            "status": "cached_visual_for_speech",
            "phase": "speech_visual_cache",
            "source_id": source,
            "source_label": source_label(source),
        },
        source,
        analysis,
    )
    state["status"] = "cached_visual_for_speech"
    state["phase"] = "speech_visual_cache"
    return state


def read_visual_cache_state(args: argparse.Namespace, source: str) -> dict:
    cache = read_json(args.visual_cache_json)
    source_cache = cache.get(source) if isinstance(cache.get(source), dict) else {}
    if not source_cache:
        return {}
    analysis = source_cache.get("cosmos_analysis") if isinstance(source_cache.get("cosmos_analysis"), dict) else {}
    if not analysis:
        return {}
    state = cache_state_from_analysis(source, analysis)
    state["cache_updated_at"] = source_cache.get("updated_at") or visual_context_updated_at(state)
    state["cache_source"] = "speech_forced_visual_cache"
    return state


def write_visual_cache_state(args: argparse.Namespace, source: str, analysis: dict) -> None:
    if not source or not analysis:
        return
    cache = read_json(args.visual_cache_json)
    if not isinstance(cache, dict):
        cache = {}
    cache[source] = {
        "source_id": source,
        "source_label": source_label(source),
        "updated_at": visual_context_updated_at(cache_state_from_analysis(source, analysis)) or time.time(),
        "cosmos_analysis": analysis,
    }
    write_json(args.visual_cache_json, cache)


def choose_newest_visual_context(args: argparse.Namespace, env_state: dict, source: str) -> tuple[dict, dict]:
    cache_state = read_visual_cache_state(args, source)
    env_updated_at = visual_context_updated_at(env_state)
    cache_updated_at = visual_context_updated_at(cache_state)
    if cache_state and cache_updated_at > env_updated_at:
        merged = {**env_state, **cache_state}
        return merged, {
            "selected": "speech_forced_cache",
            "environment_updated_at": env_updated_at,
            "cache_updated_at": cache_updated_at,
            "selected_updated_at": cache_updated_at,
            "selected_age_seconds": round(max(0.0, time.time() - cache_updated_at), 2),
        }
    return env_state, {
        "selected": "environment_agent",
        "environment_updated_at": env_updated_at,
        "cache_updated_at": cache_updated_at,
        "selected_updated_at": env_updated_at,
        "selected_age_seconds": round(max(0.0, time.time() - env_updated_at), 2) if env_updated_at > 0 else None,
    }


def wait_for_forced_cosmos(args: argparse.Namespace, cycle_id: str) -> dict:
    deadline = time.time() + max(1.0, float(args.force_visual_update_timeout))
    last: dict = {}
    while time.time() < deadline:
        analysis = read_json(args.analysis_json)
        last = analysis
        if str(analysis.get("trigger_id") or "") == cycle_id:
            status = str(analysis.get("status") or "")
            if status == "error":
                raise RuntimeError(str(analysis.get("error") or "Cosmos analysis failed"))
            if analysis.get("answer") and status not in {"capturing", "analyzing", "waiting", "loading"}:
                return analysis
        time.sleep(max(0.1, float(args.force_visual_update_poll_seconds)))
    raise TimeoutError(f"Timed out waiting for Cosmos trigger {cycle_id}; last status={last.get('status')}")


def force_cosmos_update(args: argparse.Namespace, source: str, reason: str) -> dict:
    cycle_id = f"speech-{source or 'unknown'}-{int(time.time() * 1000)}"
    now = time.time()
    trigger = {
        "request_id": cycle_id,
        "source_id": source,
        "source_label": source_label(source),
        "snapshot_url": source_snapshot_url(args.snapshot_url, source),
        "requested_at": now,
        "updated_at": now,
        "reason": reason,
    }
    with cosmos_lock(args):
        write_json(args.cosmos_trigger_json, trigger)
        return wait_for_forced_cosmos(args, cycle_id)


def apply_forced_visual_context(env_state: dict, source: str, analysis: dict) -> dict:
    answer = str(analysis.get("answer") or "").strip()
    merged = {**env_state}
    merged.update(
        {
            "status": "fresh_visual_for_speech",
            "phase": "speech_forced_cosmos",
            "source_id": source,
            "source_label": source_label(source),
            "cycle_id": analysis.get("trigger_id") or merged.get("cycle_id", ""),
            "cosmos_analysis": analysis,
        }
    )
    if answer:
        merged["visual_state"] = answer
        merged["summary"] = answer
        merged["activity"] = answer
    return merged


def capture_screenshots(args: argparse.Namespace, source: str = "") -> tuple[list[Path], list[str], str]:
    screenshot_dir = Path(args.screenshot_dir)
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    images: list[str] = []
    for index in range(max(0, args.screenshot_count)):
        try:
            request = Request(source_snapshot_url(args.snapshot_url, source), headers={"Cache-Control": "no-cache"})
            with urlopen(request, timeout=5) as response:
                payload = response.read()
            if not payload.startswith(b"\xff\xd8"):
                raise RuntimeError("snapshot endpoint did not return a JPEG")
            path = screenshot_dir / f"speech_context_{time.time_ns()}_{index}.jpg"
            path.write_bytes(payload)
            paths.append(path)
            if args.send_screenshots and args.send_images_to_nemotron:
                images.append(base64.b64encode(payload).decode("ascii"))
        except Exception:
            continue
        if index < args.screenshot_count - 1:
            time.sleep(args.screenshot_delay)

    for stale_path in sorted(screenshot_dir.glob("speech_context_*.jpg"))[:-args.screenshot_keep]:
        stale_path.unlink(missing_ok=True)

    if paths and images:
        return paths, images, "attached"
    if paths:
        return paths, [], "captured"
    return [], [], "none"


def environment_state_path(args: argparse.Namespace, source: str) -> str:
    if source == "server":
        return args.server_agent_state_json
    if source == "browser":
        return args.browser_agent_state_json
    if source == "wifi":
        return args.wifi_agent_state_json
    if source == "bulb":
        return args.bulb_agent_state_json
    return ""


def environment_database_path(args: argparse.Namespace, source: str) -> str:
    if source == "server":
        return args.server_database
    if source == "browser":
        return args.browser_database
    if source == "wifi":
        return args.wifi_database
    if source == "bulb":
        return args.bulb_database
    return args.database


def environment_visual_context(env_state: dict) -> str:
    if str(env_state.get("status") or "").lower() == "disabled":
        return ""
    cosmos = env_state.get("cosmos_analysis") if isinstance(env_state.get("cosmos_analysis"), dict) else {}
    parts = [
        str(env_state.get("summary") or ""),
        str(env_state.get("visual_state") or ""),
        str(env_state.get("activity") or ""),
        str(cosmos.get("answer") or ""),
    ]
    text = "\n".join(part for part in parts if part.strip())
    return text or "No visual context is available yet."


def environment_alert_context(env_state: dict) -> dict:
    history_analysis = env_state.get("history_analysis") if isinstance(env_state.get("history_analysis"), dict) else {}
    description = (
        env_state.get("change_assessment")
        or history_analysis.get("historical_summary")
        or env_state.get("message")
        or "No source-local environment change signal is available yet."
    )
    return {
        "status": env_state.get("status", ""),
        "risk": env_state.get("risk") or history_analysis.get("risk") or "none",
        "description": str(description),
        "source_id": env_state.get("source_id", ""),
        "cycle_id": env_state.get("cycle_id", ""),
    }


def build_prompt(args: argparse.Namespace, segment: dict, env_state: dict, alert: dict, history: list[dict], screenshot_paths: list[Path]) -> str:
    latest_visual = environment_visual_context(env_state)
    alert_text = str(alert.get("description") or alert.get("message") or "No change alert is active.")
    history = history[-max(0, int(args.voice_context_history_limit)) :]
    history_lines = "\n".join(
        f"- {item['id']}: {short_text(item['answer'], args.voice_history_item_chars)}" for item in history
    ) or "- none"
    screenshot_line = f"{len(screenshot_paths)} recent screenshot(s) captured locally." if screenshot_paths else "none"
    return f"""You are Nemotron, a concise voice agent in a live monitoring app.
Answer the user's speech directly. Use retrieved context only when it helps.
Do not volunteer scene summaries or alerts unless asked.
Reply in one complete sentence under 18 words.

Return compact JSON only:
{{"response": "spoken response text"}}

Speech ({segment.get('source', 'unknown')} mic):
{segment.get('text', '')}

Latest visual context:
{short_text(latest_visual, args.voice_visual_context_chars)}

Change/risk:
{short_text(alert_text, args.voice_alert_context_chars)}

Recent source history:
{history_lines}

Screenshots:
{screenshot_line}
"""


def request_json_model(
    args: argparse.Namespace,
    model: str,
    prompt: str,
    images: list[str] | None = None,
    num_predict: int | None = None,
    timeout: float | None = None,
    component: str = "voice",
    source: str = "",
) -> tuple[dict, str, str]:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            "temperature": float(args.nemotron_temperature),
            "num_predict": max(24, int(num_predict or args.nemotron_num_predict)),
            "num_ctx": max(1024, int(args.nemotron_num_ctx)),
        },
        "keep_alive": args.nemotron_keep_alive,
    }
    image_mode = "none"
    if images and args.send_images_to_nemotron:
        payload["images"] = images
        image_mode = "attached"

    try:
        response = ollama_json(
            args.ollama_url,
            "/api/generate",
            payload,
            timeout=timeout or args.timeout,
            component=component,
            source=source,
        )
    except RuntimeError as exc:
        message = str(exc).lower()
        if images and any(token in message for token in ("image", "vision", "multimodal", "does not support")):
            payload.pop("images", None)
            response = ollama_json(
                args.ollama_url,
                "/api/generate",
                payload,
                timeout=timeout or args.timeout,
                component=component,
                source=source,
            )
            image_mode = "text_fallback"
        else:
            raise
    raw = str(response.get("response") or response.get("thinking") or "").strip()
    try:
        return extract_json(raw), raw, image_mode
    except Exception:
        return {}, raw, image_mode


def request_nemotron(args: argparse.Namespace, model: str, prompt: str, images: list[str]) -> tuple[str, str, str]:
    parsed, raw, image_mode = request_json_model(args, model, prompt, images)
    text = " ".join(str(parsed.get("response") or "").split())
    if not text:
        text = " ".join(raw.split())
    if not text:
        text = "I heard you, but I do not have enough context to respond yet."
    return text, raw, image_mode


def build_candidate_prompt(
    args: argparse.Namespace,
    segment: dict,
    env_state: dict,
    alert: dict,
    history: list[dict],
    screenshot_paths: list[Path],
    tool_summary: str,
    candidate_count: int,
) -> str:
    latest_visual = environment_visual_context(env_state)
    alert_text = str(alert.get("description") or alert.get("message") or "No change alert is active.")
    history = history[-max(0, int(args.voice_context_history_limit)) :]
    history_lines = "\n".join(
        f"- {item['id']}: {short_text(item['answer'], args.voice_history_item_chars)}" for item in history
    ) or "- none"
    screenshot_line = f"{len(screenshot_paths)} recent screenshot(s) captured locally." if screenshot_paths else "none"
    max_words = max(18, int(args.response_max_words))
    return f"""You are Nemotron, a voice agent in a live monitoring app.
Answer the user's speech directly. Use retrieved context and tool results when helpful.
Do not volunteer scene summaries or alerts unless asked.

Speech ({segment.get('source', 'unknown')} mic):
{segment.get('text', '')}

Latest visual context:
{short_text(latest_visual, args.voice_visual_context_chars)}

Change/risk:
{short_text(alert_text, args.voice_alert_context_chars)}

Recent source history:
{history_lines}

Screenshots:
{screenshot_line}

Tool results:
{tool_summary or "No external tool results are available."}

Generate {max(1, candidate_count)} different candidate responses. Use tool results when relevant.
For simple questions, candidates may be brief. For complex or tool-backed tasks, use up to {max_words} words.
Return compact JSON only:
{{"candidates":[{{"id":"A","response":"..."}}]}}
"""


def parse_candidates(parsed: dict, raw: str) -> list[dict]:
    candidates = parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else []
    clean = []
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("response") or item.get("text") or "").split())
        if not text:
            continue
        clean.append({"id": str(item.get("id") or chr(ord("A") + len(clean))), "response": text})
    if clean:
        return clean
    fallback = ""
    if isinstance(parsed, dict):
        fallback = str(parsed.get("response") or "")
    if not fallback:
        fallback = raw
    fallback = " ".join(fallback.split())
    return [{"id": "A", "response": fallback or "I heard you, but I need a little more context to answer."}]


def timeout_fallback_response(segment: dict, error: Exception) -> str:
    text = " ".join(str(segment.get("text") or "").split())
    if text:
        return f"I heard: {text}. The local response model was too slow, so I am keeping this interaction alive."
    return f"The local response model was too slow, so I am keeping this interaction alive. Error: {short_text(str(error), 120)}"


def generate_response_candidates(
    args: argparse.Namespace,
    model: str,
    prompt: str,
    images: list[str],
    segment: dict,
    num_predict: int,
) -> tuple[list[dict], str, str, str, dict]:
    timeout = max(3.0, float(args.response_timeout))
    try:
        parsed, raw, image_mode = request_json_model(
            args,
            model,
            prompt,
            images,
            num_predict=num_predict,
            timeout=timeout,
            component="candidate_generation",
            source=str(segment.get("source") or ""),
        )
        return parse_candidates(parsed, raw), raw, image_mode, model, {"model": model, "timeout_seconds": timeout}
    except Exception as exc:
        fallback_model = str(args.response_fallback_model or "").strip()
        if fallback_model and fallback_model != model:
            try:
                parsed, raw, image_mode = request_json_model(
                    args,
                    fallback_model,
                    prompt,
                    images,
                    num_predict=num_predict,
                    timeout=timeout,
                    component="candidate_generation",
                    source=str(segment.get("source") or ""),
                )
                return (
                    parse_candidates(parsed, raw),
                    raw,
                    image_mode,
                    fallback_model,
                    {
                        "model": fallback_model,
                        "primary_model": model,
                        "primary_error": short_text(str(exc), 260),
                        "timeout_seconds": timeout,
                    },
                )
            except Exception as fallback_exc:
                return (
                    [{"id": "A", "response": timeout_fallback_response(segment, fallback_exc)}],
                    "",
                    "none",
                    fallback_model,
                    {
                        "model": fallback_model,
                        "primary_model": model,
                        "primary_error": short_text(str(exc), 260),
                        "fallback_error": short_text(str(fallback_exc), 260),
                        "timeout_seconds": timeout,
                    },
                )
        return (
            [{"id": "A", "response": timeout_fallback_response(segment, exc)}],
            "",
            "none",
            model,
            {"model": model, "error": short_text(str(exc), 260), "timeout_seconds": timeout},
        )


def score_candidates(
    args: argparse.Namespace,
    model: str,
    segment: dict,
    candidates: list[dict],
    tool_summary: str,
    env_state: dict,
) -> tuple[dict, dict, str]:
    if len(candidates) <= 1:
        return candidates[0], {"best_id": candidates[0]["id"], "scores": [{"id": candidates[0]["id"], "score": 1.0}], "reason": "single candidate"}, ""
    prompt = f"""Score candidate voice responses for a real-time monitoring assistant.
Prefer answers that directly answer the speech, use tool results accurately, avoid hallucinating, and are concise enough to speak.

Speech:
{segment.get('text', '')}

Relevant visual context:
{short_text(environment_visual_context(env_state), 300)}

Tool results:
{short_text(tool_summary, 1200)}

Candidates:
{json.dumps(candidates, ensure_ascii=False)}

Return compact JSON only:
{{"best_id":"A","scores":[{{"id":"A","score":0.0,"reason":"..."}}],"reason":"..."}}
"""
    parsed, raw, _image_mode = request_json_model(
        args,
        model,
        prompt,
        [],
        num_predict=220,
        timeout=min(args.timeout, 60),
        component="response_scoring",
        source=str(segment.get("source") or ""),
    )
    best_id = str(parsed.get("best_id") or "").strip()
    selected = next((item for item in candidates if item["id"] == best_id), candidates[0])
    return selected, parsed or {"best_id": selected["id"], "reason": "scorer returned no usable JSON"}, raw


def run_command(command: list[str], timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def synthesize_flite(text: str, audio_path: Path) -> str:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for flite TTS fallback")
    with tempfile.TemporaryDirectory(prefix="nemotron-voice-flite-") as tmp:
        text_path = Path(tmp) / "tts.txt"
        text_path.write_text(tts_safe_text(text, 0, 0), encoding="utf-8")
        run_command(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"flite=textfile={text_path}",
                "-ac",
                "1",
                "-ar",
                "22050",
                str(audio_path),
            ],
            timeout=45,
        )
    return "flite"


def wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            frame_rate = handle.getframerate()
            if frame_rate <= 0:
                return 0.0
            return float(handle.getnframes()) / float(frame_rate)
    except Exception:
        return 0.0


def concat_wavs(input_paths: list[Path], output_path: Path, pause_seconds: float = 0.12) -> Path:
    existing_paths = [Path(path) for path in input_paths if Path(path).exists()]
    if not existing_paths:
        raise RuntimeError("No TTS audio chunks were produced")
    if len(existing_paths) == 1:
        shutil.copyfile(existing_paths[0], output_path)
        return output_path

    with wave.open(str(existing_paths[0]), "rb") as first:
        params = first.getparams()
        sample_rate = first.getframerate()
        channels = first.getnchannels()
        sample_width = first.getsampwidth()
        first_frames = first.readframes(first.getnframes())

    pause_frames = max(0, int(float(pause_seconds or 0.0) * sample_rate))
    pause = b"\0" * pause_frames * channels * sample_width
    with wave.open(str(output_path), "wb") as output:
        output.setparams(params)
        output.writeframes(first_frames)
        for path in existing_paths[1:]:
            with wave.open(str(path), "rb") as handle:
                if (
                    handle.getframerate() != sample_rate
                    or handle.getnchannels() != channels
                    or handle.getsampwidth() != sample_width
                ):
                    raise RuntimeError(f"TTS chunk format mismatch: {path}")
                if pause:
                    output.writeframes(pause)
                output.writeframes(handle.readframes(handle.getnframes()))
    return output_path


def write_tensor_wav(audio_tensor, audio_len, output_path: Path, sample_rate: int) -> Path:
    try:
        import torch
    except Exception as exc:
        raise RuntimeError(f"torch is required to save Magpie audio: {exc}") from exc
    if audio_tensor is None:
        raise RuntimeError("Magpie direct TTS returned no audio tensor")
    audio = audio_tensor.detach().float().cpu()
    if audio.ndim == 2:
        audio = audio[0]
    elif audio.ndim != 1:
        audio = audio.reshape(-1)
    try:
        length = int(audio_len.detach().cpu().flatten()[0].item())
    except Exception:
        length = int(audio.numel())
    if length > 0:
        audio = audio[: min(length, int(audio.numel()))]
    if audio.numel() <= 0:
        raise RuntimeError("Magpie direct TTS produced empty audio")
    pcm = (
        torch.clamp(audio, -1.0, 1.0)
        .mul(32767.0)
        .to(torch.int16)
        .numpy()
        .tobytes()
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate or 22050))
        handle.writeframes(pcm)
    return output_path


def expected_tts_duration_seconds(text: str, playback_speed: float = 1.0) -> float:
    words = re.findall(r"[A-Za-z0-9']+", str(text or ""))
    if not words:
        return 0.0
    speed = min(2.0, max(0.5, float(playback_speed or 1.0)))
    word_count = len(words)
    words_per_second = 4.2 if word_count <= 8 else 3.4
    return max(0.45, (word_count / words_per_second) / speed)


def max_tts_duration_seconds(text: str, playback_speed: float) -> float:
    words = re.findall(r"[A-Za-z0-9']+", str(text or ""))
    if not words:
        return 0.0
    speed = min(2.0, max(0.5, float(playback_speed or 1.0)))
    return max(1.8, (len(words) / 1.25) / speed + 1.6)


def ensure_complete_tts_audio(audio_path: Path, text: str, backend: str, playback_speed: float = 1.0) -> None:
    expected_duration = expected_tts_duration_seconds(text, playback_speed)
    if expected_duration <= 0:
        return
    duration = wav_duration(audio_path)
    minimum_duration = max(0.35, expected_duration * 0.55)
    if duration > 0 and duration < minimum_duration:
        raise RuntimeError(
            f"{backend} output appears truncated: {duration:.2f}s for {len(text.split())} words"
        )


def speed_audio(input_path: Path, output_path: Path, speed: float) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for TTS speed adjustment")
    speed = min(2.0, max(0.5, speed))
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-filter:a",
            f"atempo={speed:.3f}",
            str(output_path),
        ],
        timeout=45,
    )


def trim_silence_audio(input_path: Path, output_path: Path) -> Path:
    if not shutil.which("ffmpeg"):
        return input_path
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-af",
            (
                "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-70dB,"
                "areverse,"
                "silenceremove=start_periods=1:start_duration=1.00:start_threshold=-70dB,"
                "areverse,"
                "apad=pad_dur=0.35"
            ),
            str(output_path),
        ],
        timeout=45,
    )
    if output_path.exists() and wav_duration(output_path) > 0.2:
        return output_path
    return input_path


def cap_audio_duration(input_path: Path, output_path: Path, max_seconds: float) -> Path:
    duration = wav_duration(input_path)
    cap_threshold = max(max_seconds * 2.2, max_seconds + 6.0)
    if max_seconds <= 0 or duration <= cap_threshold:
        return input_path
    if not shutil.which("ffmpeg"):
        return input_path
    run_command(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-af",
            f"atrim=0:{max_seconds:.3f},asetpts=N/SR/TB",
            str(output_path),
        ],
        timeout=45,
    )
    if output_path.exists() and wav_duration(output_path) > 0.2:
        return output_path
    return input_path


MAGPIE_VOICE_SPEAKER_INDEX = {
    "default": 0,
    "sofia": 1,
    "sophia": 1,
}


def magpie_speaker_index(args: argparse.Namespace) -> int:
    voice = str(getattr(args, "magpie_voice", "") or "").strip().lower()
    if voice:
        return int(MAGPIE_VOICE_SPEAKER_INDEX.get(voice, int(getattr(args, "magpie_speaker_index", 0) or 0)))
    return int(getattr(args, "magpie_speaker_index", 0) or 0)


class MagpieSynthesizer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.model = None
        self.runner = None
        self.model_name = ""
        self.disabled_reason = ""
        self.cache: dict[str, Path] = {}
        self.torch = None

    def ensure_context_audio(self) -> Path:
        if self.args.context_audio_path:
            context_path = Path(self.args.context_audio_path)
            if context_path.exists():
                return context_path
        context_path = Path(self.args.audio_dir) / "magpie_context.wav"
        if context_path.exists():
            return context_path
        context_path.parent.mkdir(parents=True, exist_ok=True)
        synthesize_flite(self.args.context_text, context_path)
        return context_path

    def ensure_loaded(self) -> None:
        if self.runner is not None:
            return
        if self.disabled_reason:
            raise RuntimeError(self.disabled_reason)
        model_path = Path(self.args.tts_model_path)
        codec_path = Path(self.args.tts_codec_path)
        if not model_path.exists():
            raise RuntimeError(f"Magpie TTS model not found: {model_path}")
        if not codec_path.exists():
            raise RuntimeError(f"Magpie codec model not found: {codec_path}")
        nemo_repo = Path(self.args.nemo_repo)
        if nemo_repo.exists():
            sys.path.insert(0, str(nemo_repo))
            os.chdir(nemo_repo)
        try:
            import torch
            from nemo.collections.tts.modules.magpietts_inference.inference import MagpieInferenceConfig, MagpieInferenceRunner
            from nemo.collections.tts.models.magpietts import ModelInferenceParameters
            from nemo.collections.tts.modules.magpietts_inference.utils import ModelLoadConfig, load_magpie_model

            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
            if hasattr(torch, "set_float32_matmul_precision"):
                torch.set_float32_matmul_precision("high")
            model, self.model_name = load_magpie_model(
                ModelLoadConfig(nemo_file=str(model_path), codecmodel_path=str(codec_path)),
                device="cuda",
            )
            model.eval()
            model.inference_parameters.max_decoder_steps = max(32, int(self.args.magpie_max_decoder_steps))
            model.inference_parameters.temperature = float(self.args.magpie_temperature)
            model.inference_parameters.topk = max(1, int(self.args.magpie_topk))
            self.model = model
            self.torch = torch
            inference_parameters = ModelInferenceParameters(
                max_decoder_steps=max(32, int(self.args.magpie_max_decoder_steps)),
                temperature=float(self.args.magpie_temperature),
                topk=max(1, int(self.args.magpie_topk)),
                use_LT_kv_cache=True,
            )
            config = MagpieInferenceConfig(
                batch_size=1,
                model_inference_parameters=inference_parameters,
                maskgit_n_steps=max(1, int(self.args.magpie_maskgit_steps)),
            )
            self.runner = MagpieInferenceRunner(model, config)
        except Exception as exc:
            self.disabled_reason = f"Magpie TTS load failed: {exc}"
            raise RuntimeError(self.disabled_reason) from exc

    def synthesize(self, text: str, audio_id: str) -> Path:
        self.ensure_loaded()
        assert self.runner is not None
        audio_dir = Path(self.args.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        spoken_text = tts_safe_text(text, self.args.tts_max_chars, self.args.tts_max_words)
        cache_key = f"{str(getattr(self.args, 'magpie_voice', '') or '').strip().lower()}:{magpie_speaker_index(self.args)}:{normalize_text(spoken_text)}"
        cached_path = self.cache.get(cache_key)
        if cached_path and cached_path.exists():
            return cached_path
        if bool(self.args.magpie_direct_tts):
            final_path = self._synthesize_direct(spoken_text, audio_id)
            self.cache[cache_key] = final_path
            return final_path

        chunks = split_tts_chunks(
            spoken_text,
            int(self.args.magpie_chunk_max_words),
            int(self.args.magpie_chunk_max_chars),
        )
        if len(chunks) <= 1:
            final_path = self._synthesize_single(spoken_text, audio_id)
        else:
            chunk_paths = [
                self._synthesize_single(chunk, f"{audio_id}_part{index}")
                for index, chunk in enumerate(chunks, start=1)
            ]
            final_path = audio_dir / f"voice_{audio_id}.wav"
            concat_wavs(
                chunk_paths,
                final_path,
                max(0.0, float(self.args.magpie_chunk_pause_seconds)),
            )
            ensure_complete_tts_audio(
                final_path,
                spoken_text,
                "Magpie TTS",
                float(self.args.tts_playback_speed),
            )
        self.cache[cache_key] = final_path
        return final_path

    def _synthesize_direct(self, spoken_text: str, audio_id: str) -> Path:
        if self.model is None:
            raise RuntimeError("Magpie direct TTS is unavailable because the model is not loaded")
        audio_dir = Path(self.args.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        final_path = audio_dir / f"voice_{audio_id}.wav"
        speaker_index = magpie_speaker_index(self.args)
        inference_context = self.torch.inference_mode() if self.torch is not None else contextlib.nullcontext()
        with inference_context:
            audio, audio_len = self.model.do_tts(
                transcript=spoken_text,
                language="en",
                apply_TN=True,
                use_cfg=bool(self.args.magpie_use_cfg),
                speaker_index=speaker_index,
            )
        sample_rate = int(getattr(self.model, "output_sample_rate", 22050) or 22050)
        write_tensor_wav(audio, audio_len, final_path, sample_rate)
        ensure_complete_tts_audio(final_path, spoken_text, "Magpie direct TTS", 1.0)
        return final_path

    def _synthesize_single(self, spoken_text: str, audio_id: str) -> Path:
        assert self.runner is not None
        audio_dir = Path(self.args.audio_dir)
        audio_dir.mkdir(parents=True, exist_ok=True)
        spoken_text = re.sub(r"\s+", " ", str(spoken_text or "")).strip()
        cache_key = f"magpie-chunk:{normalize_text(spoken_text)}"
        cached_path = self.cache.get(cache_key)
        if cached_path and cached_path.exists():
            return cached_path
        context_path = self.ensure_context_audio()
        output_dir = audio_dir / f"magpie_{audio_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = output_dir / "manifest.jsonl"
        context_rel = os.path.relpath(context_path, audio_dir)
        context_duration = max(1.0, float(self.args.magpie_context_duration))
        record = {
            "audio_filepath": context_rel,
            "duration": context_duration,
            "text": spoken_text,
            "context_audio_filepath": context_rel,
            "context_audio_duration": context_duration,
            "context_text": self.args.context_text,
            "language": "en",
        }
        manifest_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        dataset_meta = {"live": {"manifest_path": str(manifest_path), "audio_dir": str(audio_dir)}}
        dataset = self.runner.create_dataset(
            dataset_meta,
            context_duration_min=context_duration,
            context_duration_max=context_duration,
        )
        inference_context = self.torch.inference_mode() if self.torch is not None else contextlib.nullcontext()
        with inference_context:
            _rtf, audio_paths, _code_paths = self.runner.run_inference_on_dataset(
                dataset,
                str(output_dir),
                manifest_records=[record],
                audio_base_dir=str(audio_dir),
                save_context_audio=False,
                save_predicted_codes=False,
            )
        if not audio_paths:
            raise RuntimeError("Magpie TTS did not produce audio")
        generated_path = Path(audio_paths[0])
        trimmed_path = trim_silence_audio(generated_path, audio_dir / f"voice_{audio_id}_trimmed.wav")
        if abs(float(self.args.tts_playback_speed) - 1.0) < 0.01:
            capped_path = cap_audio_duration(
                trimmed_path,
                audio_dir / f"voice_{audio_id}_capped.wav",
                max_tts_duration_seconds(spoken_text, 1.0),
            )
            ensure_complete_tts_audio(capped_path, spoken_text, "Magpie TTS", 1.0)
            self.cache[cache_key] = capped_path
            return capped_path
        final_path = audio_dir / f"voice_{audio_id}.wav"
        speed_audio(trimmed_path, final_path, float(self.args.tts_playback_speed))
        capped_path = cap_audio_duration(
            final_path,
            audio_dir / f"voice_{audio_id}_capped.wav",
            max_tts_duration_seconds(spoken_text, float(self.args.tts_playback_speed)),
        )
        ensure_complete_tts_audio(
            capped_path,
            spoken_text,
            "Magpie TTS",
            float(self.args.tts_playback_speed),
        )
        self.cache[cache_key] = capped_path
        return capped_path


def synthesize_response(args: argparse.Namespace, magpie: MagpieSynthesizer, text: str, audio_id: str) -> tuple[Path | None, str, str]:
    audio_dir = Path(args.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    if args.tts_backend == "none":
        return None, "none", ""
    if args.tts_backend == "magpie":
        try:
            return magpie.synthesize(text, audio_id), magpie.model_name or "magpie_tts_multilingual_357m", ""
        except Exception as exc:
            fallback_path = audio_dir / f"voice_{audio_id}_flite.wav"
            try:
                synthesize_flite(text, fallback_path)
                return fallback_path, "flite_fallback", str(exc)
            except Exception as fallback_exc:
                raise RuntimeError(f"Magpie TTS failed: {exc}; flite fallback failed: {fallback_exc}") from fallback_exc
    audio_path = audio_dir / f"voice_{audio_id}.wav"
    synthesize_flite(text, audio_path)
    return audio_path, "flite", ""


def read_output_target(args: argparse.Namespace, source: str = "") -> str:
    if args.output_target_mode == "browser":
        return "browser"
    if args.output_target_mode == "server":
        return "server"
    if args.output_target_mode in {"wifi_camera", "bulb_camera"}:
        return str(args.output_target_mode)
    if args.output_target_mode == "auto":
        if source == "wifi":
            return "wifi_camera"
        if source == "bulb":
            return "bulb_camera"
        return "server" if source == "server" else "browser"
    data = read_json(args.voice_output_target_json)
    target = str(data.get("target") or "browser")
    return target if target in {"browser", "server", "wifi_camera", "bulb_camera"} else "browser"


def recent_conversation(path: str | Path, limit: int) -> list[dict]:
    data = read_json(path)
    items = data.get("conversation")
    if not isinstance(items, list):
        return []
    clean_items = []
    for item in items[-limit * 2 :]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        text = str(item.get("text") or "").strip()
        if role not in {"user", "assistant"} or not text:
            continue
        clean_items.append(
            {
                "role": role,
                "text": text,
                "source": str(item.get("source") or ""),
                "status": str(item.get("status") or "complete"),
                "updated_at": float(item.get("updated_at") or time.time()),
            }
        )
    return clean_items[-limit * 2 :]


def user_turn(segment: dict) -> dict:
    return {
        "role": "user",
        "text": str(segment.get("text") or ""),
        "source": str(segment.get("source") or "unknown"),
        "status": "complete",
        "updated_at": float(segment.get("updated_at") or time.time()),
    }


def assistant_turn(text: str, status: str = "complete") -> dict:
    return {
        "role": "assistant",
        "text": text,
        "source": "nemotron",
        "status": status,
        "updated_at": time.time(),
    }


def context_component(
    status: str,
    message: str,
    payload: dict | None = None,
) -> dict:
    return {"status": status, "message": message, "payload": payload or {}}


def voice_context_map(
    active: str = "",
    completed: set[str] | None = None,
    payloads: dict[str, dict] | None = None,
) -> dict[str, dict]:
    completed = completed or set()
    payloads = payloads or {}
    messages = {
        "context_visual": "Checked source-local visual context availability.",
        "context_alert": "Read source-local change/risk context if enabled.",
        "context_history": "Loaded this source's observation history if enabled.",
        "context_screenshots": "Applied the speech-side visual capture policy.",
        "context_prompt": "Assembled the final prompt from speech, active context, history, and tools.",
        "tool_plan": "Tool need evaluated.",
        "tool_call": "Tool calls completed or skipped.",
        "tool_results": "Tool outputs summarized for the response model.",
        "candidate_generation": "Candidate response generation completed.",
        "response_scoring": "Candidate responses scored and selected.",
    }
    active_messages = {
        "context_visual": "Checking source-local visual context availability.",
        "context_alert": "Reading source-local change/risk context if enabled.",
        "context_history": "Querying this source's SQLite observation history if enabled.",
        "context_screenshots": "Applying speech-side visual capture policy.",
        "context_prompt": "Assembling the final prompt from active context sources.",
        "tool_plan": "Routing the speech passage through the AI tool planner.",
        "tool_call": "Executing bounded external tool calls.",
        "tool_results": "Condensing tool results for model use.",
        "candidate_generation": "Generating candidate responses from context and tool results.",
        "response_scoring": "Scoring candidate responses for correctness and usefulness.",
    }
    result = {}
    for stage_id, message in messages.items():
        payload = payloads.get(stage_id) if isinstance(payloads.get(stage_id), dict) else {}
        status = "complete" if stage_id in completed else "waiting"
        stage_message = message
        if stage_id == active:
            status = "active"
            stage_message = active_messages[stage_id]
        if payload.get("stage_message"):
            stage_message = str(payload.get("stage_message"))
        result[stage_id] = context_component(status, stage_message, payload)
    return result


def publish_voice_context_state(
    args: argparse.Namespace,
    model: str,
    segment: dict,
    output_target: str,
    active: str,
    completed: set[str],
    payloads: dict[str, dict] | None = None,
    operation: str = "Preparing context for Nemotron.",
) -> None:
    context = voice_context_map(active=active, completed=completed, payloads=payloads)
    publish(
        args.voice_response_json,
        {
            "status": "thinking",
            "phase": "context",
            "operation": operation,
            "model": model,
            "input_speech": segment.get("text", ""),
            "input_source": segment.get("source", "unknown"),
            "input_updated_at": segment.get("updated_at", 0),
            "output_target": output_target,
            "message": operation,
            "conversation": (
                recent_conversation(args.voice_response_json, args.conversation_limit)
                + [user_turn(segment), assistant_turn(operation, "thinking")]
            )[-args.conversation_limit * 2 :],
            "stages": voice_stages(
                "complete",
                "waiting",
                "waiting",
                "waiting",
                "Speech passage captured. Preparing context components.",
                output_target,
                context=context,
            ),
        },
    )


def publish_operation_notice(
    args: argparse.Namespace,
    model: str,
    segment: dict,
    output_target: str,
    conversation: list[dict],
    text: str,
    active_context: str,
    completed_context: set[str],
    context_payloads: dict[str, dict] | None = None,
    extra_payload: dict | None = None,
    notification_decision: dict | None = None,
) -> None:
    notify = bool((notification_decision or {}).get("notified"))
    payload = {
        "status": "thinking",
        "phase": active_context if active_context else "operation_notice",
        "operation": text,
        "model": model,
        "input_speech": segment.get("text", ""),
        "input_source": segment.get("source", "unknown"),
        "input_updated_at": segment.get("updated_at", 0),
        "output_target": output_target,
        "message": text,
        "conversation": (
            conversation + [user_turn(segment)] + ([assistant_turn(text, "thinking")] if notify else [])
        )[-args.conversation_limit * 2 :],
        "stages": voice_stages(
            "complete",
            "waiting",
            "waiting",
            "waiting",
            text,
            output_target,
            context=voice_context_map(active=active_context, completed=completed_context, payloads=context_payloads or {}),
        ),
    }
    if notification_decision:
        maybe_add_notification_fields(payload, output_target, text, notification_decision)
    if extra_payload:
        payload.update(extra_payload)
    publish(args.voice_response_json, payload)
    if notify and output_target == "server":
        native_speak_text(text)


def play_on_server(audio_path: Path, sink: str) -> tuple[bool, str]:
    if not audio_path.exists():
        return False, f"audio file not found: {audio_path}"
    if shutil.which("paplay"):
        command = ["paplay"]
        if sink:
            command.extend(["--device", sink])
        command.append(str(audio_path))
        try:
            run_command(command, timeout=60)
            return True, ""
        except Exception as exc:
            return False, str(exc)
    if shutil.which("ffplay"):
        try:
            run_command(["ffplay", "-nodisp", "-autoexit", str(audio_path)], timeout=60)
            return True, ""
        except Exception as exc:
            return False, str(exc)
    return False, "paplay or ffplay is required for server playback"


def should_skip_echo(text: str, response_text: str, response_at: float, echo_window: float) -> bool:
    if not text or not response_text or time.time() - response_at > echo_window:
        return False
    left = normalize_text(text)
    right = normalize_text(response_text)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= 0.58


def process_segment(
    args: argparse.Namespace,
    model: str,
    magpie: MagpieSynthesizer,
    segment: dict,
) -> tuple[str, float]:
    audio_id = f"{int(time.time() * 1000)}_{abs(hash(segment.get('text', ''))) % 1_000_000}"
    source = str(segment.get("source") or "unknown")
    output_target = read_output_target(args, source)
    completed_context: set[str] = set()
    context_payloads: dict[str, dict] = {
        "context_prompt": {"source": source, "speech": short_text(segment.get("text", ""), 220)}
    }
    visual_request = visual_update_requested(str(segment.get("text") or ""))
    environment_context_enabled = bool(getattr(args, "enable_environment_context", False))
    context_payloads["context_visual"] = {
        "stage_message": (
            "Reading source-local visual context for this speech response."
            if environment_context_enabled
            else "Environment agents are disabled for this run."
        ),
        "visual_request_detected": visual_request,
        "source_id": source,
    }

    publish_voice_context_state(
        args,
        model,
        segment,
        output_target,
        "context_visual",
        completed_context,
        context_payloads,
        (
            "Reading source-local visual context for this speech response."
            if environment_context_enabled
            else "Environment agents are disabled for this run."
        ),
    )
    env_state_file = environment_state_path(args, source) if environment_context_enabled else ""
    env_state = read_json(env_state_file) if env_state_file else {}
    if not env_state:
        env_state = {
            "status": "waiting" if environment_context_enabled else "disabled",
            "source_id": source,
            "message": (
                "No visual context has been published for this source yet."
                if environment_context_enabled
                else "Environment agents are disabled while the stack is being redesigned."
            ),
        }
    env_updated_at = visual_context_updated_at(env_state)
    env_age = visual_context_age(args, env_state)
    visual_selection = {
        "selected": "source_context" if environment_context_enabled else "disabled",
        "environment_updated_at": env_updated_at,
        "selected_updated_at": env_updated_at,
        "selected_age_seconds": round(env_age, 2) if env_age is not None else None,
    }
    visual_policy: dict = {
        "requested_by_speech": visual_request,
        "speech_forced_refresh": False,
        "reason": (
            "Speech responses use the latest source-local visual context; speech does not force visual refreshes."
            if environment_context_enabled
            else "Environment agents are disabled; the classic speech responder does not query visual history."
        ),
        "freshness": visual_selection,
    }

    completed_context.add("context_visual")
    context_payloads["context_visual"] = {
        "stage_message": (
            "Loaded source-local visual context; no speech-side visual refresh was triggered."
            if environment_context_enabled
            else "Environment agents are disabled for this run."
        ),
        "environment_state": env_state_file,
        "environment_status": env_state.get("status", ""),
        "source_id": env_state.get("source_id", source),
        "cycle_id": env_state.get("cycle_id", ""),
        "latest_visual_age_seconds": (
            round(visual_context_age(args, env_state), 2) if visual_context_age(args, env_state) is not None else None
        ),
        "visual_context_source": visual_selection.get("selected", ""),
        "visual_policy": visual_policy,
        "visual_context": short_text(environment_visual_context(env_state), 260),
    }

    publish_voice_context_state(
        args,
        model,
        segment,
        output_target,
        "context_alert",
        completed_context,
        context_payloads,
        "Reading source-local change/risk context." if environment_context_enabled else "Change context is disabled.",
    )
    alert = environment_alert_context(env_state) if environment_context_enabled else {
        "status": "disabled",
        "risk": "none",
        "description": "Environment agents are disabled while the stack is being redesigned.",
        "source_id": source,
        "cycle_id": "",
    }
    completed_context.add("context_alert")
    context_payloads["context_alert"] = {
        "alert_status": alert.get("status", ""),
        "risk": alert.get("risk", ""),
        "description": short_text(alert.get("description") or alert.get("message") or "", 220),
    }

    publish_voice_context_state(
        args,
        model,
        segment,
        output_target,
        "context_history",
        completed_context,
        context_payloads,
        "Querying source-local observation history from SQLite." if environment_context_enabled else "Source observation history is disabled.",
    )
    database_path = environment_database_path(args, source) if environment_context_enabled else ""
    history = (
        read_recent_analyses(database_path, min(args.history_limit, args.voice_context_history_limit))
        if environment_context_enabled and database_path
        else []
    )
    completed_context.add("context_history")
    context_payloads["context_history"] = {
        "database": str(database_path),
        "rows_loaded": len(history),
        "latest_row_id": history[-1].get("id") if history else "",
    }

    publish_voice_context_state(
        args,
        model,
        segment,
        output_target,
        "context_screenshots",
        completed_context,
        context_payloads,
        "Skipping speech-side screenshots; environment agents are disabled.",
    )
    screenshot_paths: list[Path] = []
    images: list[str] = []
    screenshot_mode = "disabled"
    completed_context.add("context_screenshots")
    context_payloads["context_screenshots"] = {
        "mode": screenshot_mode,
        "image_count_sent": len(images),
        "paths": [str(path) for path in screenshot_paths],
        "skipped": True,
        "reason": "Environment agents are disabled while the stack is being redesigned.",
    }

    publish_voice_context_state(
        args,
        model,
        segment,
        output_target,
        "context_prompt",
        completed_context,
        context_payloads,
        "Assembling Nemotron prompt from speech and active tool context.",
    )
    prompt = build_prompt(args, segment, env_state, alert, history, screenshot_paths)
    completed_context.add("context_prompt")
    context_payloads["context_prompt"] = {
        "source": source,
        "speech": short_text(segment.get("text", ""), 220),
        "prompt_chars": len(prompt),
        "image_count": len(images),
        "history_rows": len(history),
    }

    conversation = recent_conversation(args.voice_response_json, args.conversation_limit)

    plan_input_type = notification_input_type(segment.get("text", ""))
    plan_decision = notification_operation_decision(
        args.notification_stats_db,
        "classic",
        "tool_plan",
        plan_input_type,
        args.notification_final_response_threshold,
    )
    publish_operation_notice(
        args,
        model,
        segment,
        output_target,
        conversation,
        "Planning.",
        "tool_plan",
        completed_context,
        context_payloads,
        notification_decision=plan_decision,
    )
    plan_started_at = time.time()
    tool_plan, raw_tool_plan = plan_tools(args, model, segment, env_state, alert)
    tool_calls = tool_plan.get("calls") if isinstance(tool_plan.get("calls"), list) else []
    record_operation_timing(
        args.notification_stats_db,
        "classic",
        "tool_plan",
        plan_input_type,
        segment.get("source", "unknown"),
        segment.get("text", ""),
        raw_tool_plan,
        time.time() - plan_started_at,
        {
            "model": tool_plan.get("planner_model", ""),
            "needs_tools": bool(tool_plan.get("needs_tools")),
            "tool_names": [str((call or {}).get("name") or "") for call in tool_calls],
            "tool_count": len(tool_calls),
            "planner_queue_size": tool_plan.get("planner_queue_size", 0),
            "planner_queue_after": tool_plan.get("planner_queue_after", 0),
            "notified": bool(plan_decision.get("notified")),
        },
    )
    completed_context.add("tool_plan")
    context_payloads["tool_plan"] = {
        "needs_tools": bool(tool_plan.get("needs_tools")),
        "reason": short_text(tool_plan.get("reason") or "", 220),
        "call_count": len(tool_calls),
        "planner_source": tool_plan.get("planner_source", ""),
        "planner_model": tool_plan.get("planner_model", ""),
        "planner_queue_size": tool_plan.get("planner_queue_size", 0),
        "planner_queue_after": tool_plan.get("planner_queue_after", 0),
        "planner_queue_active": tool_plan.get("planner_queue_active", []),
        "route_confidence": tool_plan.get("route_confidence", ""),
        "raw_plan": short_text(raw_tool_plan, 500),
    }

    tool_results: list[dict] = []
    if tool_plan.get("needs_tools") and tool_calls:
        publish_voice_context_state(
            args,
            model,
            segment,
            output_target,
            "tool_call",
            completed_context,
            context_payloads,
            "Executing selected tools for the speech agent.",
        )
        for call in tool_calls[: max(0, int(args.max_tool_calls))]:
            notice = tool_notification_text(call)
            tool_name = str((call or {}).get("name") or "unknown").strip() or "unknown"
            tool_operation = f"tool_call:{tool_name}"
            tool_input_type = notification_input_type(segment.get("text", ""), tool_plan)
            tool_decision = notification_operation_decision(
                args.notification_stats_db,
                "classic",
                tool_operation,
                tool_input_type,
                args.notification_final_response_threshold,
            )
            publish_operation_notice(
                args,
                model,
                segment,
                output_target,
                conversation,
                notice,
                "tool_call",
                completed_context,
                context_payloads,
                {"tool_plan": tool_plan, "tool_results": tool_results},
                notification_decision=tool_decision,
            )
            tool_started_at = time.time()
            tool_result = run_tool_call(args, call)
            tool_results.append(tool_result)
            record_operation_timing(
                args.notification_stats_db,
                "classic",
                tool_operation,
                tool_input_type,
                segment.get("source", "unknown"),
                json.dumps({"speech": segment.get("text", ""), "call": call}, ensure_ascii=False),
                json.dumps(tool_result, ensure_ascii=False),
                time.time() - tool_started_at,
                {
                    "tool_name": tool_name,
                    "status": tool_result.get("status", ""),
                    "notified": bool(tool_decision.get("notified")),
                    "call_index": len(tool_results),
                },
            )
    completed_context.add("tool_call")
    context_payloads["tool_call"] = {
        "call_count": len(tool_results),
        "tools": [item.get("name") for item in tool_results],
    }

    publish_voice_context_state(
        args,
        model,
        segment,
        output_target,
        "tool_results",
        completed_context,
        context_payloads,
        "Preparing tool outputs for response generation.",
    )
    tool_summary = summarize_tool_results(tool_results, args.tool_result_chars)
    completed_context.add("tool_results")
    context_payloads["tool_results"] = {
        "result_count": len(tool_results),
        "summary": short_text(tool_summary, 520),
    }

    candidate_count = max(1, int(args.response_candidates))
    candidate_prompt = build_candidate_prompt(args, segment, env_state, alert, history, screenshot_paths, tool_summary, candidate_count)
    generation_notice = "Answering."
    final_response_input_type = notification_input_type(segment.get("text", ""), tool_plan)
    final_response_decision = notification_operation_decision(
        args.notification_stats_db,
        "classic",
        "final_response",
        final_response_input_type,
        args.notification_final_response_threshold,
    )
    notify_final_response = bool(final_response_decision.get("notified"))
    pending_conversation_with_notice = (
        conversation
        + [user_turn(segment)]
        + ([assistant_turn(generation_notice, "thinking")] if notify_final_response else [])
    )[-args.conversation_limit * 2 :]
    generation_payload = {
        "status": "thinking",
        "phase": "nemotron_text",
        "operation": generation_notice,
        "model": model,
        "input_speech": segment.get("text", ""),
        "input_source": segment.get("source", "unknown"),
        "input_updated_at": segment.get("updated_at", 0),
        "visual_context": short_text(environment_visual_context(env_state), 1200),
        "environment_state_path": env_state_file,
        "environment_cycle_id": env_state.get("cycle_id", ""),
        "screenshot_paths": [str(path) for path in screenshot_paths],
        "screenshot_mode": screenshot_mode,
        "output_target": output_target,
        "tool_plan": tool_plan,
        "tool_results": tool_results,
        "tool_summary": tool_summary,
        "message": generation_notice,
        "notification_decision": final_response_decision,
        "conversation": pending_conversation_with_notice,
        "stages": voice_stages(
            "complete",
            "active",
            "waiting",
            "waiting",
            "Speech passage captured and context assembled.",
            output_target,
            context=voice_context_map(completed=completed_context, payloads=context_payloads),
        ),
    }
    maybe_add_notification_fields(generation_payload, output_target, generation_notice, final_response_decision)
    publish(
        args.voice_response_json,
        generation_payload,
    )
    if notify_final_response and output_target == "server":
        native_speak_text(generation_notice)

    final_response_started_at = time.time()
    candidates, raw_candidates, image_mode, response_model, response_runtime = generate_response_candidates(
        args,
        model,
        candidate_prompt,
        images,
        segment,
        max(args.nemotron_num_predict, args.response_max_words * max(1, candidate_count) * 4),
    )
    record_operation_timing(
        args.notification_stats_db,
        "classic",
        "final_response",
        final_response_input_type,
        segment.get("source", "unknown"),
        segment.get("text", ""),
        json.dumps(candidates, ensure_ascii=False),
        time.time() - final_response_started_at,
        {
            "model": response_model,
            "candidate_count": len(candidates),
            "tool_count": len(tool_results),
            "notified": notify_final_response,
        },
    )
    completed_context.add("candidate_generation")
    context_payloads["candidate_generation"] = {
        "candidate_count": len(candidates),
        "prompt_chars": len(candidate_prompt),
        "candidates": candidates,
        "response_model": response_model,
        "runtime": response_runtime,
    }

    publish_voice_context_state(
        args,
        model,
        segment,
        output_target,
        "response_scoring",
        completed_context,
        context_payloads,
        "Scoring candidate responses before speaking.",
    )
    selected_candidate, scoring, raw_scoring = score_candidates(args, model, segment, candidates, tool_summary, env_state)
    response_text = str(selected_candidate.get("response") or "").strip() or candidates[0]["response"]
    spoken_text = tts_safe_text(response_text, args.tts_max_chars, args.tts_max_words)
    spoken_text_truncated = (
        (int(args.tts_max_chars) > 0 or int(args.tts_max_words) > 0) and spoken_text != response_text
    )
    completed_context.add("response_scoring")
    context_payloads["response_scoring"] = {
        "selected_id": selected_candidate.get("id"),
        "scores": scoring.get("scores") if isinstance(scoring, dict) else [],
        "reason": short_text(scoring.get("reason") if isinstance(scoring, dict) else "", 260),
    }
    raw_response = json.dumps(
        {
            "tool_plan": tool_plan,
            "tool_results": tool_results,
            "candidates": candidates,
            "scoring": scoring,
            "raw_candidates": raw_candidates,
            "raw_scoring": raw_scoring,
            "response_model": response_model,
            "response_runtime": response_runtime,
        },
        ensure_ascii=False,
    )
    context_payloads["context_prompt"] = {
        **context_payloads["context_prompt"],
        "image_mode": image_mode if image_mode != "none" else screenshot_mode,
    }

    tts_notice = "Generating."
    tts_input_type = notification_input_type(response_text, tool_plan)
    tts_decision = notification_operation_decision(
        args.notification_stats_db,
        "classic",
        "tts_generation",
        tts_input_type,
        args.notification_final_response_threshold,
    )
    tts_payload = {
            "status": "speaking",
            "phase": "tts",
            "operation": tts_notice,
            "model": model,
            "response_model": response_model,
            "input_speech": segment.get("text", ""),
            "input_source": segment.get("source", "unknown"),
            "input_updated_at": segment.get("updated_at", 0),
            "response_text": response_text,
            "spoken_text": spoken_text,
            "spoken_text_truncated": spoken_text_truncated,
            "raw_response": raw_response,
            "visual_context": short_text(environment_visual_context(env_state), 1200),
            "environment_state_path": env_state_file,
            "environment_cycle_id": env_state.get("cycle_id", ""),
            "screenshot_paths": [str(path) for path in screenshot_paths],
            "screenshot_mode": image_mode if image_mode != "none" else screenshot_mode,
            "tool_plan": tool_plan,
            "tool_results": tool_results,
            "tool_summary": tool_summary,
            "response_candidates": candidates,
            "response_scoring": scoring,
            "response_runtime": response_runtime,
            "output_target": output_target,
            "message": tts_notice,
            "conversation": (conversation + [user_turn(segment), assistant_turn(response_text, "speaking")])[
                -args.conversation_limit * 2 :
            ],
            "stages": voice_stages(
                "complete",
                "complete",
                "active",
                "waiting",
                "Nemotron text response complete. Generating speech audio.",
                output_target,
                context=voice_context_map(completed=completed_context, payloads=context_payloads),
            ),
        }
    maybe_add_notification_fields(tts_payload, output_target, tts_notice, tts_decision)
    publish(
        args.voice_response_json,
        tts_payload,
    )
    if tts_decision.get("notified") and output_target == "server":
        native_speak_text(tts_notice)

    tts_started_at = time.time()
    audio_path, tts_backend, tts_warning = synthesize_response(args, magpie, response_text, audio_id)
    record_operation_timing(
        args.notification_stats_db,
        "classic",
        "tts_generation",
        tts_input_type,
        segment.get("source", "unknown"),
        response_text,
        str(audio_path) if audio_path else tts_warning,
        time.time() - tts_started_at,
        {
            "tts_backend": tts_backend,
            "tts_warning": tts_warning,
            "notified": bool(tts_decision.get("notified")),
            "audio_id": audio_id,
        },
    )
    output_target = read_output_target(args, source)
    played_on_server = False
    playback_error = ""
    final_conversation = (conversation + [user_turn(segment), assistant_turn(response_text)])[-args.conversation_limit * 2 :]

    publish(
        args.voice_response_json,
        {
            "status": "speaking",
            "phase": "audio_output",
            "operation": "Response audio ready. Microphone capture resumed while audio is routed.",
            "model": model,
            "response_model": response_model,
            "tts_model": tts_backend,
            "tts_warning": tts_warning,
            "input_speech": segment.get("text", ""),
            "input_source": segment.get("source", "unknown"),
            "input_updated_at": segment.get("updated_at", 0),
            "response_text": response_text,
            "spoken_text": spoken_text,
            "spoken_text_truncated": spoken_text_truncated,
            "raw_response": raw_response,
            "visual_context": short_text(environment_visual_context(env_state), 1200),
            "environment_state_path": env_state_file,
            "environment_cycle_id": env_state.get("cycle_id", ""),
            "screenshot_paths": [str(path) for path in screenshot_paths],
            "screenshot_mode": image_mode if image_mode != "none" else screenshot_mode,
            "tool_plan": tool_plan,
            "tool_results": tool_results,
            "tool_summary": tool_summary,
            "response_candidates": candidates,
            "response_scoring": scoring,
            "response_runtime": response_runtime,
            "audio_id": audio_id,
            "audio_path": str(audio_path) if audio_path else "",
            "audio_url": f"/voice-response-audio.wav?audio_id={audio_id}" if audio_path else "",
            "output_target": output_target,
            "played_on_server": False,
            "playback_error": "",
            "conversation": final_conversation,
            "stages": voice_stages(
                "complete",
                "complete",
                "complete",
                "complete",
                "Response audio is ready.",
                output_target,
                context=voice_context_map(completed=completed_context, payloads=context_payloads),
                playback="active",
            ),
        },
    )
    if audio_path and output_target == "server":
        played_on_server, playback_error = play_on_server(audio_path, args.server_audio_sink)

    publish(
        args.voice_response_json,
        {
            "status": "running",
            "phase": "complete",
            "operation": "Response complete. Waiting for new speech.",
            "model": model,
            "response_model": response_model,
            "tts_model": tts_backend,
            "tts_warning": tts_warning,
            "input_speech": segment.get("text", ""),
            "input_source": segment.get("source", "unknown"),
            "input_updated_at": segment.get("updated_at", 0),
            "response_text": response_text,
            "spoken_text": spoken_text,
            "spoken_text_truncated": spoken_text_truncated,
            "raw_response": raw_response,
            "visual_context": short_text(environment_visual_context(env_state), 1200),
            "environment_state_path": env_state_file,
            "environment_cycle_id": env_state.get("cycle_id", ""),
            "screenshot_paths": [str(path) for path in screenshot_paths],
            "screenshot_mode": image_mode if image_mode != "none" else screenshot_mode,
            "tool_plan": tool_plan,
            "tool_results": tool_results,
            "tool_summary": tool_summary,
            "response_candidates": candidates,
            "response_scoring": scoring,
            "response_runtime": response_runtime,
            "audio_id": audio_id,
            "audio_path": str(audio_path) if audio_path else "",
            "audio_url": f"/voice-response-audio.wav?audio_id={audio_id}" if audio_path else "",
            "output_target": output_target,
            "played_on_server": played_on_server,
            "playback_error": playback_error,
            "conversation": final_conversation,
            "stages": voice_stages(
                "complete",
                "complete",
                "complete",
                "complete" if not playback_error else "error",
                "Response audio is ready.",
                output_target,
                context=voice_context_map(completed=completed_context, payloads=context_payloads),
                playback="complete" if not playback_error else "error",
            ),
        },
    )
    if bool(getattr(args, "enable_environment_context", False)):
        try:
            request_environment_wake(args, source, "speech_response_complete")
        except Exception:
            pass
    return response_text, time.time()


def main() -> int:
    global ACTIVE_NEMOTRON_MODEL, ACTIVE_TOOL_PLANNER_MODEL, ACTIVE_TTS_MODEL_LABEL, ACTIVE_TTS_CODEC_LABEL, ACTIVE_TTS_DETAILS
    args = parse_args()
    ensure_notification_stats_db(args.notification_stats_db)
    publish(
        args.voice_response_json,
        {
            "status": "loading",
            "message": "Starting Nemotron voice responder.",
        },
    )
    model = choose_model(args)
    ACTIVE_NEMOTRON_MODEL = model
    ACTIVE_TOOL_PLANNER_MODEL = str(args.tool_planner_model or model)
    ACTIVE_TTS_MODEL_LABEL = Path(args.tts_model_path).name if args.tts_backend == "magpie" else args.tts_backend
    ACTIVE_TTS_CODEC_LABEL = Path(args.tts_codec_path).name if args.tts_backend == "magpie" else ""
    ACTIVE_TTS_DETAILS = {
        "backend": args.tts_backend,
        "model_path": str(Path(args.tts_model_path)) if args.tts_backend == "magpie" else "",
        "codec_path": str(Path(args.tts_codec_path)) if args.tts_backend == "magpie" else "",
        "direct_tts": bool(args.magpie_direct_tts),
        "max_decoder_steps": int(args.magpie_max_decoder_steps),
        "maskgit_steps": int(args.magpie_maskgit_steps),
        "temperature": float(args.magpie_temperature),
        "topk": int(args.magpie_topk),
        "use_cfg": bool(args.magpie_use_cfg),
    }
    if args.nemotron_warmup:
        publish(
            args.voice_response_json,
            {
                "status": "loading",
                "phase": "nemotron_warmup",
                "model": model,
                "message": "Warming up the Nemotron voice model.",
                "stages": voice_stages("waiting", "active", "waiting", "waiting", "Preloading Nemotron voice model."),
            },
        )
        try:
            request_nemotron(
                args,
                model,
                'Return compact JSON only: {"response":"spoken response text"}\nSpeech: Are you ready?\nContext: Voice model warmup.',
                [],
            )
        except Exception as exc:
            publish(
                args.voice_response_json,
                {
                    "status": "loading",
                    "phase": "nemotron_warmup_warning",
                    "model": model,
                    "message": f"Nemotron warmup failed; live requests will retry: {exc}",
                },
            )
    magpie = MagpieSynthesizer(args)
    if args.tts_backend == "magpie" and args.magpie_warmup:
        publish(
            args.voice_response_json,
            {
                "status": "loading",
                "phase": "tts_warmup",
                "model": model,
                "message": "Warming up Magpie TTS on the GPU.",
                "stages": voice_stages("waiting", "waiting", "active", "waiting", "Preloading Magpie TTS."),
            },
        )
        try:
            magpie.synthesize("Ready.", "warmup")
        except Exception as exc:
            publish(
                args.voice_response_json,
                {
                    "status": "loading",
                    "phase": "tts_warmup_warning",
                    "model": model,
                    "message": f"Magpie TTS warmup failed; runtime fallback is still enabled: {exc}",
                },
            )
    seen: set[str] = set()
    last_processed_norm = ""
    last_processed_at = 0.0
    last_response_text = ""
    last_response_at = 0.0

    if not args.process_existing_transcript:
        for segment in transcript_segments(read_json(args.transcript_json)):
            seen.add(segment_key(segment))

    last_reset_marker = reset_marker(args.voice_session_reset_json)

    publish(args.voice_response_json, waiting_state(args, model, "Waiting for a new speech passage.", last_reset_marker))

    while True:
        if selected_pipeline_mode(args.pipeline_mode_json) != "classic":
            publish(
                args.voice_response_json,
                waiting_state(
                    args,
                    model,
                    "Classic Nemotron + Magpie responder disabled; Nemotron 3 VoiceChat pipeline is selected.",
                    last_reset_marker,
                ),
            )
            if args.once:
                return 0
            time.sleep(max(0.5, float(args.interval)))
            continue

        current_reset_marker = reset_marker(args.voice_session_reset_json)
        if current_reset_marker and current_reset_marker != last_reset_marker:
            seen = {segment_key(segment) for segment in transcript_segments(read_json(args.transcript_json))}
            last_processed_norm = ""
            last_processed_at = 0.0
            last_response_text = ""
            last_response_at = 0.0
            last_reset_marker = current_reset_marker
            publish(
                args.voice_response_json,
                waiting_state(args, model, "Voice session cleared. Waiting for a new speech passage.", current_reset_marker),
            )

        transcript = read_json(args.transcript_json)
        candidates = transcript_segments(transcript)
        processed_this_tick = False
        segment, keys_to_mark, pending = next_utterance(args, candidates, seen)
        if pending:
            publish_waiting_for_utterance(args, model, pending)
            processed_this_tick = True
        elif keys_to_mark and segment is None:
            seen.update(keys_to_mark)
            processed_this_tick = True
        elif segment:
            seen.update(keys_to_mark)
            text_norm = normalize_text(str(segment.get("text") or ""))
            updated_at = float(segment.get("updated_at") or time.time())
            if text_norm == last_processed_norm and updated_at - last_processed_at < args.duplicate_window_seconds:
                processed_this_tick = True
            elif should_skip_echo(str(segment.get("text") or ""), last_response_text, last_response_at, args.echo_window_seconds):
                processed_this_tick = True
            elif not dialog_activation_reason(args, str(segment.get("text") or "")):
                publish_ignored_utterance(args, model, segment)
                processed_this_tick = True
            else:
                try:
                    last_response_text, last_response_at = process_segment(args, model, magpie, segment)
                    last_processed_norm = text_norm
                    last_processed_at = updated_at
                except Exception as exc:
                    publish(
                        args.voice_response_json,
                        {
                            "status": "error",
                            "phase": "error",
                            "operation": "Voice response failed.",
                            "model": model,
                            "input_speech": segment.get("text", ""),
                            "input_source": segment.get("source", "unknown"),
                            "input_updated_at": segment.get("updated_at", 0),
                            "output_target": read_output_target(args, str(segment.get("source") or "")),
                            "stages": voice_stages(
                                "complete",
                                "error",
                                "waiting",
                                "waiting",
                                str(exc),
                                read_output_target(args, str(segment.get("source") or "")),
                            ),
                            "error": str(exc),
                        },
                    )
                    if args.once:
                        return 1
                processed_this_tick = True

        if args.once:
            return 0
        if not processed_this_tick and len(seen) > 200:
            seen = set(list(seen)[-100:])
        time.sleep(args.interval)


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    raise SystemExit(main())

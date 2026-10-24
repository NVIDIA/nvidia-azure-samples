#!/usr/bin/env python3
"""Process automated DeepStream text requests without entering an audio loop."""

from __future__ import annotations

import argparse
import fcntl
import re
import signal
import threading
import time
from pathlib import Path

import nemotron_voicechat_pipeline as pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEECH_TEST_VOICES = {
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky", "am_adam",
    "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-json", default=str(PROJECT_ROOT / "webcam-deepstream-nemotron-input.json"))
    parser.add_argument("--response-json", default=str(PROJECT_ROOT / "webcam-voicechat-response.json"))
    parser.add_argument("--session-reset-json", default=str(PROJECT_ROOT / "webcam-voice-session-reset.json"))
    parser.add_argument("--output-target-json", default=str(PROJECT_ROOT / "webcam-voice-output-target.json"))
    parser.add_argument("--playback-lock-json", default=str(PROJECT_ROOT / "webcam-speech-playback-lock.json"))
    parser.add_argument("--component-audio-settings-json", default=str(PROJECT_ROOT / "webcam-component-audio-settings.json"))
    parser.add_argument("--tool-planner-queue-json", default=str(PROJECT_ROOT / "webcam-tool-planner-queue.json"))
    parser.add_argument("--focus-object-command-json", default=str(PROJECT_ROOT / "webcam-focus-object-command.json"))
    parser.add_argument("--focus-object-state-json", default=str(PROJECT_ROOT / "webcam-focus-object-state.json"))
    parser.add_argument("--focus-object-wake-socket", default="/tmp/dgx-spark-focus-wake.sock")
    parser.add_argument("--server-agent-state-json", default=str(PROJECT_ROOT / "webcam-server-agent-state.json"))
    parser.add_argument("--browser-agent-state-json", default=str(PROJECT_ROOT / "webcam-browser-agent-state.json"))
    parser.add_argument("--wifi-agent-state-json", default=str(PROJECT_ROOT / "webcam-wifi-agent-state.json"))
    parser.add_argument("--bulb-agent-state-json", default=str(PROJECT_ROOT / "webcam-bulb-agent-state.json"))
    parser.add_argument("--environment-wake-json", default=str(PROJECT_ROOT / "webcam-environment-wake.json"))
    parser.add_argument("--audio-dir", default=str(PROJECT_ROOT / "webcam-voicechat-audio"))
    parser.add_argument("--wifi-talk-audio-url", default="http://127.0.0.1:8090/wifi-talk-audio")
    parser.add_argument("--bulb-talk-audio-url", default="http://127.0.0.1:8090/bulb-talk-audio")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model-api-url", default="http://127.0.0.1:8010")
    parser.add_argument("--model-api-runtime", choices=("ollama", "vllm"), default="vllm")
    parser.add_argument("--tool-planner-url", default="http://127.0.0.1:11434")
    parser.add_argument("--understanding-model", default=pipeline.NEMOTRON_OMNI_MODEL)
    parser.add_argument("--answer-model", default=pipeline.NEMOTRON_OMNI_MODEL)
    parser.add_argument("--answer-timeout", type=float, default=180.0)
    parser.add_argument("--answer-max-tokens", type=int, default=320)
    parser.add_argument("--answer-num-ctx", type=int, default=pipeline.NEMOTRON_OMNI_CONTEXT_WINDOW)
    parser.add_argument("--focus-object-timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=0.02)
    parser.add_argument(
        "--dialog-quiet-seconds",
        type=float,
        default=30.0,
        help="Queue automated vision announcements until human/peer voice dialog has been quiet this long.",
    )
    return parser.parse_args()


def make_pipeline_args(args: argparse.Namespace) -> argparse.Namespace:
    return pipeline.parse_args(
        [
            "--event-only",
            "--event-speech-output",
            "--source-mode", "wifi",
            "--voicechat-manual-input-json", args.input_json,
            "--voicechat-response-json", args.response_json,
            "--voicechat-session-reset-json", args.session_reset_json,
            "--voice-output-target-json", args.output_target_json,
            "--speech-playback-lock-json", args.playback_lock_json,
            "--component-audio-settings-json", args.component_audio_settings_json,
            "--tool-planner-queue-json", args.tool_planner_queue_json,
            "--focus-object-command-json", args.focus_object_command_json,
            "--focus-object-state-json", args.focus_object_state_json,
            "--focus-object-wake-socket",
            getattr(args, "focus_object_wake_socket", "/tmp/dgx-spark-focus-wake.sock"),
            "--focus-object-timeout", str(args.focus_object_timeout),
            "--server-agent-state-json", args.server_agent_state_json,
            "--browser-agent-state-json", args.browser_agent_state_json,
            "--wifi-agent-state-json", args.wifi_agent_state_json,
            "--bulb-agent-state-json", args.bulb_agent_state_json,
            "--environment-wake-json", args.environment_wake_json,
            "--audio-dir", args.audio_dir,
            "--wifi-talk-audio-url",
            getattr(args, "wifi_talk_audio_url", "http://127.0.0.1:8090/wifi-talk-audio"),
            "--bulb-talk-audio-url",
            getattr(args, "bulb_talk_audio_url", "http://127.0.0.1:8090/bulb-talk-audio"),
            "--backend", "vllm",
            "--ollama-url", args.ollama_url,
            "--model-api-url", getattr(args, "model_api_url", "http://127.0.0.1:8010"),
            "--model-api-runtime", getattr(args, "model_api_runtime", "vllm"),
            "--tool-planner-url", getattr(args, "tool_planner_url", "http://127.0.0.1:11434"),
            "--ollama-model", args.understanding_model,
            "--answer-model", args.answer_model,
            "--answer-timeout", str(args.answer_timeout),
            "--answer-max-tokens", str(args.answer_max_tokens),
            "--answer-num-ctx", str(args.answer_num_ctx),
            "--answer-keep-alive", "-1",
            "--no-request-native-audio",
            "--no-native-audio-required",
            "--tts-backend", "kokoro",
            "--kokoro-voice", "af_heart",
            "--kokoro-device", "cuda",
            "--kokoro-warmup",
            "--native-audio-voice", "Sofia",
            "--magpie-voice", "Sofia",
            "--magpie-speaker-index", "1",
            "--no-magpie-warmup",
            "--output-target-mode", "auto",
        ]
    )


def process_speech_test(args: argparse.Namespace, request: dict, magpie) -> None:
    text = " ".join(str(request.get("text") or "").split())
    voice = str(request.get("tts_voice") or "af_heart").strip()
    if voice not in SPEECH_TEST_VOICES:
        raise ValueError(f"unsupported Kokoro speech-test voice: {voice}")
    try:
        speed = float(request.get("tts_speed") or 1.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("tts_speed must be numeric") from exc
    if not 0.5 <= speed <= 2.0:
        raise ValueError("tts_speed must be between 0.5 and 2.0")
    request_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(request.get("id") or time.time_ns()))[:120]
    audio_id = f"speech_test_{request_id}"
    started = time.monotonic()
    old_voice = args.kokoro_voice
    old_speed = args.tts_playback_speed
    pipeline.write_playback_lock(args, True, "server", "tts", audio_id, "Resident Kokoro speech test is active.")
    try:
        args.kokoro_voice = voice
        args.tts_playback_speed = speed
        audio_path, backend = pipeline.synthesize_tts_response(args, magpie, text, audio_id)
        if audio_path is None:
            raise RuntimeError("resident TTS service returned no audio")
        synthesis_seconds = time.monotonic() - started
        played = False
        playback_error = ""
        if bool(request.get("playback", True)):
            pipeline.write_playback_lock(args, True, "server", "playback", audio_id, "Playing Kokoro speech test on JBL.")
            played, playback_error = pipeline.play_audio_on_server(audio_path, args.server_audio_sink)
            if not played:
                raise RuntimeError(playback_error or "JBL playback failed")
        request["service_result"] = {
            "ok": True,
            "backend": backend,
            "voice": voice,
            "speed": speed,
            "audio_path": str(audio_path),
            "audio_seconds": round(pipeline.wav_duration_seconds(audio_path), 3),
            "synthesis_seconds": round(synthesis_seconds, 3),
            "played": played,
            "sink": args.server_audio_sink if played else "",
        }
    except Exception as exc:
        request["service_result"] = {
            "ok": False,
            "voice": voice,
            "speed": speed,
            "error": str(exc),
        }
        raise
    finally:
        args.kokoro_voice = old_voice
        args.tts_playback_speed = old_speed
        pipeline.write_playback_lock(args, False, "server", "complete", audio_id, "Resident Kokoro speech test complete.")


def process_one(args: argparse.Namespace, request: dict, magpie=None) -> None:
    source = str(request.get("source") or "wifi").strip().lower()
    if magpie is None:
        magpie = pipeline.MagpieSynthesizer(args)
    lock_path = Path(args.voicechat_response_json).with_suffix(f".{source}.nemotron.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        if str(request.get("kind") or "").strip().lower() == "speech_test":
            process_speech_test(args, request, magpie)
            return
        previous_event_speech_output = bool(getattr(args, "event_speech_output", False))
        args.event_speech_output = pipeline.manual_text_request_is_focus_acquisition(request)
        try:
            pipeline.publish_manual_text_received(args, request, "ollama")
            pipeline.process_manual_text_request(args, request, "ollama", magpie)
        finally:
            args.event_speech_output = previous_event_speech_output


def latest_dialog_activity_at(response: dict) -> float:
    """Return the newest conversational turn that was not an automated vision event."""
    conversation = response.get("conversation") if isinstance(response, dict) else []
    if not isinstance(conversation, list):
        return 0.0
    latest = 0.0
    dialog_turn_open = False
    for turn in conversation:
        if not isinstance(turn, dict):
            continue
        phase = str(turn.get("phase") or "").strip().lower()
        label = str(turn.get("label") or "").strip().lower()
        role = str(turn.get("role") or "").strip().lower()
        automated = phase == "deepstream_object_change" or label == "live stream processor"
        if role == "user":
            dialog_turn_open = not automated
        if automated or not dialog_turn_open:
            continue
        try:
            latest = max(latest, float(turn.get("updated_at") or 0.0))
        except (TypeError, ValueError):
            pass
    return latest


def dialog_has_priority(args: argparse.Namespace, pipeline_args: argparse.Namespace, now: float | None = None) -> bool:
    checked_at = float(now if now is not None else time.time())
    quiet_seconds = max(0.0, float(getattr(args, "dialog_quiet_seconds", 30.0)))
    response = pipeline.read_json(pipeline_args.voicechat_response_json)
    latest_at = latest_dialog_activity_at(response)
    if latest_at > 0.0 and checked_at - latest_at < quiet_seconds:
        return True

    lock_data = pipeline.read_json(pipeline_args.speech_playback_lock_json)
    sources = lock_data.get("sources") if isinstance(lock_data.get("sources"), dict) else {}
    for state in sources.values():
        if not isinstance(state, dict) or not bool(state.get("active")):
            continue
        try:
            expires_at = float(state.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at <= 0.0 or checked_at <= expires_at:
            return True
    return False


def run(args: argparse.Namespace) -> int:
    pipeline_args = make_pipeline_args(args)
    pipeline.ACTIVE_OLLAMA_VOICECHAT_MODEL = str(pipeline_args.ollama_model)
    pipeline.ACTIVE_ANSWER_MODEL = str(pipeline_args.answer_model)
    pipeline.ACTIVE_TTS_BACKEND = str(pipeline_args.tts_backend)
    pipeline.ACTIVE_TTS_MODEL_LABEL = "Kokoro-82M af_heart"
    pipeline.ACTIVE_TTS_CODEC_LABEL = ""
    magpie = pipeline.MagpieSynthesizer(pipeline_args)
    if bool(getattr(pipeline_args, "kokoro_warmup", False)):
        pipeline.synthesize_tts_response(pipeline_args, magpie, "Ready.", "deepstream_kokoro_warmup")
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    print("DeepStream Nemotron worker ready; audio capture is disabled.", flush=True)
    threading.Thread(
        target=pipeline.warm_ollama_answer_model,
        args=(pipeline_args,),
        name="deepstream-nemotron-omni-warmup",
        daemon=True,
    ).start()
    while not stop.is_set():
        if dialog_has_priority(args, pipeline_args):
            stop.wait(max(0.05, float(args.poll_interval)))
            continue
        request = pipeline.consume_manual_voicechat_input(pipeline_args, ["wifi", "bulb", "server"])
        if request is None:
            stop.wait(max(0.01, float(args.poll_interval)))
            continue
        try:
            process_one(pipeline_args, request, magpie)
        except Exception as exc:
            pipeline.publish_manual_text_failure(pipeline_args, request, "ollama", exc)
        finally:
            pipeline.complete_manual_voicechat_input(pipeline_args, request)
    return 0


def main() -> int:
    return run(parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

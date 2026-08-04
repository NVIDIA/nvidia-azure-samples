#!/usr/bin/env python3
"""Measure Piper voices through the real JBL-to-camera-microphone path."""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

from nemotron_voicechat_pipeline import (
    play_audio_on_server,
    play_audio_on_wifi_camera,
    playback_lock_state,
    read_json,
    smooth_playback_wav,
    write_playback_lock,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", action="append", type=Path, required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--sink", default="")
    parser.add_argument("--output-target", choices=("server", "wifi_camera"), default="server")
    parser.add_argument("--listen-source", choices=("server", "wifi"), default="wifi")
    parser.add_argument("--wifi-talk-audio-url", default="http://127.0.0.1:8090/wifi-talk-audio")
    parser.add_argument("--response-json", type=Path, default=Path("webcam-voicechat-response.json"))
    parser.add_argument("--playback-lock-json", type=Path, default=Path("webcam-speech-playback-lock.json"))
    parser.add_argument("--session-flush-url", default="http://127.0.0.1:8090/session-flush")
    parser.add_argument("--timeout", type=float, default=18.0)
    parser.add_argument("--wait-for-response-playback", action="store_true")
    parser.add_argument("--response-playback-timeout", type=float, default=20.0)
    parser.add_argument("--server-playback-lead-seconds", type=float, default=0.65)
    parser.add_argument("--camera-playback-lead-seconds", type=float, default=0.50)
    parser.add_argument("--post-flush-settle-seconds", type=float, default=4.0)
    parser.add_argument(
        "--production-playback-conditioning",
        action="store_true",
        help="Apply the same qualified server/camera wake lead and fades as production TTS playback.",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def flush_session(url: str) -> None:
    request = Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        response.read()


def words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def edit_distance(left: list[str], right: list[str]) -> int:
    row = list(range(len(right) + 1))
    for index, word in enumerate(left, 1):
        following = [index]
        for other_index, other in enumerate(right, 1):
            following.append(min(following[-1] + 1, row[other_index] + 1, row[other_index - 1] + (word != other)))
        row = following
    return row[-1]


def lane_hypotheses(path: Path, started_at: float, source: str = "wifi") -> dict | None:
    payload = read_json(path)
    state = (payload.get("sources") or {}).get(source) or {}
    stages = {str(item.get("id") or ""): item for item in state.get("stages") or [] if isinstance(item, dict)}
    voicechat = stages.get("voicechat") or {}
    details = voicechat.get("payload") or {}
    try:
        updated_at = float(details.get("understanding_model_updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    primary = str(details.get("primary_hypothesis") or "").strip()
    fast = str(details.get("fast_hypothesis") or "").strip()
    if updated_at < started_at or not (primary or fast):
        return None
    return {
        "updated_at": updated_at,
        "primary": primary,
        "fast": fast,
        "primary_seconds": details.get("primary_asr_seconds"),
        "fast_seconds": details.get("fast_asr_seconds"),
        "parallel_model_seconds": details.get("parallel_model_seconds"),
        "guard_action": details.get("acoustic_guard_action"),
    }


def main() -> int:
    args = parse_args()
    lock_args = argparse.Namespace(
        speech_playback_lock_json=str(args.playback_lock_json),
        playback_lock_stale_seconds=35.0,
    )
    reference_words = words(args.reference)
    rows = []
    for index, audio in enumerate(args.audio, 1):
        flush_session(args.session_flush_url)
        time.sleep(max(3.5, args.post_flush_settle_seconds))
        audio_id = f"physical_voice_bakeoff_{index}_{audio.stem}_{int(time.time() * 1000)}"
        playback_audio = audio
        if args.production_playback_conditioning:
            playback_audio = smooth_playback_wav(
                argparse.Namespace(
                    audio_dir=str(args.output.parent / "conditioned_playback"),
                    server_playback_lead_silence_seconds=max(0.0, args.server_playback_lead_seconds),
                    playback_lead_silence_seconds=max(0.0, args.camera_playback_lead_seconds),
                    server_playback_fade_in_seconds=0.0,
                    playback_fade_out_seconds=0.025,
                    server_playback_wake_tone_frequency=180.0,
                    server_playback_wake_tone_volume=0.006,
                ),
                audio,
                f"physical_bakeoff_{index}_{audio.stem}",
                args.output_target,
            )
        started_at = time.time()
        lock_source = "wifi" if args.output_target == "wifi_camera" else "server"
        write_playback_lock(lock_args, True, lock_source, "playback", audio_id, "Physical Piper voice bakeoff playback is active.")
        if args.output_target == "wifi_camera":
            played, error = play_audio_on_wifi_camera(playback_audio, args.wifi_talk_audio_url, 30.0)
        else:
            played, error = play_audio_on_server(playback_audio, args.sink)
        write_playback_lock(lock_args, False, lock_source, "complete", audio_id, "Physical Piper voice bakeoff playback complete.")
        deadline = time.time() + max(5.0, args.timeout)
        hypotheses = None
        while time.time() < deadline:
            hypotheses = lane_hypotheses(args.response_json, started_at, args.listen_source)
            if hypotheses:
                break
            time.sleep(0.1)
        response_playback = {}
        if args.wait_for_response_playback:
            playback_deadline = time.time() + max(2.0, args.response_playback_timeout)
            seen_response_audio_id = ""
            while time.time() < playback_deadline:
                lane_lock = playback_lock_state(read_json(args.playback_lock_json), args.listen_source)
                updated_at = float(lane_lock.get("updated_at") or 0.0)
                candidate_audio_id = str(lane_lock.get("audio_id") or "")
                phase = str(lane_lock.get("phase") or "").strip().lower()
                if updated_at >= started_at and candidate_audio_id and candidate_audio_id != audio_id:
                    seen_response_audio_id = candidate_audio_id
                    response_playback = {
                        "audio_id": candidate_audio_id,
                        "phase": phase,
                        "active": bool(lane_lock.get("active")),
                        "message": str(lane_lock.get("message") or ""),
                        "updated_at": updated_at,
                    }
                    if phase in {"complete", "listening_ready"} and not bool(lane_lock.get("active")):
                        break
                time.sleep(0.05)
            response_playback["observed"] = bool(seen_response_audio_id)
            response_playback["completed"] = bool(
                response_playback.get("phase") in {"complete", "listening_ready"}
                and not response_playback.get("active")
            )
        row = {
            "voice": audio.name.removesuffix("_playback.wav"),
            "audio": str(audio.resolve()),
            "playback_audio": str(playback_audio.resolve()),
            "production_playback_conditioning": bool(args.production_playback_conditioning),
            "server_playback_lead_seconds": args.server_playback_lead_seconds,
            "camera_playback_lead_seconds": args.camera_playback_lead_seconds,
            "output_target": args.output_target,
            "listen_source": args.listen_source,
            "played": played,
            "playback_error": error,
            "physical_wave_audio_only": True,
            "cross_lane_backend_content": False,
            "hypotheses": hypotheses or {},
            "response_playback": response_playback,
        }
        for key in ("primary", "fast"):
            hypothesis = str((hypotheses or {}).get(key) or "")
            errors = edit_distance(reference_words, words(hypothesis)) if hypothesis else len(reference_words)
            row[f"{key}_errors"] = errors
            row[f"{key}_wer"] = round(errors / max(1, len(reference_words)), 6)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        flush_session(args.session_flush_url)
        time.sleep(max(1.0, min(2.0, args.post_flush_settle_seconds)))
    payload = {
        "reference": args.reference,
        "sink": args.sink,
        "output_target": args.output_target,
        "listen_source": args.listen_source,
        "volume_policy": "unchanged_external_sink_volume",
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

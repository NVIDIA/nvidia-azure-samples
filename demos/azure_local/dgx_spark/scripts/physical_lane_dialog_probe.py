#!/usr/bin/env python3
"""Launch one physical waveform and observe subsequent microphone-only lane bounces."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

from nemotron_voicechat_pipeline import (
    play_audio_on_server,
    play_audio_on_wifi_camera_with_telemetry,
    read_json,
    smooth_playback_wav,
    write_playback_lock,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output-target", choices=("server", "wifi_camera"), default="server")
    parser.add_argument("--sink", default="")
    parser.add_argument("--server-lead-seconds", type=float, default=0.65)
    parser.add_argument("--camera-lead-seconds", type=float, default=0.50)
    parser.add_argument("--wifi-talk-audio-url", default="http://127.0.0.1:8090/wifi-talk-audio")
    parser.add_argument("--session-flush-url", default="http://127.0.0.1:8090/session-flush")
    parser.add_argument(
        "--session-flush",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Flush lane-local dialog state before playback (use --no-session-flush for a follow-up turn).",
    )
    parser.add_argument("--response-json", type=Path, default=Path("webcam-voicechat-response.json"))
    parser.add_argument("--playback-lock-json", type=Path, default=Path("webcam-speech-playback-lock.json"))
    parser.add_argument("--max-receptions", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--quiet-seconds", type=float, default=12.0)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def flush_session(url: str) -> None:
    request = Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=10) as response:
        response.read()


def stage_snapshot(state: dict) -> dict:
    wanted = {
        "voice_activity",
        "short_burst_speculation",
        "voicechat",
        "echo_relation",
        "acoustic_guard",
        "disagreement_review",
        "tool_plan",
        "tool_call",
        "tool_results",
        "voicechat_answer",
        "tts",
        "output",
        "playback",
    }
    result = {}
    for stage in state.get("stages") or []:
        if not isinstance(stage, dict) or str(stage.get("id") or "") not in wanted:
            continue
        payload = stage.get("payload") if isinstance(stage.get("payload"), dict) else {}
        result[str(stage.get("id"))] = {
            "status": stage.get("status"),
            "duration_seconds": payload.get(
                "seconds",
                stage.get("duration_seconds", stage.get("last_duration_seconds")),
            ),
            "started_at": stage.get("started_at"),
            "completed_at": stage.get("completed_at"),
            "message": stage.get("message"),
            "primary_hypothesis": payload.get("primary_hypothesis"),
            "fast_hypothesis": payload.get("fast_hypothesis"),
            "primary_asr_seconds": payload.get("primary_asr_seconds"),
            "fast_asr_seconds": payload.get("fast_asr_seconds"),
            "parallel_model_seconds": payload.get("parallel_model_seconds"),
            "reply_seconds": payload.get("reply_seconds"),
            "acoustic_guard_seconds": payload.get("acoustic_guard_seconds"),
            "decision_parallel_seconds": payload.get("decision_parallel_seconds"),
            "acoustic_guard_action": payload.get("acoustic_guard_action"),
            "acoustic_guard_primary_action": payload.get("acoustic_guard_primary_action"),
            "acoustic_guard_relay_mode": payload.get("acoustic_guard_relay_mode"),
            "acoustic_guard_adjudicator_action": payload.get("acoustic_guard_adjudicator_action"),
            "acoustic_guard_adjudicator_seconds": payload.get("acoustic_guard_adjudicator_seconds"),
            "acoustic_disagreement_adjudicator_action": payload.get("acoustic_disagreement_adjudicator_action")
            or (payload.get("action") if str(stage.get("id") or "") == "disagreement_review" else None),
            "acoustic_disagreement_adjudicator_seconds": payload.get("acoustic_disagreement_adjudicator_seconds")
            if payload.get("acoustic_disagreement_adjudicator_seconds") is not None
            else (payload.get("model_seconds") if str(stage.get("id") or "") == "disagreement_review" else None),
            "echo_relation": payload.get("echo_relation"),
            "semantic_echo": payload.get("semantic_echo"),
            "current_to_previous": payload.get("current_to_previous"),
            "previous_to_current": payload.get("previous_to_current"),
            "minimum_bidirectional_entailment": payload.get("minimum_bidirectional_entailment"),
            "runtime_policy": payload.get("runtime_policy"),
            "skipped_model_call": payload.get("skipped_model_call"),
            "trusted_tool_direct_response": payload.get("trusted_tool_direct_response"),
            "trusted_tool_name": payload.get("trusted_tool_name"),
            "current_time_args_model_selected": payload.get("current_time_args_model_selected"),
            "acoustic_endpoint": payload.get("acoustic_endpoint"),
            "speculative_understanding_launched": payload.get("speculative_understanding_launched"),
            "speculative_understanding_reused": payload.get(
                "speculative_understanding_reused",
                payload.get("reused"),
            ),
            "speculative_understanding_wait_seconds": payload.get(
                "speculative_understanding_wait_seconds",
                payload.get("wait_seconds_at_endpoint"),
            ),
            "speculative_understanding_age_seconds": payload.get(
                "speculative_understanding_age_seconds",
                payload.get("age_seconds_at_endpoint"),
            ),
            "speculative_understanding_error": payload.get(
                "speculative_understanding_error",
                payload.get("error"),
            ),
            "speculative_understanding_invalidations": payload.get(
                "speculative_understanding_invalidations",
                payload.get("invalidations"),
            ),
            "speculative_understanding_last_invalidation_reason": payload.get(
                "speculative_understanding_last_invalidation_reason",
                payload.get("last_invalidation_reason"),
            ),
        }
    return result


def main() -> int:
    args = parse_args()
    if args.session_flush:
        flush_session(args.session_flush_url)
        time.sleep(4.0)
    conditioned = smooth_playback_wav(
        argparse.Namespace(
            audio_dir=str(args.output.parent / "conditioned_playback"),
            server_playback_lead_silence_seconds=max(0.0, args.server_lead_seconds),
            playback_lead_silence_seconds=max(0.0, args.camera_lead_seconds),
            server_playback_fade_in_seconds=0.0,
            playback_fade_out_seconds=0.025,
            server_playback_wake_tone_frequency=180.0,
            server_playback_wake_tone_volume=0.006,
        ),
        args.audio,
        f"dialog_probe_{int(time.time() * 1000)}",
        args.output_target,
    )
    started_at = time.time()
    audio_id = f"physical_dialog_probe_{int(started_at * 1000)}"
    lock_source = "wifi" if args.output_target == "wifi_camera" else "server"
    lock_args = argparse.Namespace(
        speech_playback_lock_json=str(args.playback_lock_json),
        playback_lock_stale_seconds=35.0,
    )
    playback_result: dict[str, object] = {"played": False, "error": "playback did not finish"}

    def launch_physical_audio() -> None:
        write_playback_lock(lock_args, True, lock_source, "playback", audio_id, "Physical dialog probe playback active.")
        try:
            if args.output_target == "wifi_camera":
                played, error, telemetry = play_audio_on_wifi_camera_with_telemetry(
                    conditioned, args.wifi_talk_audio_url, 30.0
                )
                playback_result["transport"] = telemetry
            else:
                played, error = play_audio_on_server(conditioned, args.sink)
            playback_result.update(played=played, error=error)
        finally:
            write_playback_lock(
                lock_args,
                False,
                lock_source,
                "complete",
                audio_id,
                "Physical dialog probe playback complete.",
            )

    playback_thread = threading.Thread(target=launch_physical_audio, name="physical-dialog-playback", daemon=True)
    playback_thread.start()

    receptions: list[dict] = []
    seen: set[tuple[str, str]] = set()
    reception_indexes: dict[tuple[str, str], int] = {}
    last_new = time.time()
    deadline = time.time() + max(10.0, args.timeout)
    max_receptions = max(1, args.max_receptions)
    while time.time() < deadline:
        payload = read_json(args.response_json)
        sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
        for source in ("server", "wifi"):
            state = sources.get(source) if isinstance(sources.get(source), dict) else {}
            heard = str(state.get("input_speech") or "").strip()
            updated_at = float(state.get("input_updated_at") or state.get("updated_at") or 0)
            run_state = state.get("run_state") if isinstance(state.get("run_state"), dict) else {}
            run_id = str(run_state.get("run_id") or state.get("run_id") or "").strip()
            key = (source, run_id or heard)
            is_placeholder = heard.lower().startswith("[audio input:")
            if heard and not is_placeholder and updated_at >= started_at:
                snapshot = {
                        "index": len(receptions) + 1,
                        "source": source,
                        "run_id": run_id,
                        "heard": heard,
                        "response_text": str(state.get("response_text") or "").strip(),
                        "status": state.get("status"),
                        "phase": state.get("phase"),
                        "input_updated_at": updated_at,
                        "observed_at": time.time(),
                        "seconds_since_launch": round(updated_at - started_at, 4),
                        "stages": stage_snapshot(state),
                    }
                if key in reception_indexes:
                    index = reception_indexes[key]
                    snapshot["index"] = index + 1
                    snapshot["first_observed_at"] = receptions[index].get(
                        "first_observed_at", receptions[index].get("observed_at")
                    )
                    receptions[index] = snapshot
                    continue
                if len(receptions) >= max_receptions:
                    continue
                seen.add(key)
                reception_indexes[key] = len(receptions)
                snapshot["first_observed_at"] = snapshot["observed_at"]
                receptions.append(snapshot)
                last_new = time.time()
                print(json.dumps(receptions[-1], ensure_ascii=False), flush=True)
        if len(receptions) >= max_receptions:
            last = receptions[-1]
            playback = (last.get("stages") or {}).get("playback") or {}
            if last.get("response_text") and playback.get("status") == "complete":
                break
            if time.time() - last_new >= 20.0:
                break
        elif receptions and not playback_thread.is_alive() and time.time() - last_new >= max(2.0, args.quiet_seconds):
            break
        time.sleep(0.05)

    playback_thread.join(timeout=35.0)
    played = bool(playback_result.get("played"))
    error = str(playback_result.get("error") or "")

    result = {
        "reference": args.reference,
        "audio": str(args.audio.resolve()),
        "conditioned_audio": str(conditioned.resolve()),
        "output_target": args.output_target,
        "server_lead_seconds": args.server_lead_seconds,
        "camera_lead_seconds": args.camera_lead_seconds,
        "played": played,
        "playback_error": error,
        "playback_transport": playback_result.get("transport") or {},
        "started_at": started_at,
        "physical_wave_audio_only": True,
        "cross_lane_backend_content": False,
        "volume_policy": "unchanged_external_levels",
        "session_flushed_before_playback": bool(args.session_flush),
        "reception_count": len(receptions),
        "receptions": receptions,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if played and receptions else 1


if __name__ == "__main__":
    raise SystemExit(main())

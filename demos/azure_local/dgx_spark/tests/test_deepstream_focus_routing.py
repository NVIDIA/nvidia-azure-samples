import argparse
import base64
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import nemotron_voice_responder as responder
from scripts import nemotron_voicechat_pipeline as pipeline
from scripts import deepstream_nemotron_worker as event_worker
from scripts import webcam_stream_server as server


def detection(label, confidence):
    return {"label": label, "confidence": confidence}


def test_camera_output_volumes_are_persisted_and_clamped_per_camera():
    defaults = server.normalize_component_audio_settings({})
    adjusted = server.normalize_component_audio_settings({
        "wifi_output_volume_percent": 37.5,
        "bulb_output_volume_percent": 140,
    })
    muted = server.normalize_component_audio_settings({"wifi_output_volume_percent": -10})

    assert defaults["wifi_output_volume_percent"] == 100
    assert defaults["bulb_output_volume_percent"] == 100
    assert adjusted["wifi_output_volume_percent"] == 37.5
    assert adjusted["bulb_output_volume_percent"] == 100
    assert muted["wifi_output_volume_percent"] == 0


def test_component_audio_controls_merge_as_independent_persistent_toggles():
    settings = server.normalize_component_audio_settings({
        "component_activation_audio_enabled": True,
        "speech_output_audio_enabled": False,
        "voice_input_enabled": False,
    })

    settings = server.merge_component_audio_settings(settings, {"component_activation_audio_enabled": False})
    settings = server.merge_component_audio_settings(settings, {"speech_output_audio_enabled": True})
    settings = server.merge_component_audio_settings(settings, {"voice_input_enabled": True})

    assert settings["component_activation_audio_enabled"] is False
    assert settings["speech_output_audio_enabled"] is True
    assert settings["voice_input_enabled"] is True


def test_focus_completion_chime_uses_a_lower_pitch():
    assert server.VOICECHAT_CUE_SPECS["focus_complete"]["frequency"] == pytest.approx(293.66)


def test_deepstream_object_crop_scales_bbox_to_captured_frame_with_padding():
    crop = server.deepstream_object_crop_box(
        {"bbox": [160, 120, 320, 240], "frame_width": 640, "frame_height": 480},
        image_width=1280,
        image_height=720,
        padding_ratio=0.1,
    )

    assert crop == (256, 116, 1024, 604)


def test_deepstream_object_crop_clamps_to_image_edges():
    crop = server.deepstream_object_crop_box(
        {"bbox": [0, 0, 100, 100], "frame_width": 640, "frame_height": 480},
        image_width=640,
        image_height=480,
    )

    assert crop == (0, 0, 108, 108)


def test_camera_output_volume_scaling_applies_gain_before_backend(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return argparse.Namespace(returncode=0, stdout=b"scaled-wav", stderr=b"")

    monkeypatch.setattr(server.subprocess, "run", run)

    payload, content_type = server.scale_camera_output_audio(b"source-audio", "audio/wav", 35)

    assert payload == b"scaled-wav"
    assert content_type == "audio/wav"
    assert captured["input"] == b"source-audio"
    assert "volume=0.350000,alimiter=limit=0.95:level=false" in captured["command"]
    assert server.scale_camera_output_audio(b"same", "audio/wav", 100) == (b"same", "audio/wav")

    payload, _content_type = server.scale_camera_output_audio(b"speech", "audio/wav", 100, 0.85)
    assert payload == b"scaled-wav"
    assert "volume=1.000000,alimiter=limit=0.95:level=false,atempo=0.850" in captured["command"]


def test_single_pass_camera_conditioning_preserves_fixed_gain_and_alaw_output(monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return argparse.Namespace(returncode=0, stdout=b"alaw", stderr=b"")

    monkeypatch.setattr(server.subprocess, "run", run)
    payload = server.condition_and_transcode_talk_audio(b"speech", "audio/wav", 70, 1.0)

    assert payload == b"alaw"
    assert captured["input"] == b"speech"
    assert "volume=0.700000,alimiter=limit=0.95:level=false" in captured["command"]
    assert captured["command"][captured["command"].index("-ar") + 1] == "8000"
    assert captured["command"][captured["command"].index("-f", 8) + 1] == "alaw"


def test_one_shot_native_camera_transport_reports_applied_slider_gain(tmp_path, monkeypatch):
    args = argparse.Namespace(
        voicechat_audio_dir=str(tmp_path),
        wifi_native_persistent_speaker=False,
        wifi_talk_transcode_timeout=15.0,
        wifi_rtsp_password_env="TEST_CAMERA_PASSWORD",
        wifi_camera_host="camera.local",
        wifi_camera_native_port=37777,
        wifi_rtsp_user="admin",
        wifi_talk_channel=1,
        wifi_native_speaker_timeout=30.0,
    )
    monkeypatch.setattr(
        server,
        "native_speaker_sdk_status",
        lambda _args: {
            "backend": "x64_netsdk_qemu",
            "available": True,
            "qemu": "/usr/bin/qemu-x86_64-static",
            "x64_sysroot": "/usr/x86_64-linux-gnu",
            "helper": "/tmp/speaker-helper",
            "sdk_lib": "/tmp/libdhnetsdk.so",
        },
    )
    monkeypatch.setattr(server, "rtsp_password", lambda _args: "")
    monkeypatch.setattr(server, "wifi_reader_args", lambda _args: argparse.Namespace())
    monkeypatch.setattr(server, "x64_native_speaker_mode_args", lambda: [])

    def condition(_payload, _content_type, volume_percent, _tempo, **kwargs):
        kwargs["telemetry"].update({
            "conditioning_pipeline": "inprocess_pcm16_polyphase_alaw_v1",
            "runtime": "numpy_scipy_audioop_lts",
            "ffmpeg_process_spawned": False,
        })
        assert volume_percent == 35
        return b"encoded-alaw"

    monkeypatch.setattr(server, "condition_and_transcode_talk_audio", condition)
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *args, **kwargs: argparse.Namespace(
            returncode=0,
            stdout='{"status":"ok","backend":"x64_netsdk_qemu"}',
            stderr="",
        ),
    )

    result = server.post_audio_to_native_camera(args, b"wav", "audio/wav", volume_percent=35)

    assert result["volume_percent"] == 35
    assert result["effective_gain"] == pytest.approx(0.35)
    assert result["conditioning_pipeline"] == "inprocess_pcm16_polyphase_alaw_v1"
    assert result["conditioning_passes"] == 1


def test_x64_native_speaker_uses_amcrest_legacy_talk_modes():
    assert server.x64_native_speaker_mode_args() == [
        "--server-mode-id", "0", "--transfer-mode-id", "5",
    ]


def test_talkback_releases_existing_persistent_ptz_session(monkeypatch):
    args = argparse.Namespace(
        wifi_camera_host="camera.local",
        wifi_camera_native_port=37777,
        wifi_rtsp_user="admin",
        wifi_rtsp_password_env="CAMERA_PASSWORD",
        wifi_ptz_channel=0,
    )

    class Worker:
        stopped = False

        def stop(self):
            self.stopped = True

    worker = Worker()
    monkeypatch.setattr(
        server,
        "_PERSISTENT_X64_NATIVE_PTZ",
        {server.persistent_x64_native_ptz_key(args): worker},
    )

    assert server.release_persistent_x64_native_ptz_for_talkback(args) is True
    assert worker.stopped is True


def test_native_talkback_retries_preplayback_login_failure(monkeypatch):
    args = argparse.Namespace(
        wifi_native_speaker_login_retries=2,
        wifi_native_speaker_login_retry_delay=0.1,
    )
    attempts = []
    releases = []
    sleeps = []

    def post(*_args, **_kwargs):
        attempts.append(len(attempts) + 1)
        if len(attempts) < 3:
            raise RuntimeError("CLIENT_LoginEx2 failed, sdk_error=0x8000006b, login_error=9")
        return {"status": "ok", "backend": "x64_netsdk_qemu"}

    monkeypatch.setattr(server, "post_audio_to_native_camera", post)
    monkeypatch.setattr(server, "release_persistent_x64_native_ptz_for_talkback", lambda _args: releases.append(True) or True)
    monkeypatch.setattr(server, "stop_persistent_x64_native_speaker", lambda: None)
    monkeypatch.setattr(server.time, "sleep", sleeps.append)

    result = server.post_audio_to_native_camera_resilient(args, b"wav", "audio/wav")

    assert attempts == [1, 2, 3]
    assert len(releases) == 3
    assert sleeps == [pytest.approx(0.1), pytest.approx(0.2)]
    assert result["login_attempts"] == 3
    assert result["login_retries"] == 2
    assert len(result["login_retry_errors"]) == 2
    assert result["released_ptz_session"] is True


def test_native_talkback_does_not_retry_ambiguous_playback_failure(monkeypatch):
    args = argparse.Namespace(
        wifi_native_speaker_login_retries=2,
        wifi_native_speaker_login_retry_delay=0.1,
    )
    attempts = []

    def post(*_args, **_kwargs):
        attempts.append(True)
        raise RuntimeError("timed out waiting for speaker sent event")

    monkeypatch.setattr(server, "post_audio_to_native_camera", post)
    monkeypatch.setattr(server, "release_persistent_x64_native_ptz_for_talkback", lambda _args: False)

    with pytest.raises(RuntimeError, match="sent event"):
        server.post_audio_to_native_camera_resilient(args, b"wav", "audio/wav")

    assert attempts == [True]


def test_manual_ptz_pulse_pauses_active_focus_without_cancelling_it(tmp_path):
    command_path = tmp_path / "focus.json"
    command_path.write_text(json.dumps({
        "enabled": True,
        "source": "wifi",
        "request_id": "focus-1",
        "expires_at": 200.0,
    }))

    updated = server.pause_focus_for_manual_ptz(
        command_path, "wifi", "pulse", duration_ms=500, now=100.0, settle_seconds=0.75,
    )

    assert updated["enabled"] is True
    assert updated["request_id"] == "focus-1"
    assert updated["manual_ptz_active"] is False
    assert updated["manual_ptz_pause_until"] == pytest.approx(101.25)
    assert updated["expires_at"] == pytest.approx(201.25)
    assert "completion_status" not in updated


def test_manual_ptz_start_stays_paused_until_stop_and_extends_focus_deadline(tmp_path):
    command_path = tmp_path / "focus.json"
    command_path.write_text(json.dumps({
        "enabled": True,
        "source": "wifi",
        "request_id": "focus-1",
        "expires_at": 200.0,
    }))

    started = server.pause_focus_for_manual_ptz(command_path, "wifi", "start", now=100.0)
    stopped = server.pause_focus_for_manual_ptz(command_path, "wifi", "stop", now=102.0, settle_seconds=0.75)

    assert started["manual_ptz_active"] is True
    assert stopped["enabled"] is True
    assert stopped["manual_ptz_active"] is False
    assert stopped["manual_ptz_pause_until"] == pytest.approx(102.75)
    assert stopped["expires_at"] == pytest.approx(202.75)


def test_overlapping_manual_pulses_extend_one_contiguous_pause_without_double_counting(tmp_path):
    command_path = tmp_path / "focus.json"
    command_path.write_text(json.dumps({
        "enabled": True,
        "source": "wifi",
        "request_id": "focus-1",
        "expires_at": 200.0,
    }))

    server.pause_focus_for_manual_ptz(
        command_path, "wifi", "pulse", duration_ms=500, now=100.0, settle_seconds=0.75,
    )
    updated = server.pause_focus_for_manual_ptz(
        command_path, "wifi", "pulse", duration_ms=500, now=100.5, settle_seconds=0.75,
    )

    assert updated["manual_ptz_pause_started_at"] == 100.0
    assert updated["manual_ptz_pause_until"] == pytest.approx(101.75)
    assert updated["manual_ptz_paused_seconds"] == 0.0
    assert updated["expires_at"] == pytest.approx(201.75)


def test_background_deepstream_monitor_drives_endpoint_without_ui(monkeypatch):
    stop = threading.Event()
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 2 * 1024 * 1024
            stop.set()
            return b"{}"

    def fake_urlopen(request, timeout, context=None):
        requests.append((request, timeout, context))
        return Response()

    monkeypatch.setattr(server, "urlopen", fake_urlopen)

    server.deepstream_notification_monitor(
        stop,
        "http://127.0.0.1:8090/deepstream-detections.json?source=wifi",
        0.1,
    )

    assert len(requests) == 1
    request, timeout, context = requests[0]
    assert request.full_url.endswith("/deepstream-detections.json?source=wifi")
    assert request.get_header("X-internal-monitor") == "deepstream"
    assert timeout == 1.0
    assert context is None


def test_deepstream_event_does_not_focus_when_no_preferred_object_is_visible():
    objects = [
        detection("clock", 0.91),
        detection("tv", 0.98),
        detection("dining table", 0.93),
    ]

    prompt, target = server.deepstream_nemotron_event_prompt("wifi", "new objects", objects)

    assert target == {}
    assert "no focus tool call is required" in prompt.lower()


def test_realtime_latched_event_is_summarized_without_duplicate_focus():
    prompt, target = server.deepstream_nemotron_event_prompt(
        "wifi",
        "person appeared",
        [detection("person", 0.9)],
        ["person"],
        allow_focus=False,
    )
    assert target == {}
    assert "no focus tool call is required" in prompt.lower()


def test_deepstream_event_omits_focus_for_empty_or_non_ptz_source():
    empty_prompt, empty_target = server.deepstream_nemotron_event_prompt("wifi", "objects gone", [])
    server_prompt, server_target = server.deepstream_nemotron_event_prompt(
        "server", "new clock", [detection("clock", 0.99)]
    )

    assert empty_target == {}
    assert server_target == {}
    assert "no focus tool call is required" in empty_prompt
    assert "no focus tool call is required" in server_prompt


def test_preferred_object_order_overrides_detection_confidence():
    objects = [
        detection("tv", 0.99),
        detection("dog", 0.91),
        detection("person", 0.90),
    ]

    target = server.deepstream_focus_target(objects, "wifi", ["person", "dog"])

    assert target["target"] == "person"
    assert target["probability"] == 0.90
    assert target["preferred"] is True
    assert target["preferred_rank"] == 0


def test_highest_confidence_wins_within_first_matching_preferred_class():
    objects = [
        detection("cat", 0.91),
        detection("cat", 0.97),
        detection("person", 0.99),
    ]

    target = server.deepstream_focus_target(objects, "wifi", ["cat", "person"])

    assert target["target"] == "cat"
    assert target["probability"] == 0.97
    assert target["preferred_rank"] == 0


def test_preferred_object_normalization_is_ordered_and_persistent():
    assert server.normalize_deepstream_preferred_objects(" Person, dog\nCAT\ndog ") == [
        "person",
        "dog",
        "cat",
    ]
    settings = server.normalize_deepstream_settings({"preferred_objects": []})
    assert settings["preferred_objects"] == []
    assert server.normalize_deepstream_settings({"focus_max_steps": 37})["focus_max_steps"] == 37
    assert server.normalize_deepstream_settings({"focus_max_steps": 999})["focus_max_steps"] == 100


def test_deepstream_requests_only_skip_planning_when_explicitly_requested():
    request = {"kind": "deepstream_object_change", "trigger": "deepstream_yolo_coco"}

    assert pipeline.manual_text_request_is_deepstream_event(request) is True
    assert pipeline.manual_text_request_skips_tool_planning(request) is False
    assert pipeline.manual_text_request_skips_tool_planning({**request, "skip_tool_planning": True}) is True


def test_focus_acquisition_uses_current_target_and_rejects_stale_person_caption():
    request = {
        "kind": "deepstream_object_change",
        "trigger": "focus_object_acquired",
        "deepstream_event": {"target_label": "cat"},
    }
    stale_person_caption = (
        "Priority 1 object cat acquired. Wearing a dark jacket, light-colored shirt, and jeans. "
        "Standing near a ceiling fan in a blue-walled room with a closet visible."
    )

    assert pipeline.manual_text_request_is_focus_acquisition(request) is True
    assert pipeline.manual_text_request_uses_lane_context(request) is True
    assert pipeline.manual_text_request_focus_label(request) == "cat"
    assert "single attached focus-object image" in pipeline.focus_acquisition_instruction("cat")
    assert pipeline.focus_description_conflicts_with_target("cat", stale_person_caption) is True
    assert pipeline.focus_description_conflicts_with_target(
        "cat",
        "Priority 1 object cat acquired. A black-and-white cat is lying on a ledge.",
    ) is False
    assert pipeline.focus_description_conflicts_with_target("person", stale_person_caption) is False


def test_only_completed_focus_events_join_the_lane_agent_thread():
    generic_event = {"kind": "deepstream_object_change", "trigger": "deepstream_yolo_coco"}
    human_turn = {"kind": "manual_text", "trigger": "human"}

    assert pipeline.manual_text_request_uses_lane_context(generic_event) is False
    assert pipeline.manual_text_request_uses_lane_context(human_turn) is True


def test_focus_acquisition_label_falls_back_to_focus_tool_result():
    request = {
        "kind": "deepstream_object_change",
        "trigger": "focus_object_acquired",
        "tool_results": [{"name": "focus_object", "result": {"target_label": "Keyboard"}}],
    }

    assert pipeline.manual_text_request_focus_label(request) == "keyboard"


def test_boxed_deepstream_event_is_grounded_only_in_current_frame():
    request = {
        "kind": "deepstream_object_change",
        "trigger": "deepstream_yolo_coco",
        "deepstream_event": {
            "objects": [
                {"label": "person", "confidence": 0.91},
                {"label": "keyboard", "confidence": 0.88},
            ]
        },
    }

    assert pipeline.deepstream_event_labels(request) == ["person", "keyboard"]
    instruction = pipeline.deepstream_event_instruction(request)
    assert "person, keyboard" in instruction
    assert "Ignore prior conversation and cached scene descriptions" in instruction


def test_final_response_removes_internal_tool_annotation():
    answer = pipeline.sanitize_final_response(
        "Live Stream Processor object-change event",
        "A person is visible beside a desk. [tools: deepstream_yolo_coco]",
        {"needs_tools": False},
        [],
        [],
    )

    assert answer == "A person is visible beside a desk."


def test_noop_response_requires_exact_normalized_sentinel():
    assert pipeline.is_noop_response("<noop>") is True
    assert pipeline.is_noop_response("  <NOOP>\n") is True
    assert pipeline.is_noop_response('{"response":"<noop>"}') is True
    assert pipeline.is_noop_response("Commentary <noop>") is False
    assert pipeline.is_noop_response("<noop> commentary") is False


def test_deepstream_noop_is_not_published_as_assistant_dialog_or_sent_to_tts(tmp_path, monkeypatch):
    args = event_worker.make_pipeline_args(
        argparse.Namespace(
            input_json=str(tmp_path / "events.json"),
            response_json=str(tmp_path / "response.json"),
            session_reset_json=str(tmp_path / "reset.json"),
            output_target_json=str(tmp_path / "output.json"),
            playback_lock_json=str(tmp_path / "playback.json"),
            component_audio_settings_json=str(tmp_path / "audio-settings.json"),
            tool_planner_queue_json=str(tmp_path / "planner.json"),
            focus_object_command_json=str(tmp_path / "focus-command.json"),
            focus_object_state_json=str(tmp_path / "focus-state.json"),
            server_agent_state_json=str(tmp_path / "server-agent.json"),
            browser_agent_state_json=str(tmp_path / "browser-agent.json"),
            wifi_agent_state_json=str(tmp_path / "wifi-agent.json"),
            bulb_agent_state_json=str(tmp_path / "bulb-agent.json"),
            environment_wake_json=str(tmp_path / "wake.json"),
            audio_dir=str(tmp_path / "audio"),
            ollama_url="http://127.0.0.1:11434",
            understanding_model="nemotron3-voice-fast:latest",
            answer_model="nemotron-spark:latest",
            answer_timeout=180.0,
            answer_max_tokens=320,
            answer_num_ctx=4096,
            focus_object_timeout=180.0,
        )
    )
    published = []
    monkeypatch.setattr(pipeline, "publish", lambda _args, payload: published.append(payload))
    monkeypatch.setattr(pipeline, "environment_state", lambda *_args: {})
    monkeypatch.setattr(pipeline, "lane_conversation", lambda *_args: [])
    monkeypatch.setattr(pipeline, "play_pipeline_stage_chime", lambda *_args: (False, "disabled"))
    monkeypatch.setattr(
        pipeline,
        "run_ollama_tool_answer",
        lambda *_args, **_kwargs: {
            "backend": "vllm",
            "model": "nemotron-spark:latest",
            "response_text": "<noop>",
            "raw_response": "<noop>",
            "token_usage": {},
        },
    )
    monkeypatch.setattr(
        pipeline,
        "synthesize_tts_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("noop must not reach TTS")),
    )
    request = {
        "id": "event-noop-1",
        "source": "wifi",
        "text": "Live Stream Processor object-change event from Wi-Fi camera.",
        "kind": "deepstream_object_change",
        "trigger": "deepstream_yolo_coco",
        "skip_tool_planning": True,
        "tool_results": [
            {
                "name": "deepstream_yolo_coco",
                "status": "complete",
                "result": {"direct_answer": "Live Stream Processor detected: person: 93%."},
            }
        ],
    }

    pipeline.process_manual_text_request(args, request, "vllm", object())

    completed = published[-1]
    assert completed["phase"] == "complete"
    assert completed["response_text"] == ""
    assert completed["response_suppressed"] is True
    assert completed["suppression_sentinel"] == "<noop>"
    assert [turn["role"] for turn in completed["conversation"]] == ["user"]
    assert completed["conversation"][0]["response_suppressed"] is True
    assert completed["speech_output_audio_enabled"] is False


def test_deepstream_structured_focus_target_builds_exact_tool_call():
    request = {
        "kind": "deepstream_object_change",
        "source": "wifi",
        "focus_target": {"source": "wifi", "target": "Clock", "probability": 0.96},
    }

    assert pipeline.manual_text_request_focus_call(request) == {
        "name": "focus_object",
        "args": {"source": "wifi", "target": "clock"},
    }
    assert pipeline.manual_text_request_focus_call({**request, "focus_target": {}}) is None
    assert pipeline.manual_text_request_focus_call({**request, "source": "server", "focus_target": {"source": "server", "target": "clock"}}) is None


def test_prestarted_deepstream_focus_request_is_reused_by_tool_call():
    request = {
        "kind": "deepstream_object_change",
        "source": "wifi",
        "focus_target": {"source": "wifi", "target": "cat"},
        "focus_request": {"request_id": "focus-fast-1"},
    }

    assert pipeline.manual_text_request_focus_call(request) == {
        "name": "focus_object",
        "args": {"source": "wifi", "target": "cat", "request_id": "focus-fast-1"},
    }


def test_deepstream_focus_request_starts_before_notification_work(tmp_path):
    command_path = tmp_path / "focus-command.json"

    request = server.start_deepstream_focus_request(
        command_path,
        {"source": "wifi", "target": "Clock"},
        timeout_seconds=30,
        now=100.0,
    )

    stored = json.loads(command_path.read_text())
    assert stored == request
    assert stored["enabled"] is True
    assert stored["source"] == "wifi"
    assert stored["target_label"] == "clock"
    assert stored["requested_at"] == 100.0
    assert stored["expires_at"] == 130.0
    assert stored["trigger"] == "deepstream_yolo_coco"


def test_deepstream_focus_request_preserves_preferred_priority(tmp_path):
    command_path = tmp_path / "focus-command.json"

    request = server.start_deepstream_focus_request(
        command_path,
        {"source": "wifi", "target": "Clock", "preferred_rank": 1},
        timeout_seconds=30,
        now=100.0,
    )

    assert request["preferred_rank"] == 1
    assert request["priority"] == 2


def test_realtime_focus_completion_queues_priority_and_boxed_image(tmp_path, monkeypatch):
    focus_state_path = tmp_path / "focus-state.json"
    focus_state_path.write_text(json.dumps({
        "request_id": "focus-2",
        "status": "complete",
        "target_label": "clock",
        "bbox": [40, 30, 20, 20],
        "frame_size": [100, 100],
    }))
    args = argparse.Namespace(
        focus_object_state_path=str(focus_state_path),
        deepstream_nemotron_input_path=str(tmp_path / "nemotron-input.json"),
        voicechat_manual_input_path=str(tmp_path / "voice-input.json"),
        nemotron_system_prompt_path=str(tmp_path / "nemotron-system-prompt.json"),
    )
    server.write_json_file(
        Path(args.nemotron_system_prompt_path),
        {"system_prompt": "Always end every sentence with a smiley face."},
    )
    handler_type = server.make_handler(
        None, None, None, None, None, None, None, None, None, None, args,
    )
    handler = handler_type.__new__(handler_type)
    handler.save_focus_acquisition_image = lambda _source, _event_id, _state: {
        "url": "/deepstream-event-image.jpg?id=focus-2.jpg",
        "path": str(tmp_path / "focus-2.jpg"),
        "data_url": "data:image/jpeg;base64,boxed",
        "bytes": 5,
    }
    queued = {}

    def capture_queue(_args, source, text, extra, queue_path=None):
        queued.update({"source": source, "text": text, "extra": extra, "queue_path": queue_path})
        return {"id": "manual-focus-2", "source": source, "text": text}

    monkeypatch.setattr(server, "append_manual_voicechat_input", capture_queue)

    result = handler.queue_realtime_focus_acquisition(
        "wifi",
        {"preferred_objects": ["cat", "clock", "person"]},
        {
            "request_id": "focus-2",
            "source": "wifi",
            "target_label": "clock",
            "completion_status": "complete",
            "trigger": "realtime_deepstream_focus",
            "priority": 2,
        },
    )

    assert result["id"] == "manual-focus-2"
    assert queued["text"] == "Priority 2 object clock acquired."
    assert queued["queue_path"] == Path(args.voicechat_manual_input_path)
    assert queued["extra"]["skip_tool_planning"] is True
    assert queued["extra"]["system_prompt"] == "Always end every sentence with a smiley face."
    assert queued["extra"]["input_attachments"][0]["src"].endswith("focus-2.jpg")
    tool_result = queued["extra"]["tool_results"][0]["result"]
    assert tool_result["priority"] == 2
    assert tool_result["image_data_url"] == "data:image/jpeg;base64,boxed"
    assert tool_result["snapshot_images"][0]["kind"] == "focus_object_crop"


def test_focus_acquisition_image_crops_completed_target_bbox(tmp_path, monkeypatch):
    args = argparse.Namespace()
    handler_type = server.make_handler(
        None, None, None, None, None, None, None, None, None, None, args,
    )
    handler = handler_type.__new__(handler_type)
    source = io.BytesIO()
    Image.new("RGB", (100, 80), (20, 30, 40)).save(source, format="JPEG")

    class FrameStore:
        @staticmethod
        def wait_for_frame(_last_id, timeout=0):
            return 1, source.getvalue()

    handler.frame_store_for_deepstream_source = lambda _source: (FrameStore(), "")
    monkeypatch.setattr(server, "DEEPSTREAM_EVENT_IMAGE_DIR", tmp_path)

    result = handler.save_focus_acquisition_image(
        "wifi",
        "focus-crop",
        {"target_label": "laptop", "bbox": [20, 15, 40, 25], "frame_size": [100, 80]},
    )

    encoded = result["data_url"].split(",", 1)[1]
    crop = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert crop.size == (40, 25)
    assert result["bbox"] == [20, 15, 40, 25]
    assert result["crop_box"] == [20, 15, 60, 40]


def test_focus_acquisition_crop_prefers_raw_synchronized_deepstream_preview(tmp_path, monkeypatch):
    preview_path = tmp_path / "preview.jpg"
    raw_preview_path = server.deepstream_synced_preview_raw_path(preview_path, "wifi")
    synced = io.BytesIO()
    Image.new("RGB", (100, 80), (20, 180, 40)).save(synced, format="JPEG")
    raw_preview_path.write_bytes(synced.getvalue())
    args = argparse.Namespace(deepstream_synced_preview_path=str(preview_path))
    handler_type = server.make_handler(
        None, None, None, None, None, None, None, None, None, None, args,
    )
    handler = handler_type.__new__(handler_type)
    camera = io.BytesIO()
    Image.new("RGB", (100, 80), (180, 20, 40)).save(camera, format="JPEG")

    class FrameStore:
        @staticmethod
        def wait_for_frame(_last_id, timeout=0):
            return 1, camera.getvalue()

    handler.frame_store_for_deepstream_source = lambda _source: (FrameStore(), "")
    monkeypatch.setattr(server, "DEEPSTREAM_EVENT_IMAGE_DIR", tmp_path / "events")

    result = handler.save_focus_acquisition_image(
        "wifi",
        "focus-synced",
        {"target_label": "laptop", "bbox": [20, 15, 40, 25], "frame_size": [100, 80]},
    )

    crop = Image.open(io.BytesIO(base64.b64decode(result["data_url"].split(",", 1)[1])))
    red, green, _blue = crop.getpixel((crop.width // 2, crop.height // 2))
    assert green > red
    assert result["frame_source"] == "deepstream_synced_preview"


def test_generic_object_change_notifications_are_disabled():
    assert server.DEEPSTREAM_OBJECT_CHANGE_NOTIFICATIONS_ENABLED is False


def test_deepstream_queue_preserves_structured_focus_target():
    target = {"source": "wifi", "target": "clock", "probability": 0.96}

    assert server.clean_manual_voicechat_extra({"focus_target": target})["focus_target"] == target
    focus_request = {"request_id": "focus-fast-1"}
    assert server.clean_manual_voicechat_extra({"focus_request": focus_request})["focus_request"] == focus_request


def test_deepstream_uses_dedicated_queue_instead_of_voice_queue(tmp_path):
    voice_queue = tmp_path / "voice.json"
    event_queue = tmp_path / "events.json"
    args = argparse.Namespace(voicechat_manual_input_path=str(voice_queue))
    extra = {"kind": "deepstream_object_change", "trigger": "deepstream_yolo_coco"}

    request = server.append_manual_voicechat_input(
        args,
        "wifi",
        "focus on the cat, then summarize",
        extra,
        queue_path=event_queue,
    )

    assert request["source"] == "wifi"
    assert event_queue.exists()
    assert not voice_queue.exists()


def test_event_worker_runs_text_processor_with_routed_tts_and_without_audio_capture_loop(tmp_path, monkeypatch):
    args = event_worker.make_pipeline_args(
        argparse.Namespace(
            input_json=str(tmp_path / "events.json"),
            response_json=str(tmp_path / "response.json"),
            session_reset_json=str(tmp_path / "reset.json"),
            output_target_json=str(tmp_path / "output.json"),
            playback_lock_json=str(tmp_path / "playback.json"),
            component_audio_settings_json=str(tmp_path / "audio-settings.json"),
            tool_planner_queue_json=str(tmp_path / "planner.json"),
            focus_object_command_json=str(tmp_path / "focus-command.json"),
            focus_object_state_json=str(tmp_path / "focus-state.json"),
            server_agent_state_json=str(tmp_path / "server-agent.json"),
            browser_agent_state_json=str(tmp_path / "browser-agent.json"),
            wifi_agent_state_json=str(tmp_path / "wifi-agent.json"),
            bulb_agent_state_json=str(tmp_path / "bulb-agent.json"),
            environment_wake_json=str(tmp_path / "wake.json"),
            audio_dir=str(tmp_path / "audio"),
            ollama_url="http://127.0.0.1:11434",
            understanding_model="nemotron3-voice-fast:latest",
            answer_model="nemotron-spark:latest",
            answer_timeout=180.0,
            answer_max_tokens=320,
            answer_num_ctx=4096,
            focus_object_timeout=180.0,
        )
    )
    calls = []
    monkeypatch.setattr(event_worker.pipeline, "publish_manual_text_received", lambda *items: calls.append(("received", items[1])))
    monkeypatch.setattr(
        event_worker.pipeline,
        "process_manual_text_request",
        lambda *items: calls.append(("processed", items[1], items[0].event_speech_output)),
    )

    request = {"id": "event-1", "source": "wifi", "text": "focus on the cat, then summarize"}
    event_worker.process_one(args, request)

    assert args.event_only is True
    assert args.event_speech_output is True
    assert args.ollama_model == "nemotron3-voice-fast:latest"
    assert args.answer_model == "nemotron-spark:latest"
    assert args.tts_backend == "kokoro"
    assert args.kokoro_voice == "af_heart"
    assert args.kokoro_device == "cuda"
    assert args.kokoro_warmup is True
    assert args.native_audio_voice == "Sofia"
    assert args.magpie_voice == "Sofia"
    assert args.magpie_speaker_index == 1
    assert args.magpie_warmup is False
    assert args.answer_keep_alive == "-1"
    assert args.output_target_mode == "auto"
    assert args.wifi_talk_audio_url.endswith("/wifi-talk-audio")
    assert args.bulb_talk_audio_url.endswith("/bulb-talk-audio")
    assert event_worker.pipeline.component_activation_audio_enabled(args) is False
    assert event_worker.pipeline.speech_output_audio_enabled(args) is True
    assert [item[0] for item in calls] == ["received", "processed"]
    assert calls[1][1] == request
    assert calls[1][2] is False

    calls.clear()
    focus_request = {
        "id": "focus-event-1",
        "source": "wifi",
        "text": "Priority 3 object laptop acquired.",
        "kind": "deepstream_object_change",
        "trigger": "focus_object_acquired",
    }
    event_worker.process_one(args, focus_request)

    assert calls[1][1] == focus_request
    assert calls[1][2] is True


def test_automated_vision_waits_for_recent_voice_dialog(tmp_path):
    response_path = tmp_path / "response.json"
    lock_path = tmp_path / "playback.json"
    response_path.write_text(json.dumps({
        "conversation": [
            {"role": "user", "source": "server", "text": "laptop on desk", "updated_at": 100.0},
            {"role": "assistant", "phase": "voicechat_answer", "text": "I heard you.", "updated_at": 100.5},
        ]
    }))
    lock_path.write_text(json.dumps({"sources": {}}))
    args = argparse.Namespace(dialog_quiet_seconds=8.0)
    pipeline_args = argparse.Namespace(
        voicechat_response_json=str(response_path),
        speech_playback_lock_json=str(lock_path),
    )

    assert event_worker.latest_dialog_activity_at(json.loads(response_path.read_text())) == pytest.approx(100.5)
    assert event_worker.dialog_has_priority(args, pipeline_args, now=107.0) is True
    assert event_worker.dialog_has_priority(args, pipeline_args, now=109.0) is False


def test_automated_vision_history_does_not_extend_dialog_priority(tmp_path):
    response_path = tmp_path / "response.json"
    lock_path = tmp_path / "playback.json"
    response_path.write_text(json.dumps({
        "conversation": [
            {"role": "user", "source": "server", "text": "hello", "updated_at": 10.0},
            {"role": "assistant", "text": "hello", "updated_at": 10.5},
            {
                "role": "user",
                "label": "Live Stream Processor",
                "phase": "deepstream_object_change",
                "text": "person detected",
                "updated_at": 100.0,
            },
            {"role": "assistant", "phase": "voicechat_answer", "text": "Person detected.", "updated_at": 100.5},
        ]
    }))
    lock_path.write_text(json.dumps({"sources": {}}))
    args = argparse.Namespace(dialog_quiet_seconds=8.0)
    pipeline_args = argparse.Namespace(
        voicechat_response_json=str(response_path),
        speech_playback_lock_json=str(lock_path),
    )

    assert event_worker.latest_dialog_activity_at(json.loads(response_path.read_text())) == pytest.approx(10.5)
    assert event_worker.dialog_has_priority(args, pipeline_args, now=101.0) is False


def test_custom_nemotron_prompt_is_sent_as_actual_system_message(monkeypatch):
    captured = {}
    args = argparse.Namespace(
        answer_model="nemotron3-voice-fast:latest",
        ollama_model="nemotron3-voice-fast:latest",
        max_response_words=80,
        request_native_audio=False,
        answer_keep_alive="-1",
        answer_timeout=60.0,
        ollama_timeout=60.0,
        ollama_url="http://127.0.0.1:11434",
        ollama_openai_path="/v1/chat/completions",
    )
    observed = {}

    def fake_multimodal(_args, _source, prompt, *_items, **_kwargs):
        observed["prompt"] = prompt
        return ([{"type": "text", "text": "event"}], {})

    monkeypatch.setattr(
        pipeline,
        "omni_multimodal_content",
        fake_multimodal,
    )

    def fake_ollama(_url, _path, payload, _timeout, **_kwargs):
        captured["path"] = _path
        captured.update(payload)
        return {"choices": [{"message": {"content": "A funny note."}}]}

    monkeypatch.setattr(pipeline, "ollama_json", fake_ollama)

    pipeline.run_ollama_tool_answer(
        args,
        "wifi",
        {},
        "Live Stream Processor event",
        "",
        "No tools were used.",
        [],
        None,
        [],
        "Always add a funny note at the end.",
    )

    assert captured["messages"][0] == {
        "role": "system",
        "content": "Always add a funny note at the end.",
    }
    assert "overrides conflicting style patterns" in captured["messages"][1]["content"]
    assert captured["messages"][2]["role"] == "user"
    assert "Controller instruction: Always add a funny note at the end." in observed["prompt"]


def test_super_final_answer_is_text_only_and_receives_voice_fast_visual_state(monkeypatch):
    captured = {}
    args = argparse.Namespace(
        answer_model="nemotron-spark:latest",
        ollama_model="nemotron3-voice-fast:latest",
        max_response_words=80,
        request_native_audio=False,
        answer_keep_alive="-1",
        answer_timeout=180.0,
        ollama_timeout=180.0,
        ollama_url="http://127.0.0.1:11434",
        ollama_openai_path="/v1/chat/completions",
    )
    monkeypatch.setattr(
        pipeline,
        "omni_multimodal_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Super must not receive images or audio")),
    )

    def fake_ollama(_url, _path, payload, _timeout, **_kwargs):
        captured["path"] = _path
        captured.update(payload)
        return {"choices": [{"message": {"content": "Final commentary."}}]}

    monkeypatch.setattr(pipeline, "ollama_json", fake_ollama)
    result = pipeline.run_ollama_tool_answer(
        args,
        "wifi",
        {},
        "Live Stream Processor event",
        "",
        "Detected person at 93%.",
        [],
        None,
        [],
        "Add a funny note.",
        upstream_visual_state="A person is seated beside a laptop.",
    )

    assert captured["model"] == "nemotron-spark:latest"
    assert captured["path"] == "/api/chat"
    assert captured["keep_alive"] == -1
    assert captured["options"]["num_ctx"] == 32768
    assert captured["messages"][0] == {"role": "system", "content": "Add a funny note."}
    assert isinstance(captured["messages"][2]["content"], str)
    assert "Upstream multimodal visual understanding" in captured["messages"][2]["content"]
    assert "A person is seated beside a laptop." in captured["messages"][2]["content"]
    assert result["input_mode"] == "text_only"
    assert result["context_window_tokens"] == 32768


def test_flow_stages_show_distinct_understanding_and_super_models(monkeypatch):
    monkeypatch.setattr(pipeline, "ACTIVE_OLLAMA_VOICECHAT_MODEL", "nemotron3-voice-fast:latest")
    monkeypatch.setattr(pipeline, "ACTIVE_ANSWER_MODEL", "nemotron-spark:latest")

    stages = pipeline.voicechat_stages(
        "complete",
        "complete",
        "complete",
        "waiting",
        "waiting",
        "testing",
        backend="ollama",
        payloads={"voicechat_answer": {"model": "nemotron-spark:latest"}},
        answer="active",
    )
    by_id = {stage["id"]: stage for stage in stages}

    assert "Speech Recognition (nemotron3-voice-fast:latest)" in by_id["voicechat"]["title"]
    assert by_id["fast_asr"]["payload"]["authoritative"] is False
    assert by_id["voicechat"]["payload"]["model"] == "nemotron3-voice-fast:latest"
    assert by_id["voicechat_answer"]["title"] == "Nemotron 3 Super Text Commentary (nemotron-spark:latest)"
    assert by_id["voicechat_answer"]["payload"]["model"] == "nemotron-spark:latest"
    assert by_id["voicechat_answer"]["payload"]["input_mode"] == "text_only"
    assert by_id["voicechat_answer"]["payload"]["context_window_tokens"] == 32768
    assert by_id["acoustic_guard"]["payload"]["decision_policy"] == "ai_model_only"
    assert by_id["acoustic_guard"]["title"].startswith("Model-Only Acoustic and Echo Loop Guard")
    assert by_id["acoustic_guard"]["payload"]["echo_benchmark_accuracy"] == 1.0
    assert by_id["acoustic_guard"]["payload"]["benchmark_cases"] == 40
    assert by_id["acoustic_guard"]["payload"]["localized_disagreement_accuracy"] == 1.0
    assert by_id["acoustic_guard"]["payload"]["cross_lane_backend_content"] is False
    assert by_id["acoustic_preprocessing"]["title"] == "Content-Agnostic Acoustic Preprocessing Gate"
    assert by_id["acoustic_preprocessing"]["status"] == "complete"
    assert by_id["acoustic_preprocessing"]["payload"]["mode"] == "bypass"
    assert by_id["acoustic_preprocessing"]["payload"]["samples_modified"] is False
    assert by_id["acoustic_preprocessing"]["payload"]["volume_or_rms_normalization"] is False
    assert by_id["acoustic_preprocessing"]["payload"]["deployment_decision"] == "rejected_no_accuracy_improvement"
    assert by_id["tool_plan"]["title"] == "Model-Only Tool Decision (nemotron-spark:latest)"
    assert by_id["tool_plan"]["payload"]["decision_policy"] == "ai_model_only"
    assert by_id["tool_plan"]["payload"]["decision_contract"] == "sparse_single_action_v31_time_granularity"
    assert by_id["tool_plan"]["payload"]["statement_ack_grounded_cases"] == 11
    assert by_id["tool_plan"]["payload"]["statement_unanswerable_failures"] == 0
    assert by_id["tool_call"]["title"] == "Selected Tool Execution"
    assert by_id["tool_call"]["payload"]["selection_policy"] == "ai_model_only"


def test_manual_and_spoken_paths_read_the_persisted_system_prompt_setting(tmp_path):
    prompt_path = tmp_path / "nemotron-system-prompt.json"
    pipeline.write_json(prompt_path, {"system_prompt": "  Use the shared setting.  "})
    args = argparse.Namespace(nemotron_system_prompt_json=str(prompt_path))

    assert pipeline.configured_nemotron_system_prompt(args) == "  Use the shared setting.  "
    assert pipeline.manual_text_request_system_prompt({}, args) == "  Use the shared setting.  "
    assert pipeline.manual_text_request_system_prompt(
        {"system_prompt": "Request-specific override."},
        args,
    ) == "  Use the shared setting.  "


def test_automated_commentary_uses_current_prompt_instead_of_queued_snapshot(tmp_path):
    prompt_path = tmp_path / "nemotron-system-prompt.json"
    pipeline.write_json(prompt_path, {"system_prompt": "Current concise instruction."})
    args = argparse.Namespace(nemotron_system_prompt_json=str(prompt_path))

    assert pipeline.manual_text_request_system_prompt(
        {
            "kind": "deepstream_object_change",
            "system_prompt": "Old instruction: always add a joke.",
        },
        args,
    ) == "Current concise instruction."


def test_automated_commentary_context_merges_retained_history_from_all_sources(tmp_path):
    response_path = tmp_path / "webcam-voicechat-response.json"
    history_path = pipeline.voicechat_history_path(response_path)
    pipeline.write_json(
        history_path,
        {
            "conversation_by_source": {
                "server": [{"role": "user", "source": "server", "text": "server turn", "updated_at": 2}],
                "wifi": [{"role": "assistant", "source": "nemotron", "text": "wifi reply", "updated_at": 4}],
                "bulb": [{"role": "user", "source": "bulb", "text": "bulb turn", "updated_at": 1}],
                "browser": [{"role": "user", "source": "browser", "text": "browser turn", "updated_at": 3}],
            }
        },
    )

    merged = pipeline.all_source_conversation(response_path)

    assert [item["text"] for item in merged] == [
        "bulb turn",
        "server turn",
        "browser turn",
        "wifi reply",
    ]
    context = pipeline.conversation_context_text(merged)
    assert "user [bulb]: bulb turn" in context
    assert "user [server]: server turn" in context
    assert "user [browser]: browser turn" in context
    assert "assistant [nemotron]: wifi reply" in context


def test_conversation_context_keeps_latest_complete_window():
    conversation = [
        {"role": "user", "source": "wifi", "text": f"turn-{index} " + ("x" * 60)}
        for index in range(8)
    ]

    context = pipeline.conversation_context_text(conversation, limit=None, token_limit=55)

    assert "turn-7" in context
    assert "turn-6" in context
    assert "turn-0" not in context
    assert context.index("turn-6") < context.index("turn-7")


def test_history_budget_reserves_system_message_and_answer_tokens():
    args = argparse.Namespace(
        answer_model="small-test-model",
        ollama_model="small-test-model",
        answer_num_ctx=4096,
        answer_max_tokens=320,
    )

    short_prompt_budget = pipeline.conversation_history_token_budget(args, "brief")
    long_prompt_budget = pipeline.conversation_history_token_budget(args, "x" * 3000)

    assert long_prompt_budget < short_prompt_budget
    assert long_prompt_budget >= 0


def test_camera_output_target_uses_the_matching_talk_endpoint():
    args = argparse.Namespace(
        wifi_talk_audio_url="http://localhost/wifi-talk-audio",
        bulb_talk_audio_url="http://localhost/bulb-talk-audio",
    )

    assert pipeline.talk_audio_url_for_output_target(args, "wifi_camera").endswith("/wifi-talk-audio")
    assert pipeline.talk_audio_url_for_output_target(args, "bulb_camera").endswith("/bulb-talk-audio")


def test_deepstream_queue_suppresses_a_second_pending_notification(tmp_path):
    queue_path = tmp_path / "manual-input.json"
    args = argparse.Namespace(voicechat_manual_input_path=str(queue_path))
    extra = {"kind": "deepstream_object_change", "trigger": "deepstream_yolo_coco"}

    first = server.append_manual_voicechat_input(args, "wifi", "first event", extra)
    second = server.append_manual_voicechat_input(args, "wifi", "second event", extra)
    payload = json.loads(queue_path.read_text())

    assert first.get("suppressed") is not True
    assert second["suppressed"] is True
    assert second["existing_request_id"] == first["id"]
    assert len(payload["pending"]) == 1


def test_deepstream_active_state_blocks_until_processing_completes(tmp_path):
    queue_path = tmp_path / "manual-input.json"
    server_args = argparse.Namespace(voicechat_manual_input_path=str(queue_path))
    pipeline_args = argparse.Namespace(voicechat_manual_input_json=str(queue_path))
    extra = {"kind": "deepstream_object_change", "trigger": "deepstream_yolo_coco"}
    request = server.append_manual_voicechat_input(server_args, "wifi", "focus event", extra)

    selected = pipeline.consume_manual_voicechat_input(pipeline_args, ["wifi"])
    active_payload = json.loads(queue_path.read_text())
    busy = server.manual_voicechat_deepstream_in_flight(active_payload)
    suppressed = server.append_manual_voicechat_input(server_args, "wifi", "new event", extra)

    assert selected["id"] == request["id"]
    assert busy["state"] == "active"
    assert busy["request"]["worker_pid"] == os.getpid()
    assert suppressed["suppressed"] is True

    pipeline.complete_manual_voicechat_input(pipeline_args, selected)
    completed_payload = json.loads(queue_path.read_text())
    quiet = server.manual_voicechat_deepstream_in_flight(completed_payload)
    assert quiet["state"] == "post-completion quiet period"
    completed_at = completed_payload["recently_completed_requests"]["wifi"]["completed_at"]
    assert server.manual_voicechat_deepstream_in_flight(
        completed_payload,
        completed_at + server.DEEPSTREAM_NOTIFICATION_POST_COMPLETION_QUIET_SECONDS + 0.01,
    ) == {}


def test_stale_or_dead_active_notification_does_not_block():
    stale = {
        "active_requests": {
            "wifi": {
                "id": "old",
                "source": "wifi",
                "kind": "deepstream_object_change",
                "started_at": time.time() - 1000,
                "worker_pid": os.getpid(),
            }
        }
    }

    assert server.manual_voicechat_deepstream_in_flight(stale) == {}


def test_failed_focus_overrides_misleading_model_success_text():
    tool_results = [
        {
            "name": "deepstream_yolo_coco",
            "result": {"direct_answer": "Live Stream Processor detected: clock: 93%."},
        },
        {
            "name": "focus_object",
            "result": {"status": "failed", "error": "Camera did not move."},
        },
    ]

    answer = pipeline.sanitize_final_response(
        "focus on clock",
        "Clock focus was confirmed.",
        {"needs_tools": True},
        tool_results,
        [],
    )

    assert answer == "Live Stream Processor detected: clock: 93%. I could not focus the camera: Camera did not move."

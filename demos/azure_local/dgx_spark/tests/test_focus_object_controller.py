import argparse
import base64
import io
import json
from pathlib import Path
import socket
import sys
import threading
import time

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import focus_object_controller as controller
from scripts import nemotron_voice_responder as responder


def test_post_pulse_prefers_local_ptz_socket(tmp_path):
    socket_path = tmp_path / "ptz.sock"
    received = {}
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    server.listen(1)

    def respond():
        connection, _ = server.accept()
        with connection:
            received.update(json.loads(connection.recv(16384).decode("utf-8")))
            connection.sendall(b'{"status":"ok","backend":"onvif","transport":"unix_socket"}')

    thread = threading.Thread(target=respond)
    thread.start()
    try:
        result = controller.post_pulse(
            "http://127.0.0.1:1/wifi-ptz",
            "left",
            1,
            75,
            1.0,
            "native",
            False,
            str(socket_path),
            "wifi",
        )
    finally:
        thread.join(timeout=2)
        server.close()

    assert result["transport"] == "unix_socket"
    assert received == {
        "source": "wifi",
        "command": "left",
        "action": "pulse",
        "speed": 1,
        "duration_ms": 75,
        "backend": "native",
        "reset_native_session": False,
    }


def detection(label="cat", confidence=0.9, bbox=None):
    return {
        "label": label,
        "confidence": confidence,
        "bbox": bbox or [700, 350, 200, 200],
        "frame_width": 1000,
        "frame_height": 1000,
    }


def test_focus_completion_chime_posts_to_source_camera_when_speech_audio_enabled(tmp_path, monkeypatch):
    settings_path = tmp_path / "audio-settings.json"
    settings_path.write_text(json.dumps({
        "speech_output_audio_enabled": True,
        "component_activation_audio_enabled": True,
    }))
    args = argparse.Namespace(
        component_audio_settings_json=str(settings_path),
        completion_chime_url="http://localhost/cue.wav",
        completion_chime_talk_url="http://localhost/{source}-talk-audio",
        completion_chime_timeout=2,
    )
    requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=-1):
            return self.payload

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        if isinstance(request, str):
            return Response(b"RIFF-focus-chime")
        assert request.full_url == "http://localhost/wifi-talk-audio"
        assert request.data == b"RIFF-focus-chime"
        return Response(b'{"status":"ok","backend":"camera-test"}')

    monkeypatch.setattr(controller, "urlopen", fake_urlopen)

    result = controller.play_focus_completion_chime(args, "wifi")

    assert result == {"played": True, "route": "camera_speaker", "backend": "camera-test"}
    assert len(requests) == 2


def test_focus_completion_chime_is_suppressed_when_speech_audio_is_muted(tmp_path, monkeypatch):
    settings_path = tmp_path / "audio-settings.json"
    settings_path.write_text(json.dumps({"speech_output_audio_enabled": False}))
    args = argparse.Namespace(component_audio_settings_json=str(settings_path))
    monkeypatch.setattr(controller, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not play")))

    assert controller.play_focus_completion_chime(args, "wifi") == {
        "played": False,
        "reason": "focus_completion_audio_disabled",
    }


def test_focus_completion_chime_is_suppressed_when_component_audio_is_disabled(tmp_path, monkeypatch):
    settings_path = tmp_path / "audio-settings.json"
    settings_path.write_text(json.dumps({
        "speech_output_audio_enabled": True,
        "component_activation_audio_enabled": False,
    }))
    args = argparse.Namespace(component_audio_settings_json=str(settings_path))
    monkeypatch.setattr(controller, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not play")))

    assert controller.play_focus_completion_chime(args, "wifi") == {
        "played": False,
        "reason": "focus_completion_audio_disabled",
    }


def test_target_selection_and_geometry_validation():
    low_confidence = detection(confidence=0.4, bbox=[50, 400, 100, 100])
    high_confidence = detection(confidence=0.9, bbox=[750, 400, 100, 100])
    target = controller.choose_target([low_confidence, high_confidence], "cat")
    assert target is not None
    assert target["center_x"] == 0.8

    live_box = controller.normalized_box({
        "bbox": [656.25, 672.75, 1245.75, 398.25],
        "frame_width": 1920,
        "frame_height": 1080,
    })
    assert live_box is not None
    assert live_box["center_x"] == (656.25 + 1245.75 / 2) / 1920
    assert live_box["center_y"] == (672.75 + 398.25 / 2) / 1080

    invalid = detection(label="potted plant", bbox=[0, 1447.5, 456, 247.5])
    assert controller.normalized_box(invalid) is None
    assert controller.choose_target([invalid], "potted plant") is None


def test_target_selection_honors_requested_track_identity():
    first = {**detection(confidence=0.99, bbox=[100, 350, 200, 200]), "track_id": 4}
    tracked = {**detection(confidence=0.70, bbox=[700, 350, 200, 200]), "track_id": 9}

    target = controller.choose_target([first, tracked], "cat", track_id=9)

    assert target["track_id"] == 9
    assert target["confidence"] == pytest.approx(0.70)


def test_movement_uses_deadband_and_dominant_axis():
    centered = controller.movement_for_target({"center_x": 0.55, "center_y": 0.45}, 0.1, 0.12)
    assert centered["centered"] is True

    right = controller.movement_for_target({"center_x": 0.82, "center_y": 0.63}, 0.1, 0.12)
    assert right["direction"] == "right"

    up = controller.movement_for_target({"center_x": 0.45, "center_y": 0.12}, 0.1, 0.12)
    assert up["direction"] == "up"

    large_off_center_target = controller.movement_for_target(
        {"center_x": 0.70, "center_y": 0.75, "width": 0.60, "height": 0.50},
        0.08,
        0.08,
    )
    assert large_off_center_target["centered"] is False


def test_axis_lock_finishes_one_axis_before_switching():
    assert controller.locked_axis_for_target({"center_x": 0.80, "center_y": 0.75}, 0.02, 0.02) == "x"
    assert controller.locked_axis_for_target({"center_x": 0.65, "center_y": 0.90}, 0.02, 0.02, "x") == "x"
    assert controller.locked_axis_for_target({"center_x": 0.51, "center_y": 0.90}, 0.02, 0.02, "x") == "y"
    assert controller.locked_axis_for_target({"center_x": 0.51, "center_y": 0.51}, 0.02, 0.02, "y") == ""


def test_paired_axis_plan_uses_one_coco_box_and_orders_x_before_y():
    both = controller.paired_axis_movements({"center_x": 0.8, "center_y": 0.2}, 0.02, 0.02)
    horizontal = controller.paired_axis_movements({"center_x": 0.8, "center_y": 0.5}, 0.02, 0.02)
    vertical = controller.paired_axis_movements({"center_x": 0.5, "center_y": 0.8}, 0.02, 0.02)

    assert [(item["axis"], item["direction"]) for item in both] == [("x", "right"), ("y", "up")]
    assert [(item["axis"], item["direction"]) for item in horizontal] == [("x", "right")]
    assert [(item["axis"], item["direction"]) for item in vertical] == [("y", "down")]


def test_fixed_batch_issues_three_pulses_and_reinforces_larger_axis_error():
    both = controller.fixed_pulse_batch({"center_x": 0.65, "center_y": 0.9}, 0.02, 0.02)
    horizontal = controller.fixed_pulse_batch({"center_x": 0.8, "center_y": 0.5}, 0.02, 0.02)

    assert [(item["axis"], item["direction"]) for item in both] == [
        ("x", "right"), ("y", "down"), ("y", "down"),
    ]
    assert [(item["axis"], item["direction"]) for item in horizontal] == [
        ("x", "right"), ("x", "right"), ("x", "right"),
    ]


def test_simple_closed_loop_pulses_both_axes_before_resampling_coco(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    settings_path = tmp_path / "settings.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "axis-loop",
    }))
    settings_path.write_text(json.dumps({"focus_max_steps": 20}))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        settings_json=str(settings_path), ptz_url="http://localhost/{source}-ptz", ptz_socket="",
        heartbeat_interval=0, max_detection_age=2, minimum_confidence=0.5,
        deadzone_x=0.02, deadzone_y=0.02, vertical_completion_epsilon=0,
        stability_distance=1.0, required_stable_frames=1, idle_confirmation_frames=1,
        max_pulses=20, simple_closed_loop=True, center_median_frames=1,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, vertical_speed=1,
        vertical_pulse_multiplier=1.0, max_vertical_pulse_ms=80,
        settle_seconds=0, error_backoff=0, ptz_timeout=1, poll_interval=0.01,
        ptz_idle_trust_seconds=3600, native_only_ptz=False, invert_tilt=False,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
        motion_preview_path=str(tmp_path / "motion.pgm"),
    )
    calls = []
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values) or {"backend": values[5], "pulse_stop_ok": True})
    monkeypatch.setattr(controller, "capture_verification_snapshot", lambda *_values: {})
    monkeypatch.setattr(controller, "estimate_global_motion", lambda *_values: (_ for _ in ()).throw(AssertionError("not a control gate")))
    worker = controller.FocusController(args)
    worker.ptz_idle_verified = True
    worker.ptz_idle_verified_at = time.monotonic()

    frame_id = 0
    def publish_center(center_x, center_y, detection_monotonic_ns=None):
        nonlocal frame_id
        frame_id += 1
        detections_path.write_text(json.dumps({
            "source": "wifi",
            "source_frame_id": frame_id,
            "objects_updated_at": time.time(),
            "objects_updated_monotonic_ns": detection_monotonic_ns or time.monotonic_ns() + 10_000_000,
            "objects": [detection(bbox=[center_x * 1000 - 100, center_y * 1000 - 100, 200, 200])],
        }))
        worker.step()

    publish_center(0.80, 0.80)
    publish_center(0.65, 0.80, worker.pending_pulse_completed_monotonic_ns - 1)
    assert [call[1] for call in calls] == ["right", "down", "right"]
    settling_state = json.loads(state_path.read_text())
    assert settling_state["status"] == "settling"
    assert settling_state["object_center"] == {"x": 0.8, "y": 0.8}
    publish_center(0.65, 0.80)
    assert len(calls) == 3
    publish_center(0.51, 0.80)
    publish_center(0.51, 0.60)
    publish_center(0.51, 0.51)
    publish_center(0.51, 0.51)
    publish_center(0.51, 0.51)

    assert [call[1] for call in calls] == ["right", "down", "right"]
    assert json.loads(state_path.read_text())["status"] == "complete"
    assert json.loads(command_path.read_text())["enabled"] is False


def test_simple_closed_loop_continues_on_next_frame_without_visual_response(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "no-response",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        settings_json=str(tmp_path / "settings.json"), ptz_url="http://localhost/{source}-ptz", ptz_socket="",
        heartbeat_interval=0, max_detection_age=2, minimum_confidence=0.5,
        minimum_observed_motion=0.005, simple_response_timeout=0,
        deadzone_x=0.02, deadzone_y=0.02, vertical_completion_epsilon=0,
        stability_distance=1.0, required_stable_frames=1, idle_confirmation_frames=1,
        max_pulses=20, simple_closed_loop=True, center_median_frames=1,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, vertical_speed=1,
        vertical_pulse_multiplier=1.0, max_vertical_pulse_ms=80,
        settle_seconds=0, post_pulse_view_settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01, ptz_idle_trust_seconds=3600,
        native_only_ptz=False, invert_tilt=False,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
        motion_preview_path=str(tmp_path / "motion.pgm"),
    )
    calls = []
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values) or {"pulse_stop_ok": True})
    worker = controller.FocusController(args)
    worker.ptz_idle_verified = True
    worker.ptz_idle_verified_at = time.monotonic()

    for frame_id in (1, 2):
        detections_path.write_text(json.dumps({
            "source": "wifi", "source_frame_id": frame_id,
            "objects_updated_at": time.time(),
            "objects_updated_monotonic_ns": time.monotonic_ns() + 10_000_000,
            "objects": [detection(bbox=[700, 700, 200, 200])],
        }))
        worker.step()

    assert [call[1] for call in calls] == ["right", "down", "right"]
    state = json.loads(state_path.read_text())
    assert state["status"] == "observing"
    assert json.loads(command_path.read_text())["enabled"] is True

    detections_path.write_text(json.dumps({
        "source": "wifi", "source_frame_id": 3,
        "objects_updated_at": time.time(),
        "objects_updated_monotonic_ns": time.monotonic_ns() + 10_000_000,
        "objects": [detection(bbox=[700, 700, 200, 200])],
    }))
    worker.step()

    assert [call[1] for call in calls] == ["right", "down", "right", "right", "down", "right"]
    assert json.loads(state_path.read_text())["status"] == "focusing"


def test_vertical_centering_is_not_starved_by_horizontal_error():
    movement = controller.movement_for_target(
        {"center_x": 0.81, "center_y": 0.24},
        0.08,
        0.05,
    )

    assert movement["axis"] == "y"
    assert movement["direction"] == "up"


def test_axis_preference_can_force_fairness_when_both_axes_are_active():
    movement = controller.movement_for_target(
        {"center_x": 0.86, "center_y": 0.68},
        0.08,
        0.08,
        prefer_axis="y",
    )

    assert movement["axis"] == "y"
    assert movement["direction"] == "down"


def test_movement_can_invert_camera_tilt_polarity_without_changing_midpoint_math():
    target_above_center = {"center_x": 0.5, "center_y": 0.2}

    normal = controller.movement_for_target(target_above_center, 0.08, 0.05)
    inverted = controller.movement_for_target(target_above_center, 0.08, 0.05, invert_tilt=True)

    assert normal["direction"] == "up"
    assert inverted["direction"] == "down"
    assert inverted["axis"] == "y"
    assert inverted["offset"]["y"] == normal["offset"]["y"]


def test_adaptive_tilt_probe_requires_explicit_enablement():
    assert controller.should_probe_vertical_direction(False, "y", 2, 2) is False
    assert controller.should_probe_vertical_direction(True, "y", 2, 2) is True
    assert controller.should_probe_vertical_direction(True, "x", 2, 2) is False


def test_native_tilt_polarity_does_not_invert_onvif_recovery():
    assert controller.backend_direction("up", "native", True) == "down"
    assert controller.backend_direction("down", "native", True) == "up"
    assert controller.backend_direction("up", "onvif", True) == "up"
    assert controller.backend_direction("left", "native", True) == "left"


def test_movement_skips_a_blocked_axis_and_reports_best_effort_exhaustion():
    target = {"center_x": 0.78, "center_y": 0.38}

    horizontal_first = controller.movement_for_target(target, 0.08, 0.08)
    horizontal_fallback = controller.movement_for_target(target, 0.08, 0.08, {"y"})
    vertical_fallback = controller.movement_for_target(target, 0.08, 0.08, {"x"})
    exhausted = controller.movement_for_target(target, 0.08, 0.08, {"x", "y"})

    assert horizontal_first["axis"] == "x"
    assert horizontal_fallback["axis"] == "x"
    assert horizontal_fallback["direction"] == "right"
    assert vertical_fallback["axis"] == "y"
    assert vertical_fallback["direction"] == "up"
    assert exhausted["centered"] is False
    assert exhausted["exhausted"] is True


def test_pulse_duration_is_bounded():
    assert controller.pulse_duration(0.1, 0.1, 50, 160) == 50
    assert controller.pulse_duration(1.0, 0.1, 50, 160) == 160
    assert controller.pulse_duration(0.18, 0.1, 50, 160, 0.16) == 50
    assert controller.pulse_duration(0.42, 0.1, 50, 160, 0.16) == 100


def test_predicted_center_uses_controller_direction_duration_model():
    predicted = controller.predicted_center_for_pulses(
        (0.8, 0.2),
        [
            {"axis": "x", "direction": "right", "duration_ms": 50},
            {"axis": "y", "direction": "up", "duration_ms": 100},
        ],
        {"x": 0.1, "y": 0.05},
        50,
    )

    assert predicted["center"] == pytest.approx({"x": 0.7, "y": 0.3})
    assert predicted["delta"] == pytest.approx({"x": -0.1, "y": 0.1})


def test_predicted_center_uses_direction_gains_and_caps_aggregate_axis_delta():
    predicted = controller.predicted_center_for_pulses(
        (0.5, 0.5),
        [
            {"axis": "x", "direction": "left", "duration_ms": 1000},
            {"axis": "x", "direction": "left", "duration_ms": 1000},
            {"axis": "y", "direction": "down", "duration_ms": 1000},
        ],
        {"left": 0.0003, "right": 0.00036, "up": 0.00035, "down": 0.00063},
        50,
        0.35,
    )

    assert predicted["delta"] == pytest.approx({"x": 0.35, "y": -0.35})
    assert predicted["pulses"][1]["prediction_capped"] is True
    assert predicted["pulses"][2]["prediction_capped"] is True


def test_budgeted_batch_drops_redundant_near_center_pulses_and_limits_vertical_feedback():
    args = argparse.Namespace(
        min_pulse_ms=50, max_pulse_ms=220, speed=1, vertical_speed=8,
        vertical_pulse_multiplier=1, max_vertical_pulse_ms=80,
        max_vertical_pulses_per_step=1, native_only_ptz=False, invert_tilt=False,
    )
    gains = dict(controller.DEFAULT_DIRECTION_GAINS)
    near_vertical = controller.fixed_pulse_batch({"center_x": 0.5, "center_y": 0.467}, 0.02, 0.02)
    far_both = controller.fixed_pulse_batch({"center_x": 0.8, "center_y": 0.9}, 0.02, 0.02)

    near_specs = controller.budgeted_pulse_specs(
        near_vertical, args, 0.02, 0.02, {"x": 0.16, "y": 0.2}, {}, False, gains, True,
    )
    far_specs = controller.budgeted_pulse_specs(
        far_both, args, 0.02, 0.02, {"x": 0.16, "y": 0.2}, {}, False, gains, True,
    )

    assert [(item["direction"], item["duration_ms"]) for item in near_specs] == [("up", 50)]
    assert sum(item["axis"] == "y" for item in far_specs) == 1
    assert len(far_specs) == 2


def test_vertical_pulse_floor_can_be_lower_than_horizontal_floor():
    args = argparse.Namespace(
        min_pulse_ms=50, min_vertical_pulse_ms=30, max_pulse_ms=220,
        speed=1, vertical_speed=6, vertical_pulse_multiplier=1,
        max_vertical_pulse_ms=80, max_vertical_pulses_per_step=1,
        native_only_ptz=False, invert_tilt=False,
    )
    gains = dict(controller.DEFAULT_DIRECTION_GAINS)
    vertical = controller.fixed_pulse_batch({"center_x": 0.5, "center_y": 0.521}, 0.02, 0.02)
    horizontal = controller.fixed_pulse_batch({"center_x": 0.521, "center_y": 0.5}, 0.02, 0.02)

    vertical_specs = controller.budgeted_pulse_specs(
        vertical, args, 0.02, 0.02, {"x": 0.16, "y": 0.2}, {}, False, gains, True,
    )
    horizontal_specs = controller.budgeted_pulse_specs(
        horizontal, args, 0.02, 0.02, {"x": 0.16, "y": 0.2}, {}, False, gains, True,
    )

    assert vertical_specs[0]["duration_ms"] == 30
    assert horizontal_specs[0]["duration_ms"] == 50


def test_reverse_pulse_step_reverses_order_and_physical_directions():
    step = [
        {"axis": "x", "direction": "right", "hardware_direction": "right", "duration_ms": 120,
         "speed": 1, "backend": "native", "recovery_stage": 0},
        {"axis": "y", "direction": "up", "hardware_direction": "down", "duration_ms": 50,
         "speed": 8, "backend": "auto", "recovery_stage": 0},
    ]

    reversed_step = controller.reverse_pulse_step(step)

    assert [(item["direction"], item["hardware_direction"], item["duration_ms"]) for item in reversed_step] == [
        ("down", "up", 50),
        ("left", "left", 120),
    ]


def test_lost_target_recovery_backtracks_two_steps_then_random_searches_three_times(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    args = argparse.Namespace(
        max_pulses=20, settings_json=str(tmp_path / "settings.json"),
        state_json=str(tmp_path / "state.json"), command_json=str(command_path),
        heartbeat_interval=0, ptz_url="http://localhost/{source}-ptz", ptz_socket="",
        ptz_timeout=1, settle_seconds=0, post_pulse_view_settle_seconds=0,
        min_pulse_ms=50, max_vertical_pulse_ms=80, speed=1, vertical_speed=8, invert_tilt=False,
    )
    calls = []
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values) or {"status": "ok"})
    worker = controller.FocusController(args)
    worker.request_max_steps = 20
    worker.movement_step_history = [
        [{"axis": "x", "direction": "left", "hardware_direction": "left", "duration_ms": 80,
          "speed": 1, "backend": "native", "recovery_stage": 0}],
        [
            {"axis": "x", "direction": "right", "hardware_direction": "right", "duration_ms": 120,
             "speed": 1, "backend": "native", "recovery_stage": 0},
            {"axis": "y", "direction": "up", "hardware_direction": "up", "duration_ms": 50,
             "speed": 8, "backend": "auto", "recovery_stage": 0},
        ],
        [{"axis": "y", "direction": "down", "hardware_direction": "down", "duration_ms": 60,
          "speed": 8, "backend": "auto", "recovery_stage": 0}],
    ]
    command = {
        "enabled": True, "request_id": "recover", "source": "wifi", "target_label": "clock",
        "requested_at": 100.0,
    }
    command_path.write_text(json.dumps(command))

    worker.recover_lost_target(command, "wifi", "clock")

    assert [call[1] for call in calls] == ["up"]
    assert worker.target_recovery_mode == "backtracking"
    assert worker.target_recovery_backtrack_steps == 1
    assert worker.pulse_count == 1

    worker.recover_lost_target(command, "wifi", "clock")

    assert [call[1] for call in calls] == ["up", "down", "left"]
    assert worker.target_recovery_mode == "backtracking"
    assert worker.target_recovery_backtrack_steps == 2
    assert worker.pulse_count == 3

    worker.recovery_rng.sample = lambda _directions, count: ["left", "up", "right"]
    worker.recover_lost_target(command, "wifi", "clock")

    assert [call[1] for call in calls] == ["up", "down", "left", "left", "up", "right"]
    assert worker.target_recovery_mode == "random_search"
    assert worker.target_random_search_steps == 1
    assert worker.pulse_count == 6

    worker.recover_lost_target(command, "wifi", "clock")
    worker.recover_lost_target(command, "wifi", "clock")

    assert [call[1] for call in calls[-6:]] == ["left", "up", "right", "left", "up", "right"]
    assert worker.target_random_search_steps == 3
    assert worker.pulse_count == 12

    worker.recover_lost_target(command, "wifi", "clock")

    finished = json.loads(command_path.read_text())
    assert finished["completion_status"] == "failed"
    assert "trying 3 random search moves" in finished["completion_message"]
    assert len(worker.movement_step_history) == 1


def test_lost_target_recovery_fails_only_after_step_budget_is_exhausted(tmp_path):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    command = {
        "enabled": True, "request_id": "recover-budget", "source": "wifi", "target_label": "clock",
        "requested_at": 100.0,
    }
    command_path.write_text(json.dumps(command))
    args = argparse.Namespace(
        max_pulses=2, settings_json=str(tmp_path / "settings.json"),
        state_json=str(state_path), command_json=str(command_path), heartbeat_interval=0,
    )
    worker = controller.FocusController(args)
    worker.request_max_steps = 2
    worker.pulse_count = 2

    worker.recover_lost_target(command, "wifi", "clock")

    finished = json.loads(command_path.read_text())
    assert finished["enabled"] is False
    assert finished["completion_status"] == "failed"
    assert "trying 0 random search moves" in finished["completion_message"]


def test_direction_gain_learning_accepts_only_clean_correct_sign_observations():
    pending = {
        "pulses": [
            {"axis": "x", "direction": "left", "duration_ms": 100},
            {"axis": "y", "direction": "up", "duration_ms": 50},
        ],
    }
    gains = dict(controller.DEFAULT_DIRECTION_GAINS)

    updates = controller.learned_gain_updates(pending, {"x": 0.04, "y": 0.02}, gains, 0.2)
    wrong_sign = controller.learned_gain_updates(pending, {"x": -0.04, "y": -0.02}, gains, 0.2)

    assert set(updates) == {"left", "up"}
    assert updates["left"][1] == pytest.approx(0.0004)
    assert updates["up"][1] == pytest.approx(0.0004)
    assert wrong_sign == {}


def test_prediction_error_is_actual_minus_predicted():
    error = controller.pulse_prediction_error({"x": 0.62, "y": 0.48}, (0.58, 0.51))

    assert error["x"] == pytest.approx(-0.04)
    assert error["y"] == pytest.approx(0.03)
    assert error["distance"] == pytest.approx(0.05)


def test_pulse_prediction_events_append_across_controller_restart(tmp_path):
    history_path = tmp_path / "prediction-errors.jsonl"
    args = argparse.Namespace(
        max_pulses=8,
        min_pulse_ms=50,
        minimum_controllable_x=0.1,
        minimum_controllable_y=0.05,
        state_json=str(tmp_path / "state.json"),
        prediction_error_jsonl=str(history_path),
    )
    pulse = {
        "axis": "x", "direction": "right", "hardware_direction": "right",
        "duration_ms": 50, "speed": 1, "backend": "native", "recovery_stage": 0,
    }
    command = {"request_id": "focus-1", "source": "wifi", "target_label": "clock"}
    first = controller.FocusController(args)
    first.begin_pulse_prediction(command, (0.8, 0.5), [pulse], 10, 0, 1, 100)
    observed = first.finish_pulse_prediction("observed", (0.72, 0.51), 11)

    second = controller.FocusController(args)
    second.begin_pulse_prediction({**command, "request_id": "focus-2"}, (0.7, 0.5), [pulse], 12, 0, 1, 200)
    second.finish_pulse_prediction("target_lost", None, 13)

    events = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert [event["event"] for event in events] == [
        "pulse_dispatched", "pulse_observed", "pulse_dispatched", "pulse_observed",
    ]
    assert observed["predicted_center"] == pytest.approx({"x": 0.782, "y": 0.5})
    assert observed["actual_center"] == pytest.approx({"x": 0.72, "y": 0.51})
    assert observed["prediction_error"]["x"] == pytest.approx(-0.062)
    assert events[-1]["outcome"] == "target_lost"
    assert events[-1]["actual_center"] is None
    assert events[-1]["prediction_error"] is None


def test_clean_observation_updates_and_persists_direction_gain(tmp_path):
    gain_path = tmp_path / "direction-gains.json"
    args = argparse.Namespace(
        max_pulses=8,
        min_pulse_ms=50,
        minimum_controllable_x=0.1,
        minimum_controllable_y=0.05,
        state_json=str(tmp_path / "state.json"),
        prediction_error_jsonl="",
        direction_gain_json=str(gain_path),
        direction_gain_learning_rate=0.2,
    )
    pulse = {
        "axis": "x", "direction": "left", "hardware_direction": "left",
        "duration_ms": 100, "speed": 1, "backend": "native", "recovery_stage": 0,
    }
    worker = controller.FocusController(args)
    worker.begin_pulse_prediction(
        {"request_id": "gain-1", "source": "wifi", "target_label": "clock"},
        (0.4, 0.5), [pulse], 1, 0, 1, 100,
    )

    event = worker.finish_pulse_prediction("observed", (0.44, 0.5), 2)
    persisted = json.loads(gain_path.read_text())

    assert event["direction_gain_updates"]["left"]["raw_sample"] == pytest.approx(0.0004)
    assert persisted["gains"]["left"] == pytest.approx(0.00032)
    assert persisted["sample_counts"]["left"] == 1
    reloaded = controller.FocusController(args)
    assert reloaded.direction_gains["left"] == pytest.approx(0.00032)


def test_vertical_pulse_duration_is_capped_without_affecting_horizontal_steps():
    assert controller.adjusted_axis_pulse_duration(50, "y", 6.0, 80, 0) == 80
    assert controller.adjusted_axis_pulse_duration(100, "y", 1.0, 80, 2) == 80
    assert controller.adjusted_axis_pulse_duration(100, "x", 1.0, 80, 1) == 200


def test_vertical_recovery_uses_onvif_after_native_stages():
    args = argparse.Namespace(
        min_pulse_ms=50, min_vertical_pulse_ms=30, max_pulse_ms=220,
        speed=1, vertical_speed=6, vertical_pulse_multiplier=1,
        max_vertical_pulse_ms=160, invert_tilt=False, native_only_ptz=False,
    )
    movement = {"axis": "y", "direction": "down", "error": 0.2}

    initial = controller.pulse_command_spec(movement, args, 0.02, 0.02, {"y": 0.2}, 0, False)
    fallback = controller.pulse_command_spec(movement, args, 0.02, 0.02, {"y": 0.2}, 2, False)

    assert initial["backend"] == "auto"
    assert fallback["backend"] == "onvif"


def test_vertical_completion_epsilon_avoids_boundary_chatter_only_on_tilt_axis():
    assert round(controller.effective_centering_deadzone(0.05, "y", 0.002), 4) == 0.052
    assert controller.effective_centering_deadzone(0.08, "x", 0.002) == 0.08
    movement = controller.movement_for_target({"center_x": 0.5, "center_y": 0.5508}, 0.08, 0.052)
    assert movement["centered"] is True


def test_global_motion_uses_scene_translation_not_box_jitter():
    rng = np.random.default_rng(42)
    before = rng.integers(0, 256, size=(180, 320), dtype=np.uint8)
    matrix = np.float32([[1, 0, 12], [0, 1, -9]])
    after = cv2.warpAffine(before, matrix, (320, 180), borderMode=cv2.BORDER_REFLECT)

    motion = controller.estimate_global_motion(before, after)

    assert motion["available"] is True
    assert motion["response"] > 0.5
    assert abs(abs(motion["x"]) - 12 / 320) < 0.005
    assert abs(abs(motion["y"]) - 9 / 180) < 0.005


def test_centering_tolerance_is_not_inflated_by_ptz_step_size():
    movement = controller.movement_for_target(
        {"center_x": 0.434, "center_y": 0.401},
        0.08,
        0.05,
    )

    assert movement["centered"] is False
    assert movement["axis"] == "y"


def test_target_axis_progress_verifies_vertical_midpoint_motion_toward_center():
    progress = controller.target_axis_progress(-0.2635, -0.246, 0.005)
    wrong_way = controller.target_axis_progress(-0.2635, -0.275, 0.005)

    assert progress["verified"] is True
    assert progress["center_progress"] > 0.017
    assert wrong_way["verified"] is False


def test_vertical_target_progress_accepts_cross_axis_scene_translation():
    scene = {"available": True, "response": 0.55, "magnitude": 0.105, "x": -0.10494, "y": 0.00363}
    progress = controller.target_axis_progress(-0.2635, -0.246, 0.005)

    verified = controller.verify_pulse_motion(scene, "y", progress, 0.08, 0.008)

    assert verified["scene_motion_verified"] is True
    assert verified["global_axis_verified"] is False
    assert verified["target_motion_verified"] is True
    assert verified["verified"] is True
    assert verified["verified_axis_motion"] > 0.017


def test_cross_axis_scene_translation_without_vertical_progress_is_rejected():
    scene = {"available": True, "response": 0.55, "magnitude": 0.105, "x": -0.10494, "y": 0.00363}
    progress = controller.target_axis_progress(-0.2635, -0.275, 0.005)

    verified = controller.verify_pulse_motion(scene, "y", progress, 0.08, 0.008)

    assert verified["scene_motion_verified"] is True
    assert verified["verified"] is False


def test_global_axis_motion_wrong_way_is_not_centering_progress():
    scene = {"available": True, "response": 0.55, "magnitude": 0.06, "x": 0.001, "y": 0.06}
    progress = controller.target_axis_progress(0.30, 0.34, 0.005)

    verified = controller.verify_pulse_motion(scene, "y", progress, 0.08, 0.008)

    assert verified["global_axis_verified"] is True
    assert verified["global_centering_verified"] is False
    assert verified["target_motion_verified"] is False
    assert verified["verified"] is False


def test_bbox_jitter_without_physical_scene_motion_is_rejected():
    scene = {"available": True, "response": 0.9, "magnitude": 0.001, "x": 0.0005, "y": 0.0008}
    progress = controller.target_axis_progress(-0.2635, -0.246, 0.005)

    verified = controller.verify_pulse_motion(scene, "y", progress, 0.08, 0.008)

    assert verified["scene_motion_verified"] is False
    assert verified["target_motion_verified"] is False
    assert verified["verified"] is False


def test_controller_reads_persistent_focus_step_limit(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"focus_max_steps": 23}))
    worker = controller.FocusController(argparse.Namespace(max_pulses=100, settings_json=str(settings_path)))

    assert worker.configured_max_steps("wifi") == 23


def test_auto_centering_runtime_prefers_monotonic_request_clock():
    runtime = controller.auto_centering_runtime_seconds(
        {"requested_at": 10.0, "requested_monotonic_ns": 2_000_000_000},
        now=999.0,
        monotonic_ns=5_456_000_000,
    )

    assert runtime == 3.456


def test_auto_centering_runtime_falls_back_to_wall_clock_and_never_goes_negative():
    assert controller.auto_centering_runtime_seconds({"requested_at": 10.0}, now=12.3456) == 2.346
    assert controller.auto_centering_runtime_seconds({"requested_at": 20.0}, now=12.0) == 0.0
    assert controller.auto_centering_runtime_seconds({
        "enabled": False, "requested_at": 10.0, "completed_at": 14.25, "requested_monotonic_ns": 1,
    }, now=99.0, monotonic_ns=999_000_000_000) == 4.25


def test_auto_centering_runtime_freezes_during_manual_pause_and_discounts_it_afterward():
    paused = {
        "enabled": True,
        "requested_at": 100.0,
        "manual_ptz_pause_started_at": 104.0,
        "manual_ptz_active": True,
    }
    assert controller.auto_centering_runtime_seconds(paused, now=106.0) == 4.0
    assert controller.auto_centering_runtime_seconds(paused, now=109.0) == 4.0

    resumed = {
        **paused,
        "manual_ptz_active": False,
        "manual_ptz_pause_until": 110.0,
    }
    assert controller.auto_centering_runtime_seconds(resumed, now=112.0) == 6.0
    assert controller.manual_ptz_paused_seconds(resumed, now=112.0) == 6.0


def test_auto_centering_runtime_combines_accumulated_and_current_pause_without_overlap():
    command = {
        "enabled": False,
        "requested_at": 100.0,
        "completed_at": 120.0,
        "manual_ptz_paused_seconds": 3.0,
        "manual_ptz_pause_started_at": 115.0,
        "manual_ptz_pause_until": 118.0,
    }

    assert controller.manual_ptz_paused_seconds(command, now=120.0) == 6.0
    assert controller.auto_centering_runtime_seconds(command, now=120.0) == 14.0


def test_finish_request_persists_runtime_and_bounded_history(tmp_path, monkeypatch, capsys):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    command = {
        "enabled": True,
        "source": "wifi",
        "target_label": "cat",
        "request_id": "focus-runtime-1",
        "requested_at": 100.0,
    }
    command_path.write_text(json.dumps(command))
    worker = controller.FocusController(argparse.Namespace(
        max_pulses=8,
        settings_json=str(tmp_path / "settings.json"),
        command_json=str(command_path),
        state_json=str(state_path),
        heartbeat_interval=1.0,
    ))
    worker.pulse_count = 4
    monkeypatch.setattr(controller.time, "time", lambda: 106.25)

    worker.finish_request(command, "failed", "Stopped for test.")

    saved_command = json.loads(command_path.read_text())
    saved_state = json.loads(state_path.read_text())
    assert saved_command["auto_centering_runtime_seconds"] == 6.25
    assert saved_state["auto_centering_runtime_seconds"] == 6.25
    assert saved_state["history"][-1]["runtime_seconds"] == 6.25
    assert saved_state["history"][-1]["pulse_count"] == 4
    assert '"event": "auto_centering_finished"' in capsys.readouterr().out


def test_controller_unix_datagram_wake_socket(tmp_path):
    wake_path = tmp_path / "focus-wake.sock"
    worker = controller.FocusController(argparse.Namespace(max_pulses=8, wake_socket=str(wake_path)))
    worker.open_wake_socket()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(b"detection", str(wake_path))
        worker.wait_for_wake(0.2)
        assert wake_path.exists()
    finally:
        client.close()
        worker.close_wake_socket()
    assert not wake_path.exists()


def test_controller_accepts_full_realtime_focus_frame(tmp_path):
    wake_path = tmp_path / "focus-frame.sock"
    worker = controller.FocusController(argparse.Namespace(max_pulses=8, wake_socket=str(wake_path)))
    worker.open_wake_socket()
    envelope = {
        "v": 1,
        "type": "focus_frame",
        "source": "wifi",
        "frame_id": 42,
        "detections": {
            "source": "wifi",
            "source_frame_id": 42,
            "objects_updated_at": time.time(),
            "objects": [detection("cat")],
        },
        "command": {
            "enabled": True,
            "source": "wifi",
            "target_label": "cat",
            "request_id": "focus_rt_1",
        },
    }
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        client.sendto(json.dumps(envelope).encode("utf-8"), str(wake_path))
        worker.wait_for_wake(0.2)
    finally:
        client.close()
        worker.close_wake_socket()

    assert worker.latest_detection_payload["source_frame_id"] == 42
    assert worker.pending_command_payload["request_id"] == "focus_rt_1"


def test_controller_waits_for_stable_fresh_target_before_ptz(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({"enabled": True, "source": "wifi", "target_label": "cat", "request_id": "one"}))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=2,
        max_pulses=8,
        motion_verification_timeout=0,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0.45, error_backoff=2,
        ptz_timeout=1, poll_interval=0.01,
    )
    calls = []
    clears = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *values: clears.append(values) or [])
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values) or {"backend": "test"})
    worker = controller.FocusController(args)

    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time(), "objects": [detection()]
    }))
    worker.step()
    state = json.loads(state_path.read_text())
    assert state["status"] == "acquiring"
    assert state["pulse_count"] == 0
    assert state["stable_frames"] == 1
    assert calls == []
    assert clears == []

    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.001, "objects": [detection()]
    }))
    worker.step()
    state = json.loads(state_path.read_text())
    assert state["status"] == "focusing"
    assert state["pulse_count"] == 1
    assert state["stable_frames"] == 2
    assert calls and calls[0][1] == "right"
    assert len(clears) == 1

    worker.cooldown_until = 0
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.002,
        "objects": [detection(bbox=[400, 400, 200, 200])],
    }))
    worker.step()
    state = json.loads(state_path.read_text())
    assert state["status"] == "acquiring"
    assert state["pulse_count"] == 1
    assert state["stable_frames"] == 1
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.003,
        "objects": [detection(bbox=[400, 400, 200, 200])],
    }))
    worker.step()
    assert json.loads(state_path.read_text())["status"] == "complete"
    assert json.loads(command_path.read_text())["enabled"] is False

    worker.step()
    assert len(calls) == 1


def test_controller_never_moves_from_stale_detection(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({"enabled": True, "source": "wifi", "target_label": "cat"}))
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() - 30, "objects": [detection()]
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=1,
        max_pulses=8,
        motion_verification_timeout=0,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0.45, error_backoff=2,
        ptz_timeout=1, poll_interval=0.01,
    )
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(controller, "post_pulse", lambda *_values: (_ for _ in ()).throw(AssertionError("must not move")))
    controller.FocusController(args).step()
    assert json.loads(state_path.read_text())["status"] == "stale"


def test_controller_skips_cleanup_when_target_is_already_centered(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "centered",
    }))
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time(),
        "objects": [detection(bbox=[400, 400, 200, 200])],
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=1,
        max_pulses=8,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
    )
    clears = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *values: clears.append(values) or [])

    worker = controller.FocusController(args)
    worker.step()
    assert json.loads(state_path.read_text())["status"] == "acquiring"
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.01,
        "objects": [detection(bbox=[400, 400, 200, 200])],
    }))
    worker.step()

    assert clears == []
    assert json.loads(state_path.read_text())["status"] == "complete"


def test_controller_reuses_verified_idle_ptz_state(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=1,
        max_pulses=8, motion_verification_timeout=0,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
    )
    clears = []
    pulses = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *values: clears.append(values) or [])
    monkeypatch.setattr(
        controller,
        "post_pulse",
        lambda *values: pulses.append(values) or {"backend": "x64_netsdk_qemu_persistent", "pulse_stop_ok": True},
    )
    worker = controller.FocusController(args)

    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "first",
    }))
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time(), "objects": [detection()],
    }))
    worker.step()
    assert len(clears) == 1
    assert worker.ptz_idle_verified is True

    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.01,
        "objects": [detection(bbox=[400, 400, 200, 200])],
    }))
    worker.step()
    assert json.loads(state_path.read_text())["status"] == "complete"

    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "second",
    }))
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.02, "objects": [detection()],
    }))
    worker.step()

    assert len(clears) == 1
    assert len(pulses) == 2


def test_controller_verifies_after_ptz_response_timeout(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "timeout-step",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=1,
        max_pulses=8,
        motion_verification_timeout=0,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
    )
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(controller, "post_pulse", lambda *_values: (_ for _ in ()).throw(TimeoutError("timed out")))
    monkeypatch.setattr(controller, "capture_verification_snapshot", lambda *_values: {"captured_at": time.time()})
    worker = controller.FocusController(args)

    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time(), "objects": [detection()],
    }))
    worker.step()
    state = json.loads(state_path.read_text())
    assert state["status"] == "verifying"
    assert state["pulse_count"] == 1
    assert state["warning"] == "PTZ step response failed: timed out"
    assert json.loads(command_path.read_text())["enabled"] is True

    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.001,
        "objects": [detection(bbox=[400, 400, 200, 200])],
    }))
    worker.step()
    assert json.loads(state_path.read_text())["status"] == "acquiring"
    detections_path.write_text(json.dumps({
        "source": "wifi", "objects_updated_at": time.time() + 0.002,
        "objects": [detection(bbox=[400, 400, 200, 200])],
    }))
    worker.step()
    assert json.loads(state_path.read_text())["status"] == "complete"
    assert json.loads(command_path.read_text())["enabled"] is False


def test_controller_recovers_no_motion_without_short_circuiting_step_budget(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "recover-ptz",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        settings_json=str(tmp_path / "settings.json"),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5, minimum_observed_motion=0.005,
        deadzone_x=0.08, deadzone_y=0.08, stability_distance=0.18, required_stable_frames=1,
        max_pulses=22, max_no_progress_pulses=22, max_axis_no_progress_pulses=1,
        motion_verification_timeout=0,
        min_pulse_ms=50, max_pulse_ms=50, speed=1, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
    )
    calls = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(
        controller,
        "post_pulse",
        lambda *values: calls.append(values) or {"backend": values[5]},
    )
    monkeypatch.setattr(controller, "capture_verification_snapshot", lambda *_values: {"captured_at": time.time()})
    worker = controller.FocusController(args)

    for index in range(4):
        detections_path.write_text(json.dumps({
            "source": "wifi",
            "objects_updated_at": time.time() + index * 0.001,
            "objects": [detection(bbox=[700, 400, 200, 200])],
        }))
        worker.step()

    state = json.loads(state_path.read_text())
    assert state["status"] == "focusing"
    assert state["enabled"] is True
    assert state["pulse_count"] == 4
    assert state["blocked_axes"] == []
    assert [(call[5], call[6], call[3]) for call in calls] == [
        ("native", False, 50),
        ("native", True, 100),
        ("onvif", False, 200),
        ("onvif", False, 200),
    ]


def test_controller_probes_opposite_vertical_direction_after_no_motion(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "probe-tilt",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        settings_json=str(tmp_path / "settings.json"),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5, minimum_observed_motion=0.005,
        deadzone_x=0.08, deadzone_y=0.05, stability_distance=0.18, required_stable_frames=1,
        max_pulses=10, max_no_progress_pulses=10, max_axis_no_progress_pulses=6,
        vertical_direction_probe_pulses=2, adaptive_tilt_direction_probe=True, motion_verification_timeout=0,
        min_pulse_ms=50, max_pulse_ms=50, speed=1, vertical_speed=1,
        vertical_pulse_multiplier=1.0, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
    )
    calls = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(
        controller,
        "post_pulse",
        lambda *values: calls.append(values) or {"backend": values[5]},
    )
    monkeypatch.setattr(controller, "capture_verification_snapshot", lambda *_values: {"captured_at": time.time()})
    worker = controller.FocusController(args)

    for index in range(3):
        detections_path.write_text(json.dumps({
            "source": "wifi",
            "objects_updated_at": time.time() + index * 0.001,
            "objects": [detection(bbox=[400, 100, 200, 200])],
        }))
        worker.step()

    state = json.loads(state_path.read_text())
    assert state["status"] == "focusing"
    assert state["enabled"] is True
    assert state["pulse_count"] == 3
    assert state["vertical_direction_inverted"] is True
    assert state["vertical_direction_probe_count"] == 1
    assert [call[1] for call in calls] == ["up", "up", "down"]


def test_controller_sends_vertical_pulses_through_auto_backend(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "vertical-auto",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        settings_json=str(tmp_path / "settings.json"),
        ptz_url="http://localhost/{source}-ptz", ptz_socket="", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5, minimum_observed_motion=0.005,
        deadzone_x=0.08, deadzone_y=0.05, stability_distance=0.18, required_stable_frames=1,
        max_pulses=10, max_no_progress_pulses=10, max_axis_no_progress_pulses=6,
        axis_fairness_pulses=2, vertical_direction_probe_pulses=2, motion_verification_timeout=0.45,
        min_pulse_ms=50, max_pulse_ms=50, speed=1, vertical_speed=8,
        vertical_pulse_multiplier=1.0, native_only_ptz=False, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
    )
    calls = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(
        controller,
        "post_pulse",
        lambda *values: calls.append(values) or {"backend": "onvif"},
    )
    worker = controller.FocusController(args)

    detections_path.write_text(json.dumps({
        "source": "wifi",
        "objects_updated_at": time.time(),
        "objects": [detection(bbox=[400, 100, 200, 200])],
    }))
    worker.step()

    state = json.loads(state_path.read_text())
    assert state["status"] == "focusing"
    assert state["direction"] == "up"
    assert state["pending_pulse_backend"] == "auto"
    assert calls[0][1] == "up"
    assert calls[0][5] == "auto"
    assert calls[0][2] == 8


def test_controller_reports_calibrated_action_separately_from_inverted_tilt_command(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "vertical-inverted",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        settings_json=str(tmp_path / "settings.json"),
        ptz_url="http://localhost/{source}-ptz", ptz_socket="", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5, minimum_observed_motion=0.005,
        deadzone_x=0.08, deadzone_y=0.05, stability_distance=0.18, required_stable_frames=1,
        max_pulses=10, max_no_progress_pulses=10, max_axis_no_progress_pulses=6,
        axis_fairness_pulses=2, vertical_direction_probe_pulses=2, motion_verification_timeout=0.45,
        min_pulse_ms=50, max_pulse_ms=50, speed=1, vertical_speed=8,
        vertical_pulse_multiplier=1.0, native_only_ptz=False, invert_tilt=True, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
    )
    calls = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(
        controller,
        "post_pulse",
        lambda *values: calls.append(values) or {"backend": "x64_netsdk_qemu_persistent", "pulse_stop_ok": True},
    )
    worker = controller.FocusController(args)

    detections_path.write_text(json.dumps({
        "source": "wifi",
        "objects_updated_at": time.time(),
        "objects": [detection(bbox=[400, 100, 200, 200])],
    }))
    worker.step()

    state = json.loads(state_path.read_text())
    assert state["status"] == "focusing"
    assert state["direction"] == "up"
    assert state["hardware_direction"] == "down"
    assert state["pending_pulse_action_direction"] == "up"
    assert state["pending_pulse_direction"] == "down"
    assert calls[0][1] == "down"


def test_controller_reverses_previous_step_when_target_disappears(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "lost-target",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=1,
        max_pulses=8, missing_target_frames=1,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
    )
    calls = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values) or {"backend": "test"})
    monkeypatch.setattr(controller, "capture_verification_snapshot", lambda *_values: {"captured_at": time.time()})
    worker = controller.FocusController(args)

    for index, objects in enumerate(([detection()], [])):
        detections_path.write_text(json.dumps({
            "source": "wifi", "objects_updated_at": time.time() + index * 0.001, "objects": objects,
        }))
        worker.step()

    state = json.loads(state_path.read_text())
    assert state["status"] == "recovering_target"
    assert state["target_recovery_mode"] == "backtracking"
    assert len(calls) == 2
    assert calls[0][1] != calls[1][1]
    assert json.loads(command_path.read_text())["enabled"] is True


def test_controller_holds_during_dropout_and_confirms_reacquisition(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "dropout-grace",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=1,
        max_pulses=8, missing_target_frames=1, missing_target_grace_seconds=0.8,
        reacquisition_confirmation_frames=2, max_target_random_search_moves=0,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
        snapshot_url="http://localhost/{source}-snapshot.jpg",
        snapshot_path=str(tmp_path / "verification.jpg"), snapshot_timeout=1,
    )
    calls = []
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values) or {"backend": "test"})
    monkeypatch.setattr(controller, "capture_verification_snapshot", lambda *_values: {"captured_at": time.time()})
    worker = controller.FocusController(args)

    def publish(objects, index):
        detections_path.write_text(json.dumps({
            "source": "wifi", "objects_updated_at": time.time() + index * 0.001, "objects": objects,
        }))
        worker.cooldown_until = 0
        worker.step()
        return json.loads(state_path.read_text())

    publish([detection()], 0)
    anchor = worker.previous_center
    assert len(calls) == 1

    state = publish([], 1)
    assert state["status"] == "awaiting_target"
    assert state["missing_target_grace_seconds"] == 0.8
    assert worker.previous_center == anchor
    assert len(calls) == 1

    state = publish([detection()], 2)
    assert state["status"] == "reacquiring_target"
    assert state["reacquisition_confirmation_frames"] == 1
    assert len(calls) == 1

    publish([detection()], 3)
    assert worker.missing_target_since_monotonic == 0.0
    assert worker.reacquisition_confirmation_frames == 0
    assert len(calls) >= 1


def test_controller_can_disable_random_search(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    command = {
        "enabled": True, "request_id": "no-random-search", "source": "wifi", "target_label": "cat",
        "requested_at": 100.0,
    }
    command_path.write_text(json.dumps(command))
    args = argparse.Namespace(
        max_pulses=8, max_target_random_search_moves=0,
        settings_json=str(tmp_path / "settings.json"),
        state_json=str(state_path), command_json=str(command_path), heartbeat_interval=0,
    )
    calls = []
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values))
    worker = controller.FocusController(args)
    worker.request_max_steps = 8

    worker.recover_lost_target(command, "wifi", "cat")

    finished = json.loads(command_path.read_text())
    assert finished["completion_status"] == "failed"
    assert "trying 0 random search moves" in finished["completion_message"]
    assert calls == []


def test_controller_randomly_searches_when_target_is_already_gone(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    detections_path = tmp_path / "detections.json"
    command_path.write_text(json.dumps({
        "enabled": True, "source": "wifi", "target_label": "cat", "request_id": "already-gone",
    }))
    args = argparse.Namespace(
        command_json=str(command_path), state_json=str(state_path), detections_json=str(detections_path),
        ptz_url="http://localhost/{source}-ptz", heartbeat_interval=0, max_detection_age=2,
        minimum_confidence=0.5,
        deadzone_x=0.1, deadzone_y=0.12, stability_distance=0.18, required_stable_frames=1,
        max_pulses=8, missing_target_frames=1,
        min_pulse_ms=50, max_pulse_ms=160, speed=1, settle_seconds=0, error_backoff=0,
        ptz_timeout=1, poll_interval=0.01,
    )
    monkeypatch.setattr(controller, "clear_ptz_motion", lambda *_values: [])
    calls = []
    monkeypatch.setattr(controller, "post_pulse", lambda *values: calls.append(values) or {"status": "ok"})
    worker = controller.FocusController(args)

    for index in range(1):
        detections_path.write_text(json.dumps({
            "source": "wifi", "objects_updated_at": time.time() + index * 0.001, "objects": [],
        }))
        worker.step()

    state = json.loads(state_path.read_text())
    assert state["status"] == "recovering_target"
    assert state["target_recovery_mode"] == "random_search"
    assert state["pulse_count"] == 3
    assert len(calls) == 3
    assert len({call[1] for call in calls}) == 3
    assert json.loads(command_path.read_text())["enabled"] is True


def test_focus_tool_request_and_direct_router(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"preferred_objects": ["dog", "cat", "clock"]}))
    args = argparse.Namespace(
        focus_object_enabled=True,
        focus_object_command_json=str(command_path),
        focus_object_state_json=str(state_path),
        focus_object_timeout=20,
        focus_object_ack_timeout=0.1,
        camera_ptz_default_source="wifi",
        deepstream_settings_json=str(settings_path),
    )
    monkeypatch.setattr(
        responder,
        "wait_for_focus_ack",
        lambda _path, request_id, _timeout: {"request_id": request_id, "status": "acquiring"},
    )
    monkeypatch.setattr(
        responder,
        "wait_for_focus_completion",
        lambda _path, request_id, _timeout: {
            "request_id": request_id,
            "status": "complete",
            "message": "Focused on cat.",
        },
    )
    monkeypatch.setattr(
        responder,
        "boxed_focus_snapshot",
        lambda _args, _source, _label, _state: {
            "image_data_url": "data:image/jpeg;base64,boxed",
            "image_bytes": 5,
            "snapshot_count": 1,
            "snapshot_images": [{"data_url": "data:image/jpeg;base64,boxed"}],
        },
    )
    started = responder.focus_object_tool(args, "wifi", "the cats")
    assert started["target_label"] == "cat"
    assert started["priority"] == 2
    assert started["preferred_rank"] == 1
    assert started["direct_answer"] == "Priority 2 object cat acquired."
    assert started["image_data_url"] == "data:image/jpeg;base64,boxed"
    command = json.loads(command_path.read_text())
    assert command["enabled"] is True
    assert command["expires_at"] > command["requested_at"]

def test_focus_tool_boxed_snapshot_is_exact_bbox_crop(monkeypatch):
    source = io.BytesIO()
    from PIL import Image

    Image.new("RGB", (120, 90), (50, 60, 70)).save(source, format="JPEG")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return source.getvalue()

    monkeypatch.setattr(responder, "urlopen", lambda *_args, **_kwargs: Response())
    result = responder.boxed_focus_snapshot(
        argparse.Namespace(
            snapshot_url="http://localhost/snapshot.jpg",
            wifi_snapshot_url="http://localhost/wifi-snapshot.jpg",
            tool_snapshot_timeout=1,
            tool_snapshot_max_bytes=1_000_000,
        ),
        "wifi",
        "clock",
        {"bbox": [30, 20, 48, 36], "frame_size": [120, 90]},
    )

    encoded = result["image_data_url"].split(",", 1)[1]
    cropped = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert cropped.size == (48, 36)


def test_focus_tool_does_not_report_success_without_controller(tmp_path):
    command_path = tmp_path / "command.json"
    args = argparse.Namespace(
        focus_object_enabled=True,
        focus_object_command_json=str(command_path),
        focus_object_state_json=str(tmp_path / "missing-state.json"),
        focus_object_timeout=20,
        focus_object_ack_timeout=0.1,
        camera_ptz_default_source="wifi",
    )
    result = responder.focus_object_tool(args, "wifi", "cat")
    assert result["status"] == "controller_unavailable"
    assert "error" in result
    assert json.loads(command_path.read_text())["enabled"] is False


def test_focus_tool_waits_for_terminal_failure(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    args = argparse.Namespace(
        focus_object_enabled=True,
        focus_object_command_json=str(command_path),
        focus_object_state_json=str(state_path),
        focus_object_timeout=20,
        focus_object_ack_timeout=0.1,
        camera_ptz_default_source="wifi",
    )
    monkeypatch.setattr(
        responder,
        "wait_for_focus_ack",
        lambda _path, request_id, _timeout: {"request_id": request_id, "status": "acquiring"},
    )
    monkeypatch.setattr(
        responder,
        "wait_for_focus_completion",
        lambda _path, request_id, _timeout: {
            "request_id": request_id,
            "status": "failed",
            "message": "Camera did not move.",
        },
    )

    result = responder.focus_object_tool(args, "wifi", "clock")

    assert result["status"] == "failed"
    assert result["error"] == "Camera did not move."
    assert "direct_answer" not in result


def test_focus_tool_attaches_to_prestarted_request_without_rewriting_command(tmp_path, monkeypatch):
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    original = {
        "enabled": True,
        "source": "wifi",
        "target_label": "cat",
        "request_id": "focus-fast-1",
        "requested_at": 123.0,
        "expires_at": 303.0,
    }
    command_path.write_text(json.dumps(original))
    args = argparse.Namespace(
        focus_object_enabled=True,
        focus_object_command_json=str(command_path),
        focus_object_state_json=str(state_path),
        focus_object_timeout=20,
        focus_object_ack_timeout=0.1,
        camera_ptz_default_source="wifi",
    )
    monkeypatch.setattr(
        responder,
        "wait_for_focus_ack",
        lambda _path, request_id, _timeout: {"request_id": request_id, "status": "focusing"},
    )
    monkeypatch.setattr(
        responder,
        "wait_for_focus_completion",
        lambda _path, request_id, _timeout: {
            "request_id": request_id,
            "status": "complete",
            "message": "Focused on cat.",
        },
    )

    result = responder.focus_object_tool(args, "wifi", "cat", "focus-fast-1")

    assert result["status"] == "complete"
    assert json.loads(command_path.read_text()) == original


def test_run_tool_call_executes_focus_object(monkeypatch):
    monkeypatch.setattr(
        responder,
        "focus_object_tool",
        lambda _args, source, target: {"status": "acquiring", "source": source, "target_label": target},
    )
    result = responder.run_tool_call(
        argparse.Namespace(),
        {"name": "focus_object", "args": {"source": "wifi", "target": "clock"}},
    )
    assert result["name"] == "focus_object"
    assert result["result"] == {"status": "acquiring", "source": "wifi", "target_label": "clock"}


def test_manual_camera_tool_does_not_cancel_object_focus(tmp_path):
    command_path = tmp_path / "command.json"
    command_path.write_text(json.dumps({"enabled": True, "source": "wifi", "target_label": "cat"}))
    args = argparse.Namespace(
        camera_ptz_enabled=False,
        focus_object_command_json=str(command_path),
    )
    # A disabled PTZ tool cannot move and therefore must not cancel the focus request.
    responder.camera_ptz_tool(args, "wifi", "left")
    assert json.loads(command_path.read_text())["enabled"] is True

    args.camera_ptz_enabled = True
    args.camera_ptz_default_source = "wifi"
    args.camera_ptz_degrees = 5
    args.camera_ptz_pulse_ms = 120
    args.camera_ptz_speed = 1
    args.camera_ptz_url = ""
    responder.camera_ptz_tool(args, "wifi", "left")
    command = json.loads(command_path.read_text())
    assert command["enabled"] is True
    assert "completion_status" not in command


def test_gru_axis_selection_optimizes_only_selected_axis(monkeypatch):
    focus = controller.FocusController.__new__(controller.FocusController)
    focus.args = argparse.Namespace(
        settings_json="", min_pulse_ms=50, max_pulse_ms=220, max_vertical_pulse_ms=80,
    )
    focus.settings_cache = {"focus_horizontal_model": "gru", "focus_vertical_model": "linear"}
    focus.settings_cache_checked_monotonic = time.monotonic()
    focus.settings_cache_stamp = None
    focus.pulse_count = 3
    focus.gru_last_error = ""

    def prediction(_command, _center, specs, _pulse_count):
        horizontal_ms = sum(spec["duration_ms"] for spec in specs if spec["axis"] == "x")
        return {
            "predicted_delta": {"x": -0.0004 * horizontal_ms, "y": 0.0},
            "predicted_std": {"x": 0.01, "y": 0.01},
        }

    monkeypatch.setattr(focus, "gru_pulse_prediction", prediction)
    horizontal_movement = {"offset": {"x": 0.2, "y": 0.0}}
    vertical_movement = {"offset": {"x": 0.0, "y": -0.1}}
    specs = [
        {"axis": "x", "direction": "right", "duration_ms": 50, "movement": horizontal_movement},
        {"axis": "y", "direction": "up", "duration_ms": 60, "movement": vertical_movement},
        {"axis": "x", "direction": "right", "duration_ms": 50, "movement": horizontal_movement},
    ]

    selected, diagnostics = focus.apply_selected_response_models({}, (0.7, 0.4), specs, 0.02, 0.02)

    assert [spec["duration_ms"] for spec in selected if spec["axis"] == "x"] == [220, 220]
    assert [spec["planning_model"] for spec in selected] == ["gru", "linear", "gru"]
    assert diagnostics["axis_models"] == {"x": "gru", "y": "linear"}
    assert diagnostics["axes"]["x"]["candidate_count"] > 1


def test_gru_axis_selection_falls_back_to_linear(monkeypatch):
    focus = controller.FocusController.__new__(controller.FocusController)
    focus.args = argparse.Namespace(
        settings_json="", min_pulse_ms=50, max_pulse_ms=220, max_vertical_pulse_ms=80,
    )
    focus.settings_cache = {"focus_horizontal_model": "linear", "focus_vertical_model": "gru"}
    focus.settings_cache_checked_monotonic = time.monotonic()
    focus.settings_cache_stamp = None
    focus.pulse_count = 0
    focus.gru_last_error = "checkpoint unavailable"
    monkeypatch.setattr(focus, "gru_pulse_prediction", lambda *_args: None)
    movement = {"offset": {"x": 0.0, "y": 0.2}}
    specs = [{"axis": "y", "direction": "down", "duration_ms": 60, "movement": movement}]

    selected, diagnostics = focus.apply_selected_response_models({}, (0.5, 0.7), specs, 0.02, 0.02)

    assert selected[0]["duration_ms"] == 60
    assert selected[0]["planning_model"] == "linear_fallback"
    assert diagnostics["axes"]["y"]["model"] == "linear_fallback"


def test_actual_overshoot_requires_crossing_beyond_opposite_deadzone(tmp_path):
    args = argparse.Namespace(
        max_pulses=10, minimum_controllable_x=0.1, minimum_controllable_y=0.1,
        state_json=str(tmp_path / "state.json"), deadzone_x=0.02, deadzone_y=0.03,
    )
    worker = controller.FocusController(args)
    worker.reset_tracking()
    pending = {"pre_center": {"x": 0.7, "y": 0.4}, "pulses": [{"axis": "x"}, {"axis": "y"}]}

    overshoots = worker.record_observed_performance(pending, (0.47, 0.51))

    assert overshoots == [{"axis": "x", "amount": 0.01}]
    assert worker.request_performance["x"]["overshoot_count"] == 1
    assert worker.request_performance["y"]["overshoot_count"] == 0
    assert worker.request_performance["x"]["observed_batches"] == 1


def test_dispatch_performance_attributes_gru_fallback_to_linear(tmp_path):
    worker = controller.FocusController(argparse.Namespace(
        max_pulses=10, minimum_controllable_x=0.1, minimum_controllable_y=0.1,
        state_json=str(tmp_path / "state.json"),
    ))
    worker.reset_tracking()

    worker.record_dispatched_performance([
        {"axis": "x", "planning_model": "gru"},
        {"axis": "x", "planning_model": "gru"},
        {"axis": "y", "planning_model": "linear_fallback"},
    ])

    assert worker.request_performance["x"]["steps"] == 2
    assert worker.request_performance["x"]["models_used"] == {"gru"}
    assert worker.request_performance["y"]["models_used"] == {"linear"}
    assert worker.request_performance["y"]["gru_fallback_steps"] == 1


def test_adaptive_response_timeout_uses_predictions_and_hard_cap():
    assert controller.adaptive_response_timeout(1.0, 0.4, 0.1, [0.3, 0.35]) == pytest.approx(0.58)
    assert controller.adaptive_response_timeout(1.0, 2.0, 0.1, [1.5]) == 1.0
    assert controller.adaptive_response_timeout(1.0, 0.8, 0.7, []) == 0.55
    assert controller.adaptive_response_timeout(0.0, 0.4, 0.1, []) == 0.0


def test_response_frame_confirmation_drops_only_for_strong_low_risk_motion():
    progress = {"x": {"axis_motion": 0.03}, "y": {"axis_motion": 0.02}}
    assert controller.response_frames_required(2, progress, 0.1) == 1
    assert controller.response_frames_required(2, progress, 0.4) == 2
    assert controller.response_frames_required(2, {"x": {"axis_motion": 0.005}}, 0.1) == 2


def test_confidence_aware_batch_caps_uncertain_and_high_risk_batches():
    worker = controller.FocusController.__new__(controller.FocusController)
    worker.args = argparse.Namespace(deadzone_x=0.02, deadzone_y=0.02, minimum_confidence=0.25)
    worker.current_target_context = {"target_confidence": 0.9}
    movement_x = {"offset": {"x": 0.2, "y": 0.0}}
    movement_y = {"offset": {"x": 0.0, "y": 0.1}}
    specs = [
        {"axis": "x", "movement": movement_x},
        {"axis": "y", "movement": movement_y},
        {"axis": "x", "movement": movement_x},
    ]
    uncertain = {"axes": {"x": {"model": "gru", "predicted_std": 0.14, "visibility_probability": 0.9, "no_response_probability": 0.1}}}
    trimmed, info = worker.confidence_aware_batch(specs, uncertain)
    assert [item["axis"] for item in trimmed] == ["x", "y"]
    assert info["policy"] == "uncertainty-capped"
    high = {"axes": {"x": {"model": "gru", "predicted_std": 0.01, "visibility_probability": 0.5, "no_response_probability": 0.1}}}
    trimmed, info = worker.confidence_aware_batch(specs, high)
    assert len(trimmed) == 1 and trimmed[0]["axis"] == "x"
    assert info["policy"] == "high-risk-probe"

import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.deepstream_realtime_focus import RealtimeFocusDispatcher, select_target
from scripts import webcam_stream_server as server


def detected(label, confidence, center_x=0.8, center_y=0.5):
    width = 100
    height = 100
    frame_width = 1000
    frame_height = 1000
    return {
        "label": label,
        "confidence": confidence,
        "bbox": [center_x * frame_width - width / 2, center_y * frame_height - height / 2, width, height],
        "frame_width": frame_width,
        "frame_height": frame_height,
    }


def make_dispatcher(tmp_path, settings):
    settings_path = tmp_path / "settings.json"
    command_path = tmp_path / "command.json"
    state_path = tmp_path / "state.json"
    settings_path.write_text(json.dumps(settings))
    return RealtimeFocusDispatcher(
        source="wifi",
        settings_path=settings_path,
        command_path=command_path,
        state_path=state_path,
    ), command_path


def test_realtime_selection_only_uses_preferred_objects_in_preferred_order():
    objects = [detected("clock", 0.99), detected("cat", 0.61), detected("person", 0.55)]
    selected = select_target(objects, {"preferred_objects": ["person", "cat"], "minimum_object_confidence_percent": 25})
    assert selected["label"] == "person"

    fallback = select_target(objects, {"preferred_objects": ["dog"], "minimum_object_confidence_percent": 25})
    assert fallback is None
    assert select_target(objects, {"preferred_objects": [], "minimum_object_confidence_percent": 25}) is None


def test_realtime_does_not_dispatch_when_only_non_preferred_objects_are_visible(tmp_path):
    dispatcher, command_path = make_dispatcher(
        tmp_path,
        {"preferred_objects": ["dog"], "minimum_object_confidence_percent": 25},
    )

    result = dispatcher.consider(
        [detected("clock", 0.99), detected("cat", 0.95)],
        frame_id=1,
        objects_updated_at=1,
        objects_updated_monotonic_ns=1,
    )

    assert result == {}
    assert not command_path.exists()


def test_realtime_dispatches_on_first_frame_and_latches(tmp_path):
    dispatcher, command_path = make_dispatcher(
        tmp_path,
        {
            "preferred_objects": ["cat", "clock"],
            "minimum_object_confidence_percent": 25,
            "realtime_focus_stable_frames": 1,
        },
    )
    first = dispatcher.consider(
        [detected("clock", 0.99), detected("cat", 0.6)],
        frame_id=10,
        objects_updated_at=100.0,
        objects_updated_monotonic_ns=1000,
    )
    assert first["target_label"] == "cat"
    assert first["trigger"] == "realtime_deepstream_focus"
    assert first["preferred_rank"] == 0
    assert first["priority"] == 1
    assert first["source_frame_id"] == 10
    assert json.loads(command_path.read_text())["request_id"] == first["request_id"]

    command_path.write_text(json.dumps({**first, "enabled": False, "completion_status": "complete"}))
    assert dispatcher.consider(
        [detected("cat", 0.9, center_x=0.79)],
        frame_id=11,
        objects_updated_at=100.01,
        objects_updated_monotonic_ns=1010,
    ) == {}


def test_realtime_request_carries_detector_track_identity(tmp_path):
    dispatcher, _command_path = make_dispatcher(
        tmp_path,
        {"preferred_objects": ["keyboard"], "realtime_focus_stable_frames": 1},
    )
    keyboard = {**detected("keyboard", 0.9), "track_id": 17, "track_age_frames": 8}

    request = dispatcher.consider(
        [keyboard], frame_id=10, objects_updated_at=100.0, objects_updated_monotonic_ns=1000,
    )

    assert request["target_track_id"] == 17


def test_realtime_rearms_after_configured_absence(tmp_path):
    dispatcher, command_path = make_dispatcher(
        tmp_path,
        {
            "preferred_objects": ["chair"],
            "realtime_focus_rearm_absent_frames": 2,
            "realtime_focus_rearm_absent_seconds": 0,
            "realtime_focus_cooldown_seconds": 0,
        },
    )
    first = dispatcher.consider(
        [detected("chair", 0.8)], frame_id=1, objects_updated_at=1, objects_updated_monotonic_ns=1
    )
    command_path.write_text(json.dumps({**first, "enabled": False}))
    dispatcher.consider([], frame_id=2, objects_updated_at=2, objects_updated_monotonic_ns=2)
    dispatcher.consider([], frame_id=3, objects_updated_at=3, objects_updated_monotonic_ns=3)
    second = dispatcher.consider(
        [detected("chair", 0.8)], frame_id=4, objects_updated_at=4, objects_updated_monotonic_ns=4
    )
    assert second["request_id"] != first["request_id"]


def test_realtime_rearms_when_latched_target_is_gone_but_other_objects_remain(tmp_path):
    dispatcher, command_path = make_dispatcher(
        tmp_path,
        {
            "preferred_objects": ["laptop", "person"],
            "realtime_focus_rearm_absent_frames": 2,
            "realtime_focus_rearm_absent_seconds": 0,
            "realtime_focus_cooldown_seconds": 0,
        },
    )
    first = dispatcher.consider(
        [detected("laptop", 0.8)], frame_id=1, objects_updated_at=1, objects_updated_monotonic_ns=1
    )
    command_path.write_text(json.dumps({**first, "enabled": False}))
    assert dispatcher.consider(
        [detected("person", 0.9)], frame_id=2, objects_updated_at=2, objects_updated_monotonic_ns=2
    ) == {}
    second = dispatcher.consider(
        [detected("person", 0.9)], frame_id=3, objects_updated_at=3, objects_updated_monotonic_ns=3
    )
    assert second["target_label"] == "person"


def test_realtime_never_overwrites_an_active_manual_command(tmp_path):
    dispatcher, command_path = make_dispatcher(tmp_path, {"preferred_objects": ["person"]})
    manual = {"enabled": True, "request_id": "manual", "target_label": "clock", "source": "wifi"}
    command_path.write_text(json.dumps(manual))
    assert dispatcher.consider(
        [detected("person", 0.99)], frame_id=1, objects_updated_at=1, objects_updated_monotonic_ns=1
    ) == {}
    assert json.loads(command_path.read_text()) == manual


def test_realtime_preempts_active_lower_priority_realtime_target(tmp_path):
    dispatcher, command_path = make_dispatcher(
        tmp_path,
        {
            "preferred_objects": ["laptop", "person"],
            "realtime_focus_cooldown_seconds": 0,
        },
    )
    person = dispatcher.consider(
        [detected("person", 0.9)], frame_id=1, objects_updated_at=1, objects_updated_monotonic_ns=1
    )

    laptop = dispatcher.consider(
        [detected("person", 0.9), detected("laptop", 0.7)],
        frame_id=2,
        objects_updated_at=2,
        objects_updated_monotonic_ns=2,
    )

    assert laptop["target_label"] == "laptop"
    assert laptop["preempted_request_id"] == person["request_id"]
    assert laptop["preempted_target_label"] == "person"
    assert json.loads(command_path.read_text())["request_id"] == laptop["request_id"]


def test_normalized_settings_enable_single_frame_general_realtime_focus():
    settings = server.normalize_deepstream_settings({})
    assert settings["realtime_focus_enabled"] is True
    assert settings["realtime_focus_stable_frames"] == 5

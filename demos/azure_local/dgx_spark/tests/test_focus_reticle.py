from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from scripts.webcam_stream_server import draw_focus_reticle_on_image, focus_navigation_lines, focus_reticle_visible


def active_focus_state():
    return {
        "enabled": True,
        "status": "focusing",
        "source": "wifi",
        "target_label": "cat",
        "goal_center": {"x": 0.5, "y": 0.5},
        "object_center": {"x": 0.72, "y": 0.31},
        "effective_deadzone": {"x": 0.08, "y": 0.052},
        "bbox": [400, 100, 200, 200],
        "frame_size": [1000, 500],
        "pulse_count": 7,
        "max_steps": 100,
        "pending_pulse_action_direction": "up",
        "pending_pulse_direction": "down",
        "pending_pulse_duration_ms": 80,
        "pending_pulse_backend": "auto",
        "offset": {"x": 0.22, "y": -0.19},
        "distance_to_center": 0.2907,
        "auto_centering_runtime_seconds": 6.375,
    }


def test_focus_reticle_is_burned_into_active_preview_image():
    image = Image.new("RGB", (1000, 500), (0, 0, 0))
    state = active_focus_state()
    assert focus_reticle_visible(state, "wifi") is True
    assert draw_focus_reticle_on_image(image, state, "wifi") is True
    assert image.getpixel((500, 250)) != (0, 0, 0)
    assert image.getpixel((720, 155)) != (0, 0, 0)
    # The detected box is 20% x 40%; its ghost alignment box is projected at
    # image center (400,150)-(600,350), not at the detected source location.
    assert image.getpixel((400, 150)) != (0, 0, 0)
    assert image.getpixel((400, 100)) == (0, 0, 0)


def test_focus_reticle_does_not_modify_other_camera_source():
    image = Image.new("RGB", (640, 480), (0, 0, 0))
    assert draw_focus_reticle_on_image(image, active_focus_state(), "bulb") is False
    assert image.getbbox() is None


def test_focus_navigation_lines_show_step_direction_and_distance():
    lines = focus_navigation_lines(active_focus_state())
    assert lines[0] == "STEP 7/100  ^ UP  CMD DOWN"
    assert lines[1] == "DX +0.2200  DY -0.1900  DIST 0.2907"
    assert lines[2] == "FOCUSING  80ms  auto"
    assert lines[3] == "MODELS  LEFT/RIGHT: LINEAR  UP/DOWN: LINEAR"
    assert lines[4] == "LAST RUN 6.38s"


def test_focus_navigation_lines_warn_when_lost_target_is_backtracking():
    state = {
        **active_focus_state(),
        "status": "recovering_target",
        "target_recovery_mode": "backtracking",
        "target_recovery_steps": 4,
        "movement_history_depth": 2,
    }

    lines = focus_navigation_lines(state)

    assert lines[0] == "!! OBJECT LOST - BACKTRACKING  REV 4  HIST 2"
    assert lines[1] == "STEP 7/100  ^ UP  CMD DOWN"

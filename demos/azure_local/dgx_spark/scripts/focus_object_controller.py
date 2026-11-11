#!/usr/bin/env python3
"""Center a requested detected object once with conservative PTZ pulses."""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
import math
import os
import random
import select
import signal
import socket
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRECTION_GAINS = {
    "left": 0.00030,
    "right": 0.00036,
    "up": 0.00035,
    "down": 0.00063,
}
MAX_TARGET_REACQUISITION_BACKTRACK_STEPS = 2
MAX_TARGET_RANDOM_SEARCH_MOVES = 3
TARGET_RANDOM_SEARCH_DIRECTIONS_PER_MOVE = 3


def finite_float(value: object, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def read_json(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: str | Path, payload: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output)


def load_direction_gain_state(path: str | Path) -> tuple[dict[str, float], dict[str, int]]:
    payload = read_json(path) if str(path or "").strip() else {}
    stored_gains = payload.get("gains") if isinstance(payload.get("gains"), dict) else {}
    stored_counts = payload.get("sample_counts") if isinstance(payload.get("sample_counts"), dict) else {}
    gains = {}
    counts = {}
    for direction, default in DEFAULT_DIRECTION_GAINS.items():
        try:
            gains[direction] = max(0.00005, min(0.003, float(stored_gains.get(direction, default))))
        except (TypeError, ValueError):
            gains[direction] = default
        try:
            counts[direction] = max(0, int(stored_counts.get(direction, 0)))
        except (TypeError, ValueError):
            counts[direction] = 0
    return gains, counts


def learned_gain_updates(pending: dict, actual_delta: dict, current_gains: dict[str, float],
                         alpha: float = 0.15) -> dict[str, tuple[float, float]]:
    """Return clean per-direction EWMA updates as direction: (new gain, raw sample)."""
    pulses = pending.get("pulses") if isinstance(pending.get("pulses"), list) else []
    signs = {"left": 1.0, "right": -1.0, "up": 1.0, "down": -1.0}
    updates = {}
    for axis in ("x", "y"):
        axis_pulses = [item for item in pulses if isinstance(item, dict) and item.get("axis") == axis]
        directions = {str(item.get("direction") or "") for item in axis_pulses}
        if not axis_pulses or len(directions) != 1:
            continue
        direction = next(iter(directions))
        sign = signs.get(direction)
        duration_ms = sum(max(0, int(item.get("duration_ms") or 0)) for item in axis_pulses)
        try:
            projected_motion = float(actual_delta.get(axis) or 0.0) * float(sign or 0.0)
        except (TypeError, ValueError):
            continue
        if sign is None or duration_ms < 30 or projected_motion < 0.005 or projected_motion > 0.45:
            continue
        raw_sample = projected_motion / duration_ms
        if not 0.00005 <= raw_sample <= 0.003:
            continue
        old_gain = max(0.00005, min(0.003, float(current_gains.get(direction, DEFAULT_DIRECTION_GAINS[direction]))))
        clipped_sample = max(old_gain * 0.35, min(old_gain * 2.5, raw_sample))
        weight = max(0.01, min(0.5, float(alpha)))
        updates[direction] = ((1.0 - weight) * old_gain + weight * clipped_sample, raw_sample)
    return updates


def focus_completion_audio_enabled(settings_path: str | Path) -> bool:
    settings = read_json(settings_path)
    speech_enabled = bool(
        settings.get(
            "speech_output_audio_enabled",
            not bool(settings.get("speech_output_muted", False)),
        )
    )
    component_audio_enabled = bool(
        settings.get(
            "component_activation_audio_enabled",
            settings.get("stage_chimes_enabled", True),
        )
    )
    return speech_enabled and component_audio_enabled


def play_focus_completion_chime(args: argparse.Namespace, source: str) -> dict:
    if not focus_completion_audio_enabled(getattr(args, "component_audio_settings_json", "")):
        return {"played": False, "reason": "focus_completion_audio_disabled"}
    cue_url = str(getattr(args, "completion_chime_url", "") or "").strip()
    talk_template = str(getattr(args, "completion_chime_talk_url", "") or "").strip()
    if not cue_url or not talk_template:
        return {"played": False, "reason": "completion_chime_route_unconfigured"}
    timeout = max(1.0, float(getattr(args, "completion_chime_timeout", 6.0) or 6.0))
    try:
        with urlopen(cue_url, timeout=timeout) as response:
            audio = response.read(256 * 1024)
        if not audio:
            return {"played": False, "reason": "completion_chime_empty"}
        request = Request(
            talk_template.format(source=str(source or "wifi").strip().lower()),
            data=audio,
            headers={"Content-Type": "audio/wav"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            body = response.read(16 * 1024).decode("utf-8", "replace")
        result = json.loads(body) if body.strip() else {}
        if isinstance(result, dict) and result.get("status") not in {None, "ok"}:
            return {"played": False, "reason": str(result.get("error") or result)[:400]}
        return {
            "played": True,
            "route": "camera_speaker",
            "backend": str(result.get("backend") or "") if isinstance(result, dict) else "",
        }
    except Exception as exc:
        return {"played": False, "reason": str(exc)[:400]}


def normalize_label(value: object) -> str:
    label = " ".join(str(value or "").strip().lower().replace("_", " ").split())
    for prefix in ("the ", "a ", "an "):
        if label.startswith(prefix):
            label = label[len(prefix) :].strip()
            break
    return label


def source_payload(payload: dict, source: str) -> dict:
    sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
    nested = sources.get(source)
    if isinstance(nested, dict):
        return nested
    payload_source = str(payload.get("source") or "").strip().lower()
    return payload if not payload_source or payload_source == source else {}


def detection_timestamp(payload: dict) -> float:
    for key in ("objects_updated_at", "deepstream_output_updated_at", "updated_at"):
        try:
            value = float(payload.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0.0


def detection_marker(payload: dict) -> tuple[str, int | float]:
    for key in ("source_frame_id", "object_frame_id", "frame_id", "deepstream_frame_id"):
        try:
            value = int(payload.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return ("frame", value)
    return ("timestamp", detection_timestamp(payload))


def preserved_focus_geometry(state: dict) -> dict:
    """Keep the visual target stable while waiting for delayed camera feedback."""
    fields = {
        "object_center", "goal_center", "offset", "distance_to_center", "bbox", "frame_size",
        "effective_deadzone", "confidence", "focus_point_mode", "axis_preference", "active_axis",
        "next_axis", "next_direction", "control_mode",
    }
    return {key: value for key, value in state.items() if key in fields}


def object_probability(item: dict) -> float:
    try:
        value = float(item.get("confidence", item.get("score", item.get("probability", 0.0))) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value / 100.0 if value > 1.0 else value))


def normalized_box(item: dict) -> dict | None:
    bbox = item.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return None
    try:
        left, top, width, height = (float(value) for value in bbox[:4])
        frame_width = float(item.get("frame_width") or item.get("image_width") or 0)
        frame_height = float(item.get("frame_height") or item.get("image_height") or 0)
    except (TypeError, ValueError):
        return None
    values = (left, top, width, height, frame_width, frame_height)
    if not all(math.isfinite(value) for value in values):
        return None
    if frame_width <= 0 or frame_height <= 0 or width <= 0 or height <= 0:
        return None
    # Invalid metadata must never move a physical camera. A small tolerance allows
    # detector boxes that land fractionally outside the image edge.
    tolerance = 0.02
    if left < -frame_width * tolerance or top < -frame_height * tolerance:
        return None
    if left + width > frame_width * (1.0 + tolerance) or top + height > frame_height * (1.0 + tolerance):
        return None
    center_x = (left + width / 2.0) / frame_width
    center_y = (top + height / 2.0) / frame_height
    if not (-tolerance <= center_x <= 1.0 + tolerance and -tolerance <= center_y <= 1.0 + tolerance):
        return None
    return {
        "center_x": center_x,
        "center_y": center_y,
        "width": width / frame_width,
        "height": height / frame_height,
        "bbox": [left, top, width, height],
        "frame_size": [int(frame_width), int(frame_height)],
    }


def choose_target(
    objects: list[dict],
    label: str,
    previous_center: tuple[float, float] | None = None,
    track_id: object | None = None,
) -> dict | None:
    candidates = []
    for item in objects:
        if not isinstance(item, dict) or normalize_label(item.get("label") or item.get("class") or item.get("name")) != label:
            continue
        if track_id is not None and str(item.get("track_id")) != str(track_id):
            continue
        box = normalized_box(item)
        if box is None:
            continue
        probability = object_probability(item)
        distance = 0.0
        if previous_center is not None:
            distance = math.hypot(box["center_x"] - previous_center[0], box["center_y"] - previous_center[1])
        # Confidence acquires a target; proximity keeps identity stable when several
        # objects share the same class and no tracker ID is available yet.
        score = probability - min(0.35, distance * 0.5)
        candidates.append({
            **box,
            "confidence": probability,
            "score": score,
            "track_id": item.get("track_id"),
            "track_age_frames": item.get("track_age_frames"),
            "raw": item,
        })
    return max(candidates, key=lambda item: item["score"], default=None)


def movement_for_target(
    target: dict,
    deadzone_x: float,
    deadzone_y: float,
    blocked_axes: set[str] | None = None,
    invert_tilt: bool = False,
    prefer_axis: str = "",
) -> dict:
    dx = float(target["center_x"]) - 0.5
    dy = float(target["center_y"]) - 0.5
    blocked = blocked_axes or set()
    x_outside = abs(dx) > deadzone_x
    y_outside = abs(dy) > deadzone_y
    x_active = x_outside and "x" not in blocked
    y_active = y_outside and "y" not in blocked
    if not x_active and not y_active:
        return {
            "centered": not x_outside and not y_outside,
            "exhausted": x_outside or y_outside,
            "direction": "",
            "axis": "",
            "error": 0.0,
            "offset": {"x": dx, "y": dy},
        }
    x_error = abs(dx)
    y_error = abs(dy)
    x_ratio = x_error / max(1e-6, float(deadzone_x))
    y_ratio = y_error / max(1e-6, float(deadzone_y))
    if prefer_axis == "x" and x_active:
        axis = "x"
    elif prefer_axis == "y" and y_active:
        axis = "y"
    elif x_active and y_active:
        axis = "y" if y_ratio > x_ratio else "x"
    elif x_active:
        axis = "x"
    else:
        axis = "y"
    if axis == "x":
        direction = "right" if dx > 0 else "left"
        error = x_error
    else:
        direction = "down" if dy > 0 else "up"
        if invert_tilt:
            direction = "up" if direction == "down" else "down"
        error = y_error
    return {
        "centered": False,
        "exhausted": False,
        "direction": direction,
        "axis": axis,
        "error": error,
        "offset": {"x": dx, "y": dy},
        "normalized_error": {"x": x_ratio, "y": y_ratio},
    }


def locked_axis_for_target(
    target: dict,
    deadzone_x: float,
    deadzone_y: float,
    active_axis: str = "",
) -> str:
    dx = float(target["center_x"]) - 0.5
    dy = float(target["center_y"]) - 0.5
    x_outside = abs(dx) > float(deadzone_x)
    y_outside = abs(dy) > float(deadzone_y)
    if active_axis == "x" and x_outside:
        return "x"
    if active_axis == "y" and y_outside:
        return "y"
    if x_outside and y_outside:
        x_ratio = abs(dx) / max(1e-6, float(deadzone_x))
        y_ratio = abs(dy) / max(1e-6, float(deadzone_y))
        return "y" if y_ratio > x_ratio else "x"
    if x_outside:
        return "x"
    if y_outside:
        return "y"
    return ""


def paired_axis_movements(
    target: dict,
    deadzone_x: float,
    deadzone_y: float,
    blocked_axes: set[str] | None = None,
) -> list[dict]:
    """Plan horizontal then vertical corrections from the same detector box."""
    blocked = set(blocked_axes or set())
    plan = []
    for axis, other_axis in (("x", "y"), ("y", "x")):
        if axis in blocked:
            continue
        movement = movement_for_target(
            target,
            deadzone_x,
            deadzone_y,
            blocked | {other_axis},
            prefer_axis=axis,
        )
        if movement.get("axis") == axis and not movement.get("centered"):
            plan.append(movement)
    return plan


def fixed_pulse_batch(
    target: dict,
    deadzone_x: float,
    deadzone_y: float,
    blocked_axes: set[str] | None = None,
    pulse_count: int = 3,
) -> list[dict]:
    """Build a fixed-size batch without consulting another detector frame."""
    base = paired_axis_movements(target, deadzone_x, deadzone_y, blocked_axes)
    if not base:
        return []
    count = max(1, int(pulse_count))
    batch = list(base[:count])
    dominant = max(
        base,
        key=lambda item: abs(float(item["offset"][str(item["axis"])]))
        / max(1e-6, deadzone_x if item["axis"] == "x" else deadzone_y),
    )
    while len(batch) < count:
        batch.append(dominant)
    return batch


def target_axis_progress(previous_offset: float, current_offset: float, minimum_progress: float) -> dict:
    """Measure whether the tracked bounding-box midpoint moved toward frame center."""
    previous = float(previous_offset)
    current = float(current_offset)
    axis_motion = abs(current - previous)
    center_progress = abs(previous) - abs(current)
    return {
        "axis_motion": axis_motion,
        "center_progress": center_progress,
        "verified": center_progress >= max(0.0, float(minimum_progress)),
    }


def verify_pulse_motion(
    global_motion: dict,
    pulse_axis: str,
    target_progress: dict,
    minimum_phase_response: float,
    minimum_global_motion: float,
) -> dict:
    """Corroborate target progress with physical whole-frame camera motion."""
    axis_motion = abs(float(global_motion.get(pulse_axis, 0.0)))
    scene_motion_verified = bool(
        global_motion.get("available")
        and float(global_motion.get("response", 0.0)) >= float(minimum_phase_response)
        and float(global_motion.get("magnitude", 0.0)) >= float(minimum_global_motion)
    )
    global_axis_verified = bool(
        scene_motion_verified and axis_motion >= float(minimum_global_motion)
    )
    target_motion_verified = bool(
        scene_motion_verified and target_progress.get("verified")
    )
    target_not_farther_from_center = float(target_progress.get("center_progress", 0.0)) >= 0.0
    global_centering_verified = bool(global_axis_verified and target_not_farther_from_center)
    return {
        "axis_motion": axis_motion,
        "scene_motion_verified": scene_motion_verified,
        "global_axis_verified": global_axis_verified,
        "global_centering_verified": global_centering_verified,
        "target_motion_verified": target_motion_verified,
        "verified": global_centering_verified or target_motion_verified,
        "verified_axis_motion": max(
            axis_motion if global_centering_verified else 0.0,
            float(target_progress.get("axis_motion", 0.0)) if target_motion_verified else 0.0,
        ),
    }


def backend_direction(logical_direction: str, backend: str, invert_native_tilt: bool) -> str:
    """Translate logical view direction to a backend-specific camera command."""
    direction = str(logical_direction)
    if invert_native_tilt and backend in {"auto", "native"} and direction in {"up", "down"}:
        return "down" if direction == "up" else "up"
    return direction


def should_probe_vertical_direction(adaptive_enabled: bool, axis: str, no_progress_pulses: int, probe_limit: int) -> bool:
    return bool(
        adaptive_enabled
        and axis == "y"
        and int(no_progress_pulses) >= max(1, int(probe_limit))
    )


def pulse_duration(
    error: float,
    deadzone: float,
    minimum_ms: int,
    maximum_ms: int,
    minimum_controllable_step: float = 0.0,
) -> int:
    minimum_ms = max(1, int(minimum_ms))
    maximum_ms = max(minimum_ms, int(maximum_ms))
    correction = max(0.0, float(error) - float(deadzone))
    if minimum_controllable_step > 0:
        # A minimum-duration pulse moves approximately one calibrated step. Use
        # that physical response for a coarse first correction, then naturally
        # fall back to the minimum pulse as the target approaches the deadzone.
        calibrated = minimum_ms * max(1.0, correction / minimum_controllable_step)
        return max(minimum_ms, min(maximum_ms, int(round(calibrated))))
    span = max(1e-6, 0.5 - deadzone)
    ratio = max(0.0, min(1.0, correction / span))
    return int(round(minimum_ms + ratio * (maximum_ms - minimum_ms)))


def adjusted_axis_pulse_duration(
    base_duration_ms: int,
    axis: str,
    vertical_multiplier: float,
    max_vertical_pulse_ms: int,
    recovery_stage: int,
) -> int:
    duration_ms = int(base_duration_ms)
    if axis == "y":
        duration_ms = int(round(duration_ms * max(0.25, float(vertical_multiplier))))
    duration_ms *= 1 if recovery_stage <= 0 else 2 if recovery_stage == 1 else 4
    if axis == "y" and max_vertical_pulse_ms > 0:
        duration_ms = min(duration_ms, int(max_vertical_pulse_ms))
    return max(30, min(1000, duration_ms))


def adaptive_response_timeout(
    configured_seconds: float,
    predicted_seconds: float | None,
    no_response_probability: float | None,
    recent_seconds: list[float] | None = None,
) -> float:
    """Return a conservative learned wait bounded by the configured timeout."""
    if float(configured_seconds) <= 0:
        return 0.0
    configured = max(0.2, float(configured_seconds))
    candidates = []
    if predicted_seconds is not None and math.isfinite(float(predicted_seconds)):
        candidates.append(max(0.0, float(predicted_seconds)) * 1.25 + 0.08)
    clean_recent = sorted(
        float(value) for value in (recent_seconds or [])
        if math.isfinite(float(value)) and float(value) >= 0
    )
    if clean_recent:
        candidates.append(clean_recent[min(len(clean_recent) - 1, int(0.85 * len(clean_recent)))] + 0.05)
    learned = max(candidates) if candidates else configured
    if no_response_probability is not None and float(no_response_probability) >= 0.55:
        learned = min(learned, 0.55)
    return max(0.25, min(configured, learned))


def response_frames_required(configured_frames: int, progress_by_axis: dict, no_response_probability: float | None) -> int:
    configured = max(1, int(configured_frames))
    if configured <= 1 or no_response_probability is None or float(no_response_probability) > 0.20:
        return configured
    motions = [float(item.get("axis_motion") or 0.0) for item in progress_by_axis.values() if isinstance(item, dict)]
    return 1 if motions and min(motions) >= 0.01 else configured


def pulse_command_spec(
    movement: dict,
    args: argparse.Namespace,
    deadzone_x: float,
    deadzone_y: float,
    minimum_step: dict[str, float],
    recovery_stage: int,
    vertical_direction_inverted: bool,
    direction_gains: dict[str, float] | None = None,
) -> dict:
    direction = str(movement["direction"])
    axis = str(movement.get("axis") or "")
    relevant_deadzone = deadzone_x if axis == "x" else deadzone_y
    minimum_pulse_ms = max(30, int(args.min_pulse_ms))
    if axis == "y" and int(getattr(args, "min_vertical_pulse_ms", 0) or 0) > 0:
        minimum_pulse_ms = max(30, min(minimum_pulse_ms, int(args.min_vertical_pulse_ms)))
    learned_step = 0.0
    if isinstance(direction_gains, dict):
        try:
            learned_step = float(direction_gains.get(direction) or 0.0) * minimum_pulse_ms
        except (TypeError, ValueError):
            learned_step = 0.0
    duration_ms = pulse_duration(
        float(movement["error"]),
        relevant_deadzone,
        minimum_pulse_ms,
        args.max_pulse_ms,
        learned_step or minimum_step.get(axis, 0.0),
    )
    speed = int(getattr(args, "vertical_speed", 0) or args.speed) if axis == "y" else int(args.speed)
    if bool(getattr(args, "native_only_ptz", False)):
        backend = "native"
    elif axis == "y":
        backend = "auto" if recovery_stage < 2 else "onvif"
    else:
        backend = "native" if recovery_stage < 2 else "onvif"
    invert_tilt = bool(getattr(args, "invert_tilt", False))
    if axis == "y" and vertical_direction_inverted:
        invert_tilt = not invert_tilt
    hardware_direction = backend_direction(direction, backend, invert_tilt)
    duration_ms = adjusted_axis_pulse_duration(
        duration_ms,
        axis,
        float(getattr(args, "vertical_pulse_multiplier", 1.0)),
        int(getattr(args, "max_vertical_pulse_ms", 0) or 0),
        recovery_stage,
    )
    return {
        "axis": axis,
        "direction": direction,
        "hardware_direction": hardware_direction,
        "duration_ms": duration_ms,
        "speed": speed,
        "backend": backend,
        "recovery_stage": recovery_stage,
        "reset_native_session": recovery_stage == 1,
        "movement": movement,
    }


def predicted_center_for_pulses(
    center: tuple[float, float],
    pulses: list[dict],
    response_model: dict[str, float],
    minimum_pulse_ms: int,
    maximum_aggregate_delta: float = 0.35,
) -> dict:
    """Apply per-direction gains and cap each axis's aggregate batch prediction."""
    predicted_x, predicted_y = map(float, center)
    pulse_floor = max(1, int(minimum_pulse_ms))
    predicted_pulses = []
    signs = {"left": 1.0, "right": -1.0, "up": 1.0, "down": -1.0}
    aggregate = {"x": 0.0, "y": 0.0}
    cap = max(0.02, min(0.75, float(maximum_aggregate_delta)))
    for pulse in pulses:
        axis = str(pulse.get("axis") or "")
        direction = str(pulse.get("direction") or "")
        duration_ms = max(0, int(pulse.get("duration_ms") or 0))
        if direction in response_model:
            magnitude = max(0.0, float(response_model.get(direction, 0.0))) * duration_ms
        else:
            # Backward-compatible axis-step model for standalone callers.
            magnitude = max(0.0, float(response_model.get(axis, 0.0))) * duration_ms / pulse_floor
        requested_signed = signs.get(direction, 0.0) * magnitude
        remaining = max(0.0, cap - abs(aggregate.get(axis, 0.0)))
        signed = math.copysign(min(abs(requested_signed), remaining), requested_signed) if requested_signed else 0.0
        if axis in aggregate:
            aggregate[axis] += signed
        delta = {"x": signed if axis == "x" else 0.0, "y": signed if axis == "y" else 0.0}
        predicted_x += delta["x"]
        predicted_y += delta["y"]
        predicted_pulses.append({
            **pulse,
            "predicted_target_delta": delta,
            "prediction_capped": abs(signed) + 1e-12 < abs(requested_signed),
        })
    return {
        "center": {"x": predicted_x, "y": predicted_y},
        "delta": {"x": predicted_x - center[0], "y": predicted_y - center[1]},
        "pulses": predicted_pulses,
    }


def budgeted_pulse_specs(
    movements: list[dict],
    args: argparse.Namespace,
    deadzone_x: float,
    deadzone_y: float,
    minimum_step: dict[str, float],
    recovery_stage_by_axis: dict[str, int],
    vertical_direction_inverted: bool,
    direction_gains: dict[str, float],
    simple_closed_loop: bool,
) -> list[dict]:
    """Drop redundant batch pulses once learned displacement covers the correction budget."""
    remaining_by_axis: dict[str, float] = {}
    specs = []
    vertical_pulses = 0
    max_vertical_pulses = max(1, int(getattr(args, "max_vertical_pulses_per_step", 1)))
    for movement in movements:
        axis = str(movement.get("axis") or "")
        direction = str(movement.get("direction") or "")
        if axis not in {"x", "y"} or direction not in DEFAULT_DIRECTION_GAINS:
            continue
        if simple_closed_loop and axis == "y" and vertical_pulses >= max_vertical_pulses:
            continue
        deadzone = deadzone_x if axis == "x" else deadzone_y
        if axis not in remaining_by_axis:
            remaining_by_axis[axis] = max(0.0, float(movement.get("error") or 0.0) - deadzone)
        remaining = remaining_by_axis[axis]
        if simple_closed_loop and remaining <= 0.0:
            continue
        adjusted = movement
        if simple_closed_loop:
            adjusted = {**movement, "error": deadzone + remaining}
        spec = pulse_command_spec(
            adjusted,
            args,
            deadzone_x,
            deadzone_y,
            minimum_step,
            0 if simple_closed_loop else recovery_stage_by_axis.get(axis, 0),
            vertical_direction_inverted,
            direction_gains,
        )
        specs.append(spec)
        if axis == "y":
            vertical_pulses += 1
        if simple_closed_loop:
            gain = max(0.00005, float(direction_gains.get(direction, DEFAULT_DIRECTION_GAINS[direction])))
            remaining_by_axis[axis] = max(0.0, remaining - gain * int(spec["duration_ms"]))
    return specs


def reverse_pulse_step(step: list[dict]) -> list[dict]:
    """Reverse one dispatched PTZ batch in reverse execution order."""
    opposite = {"left": "right", "right": "left", "up": "down", "down": "up"}
    reversed_specs = []
    for pulse in reversed(step):
        logical = opposite.get(str(pulse.get("direction") or ""), "")
        hardware = opposite.get(str(pulse.get("hardware_direction") or ""), "")
        if not logical or not hardware:
            continue
        reversed_specs.append({
            "axis": str(pulse.get("axis") or ""),
            "direction": logical,
            "hardware_direction": hardware,
            "duration_ms": max(30, min(1000, int(pulse.get("duration_ms") or 30))),
            "speed": max(1, min(8, int(pulse.get("speed") or 1))),
            "backend": str(pulse.get("backend") or "auto"),
            "recovery_stage": int(pulse.get("recovery_stage") or 0),
            "reset_native_session": False,
            "planning_model": str(pulse.get("planning_model") or "linear"),
        })
    return reversed_specs


def pulse_prediction_error(predicted_center: dict, actual_center: tuple[float, float] | None) -> dict | None:
    if actual_center is None:
        return None
    dx = float(actual_center[0]) - float(predicted_center["x"])
    dy = float(actual_center[1]) - float(predicted_center["y"])
    return {
        "x": dx,
        "y": dy,
        "absolute_x": abs(dx),
        "absolute_y": abs(dy),
        "distance": math.hypot(dx, dy),
        "squared_distance": dx * dx + dy * dy,
    }


def append_prediction_event(path: str | Path, event: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def effective_centering_deadzone(base_deadzone: float, axis: str, vertical_completion_epsilon: float) -> float:
    epsilon = max(0.0, float(vertical_completion_epsilon)) if axis == "y" else 0.0
    return min(0.45, max(0.01, float(base_deadzone)) + epsilon)


def motion_preview_marker(path: str | Path) -> tuple[int, int] | None:
    try:
        stat = Path(path).stat()
    except (OSError, TypeError, ValueError):
        return None
    return (stat.st_mtime_ns, stat.st_size)


def manual_ptz_paused_seconds(command: dict, *, now: float | None = None) -> float:
    """Return completed plus currently elapsed manual-yield time without double-counting overlaps."""
    current_time = time.time() if now is None else float(now)
    try:
        accumulated = max(0.0, float(command.get("manual_ptz_paused_seconds") or 0.0))
        started_at = float(command.get("manual_ptz_pause_started_at") or 0.0)
        pause_until = float(command.get("manual_ptz_pause_until") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if started_at <= 0.0:
        return round(accumulated, 3)
    if bool(command.get("manual_ptz_active")):
        current_pause = max(0.0, current_time - started_at)
    else:
        current_pause = max(0.0, min(current_time, pause_until) - started_at)
    return round(accumulated + current_pause, 3)


def auto_centering_runtime_seconds(command: dict, *, now: float | None = None, monotonic_ns: int | None = None) -> float:
    """Return request-to-finish runtime, preferring the same-host monotonic clock."""
    try:
        recorded_runtime = float(command.get("auto_centering_runtime_seconds"))
    except (TypeError, ValueError):
        recorded_runtime = -1.0
    if recorded_runtime >= 0.0:
        return round(recorded_runtime, 3)
    try:
        requested_at = float(command.get("requested_at") or 0.0)
        completed_at = float(command.get("completed_at") or command.get("stopped_at") or 0.0)
    except (TypeError, ValueError):
        requested_at = 0.0
        completed_at = 0.0
    current_time = time.time() if now is None else float(now)
    runtime_endpoint = completed_at if not command.get("enabled") and completed_at > 0.0 else current_time
    paused_seconds = manual_ptz_paused_seconds(command, now=runtime_endpoint)
    if not command.get("enabled") and requested_at > 0.0 and completed_at > 0.0:
        return round(max(0.0, completed_at - requested_at - paused_seconds), 3)
    try:
        requested_monotonic_ns = int(command.get("requested_monotonic_ns") or 0)
    except (TypeError, ValueError):
        requested_monotonic_ns = 0
    if requested_monotonic_ns > 0:
        current_monotonic_ns = time.monotonic_ns() if monotonic_ns is None else int(monotonic_ns)
        return round(max(0.0, (current_monotonic_ns - requested_monotonic_ns) / 1_000_000_000 - paused_seconds), 3)
    return round(max(0.0, current_time - requested_at - paused_seconds), 3) if requested_at > 0 else 0.0


def ptz_result_confirms_idle(result: dict) -> bool:
    if not isinstance(result, dict):
        return False
    backend = str(result.get("backend") or "").strip().lower()
    if backend == "onvif" or result.get("pulse_stop_ok") is True:
        return True
    explicit_stop = result.get("explicit_stop")
    return isinstance(explicit_stop, dict) and str(explicit_stop.get("status") or "ok").lower() != "error"


def ptz_url(template: str, source: str) -> str:
    if "{source}" in template:
        return template.format(source=source)
    if source == "bulb" and template.endswith("/wifi-ptz"):
        return template[: -len("/wifi-ptz")] + "/bulb-ptz"
    if source == "wifi" and template.endswith("/bulb-ptz"):
        return template[: -len("/bulb-ptz")] + "/wifi-ptz"
    return template


def post_pulse(
    url: str,
    direction: str,
    speed: int,
    duration_ms: int,
    timeout: float,
    backend: str = "onvif",
    reset_native_session: bool = False,
    socket_path: str = "",
    source: str = "wifi",
) -> dict:
    local_payload = {
        "source": source,
        "command": direction,
        "action": "pulse",
        "speed": speed,
        "duration_ms": duration_ms,
        "backend": backend,
        "reset_native_session": bool(reset_native_session),
    }
    if str(socket_path or "").strip():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(json.dumps(local_payload, separators=(",", ":")).encode("utf-8"))
            raw = client.recv(16384).decode("utf-8", "replace")
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
            raw = ""
        finally:
            client.close()
        if raw:
            result = json.loads(raw)
            if isinstance(result, dict) and result.get("status") == "error":
                raise RuntimeError(str(result.get("error") or "PTZ command failed"))
            return result if isinstance(result, dict) else {}
    request = Request(
        url,
        data=json.dumps(local_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Camera-Control-Owner": "focus-object"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(8192).decode("utf-8", "replace")
        result = json.loads(raw) if raw.strip() else {}
        if isinstance(result, dict) and result.get("status") == "error":
            raise RuntimeError(str(result.get("error") or "PTZ command failed"))
        result = result if isinstance(result, dict) else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"PTZ HTTP {exc.code}: {body}") from exc
    backend = str(result.get("backend") or "").strip().lower()
    if backend == "onvif" or (backend == "x64_netsdk_qemu_persistent" and result.get("pulse_stop_ok") is True):
        return result
    # Native ASH21 pulses can acknowledge their stop while leaving PTZ in a
    # stale motion state. A separate ONVIF stop releases the motor.
    try:
        stop_result = post_stop(url, direction, timeout)
        return {**result, "explicit_stop": stop_result}
    except Exception as exc:
        return {**result, "explicit_stop_error": str(exc)[:500]}


def post_stop(
    url: str,
    direction: str,
    timeout: float,
    socket_path: str = "",
    source: str = "wifi",
) -> dict:
    local_payload = {
        "source": source,
        "command": direction,
        "action": "stop",
        "speed": 1,
        "backend": "onvif",
    }
    if str(socket_path or "").strip():
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(timeout)
            client.connect(str(socket_path))
            client.sendall(json.dumps(local_payload, separators=(",", ":")).encode("utf-8"))
            raw = client.recv(16384).decode("utf-8", "replace")
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError):
            raw = ""
        finally:
            client.close()
        if raw:
            result = json.loads(raw)
            if isinstance(result, dict) and result.get("status") == "error":
                raise RuntimeError(str(result.get("error") or "PTZ stop failed"))
            return result if isinstance(result, dict) else {}
    request = Request(
        url,
        data=json.dumps(local_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Camera-Control-Owner": "focus-object"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(8192).decode("utf-8", "replace")
        result = json.loads(raw) if raw.strip() else {}
        if isinstance(result, dict) and result.get("status") == "error":
            raise RuntimeError(str(result.get("error") or "PTZ stop failed"))
        return result if isinstance(result, dict) else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"PTZ stop HTTP {exc.code}: {body}") from exc


def clear_ptz_motion(
    url: str,
    timeout: float,
    socket_path: str = "",
    source: str = "wifi",
) -> list[dict]:
    # ONVIF Stop sets PanTilt=true, so one request clears both motion axes.
    # Sending four directional variants only repeats the same SOAP operation.
    return [post_stop(url, "up", timeout, socket_path, source)]


def query_ptz_idle(socket_path: str, source: str = "wifi", timeout: float = 1.0) -> dict:
    if not str(socket_path or "").strip():
        return {}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(json.dumps({"source": source, "action": "status"}, separators=(",", ":")).encode("utf-8"))
        result = json.loads(client.recv(16384).decode("utf-8", "replace"))
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}
    finally:
        client.close()


def prewarm_motion_backend() -> None:
    import cv2
    import numpy  # noqa: F401

    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)


def snapshot_url(template: str, source: str) -> str:
    return template.format(source=source) if "{source}" in template else template


def capture_verification_snapshot(url: str, output_path: str | Path, timeout: float) -> dict:
    with urlopen(Request(url, headers={"Cache-Control": "no-cache"}), timeout=timeout) as response:
        image = response.read(8 * 1024 * 1024)
    if not image:
        raise RuntimeError("camera verification snapshot was empty")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_bytes(image)
    temporary.replace(output)
    return {
        "captured_at": time.time(),
        "bytes": len(image),
        "sha256": hashlib.sha256(image).hexdigest(),
        "path": str(output),
    }


def load_motion_preview(path: str | Path, width: int = 160, height: int = 120):
    """Load the atomic DeepStream preview as a compact grayscale motion frame."""
    if not str(path or "").strip():
        return None
    import cv2

    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None or image.size == 0:
        return None
    return cv2.resize(image, (max(32, width), max(18, height)), interpolation=cv2.INTER_AREA)


def estimate_global_motion(before, after) -> dict:
    """Estimate whole-frame translation; detector-box jitter cannot create this signal."""
    if before is None or after is None or getattr(before, "shape", None) != getattr(after, "shape", None):
        return {"available": False, "x": 0.0, "y": 0.0, "magnitude": 0.0, "response": 0.0}
    import cv2
    import numpy as np

    height, width = before.shape[:2]
    shift, response = cv2.phaseCorrelate(np.float32(before), np.float32(after))
    dx = float(shift[0]) / max(1, width)
    dy = float(shift[1]) / max(1, height)
    return {
        "available": True,
        "x": dx,
        "y": dy,
        "magnitude": math.hypot(dx, dy),
        "response": float(response),
    }


class FocusController:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.stop_event = threading.Event()
        self.command_signature = ""
        self.detection_marker: tuple[str, int | float] | None = None
        self.previous_center: tuple[float, float] | None = None
        self.stable_frames = 0
        self.pulse_count = 0
        self.cooldown_until = 0.0
        self.pending_pulse_distance: float | None = None
        self.no_progress_pulses = 0
        self.pending_pulse_direction = ""
        self.pending_pulse_action_direction = ""
        self.pending_pulse_duration_ms = 0
        self.pending_pulse_backend = ""
        self.pending_pulse_completed_monotonic_ns = 0
        self.pending_pulse_axis = ""
        self.pending_pulse_offsets: dict[str, float] = {}
        self.pending_pulses: list[dict] = []
        self.pending_prediction: dict = {}
        self.last_prediction_result: dict = {}
        self.axis_no_progress = {"x": 0, "y": 0}
        self.axis_recovery_stage = {"x": 0, "y": 0}
        self.blocked_axes: set[str] = set()
        self.last_pulse_axis = ""
        self.same_axis_pulses = 0
        self.active_axis = ""
        self.vertical_direction_inverted = False
        self.vertical_direction_probe_count = 0
        self.center_samples: deque[tuple[float, float]] = deque(maxlen=max(1, int(getattr(args, "center_median_frames", 3))))
        self.pending_motion_preview = None
        self.pending_motion_preview_marker: tuple[int, int] | None = None
        self.last_verification_preview_marker: tuple[int, int] | None = None
        self.pending_verification_started_at = 0.0
        self.verification_frames_seen = 0
        self.verification_best_motion: dict = {}
        self.last_global_motion: dict = {}
        self.verified_motion_count = 0
        self.axis_minimum_step = {
            "x": float(getattr(args, "minimum_controllable_x", 0.16)),
            "y": float(getattr(args, "minimum_controllable_y", 0.20)),
        }
        self.direction_gain_path = str(getattr(args, "direction_gain_json", "") or "").strip()
        self.direction_gains, self.direction_gain_sample_counts = load_direction_gain_state(self.direction_gain_path)
        self.gru_checkpoint_path = str(getattr(args, "gru_checkpoint", "") or "").strip()
        self.gru_checkpoint_stamp: tuple[int, int] | None = None
        self.gru_runtime: tuple | None = None
        self.gru_event_stamp: tuple[int, int] | None = None
        self.gru_event_records: list[dict] = []
        self.gru_last_error = ""
        self.current_gru_planning: dict = {}
        self.response_latency_history: dict[str, deque[float]] = {
            direction: deque(maxlen=50) for direction in DEFAULT_DIRECTION_GAINS
        }
        self.movement_step_history: list[list[dict]] = []
        self.target_recovery_mode = ""
        self.target_recovery_steps = 0
        self.target_recovery_backtrack_steps = 0
        self.target_random_search_steps = 0
        self.recovery_rng = random.Random()
        self.missing_target_frames = 0
        self.missing_target_since_monotonic = 0.0
        self.reacquisition_confirmation_frames = 0
        self.verification_snapshot: dict = {}
        self.last_publish = 0.0
        self.last_state: dict = {}
        self.prepared_request_id = ""
        self.ptz_idle_verified = False
        self.ptz_idle_verified_at = 0.0
        self.request_max_steps = int(getattr(args, "max_pulses", 100))
        self.wake_socket: socket.socket | None = None
        self.wake_socket_path: Path | None = None
        self.latest_detection_payload: dict = {}
        self.latest_detection_received_monotonic = 0.0
        self.pending_command_payload: dict = {}
        self.request_telemetry: dict = {}
        self.current_target_context: dict = {}
        self.request_performance: dict = {
            axis: {
                "steps": 0, "batches": 0, "observed_batches": 0,
                "overshoot_count": 0, "overshoot_amount": 0.0,
                "models_used": set(), "gru_fallback_steps": 0,
            }
            for axis in ("x", "y")
        }
        self.settings_cache: dict = {}
        self.settings_cache_stamp: tuple[int, int] | None = None
        self.settings_cache_checked_monotonic = 0.0
        previous_state = read_json(getattr(self.args, "state_json", ""))
        previous_history = previous_state.get("history") if isinstance(previous_state.get("history"), list) else []
        self.run_history = [item for item in previous_history[-20:] if isinstance(item, dict)]
        previous_performance = previous_state.get("performance_history") if isinstance(previous_state.get("performance_history"), list) else []
        self.performance_history = [item for item in previous_performance[-100:] if isinstance(item, dict)]
        self.logged_request_id = ""

    def open_wake_socket(self) -> None:
        path_value = str(getattr(self.args, "wake_socket", "") or "").strip()
        if not path_value:
            return
        path = Path(path_value)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        wake_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        wake_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        wake_socket.setblocking(False)
        wake_socket.bind(str(path))
        self.wake_socket = wake_socket
        self.wake_socket_path = path

    def close_wake_socket(self) -> None:
        if self.wake_socket is not None:
            try:
                self.wake_socket.close()
            finally:
                self.wake_socket = None
        if self.wake_socket_path is not None:
            try:
                self.wake_socket_path.unlink()
            except FileNotFoundError:
                pass
            self.wake_socket_path = None

    def wait_for_wake(self, timeout: float) -> None:
        wait_seconds = max(0.01, float(timeout))
        if self.wake_socket is None:
            self.stop_event.wait(wait_seconds)
            return
        readable, _, _ = select.select([self.wake_socket], [], [], wait_seconds)
        if not readable:
            return
        try:
            while True:
                raw = self.wake_socket.recv(65536)
                if not raw or len(raw) > 65535:
                    continue
                try:
                    envelope = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(envelope, dict) or envelope.get("v") != 1 or envelope.get("type") != "focus_frame":
                    continue
                detections = envelope.get("detections")
                if isinstance(detections, dict) and isinstance(detections.get("objects"), list):
                    self.latest_detection_payload = detections
                    self.latest_detection_received_monotonic = time.monotonic()
                command = envelope.get("command")
                if isinstance(command, dict) and command.get("enabled") and command.get("request_id"):
                    self.pending_command_payload = command
        except BlockingIOError:
            pass

    def refresh_settings(self) -> dict:
        settings_path = Path(str(getattr(self.args, "settings_json", "") or ""))
        now = time.monotonic()
        if now - self.settings_cache_checked_monotonic >= 0.5:
            self.settings_cache_checked_monotonic = now
            try:
                stat = settings_path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamp = None
            if stamp != self.settings_cache_stamp:
                self.settings_cache = read_json(settings_path)
                self.settings_cache_stamp = stamp
        return self.settings_cache

    def configured_max_steps(self, source: str) -> int:
        settings = self.refresh_settings()
        source_settings = settings.get("focus_max_steps_by_source")
        source_settings = source_settings if isinstance(source_settings, dict) else {}
        try:
            value = int(source_settings.get(source, settings.get("focus_max_steps", self.args.max_pulses)))
        except (TypeError, ValueError):
            value = int(self.args.max_pulses)
        return max(1, min(100, value))

    def configured_response_models(self) -> dict[str, str]:
        settings = self.refresh_settings()
        return {
            "x": "gru" if str(settings.get("focus_horizontal_model") or "linear").lower() == "gru" else "linear",
            "y": "gru" if str(settings.get("focus_vertical_model") or "linear").lower() == "gru" else "linear",
        }

    def load_gru_runtime(self) -> tuple | None:
        if not self.gru_checkpoint_path:
            self.gru_last_error = "GRU checkpoint path is not configured"
            return None
        checkpoint = Path(self.gru_checkpoint_path)
        try:
            stat = checkpoint.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError as exc:
            self.gru_last_error = f"GRU checkpoint unavailable: {exc}"
            return None
        if self.gru_runtime is not None and stamp == self.gru_checkpoint_stamp:
            return self.gru_runtime
        try:
            from focus_gru_shadow_trainer import load_checkpoint

            loaded = load_checkpoint(checkpoint, "cpu")
            if loaded is None:
                raise RuntimeError("checkpoint could not be loaded")
            self.gru_runtime = loaded
            self.gru_checkpoint_stamp = stamp
            self.gru_last_error = ""
            return loaded
        except Exception as exc:
            self.gru_runtime = None
            self.gru_last_error = f"GRU load failed: {str(exc)[:240]}"
            return None

    def load_gru_event_records(self) -> list[dict]:
        event_path = Path(str(getattr(self.args, "prediction_error_jsonl", "") or ""))
        try:
            stat = event_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return self.gru_event_records
        if stamp == self.gru_event_stamp:
            return self.gru_event_records
        try:
            from focus_gru_shadow_trainer import read_observed_records

            self.gru_event_records = read_observed_records(event_path)
            self.gru_event_stamp = stamp
        except Exception as exc:
            self.gru_last_error = f"GRU history load failed: {str(exc)[:240]}"
        return self.gru_event_records

    def gru_pulse_prediction(
        self,
        command: dict,
        center: tuple[float, float],
        pulse_specs: list[dict],
        pulse_count_before: int,
    ) -> dict | None:
        runtime = self.load_gru_runtime()
        if runtime is None:
            return None
        try:
            from focus_gru_shadow_trainer import predict, query_sequence

            model, mean, std, metadata = runtime
            compact_pulses = [
                {key: spec.get(key) for key in (
                    "axis", "direction", "hardware_direction", "duration_ms", "speed", "backend", "recovery_stage"
                )}
                for spec in pulse_specs
            ]
            pending = {
                "event": "pulse_dispatched",
                "step_id": f"planning:{command.get('request_id') or 'focus'}:{pulse_count_before}",
                "request_id": str(command.get("request_id") or ""),
                "source": str(command.get("source") or "wifi"),
                "target_label": str(command.get("target_label") or ""),
                "control_mode": "coco_three_pulse_batch" if bool(getattr(self.args, "simple_closed_loop", False)) else "verified_adaptive",
                "pulse_count_before": int(pulse_count_before),
                "pre_center": {"x": float(center[0]), "y": float(center[1])},
                "pulses": compact_pulses,
                "prediction_model": {"direction_gains": dict(self.direction_gains)},
                "context": self.dispatch_context(),
                "dispatched_monotonic_ns": time.monotonic_ns(),
            }
            sequence_length = max(1, int(metadata.get("sequence_length", 12)))
            features, mask = query_sequence(self.load_gru_event_records(), pending, sequence_length)
            result = predict(model, mean, std, features, mask, "cpu")
            self.gru_last_error = ""
            return result
        except Exception as exc:
            self.gru_last_error = f"GRU inference failed: {str(exc)[:240]}"
            return None

    def dispatch_context(self) -> dict:
        target = self.current_target_context
        started_ns = int(self.request_telemetry.get("controller_received_monotonic_ns") or 0)
        session_age = max(0.0, (time.monotonic_ns() - started_ns) / 1e9) if started_ns else 0.0
        return {
            "target_bbox": dict(target.get("target_bbox") or {}),
            "target_confidence": finite_float(target.get("target_confidence"), 0.0),
            "previous_global_motion": dict(self.last_global_motion),
            "ptz_session_age_seconds": session_age,
        }

    def record_dispatched_performance(self, pulse_specs: list[dict]) -> None:
        for axis in ("x", "y"):
            axis_specs = [spec for spec in pulse_specs if str(spec.get("axis") or "") == axis]
            if not axis_specs:
                continue
            metric = self.request_performance[axis]
            metric["steps"] += len(axis_specs)
            metric["batches"] += 1
            models = {
                "linear" if str(spec.get("planning_model") or "linear") in {"linear", "linear_fallback"}
                else "gru"
                for spec in axis_specs
            }
            for model in models:
                metric["models_used"].add(model)
            metric["gru_fallback_steps"] += sum(
                str(spec.get("planning_model") or "") == "linear_fallback" for spec in axis_specs
            )

    def record_observed_performance(self, pending: dict, actual_center: tuple[float, float]) -> list[dict]:
        overshoots = []
        axes = {str(pulse.get("axis") or "") for pulse in pending.get("pulses", [])}
        pre = pending.get("pre_center") if isinstance(pending.get("pre_center"), dict) else {}
        for axis in axes & {"x", "y"}:
            metric = self.request_performance[axis]
            metric["observed_batches"] += 1
            pre_offset = finite_float(pre.get(axis), 0.5) - 0.5
            post_offset = float(actual_center[0 if axis == "x" else 1]) - 0.5
            deadzone = float(
                getattr(self.args, "deadzone_x", 0.10)
                if axis == "x" else getattr(self.args, "deadzone_y", 0.12)
            )
            if pre_offset * post_offset < 0 and abs(post_offset) > deadzone:
                amount = max(0.0, abs(post_offset) - deadzone)
                metric["overshoot_count"] += 1
                metric["overshoot_amount"] += amount
                overshoots.append({"axis": axis, "amount": round(amount, 6)})
        return overshoots

    def pending_response_timeout(self) -> float:
        pending = self.pending_prediction
        prediction_model = pending.get("prediction_model") if isinstance(pending.get("prediction_model"), dict) else {}
        directions = {
            str(pulse.get("direction") or "") for pulse in pending.get("pulses", []) if isinstance(pulse, dict)
        }
        recent = [value for direction in directions for value in self.response_latency_history.get(direction, [])]
        return adaptive_response_timeout(
            float(getattr(self.args, "simple_response_timeout", 1.0)),
            finite_float(prediction_model.get("expected_response_seconds"), float("nan")),
            finite_float(prediction_model.get("no_response_probability"), float("nan")),
            recent,
        )

    def apply_selected_response_models(
        self,
        command: dict,
        center: tuple[float, float],
        pulse_specs: list[dict],
        deadzone_x: float,
        deadzone_y: float,
    ) -> tuple[list[dict], dict]:
        models = self.configured_response_models()
        selected = [{**spec, "planning_model": models.get(str(spec.get("axis") or ""), "linear")} for spec in pulse_specs]
        diagnostics: dict = {"axis_models": dict(models), "axes": {}}
        for axis in ("x", "y"):
            indices = [index for index, spec in enumerate(selected) if spec.get("axis") == axis]
            if not indices or models[axis] != "gru":
                continue
            first = selected[indices[0]]
            offset = float(first.get("movement", {}).get("offset", {}).get(axis, 0.0))
            deadzone = deadzone_x if axis == "x" else deadzone_y
            desired = -math.copysign(max(0.0, abs(offset) - deadzone), offset) if offset else 0.0
            minimum = max(30, int(getattr(self.args, "min_pulse_ms", 50)))
            if axis == "y" and int(getattr(self.args, "min_vertical_pulse_ms", 0) or 0) > 0:
                minimum = max(30, min(minimum, int(self.args.min_vertical_pulse_ms)))
            maximum = max(minimum, int(getattr(self.args, "max_pulse_ms", 220)))
            if axis == "y" and int(getattr(self.args, "max_vertical_pulse_ms", 0) or 0) > 0:
                maximum = max(minimum, min(maximum, int(self.args.max_vertical_pulse_ms)))
            candidates = sorted(set(range(minimum, maximum + 1, 10)) | {maximum, int(first.get("duration_ms") or minimum)})
            best: tuple[float, int, dict, list[dict]] | None = None
            safe_best: tuple[float, int, dict, list[dict]] | None = None
            for duration in candidates:
                trial = [{**spec} for spec in selected]
                for index in indices:
                    trial[index]["duration_ms"] = duration
                prediction = self.gru_pulse_prediction(command, center, trial, self.pulse_count)
                if prediction is None:
                    break
                predicted_delta = float(prediction.get("predicted_delta", {}).get(axis, 0.0))
                predicted_std = max(0.0, float(prediction.get("predicted_std", {}).get(axis, 0.0)))
                sign_penalty = 1.0 if desired and predicted_delta * desired <= 0 else 0.0
                overshoot = max(0.0, abs(predicted_delta) - abs(desired))
                visibility = max(0.0, min(1.0, finite_float(prediction.get("visibility_probability"), 1.0)))
                no_response = max(0.0, min(1.0, finite_float(prediction.get("no_response_probability"), 0.0)))
                response_seconds = max(0.0, finite_float(prediction.get("response_seconds"), 0.0))
                bbox = getattr(self, "current_target_context", {}).get("target_bbox") or {}
                extent = finite_float(bbox.get("width" if axis == "x" else "height"), 0.0) / 2.0
                predicted_center = float(center[0 if axis == "x" else 1]) + predicted_delta
                safe_low, safe_high = extent + 0.03, 1.0 - extent - 0.03
                safety_penalty = 2.0 if predicted_center - 2.0 * predicted_std < safe_low or predicted_center + 2.0 * predicted_std > safe_high else 0.0
                score = (
                    abs(predicted_delta - desired) + 0.15 * predicted_std + 0.5 * overshoot + sign_penalty
                    + 0.02 * (duration / 1000.0) + 0.03 * response_seconds
                    + 0.08 * no_response + 0.20 * (1.0 - visibility) + safety_penalty
                )
                if best is None or score < best[0]:
                    best = (score, duration, prediction, trial)
                if safety_penalty == 0.0 and (safe_best is None or score < safe_best[0]):
                    safe_best = (score, duration, prediction, trial)
            if safe_best is not None:
                best = safe_best
            if best is not None:
                selected = best[3]
                diagnostics["axes"][axis] = {
                    "model": "gru", "duration_ms": best[1], "desired_delta": round(desired, 6),
                    "predicted_delta": round(float(best[2]["predicted_delta"][axis]), 6),
                    "predicted_std": round(float(best[2]["predicted_std"][axis]), 6),
                    "visibility_probability": round(float(best[2].get("visibility_probability", 0.0)), 4),
                    "no_response_probability": round(float(best[2].get("no_response_probability", 0.0)), 4),
                    "expected_response_seconds": round(float(best[2].get("response_seconds", 0.0)), 4),
                    "candidate_count": len(candidates),
                }
            else:
                for index in indices:
                    selected[index]["planning_model"] = "linear_fallback"
                diagnostics["axes"][axis] = {"model": "linear_fallback", "error": self.gru_last_error}
        return selected, diagnostics

    def confidence_aware_batch(self, pulse_specs: list[dict], diagnostics: dict) -> tuple[list[dict], dict]:
        """Reduce risky batches while retaining the existing fast three-pulse path when confident."""
        if len(pulse_specs) <= 1:
            return pulse_specs, {"policy": "single", "original_count": len(pulse_specs), "final_count": len(pulse_specs)}
        axes = diagnostics.get("axes") if isinstance(diagnostics.get("axes"), dict) else {}
        metrics = [item for item in axes.values() if isinstance(item, dict) and item.get("model") == "gru"]
        if not metrics:
            return pulse_specs, {"policy": "legacy", "original_count": len(pulse_specs), "final_count": len(pulse_specs)}
        confidence = finite_float(getattr(self, "current_target_context", {}).get("target_confidence"), 1.0)
        high_risk = confidence < float(getattr(self.args, "minimum_confidence", 0.25)) + 0.10 or any(
            finite_float(item.get("visibility_probability"), 1.0) < 0.65
            or finite_float(item.get("no_response_probability"), 0.0) > 0.50
            for item in metrics
        )
        uncertainty_threshold = max(
            0.12,
            4.0 * max(float(getattr(self.args, "deadzone_x", 0.02)), float(getattr(self.args, "deadzone_y", 0.02))),
        )
        uncertain = any(
            finite_float(item.get("predicted_std"), 0.0)
            > uncertainty_threshold
            for axis, item in axes.items() if isinstance(item, dict) and item.get("model") == "gru"
        )
        if high_risk:
            chosen = max(
                pulse_specs,
                key=lambda spec: abs(finite_float(spec.get("movement", {}).get("offset", {}).get(str(spec.get("axis") or "")))),
            )
            trimmed, policy = [chosen], "high-risk-probe"
        elif uncertain:
            trimmed = []
            seen_axes = set()
            for spec in pulse_specs:
                axis = str(spec.get("axis") or "")
                if axis in seen_axes:
                    continue
                trimmed.append(spec)
                seen_axes.add(axis)
                if len(trimmed) >= 2:
                    break
            policy = "uncertainty-capped"
        else:
            trimmed, policy = pulse_specs, "confident-batch"
        return trimmed, {
            "policy": policy,
            "original_count": len(pulse_specs),
            "final_count": len(trimmed),
            "uncertainty_threshold": uncertainty_threshold,
        }

    def fairness_axis_preference(self, target: dict, deadzone_x: float, deadzone_y: float) -> str:
        if self.same_axis_pulses < max(1, int(getattr(self.args, "axis_fairness_pulses", 2))):
            return ""
        if self.last_pulse_axis not in {"x", "y"}:
            return ""
        alternate = "y" if self.last_pulse_axis == "x" else "x"
        if alternate in self.blocked_axes:
            return ""
        try:
            dx = float(target["center_x"]) - 0.5
            dy = float(target["center_y"]) - 0.5
        except (KeyError, TypeError, ValueError):
            return ""
        if alternate == "x" and abs(dx) > deadzone_x:
            return alternate
        if alternate == "y" and abs(dy) > deadzone_y:
            return alternate
        return ""

    def remember_pulse_axis(self, axis: str) -> None:
        if axis not in {"x", "y"}:
            return
        if axis == self.last_pulse_axis:
            self.same_axis_pulses += 1
        else:
            self.last_pulse_axis = axis
            self.same_axis_pulses = 1

    def remember_movement_step(self, specs: list[dict]) -> None:
        compact = [
            {key: spec.get(key) for key in (
                "axis", "direction", "hardware_direction", "duration_ms", "speed", "backend", "recovery_stage"
            )}
            for spec in specs
            if isinstance(spec, dict) and spec.get("direction")
        ]
        if compact:
            self.movement_step_history.append(compact)

    def clear_pending_pulse_state(self) -> None:
        self.pending_pulse_distance = None
        self.pending_pulse_axis = ""
        self.pending_pulse_offsets = {}
        self.pending_pulses = []
        self.pending_pulse_direction = ""
        self.pending_pulse_action_direction = ""
        self.pending_pulse_duration_ms = 0
        self.pending_pulse_backend = ""
        self.pending_pulse_completed_monotonic_ns = 0
        self.pending_motion_preview = None
        self.pending_motion_preview_marker = None
        self.last_verification_preview_marker = None
        self.pending_verification_started_at = 0.0
        self.verification_frames_seen = 0
        self.verification_best_motion = {}
        self.center_samples.clear()

    def random_target_search_specs(self) -> list[dict]:
        directions = self.recovery_rng.sample(
            ("left", "right", "up", "down"),
            TARGET_RANDOM_SEARCH_DIRECTIONS_PER_MOVE,
        )
        specs = []
        for direction in directions:
            axis = "x" if direction in {"left", "right"} else "y"
            backend = "native" if axis == "x" else "auto"
            invert_tilt = bool(getattr(self.args, "invert_tilt", False))
            if axis == "y" and self.vertical_direction_inverted:
                invert_tilt = not invert_tilt
            duration_ms = max(30, int(getattr(self.args, "min_pulse_ms", 50)))
            if axis == "y":
                vertical_cap = int(getattr(self.args, "max_vertical_pulse_ms", 0) or 0)
                if vertical_cap > 0:
                    duration_ms = min(duration_ms, vertical_cap)
            specs.append({
                "axis": axis,
                "direction": direction,
                "hardware_direction": backend_direction(direction, backend, invert_tilt),
                "duration_ms": duration_ms,
                "speed": (
                    int(getattr(self.args, "vertical_speed", 0) or self.args.speed)
                    if axis == "y" else int(self.args.speed)
                ),
                "backend": backend,
                "recovery_stage": 0,
                "reset_native_session": False,
            })
        return specs

    def recover_lost_target(self, command: dict, source: str, label: str) -> None:
        remaining_budget = self.request_max_steps - self.pulse_count
        max_random_search_moves = max(0, int(getattr(
            self.args, "max_target_random_search_moves", MAX_TARGET_RANDOM_SEARCH_MOVES
        )))
        can_backtrack = (
            self.target_recovery_backtrack_steps < MAX_TARGET_REACQUISITION_BACKTRACK_STEPS
            and bool(self.movement_step_history)
        )
        can_random_search = (
            self.target_random_search_steps < max_random_search_moves
            and remaining_budget >= TARGET_RANDOM_SEARCH_DIRECTIONS_PER_MOVE
        )
        if remaining_budget <= 0 or (not can_backtrack and not can_random_search):
            steps = self.target_recovery_backtrack_steps
            step_word = "step" if steps == 1 else "steps"
            self.finish_request(
                command,
                "failed",
                f"Could not reacquire {label} after backtracking {steps} autofocus {step_word} "
                f"and trying {self.target_random_search_steps} random search moves.",
                lost_target=True,
                target_recovery_mode=self.target_recovery_mode or "exhausted",
                target_recovery_steps=self.target_recovery_steps,
                target_recovery_backtrack_steps=steps,
                target_random_search_steps=self.target_random_search_steps,
            )
            return

        if can_backtrack:
            specs = reverse_pulse_step(self.movement_step_history.pop())[:remaining_budget]
            mode = "backtracking"
            self.target_recovery_backtrack_steps += 1
        else:
            specs = self.random_target_search_specs()
            mode = "random_search"
            self.target_random_search_steps += 1
        if not specs:
            self.recover_lost_target(command, source, label)
            return

        attempted = []
        error = ""
        for spec in specs:
            attempted.append(spec)
            try:
                post_pulse(
                    ptz_url(self.args.ptz_url, source),
                    spec["hardware_direction"],
                    spec["speed"],
                    spec["duration_ms"],
                    self.args.ptz_timeout,
                    spec["backend"],
                    False,
                    getattr(self.args, "ptz_socket", ""),
                    source,
                )
            except Exception as exc:
                error = str(exc)[:400]
                break
        self.pulse_count += len(attempted)
        self.record_dispatched_performance(attempted)
        self.target_recovery_steps += len(attempted)
        self.target_recovery_mode = mode
        self.cooldown_until = time.monotonic() + max(
            float(getattr(self.args, "settle_seconds", 0.05)),
            float(getattr(self.args, "post_pulse_view_settle_seconds", 0.75)),
        )
        self.clear_pending_pulse_state()
        self.ptz_idle_verified = False
        self.publish(
            "recovering_target",
            command,
            message=(
                f"Reversing autofocus step {self.target_recovery_backtrack_steps} of "
                f"{MAX_TARGET_REACQUISITION_BACKTRACK_STEPS} to reacquire {label}."
                if mode == "backtracking"
                else (
                    f"Trying random search move {self.target_random_search_steps} of "
                    f"{max_random_search_moves} for {label}, using three unique directions."
                )
            ),
            warning=f"Recovery PTZ response failed: {error}" if error else "",
            pulse_count=self.pulse_count,
            target_recovery_mode=mode,
            target_recovery_steps=self.target_recovery_steps,
            target_recovery_backtrack_steps=self.target_recovery_backtrack_steps,
            target_random_search_steps=self.target_random_search_steps,
            movement_history_depth=len(self.movement_step_history),
            recovery_pulses=attempted,
            direction="+".join(str(item.get("direction") or "") for item in attempted),
            hardware_direction="+".join(str(item.get("hardware_direction") or "") for item in attempted),
            duration_ms=sum(int(item.get("duration_ms") or 0) for item in attempted),
            backend="+".join(str(item.get("backend") or "") for item in attempted),
        )

    def begin_pulse_prediction(
        self,
        command: dict,
        center: tuple[float, float],
        pulse_specs: list[dict],
        source_frame_id: int | float | None,
        pulse_count_before: int,
        pulse_count_after: int,
        dispatched_monotonic_ns: int,
        submitted_monotonic_ns: int = 0,
    ) -> None:
        compact_pulses = [
            {key: spec.get(key) for key in (
                "axis", "direction", "hardware_direction", "duration_ms", "speed", "backend", "recovery_stage", "planning_model"
            )}
            for spec in pulse_specs
        ]
        linear_prediction = predicted_center_for_pulses(
            center,
            compact_pulses,
            self.direction_gains,
            int(getattr(self.args, "min_pulse_ms", 50)),
            float(getattr(self.args, "prediction_max_aggregate_delta", 0.35)),
        )
        axis_models = self.configured_response_models()
        gru_axes = {
            str(spec.get("axis") or "")
            for spec in compact_pulses
            if str(spec.get("planning_model") or "") == "gru"
        }
        # The auxiliary GRU timing/visibility heads are useful even when the
        # selected displacement controller remains linear.
        gru_prediction = self.gru_pulse_prediction(command, center, pulse_specs, pulse_count_before)
        combined_delta = dict(linear_prediction["delta"])
        if gru_prediction is not None:
            for axis in gru_axes:
                if axis in {"x", "y"}:
                    combined_delta[axis] = float(gru_prediction.get("predicted_delta", {}).get(axis, combined_delta[axis]))
        prediction = {
            **linear_prediction,
            "delta": combined_delta,
            "center": {"x": float(center[0]) + combined_delta["x"], "y": float(center[1]) + combined_delta["y"]},
        }
        step_id = f"{command.get('request_id') or 'focus'}:{pulse_count_before}-{pulse_count_after}:{time.time_ns()}"
        pending = {
            "schema_version": 2,
            "step_id": step_id,
            "request_id": str(command.get("request_id") or ""),
            "source": str(command.get("source") or "wifi"),
            "target_label": str(command.get("target_label") or ""),
            "control_mode": "coco_three_pulse_batch" if bool(getattr(self.args, "simple_closed_loop", False)) else "verified_adaptive",
            "pulse_count_before": int(pulse_count_before),
            "pulse_count_after": int(pulse_count_after),
            "pre_source_frame_id": source_frame_id,
            "pre_center": {"x": float(center[0]), "y": float(center[1])},
            "predicted_center": prediction["center"],
            "predicted_delta": prediction["delta"],
            "linear_predicted_delta": linear_prediction["delta"],
            "context": self.dispatch_context(),
            "pulses": prediction["pulses"],
            "prediction_model": {
                "name": "axis_selected_linear_gru_v1" if gru_axes else "learned_per_direction_gain_v2",
                "axis_models": axis_models,
                "gru_axes": sorted(gru_axes),
                "gru_checkpoint": self.gru_checkpoint_path if gru_axes else "",
                "gru_available": bool(gru_prediction is not None) if gru_axes else None,
                "gru_error": self.gru_last_error if gru_axes and gru_prediction is None else "",
                "expected_response_seconds": gru_prediction.get("response_seconds") if gru_prediction else None,
                "no_response_probability": gru_prediction.get("no_response_probability") if gru_prediction else None,
                "visibility_probability": gru_prediction.get("visibility_probability") if gru_prediction else None,
                "minimum_pulse_ms": int(getattr(self.args, "min_pulse_ms", 50)),
                "direction_gains": dict(self.direction_gains),
                "sample_counts": dict(self.direction_gain_sample_counts),
                "maximum_aggregate_delta": float(getattr(self.args, "prediction_max_aggregate_delta", 0.35)),
            },
            "dispatched_monotonic_ns": int(dispatched_monotonic_ns),
            "latency_breakdown_ms": {
                "ptz_round_trip": round(max(0.0, (int(dispatched_monotonic_ns) - int(submitted_monotonic_ns)) / 1_000_000), 3)
                if submitted_monotonic_ns else None,
            },
        }
        self.pending_prediction = pending
        event = {**pending, "event": "pulse_dispatched", "recorded_at": time.time(), "outcome": "pending"}
        prediction_path = str(getattr(self.args, "prediction_error_jsonl", "") or "").strip()
        try:
            if prediction_path:
                append_prediction_event(prediction_path, event)
        except OSError as exc:
            print(json.dumps({"event": "pulse_prediction_persist_error", "error": str(exc)[:300]}), flush=True)

    def finish_pulse_prediction(
        self,
        outcome: str,
        actual_center: tuple[float, float] | None = None,
        post_source_frame_id: int | float | None = None,
        **extra: object,
    ) -> dict:
        if not self.pending_prediction:
            return {}
        pending = self.pending_prediction
        actual = None if actual_center is None else {"x": float(actual_center[0]), "y": float(actual_center[1])}
        pre = pending["pre_center"]
        actual_delta = None if actual is None else {
            "x": actual["x"] - float(pre["x"]),
            "y": actual["y"] - float(pre["y"]),
        }
        actual_overshoots = self.record_observed_performance(pending, actual_center) if actual_center is not None else []
        gain_updates = {}
        if str(outcome) == "observed" and actual_delta is not None:
            gain_updates = learned_gain_updates(
                pending,
                actual_delta,
                self.direction_gains,
                float(getattr(self.args, "direction_gain_learning_rate", 0.15)),
            )
            for direction, (new_gain, _raw_sample) in gain_updates.items():
                self.direction_gains[direction] = round(float(new_gain), 8)
                self.direction_gain_sample_counts[direction] = self.direction_gain_sample_counts.get(direction, 0) + 1
            if gain_updates and self.direction_gain_path:
                try:
                    write_json(self.direction_gain_path, {
                        "schema_version": 1,
                        "gains": dict(self.direction_gains),
                        "sample_counts": dict(self.direction_gain_sample_counts),
                        "updated_at": time.time(),
                    })
                except OSError as exc:
                    print(json.dumps({"event": "direction_gain_persist_error", "error": str(exc)[:300]}), flush=True)
        error = pulse_prediction_error(pending["predicted_center"], actual_center)
        event = {
            **pending,
            "event": "pulse_observed",
            "recorded_at": time.time(),
            "outcome": str(outcome),
            "post_source_frame_id": post_source_frame_id,
            "actual_center": actual,
            "actual_delta": actual_delta,
            "prediction_error": error,
            "direction_gain_updates": {
                direction: {"gain": round(new_gain, 8), "raw_sample": round(raw_sample, 8)}
                for direction, (new_gain, raw_sample) in gain_updates.items()
            },
            "direction_gains_after": dict(self.direction_gains),
            "actual_overshoots": actual_overshoots,
            **extra,
        }
        response_elapsed = finite_float(extra.get("response_elapsed_seconds"), float("nan"))
        if str(outcome) == "observed" and math.isfinite(response_elapsed):
            for pulse in pending.get("pulses", []):
                direction = str(pulse.get("direction") or "") if isinstance(pulse, dict) else ""
                if direction in self.response_latency_history:
                    self.response_latency_history[direction].append(response_elapsed)
            samples = int(self.request_telemetry.get("response_sample_count") or 0) + 1
            total = float(self.request_telemetry.get("response_elapsed_total_seconds") or 0.0) + response_elapsed
            self.request_telemetry.update({
                "response_sample_count": samples,
                "response_elapsed_total_seconds": round(total, 4),
                "response_elapsed_average_seconds": round(total / samples, 4),
                "response_elapsed_last_seconds": round(response_elapsed, 4),
                "response_elapsed_max_seconds": round(max(response_elapsed, float(self.request_telemetry.get("response_elapsed_max_seconds") or 0.0)), 4),
            })
        prediction_path = str(getattr(self.args, "prediction_error_jsonl", "") or "").strip()
        try:
            if prediction_path:
                append_prediction_event(prediction_path, event)
        except OSError as exc:
            print(json.dumps({"event": "pulse_prediction_persist_error", "error": str(exc)[:300]}), flush=True)
        self.last_prediction_result = {
            key: event.get(key) for key in (
                "step_id", "outcome", "pre_center", "predicted_center", "actual_center", "prediction_error"
            )
        }
        self.pending_prediction = {}
        return event

    def stop(self, _signum: int | None = None, _frame: object | None = None) -> None:
        self.stop_event.set()

    def publish(self, status: str, command: dict, **extra: object) -> None:
        now = time.time()
        elapsed = auto_centering_runtime_seconds(command, now=now)
        paused = manual_ptz_paused_seconds(command, now=now)
        state = {
            "status": status,
            "enabled": bool(command.get("enabled")),
            "source": command.get("source", "wifi"),
            "target_label": command.get("target_label", ""),
            "request_id": command.get("request_id", ""),
            "controller_pid": os.getpid(),
            "max_steps": self.request_max_steps,
            "pulse_count": self.pulse_count,
            "stable_frames": self.stable_frames,
            "auto_centering_elapsed_seconds": elapsed,
            "auto_centering_paused_seconds": paused,
            "history": list(self.run_history),
            "performance_history": list(self.performance_history),
            "last_pulse_prediction": dict(self.last_prediction_result),
            "focus_response_models": self.configured_response_models(),
            "gru_planning": dict(self.current_gru_planning),
            "updated_at": now,
            **({"auto_centering_runtime_seconds": elapsed} if not command.get("enabled") else {}),
            **extra,
        }
        comparable = {key: value for key, value in state.items() if key not in {"updated_at"}}
        previous = {key: value for key, value in self.last_state.items() if key not in {"updated_at"}}
        if comparable != previous or now - self.last_publish >= self.args.heartbeat_interval:
            write_json(self.args.state_json, state)
            self.last_state = state
            self.last_publish = now

    def reset_tracking(self) -> None:
        self.detection_marker = None
        self.previous_center = None
        self.stable_frames = 0
        self.pulse_count = 0
        self.cooldown_until = 0.0
        self.pending_pulse_distance = None
        self.no_progress_pulses = 0
        self.pending_pulse_direction = ""
        self.pending_pulse_action_direction = ""
        self.pending_pulse_duration_ms = 0
        self.pending_pulse_backend = ""
        self.pending_pulse_completed_monotonic_ns = 0
        self.pending_pulse_axis = ""
        self.pending_pulse_offsets = {}
        self.pending_pulses = []
        self.pending_prediction = {}
        self.axis_no_progress = {"x": 0, "y": 0}
        self.axis_recovery_stage = {"x": 0, "y": 0}
        self.blocked_axes = set()
        self.last_pulse_axis = ""
        self.same_axis_pulses = 0
        self.active_axis = ""
        self.vertical_direction_inverted = False
        self.vertical_direction_probe_count = 0
        self.center_samples.clear()
        self.pending_motion_preview = None
        self.pending_motion_preview_marker = None
        self.last_verification_preview_marker = None
        self.pending_verification_started_at = 0.0
        self.verification_frames_seen = 0
        self.verification_best_motion = {}
        self.last_global_motion = {}
        self.verified_motion_count = 0
        self.axis_minimum_step = {
            "x": float(getattr(self.args, "minimum_controllable_x", 0.16)),
            "y": float(getattr(self.args, "minimum_controllable_y", 0.20)),
        }
        self.missing_target_frames = 0
        self.missing_target_since_monotonic = 0.0
        self.reacquisition_confirmation_frames = 0
        self.verification_snapshot = {}
        self.movement_step_history = []
        self.target_recovery_mode = ""
        self.target_recovery_steps = 0
        self.target_recovery_backtrack_steps = 0
        self.target_random_search_steps = 0
        self.request_telemetry = {"controller_received_monotonic_ns": time.monotonic_ns()}
        self.current_gru_planning = {}
        self.current_target_context = {}
        self.request_performance = {
            axis: {
                "steps": 0, "batches": 0, "observed_batches": 0,
                "overshoot_count": 0, "overshoot_amount": 0.0,
                "models_used": set(), "gru_fallback_steps": 0,
            }
            for axis in ("x", "y")
        }

    def finish_request(self, command: dict, status: str, message: str, **extra: object) -> None:
        latest = read_json(self.args.command_json)
        if latest.get("request_id") != command.get("request_id"):
            return
        completed_at = time.time()
        runtime_seconds = auto_centering_runtime_seconds(latest, now=completed_at)
        paused_seconds = manual_ptz_paused_seconds(latest, now=completed_at)
        finished = {
            **latest,
            "enabled": False,
            "completion_status": status,
            "completed_at": completed_at,
            "completion_message": message,
            "auto_centering_runtime_seconds": runtime_seconds,
            "manual_ptz_paused_seconds": paused_seconds,
            "manual_ptz_active": False,
            "manual_ptz_pause_started_at": 0.0,
            "manual_ptz_pause_until": 0.0,
        }
        history_item = {
            "request_id": str(finished.get("request_id") or ""),
            "source": str(finished.get("source") or "wifi"),
            "target_label": str(finished.get("target_label") or ""),
            "target_track_id": finished.get("target_track_id"),
            "status": status,
            "requested_at": finished.get("requested_at"),
            "completed_at": completed_at,
            "runtime_seconds": runtime_seconds,
            "pulse_count": self.pulse_count,
            "message": message,
        }
        for key in ("object_center", "bbox", "frame_size", "effective_deadzone"):
            if extra.get(key) is not None:
                history_item[key] = extra[key]
        configured_models = self.configured_response_models()
        axis_metrics = {}
        for axis in ("x", "y"):
            metric = self.request_performance.get(axis, {})
            models_used = sorted(metric.get("models_used") or [])
            effective_model = models_used[0] if len(models_used) == 1 else "mixed" if models_used else configured_models[axis]
            observed_batches = int(metric.get("observed_batches") or 0)
            overshoot_count = int(metric.get("overshoot_count") or 0)
            axis_metrics[axis] = {
                "configured_model": configured_models[axis],
                "effective_model": effective_model,
                "steps": int(metric.get("steps") or 0),
                "batches": int(metric.get("batches") or 0),
                "observed_batches": observed_batches,
                "overshoot_count": overshoot_count,
                "overshoot_amount": round(float(metric.get("overshoot_amount") or 0.0), 6),
                "overshoot_rate": overshoot_count / observed_batches if observed_batches else 0.0,
                "gru_fallback_steps": int(metric.get("gru_fallback_steps") or 0),
            }
        performance_item = {
            **history_item,
            "performance_schema_version": 1,
            "metrics_complete": True,
            "axis_metrics": axis_metrics,
            "total_steps": self.pulse_count,
            "total_overshoots": sum(item["overshoot_count"] for item in axis_metrics.values()),
        }
        history_item.update({
            "total_steps": performance_item["total_steps"],
            "total_overshoots": performance_item["total_overshoots"],
            "axis_metrics": axis_metrics,
        })
        self.performance_history = self.performance_history[-99:] + [performance_item]
        self.run_history = [
            item for item in self.run_history
            if str(item.get("request_id") or "") != history_item["request_id"]
        ][-19:] + [history_item]
        write_json(self.args.command_json, finished)
        print(json.dumps({"event": "auto_centering_finished", **history_item}, sort_keys=True), flush=True)
        prediction_path = str(getattr(self.args, "prediction_error_jsonl", "") or "").strip()
        if prediction_path:
            try:
                append_prediction_event(prediction_path, {"event": "autofocus_run_completed", **performance_item})
            except OSError as exc:
                print(json.dumps({"event": "run_performance_persist_error", "error": str(exc)[:300]}), flush=True)
        self.command_signature = json.dumps(
            {
                "enabled": False,
                "source": finished.get("source"),
                "target_label": finished.get("target_label"),
                "request_id": finished.get("request_id"),
            },
            sort_keys=True,
        )
        self.publish(
            status,
            finished,
            message=message,
            pulse_count=self.pulse_count,
            auto_centering_runtime_seconds=runtime_seconds,
            **extra,
        )
        if status == "complete":
            completion_chime = play_focus_completion_chime(self.args, str(finished.get("source") or "wifi"))
            self.publish(
                status,
                finished,
                message=message,
                pulse_count=self.pulse_count,
                auto_centering_runtime_seconds=runtime_seconds,
                completion_chime=completion_chime,
                **extra,
            )

    def step(self) -> None:
        command = self.pending_command_payload or read_json(self.args.command_json)
        self.pending_command_payload = {}
        signature = json.dumps(
            {
                "enabled": bool(command.get("enabled")),
                "source": command.get("source"),
                "target_label": command.get("target_label"),
                "request_id": command.get("request_id"),
            },
            sort_keys=True,
        )
        if signature != self.command_signature:
            if self.pending_prediction:
                outcome = "cancelled" if not command.get("enabled") else "superseded"
                self.finish_pulse_prediction(
                    outcome,
                    superseded_by_request_id=str(command.get("request_id") or ""),
                )
            self.command_signature = signature
            self.reset_tracking()
            if command.get("enabled") and str(command.get("request_id") or "") != self.logged_request_id:
                self.logged_request_id = str(command.get("request_id") or "")
                print(json.dumps({
                    "event": "auto_centering_started",
                    "request_id": self.logged_request_id,
                    "source": str(command.get("source") or "wifi"),
                    "target_label": str(command.get("target_label") or ""),
                    "requested_at": command.get("requested_at"),
                }, sort_keys=True), flush=True)

        if not command.get("enabled"):
            if str(command.get("stop_reason") or "").strip().lower() == "manual ptz override":
                self.ptz_idle_verified = False
            completion_status = str(command.get("completion_status") or "disabled")
            message = str(command.get("completion_message") or "No object-focus request is active.")
            request_id = str(command.get("request_id") or "")
            terminal_statuses = {"complete", "failed", "timed_out", "error", "cancelled", "controller_unavailable"}
            if request_id and completion_status in terminal_statuses and not any(
                str(item.get("request_id") or "") == request_id for item in self.run_history
            ):
                runtime_seconds = auto_centering_runtime_seconds(command)
                self.run_history = self.run_history[-19:] + [{
                    "request_id": request_id,
                    "source": str(command.get("source") or "wifi"),
                    "target_label": str(command.get("target_label") or ""),
                    "status": completion_status,
                    "requested_at": command.get("requested_at"),
                    "completed_at": command.get("completed_at") or command.get("stopped_at"),
                    "runtime_seconds": runtime_seconds,
                    "pulse_count": int(self.last_state.get("pulse_count") or 0),
                    "message": message,
                }]
            preserved = {}
            if self.last_state.get("request_id") == command.get("request_id"):
                preserved = {
                    key: value
                    for key, value in self.last_state.items()
                    if key not in {
                        "status", "enabled", "source", "target_label", "request_id",
                        "controller_pid", "updated_at", "message",
                    }
                }
            self.publish(completion_status, command, message=message, **preserved)
            return
        source = str(command.get("source") or "wifi").strip().lower()
        self.request_max_steps = self.configured_max_steps(source)
        label = normalize_label(command.get("target_label"))
        if source not in {"wifi", "bulb"} or not label:
            self.finish_request(command, "error", "Focus request requires a wifi/bulb source and target label.")
            return
        try:
            manual_pause_until = float(command.get("manual_ptz_pause_until") or 0.0)
        except (TypeError, ValueError):
            manual_pause_until = 0.0
        if bool(command.get("manual_ptz_active")) or time.time() < manual_pause_until:
            if self.pending_prediction:
                self.finish_pulse_prediction("manual_yield")
            self.pending_pulse_distance = None
            self.pending_pulse_axis = ""
            self.pending_pulse_offsets = {}
            self.pending_pulses = []
            self.pending_pulse_direction = ""
            self.pending_pulse_action_direction = ""
            self.pending_pulse_duration_ms = 0
            self.pending_pulse_backend = ""
            self.pending_pulse_completed_monotonic_ns = 0
            self.pending_motion_preview = None
            self.pending_motion_preview_marker = None
            self.last_verification_preview_marker = None
            self.pending_verification_started_at = 0.0
            self.verification_frames_seen = 0
            self.verification_best_motion = {}
            self.center_samples.clear()
            self.detection_marker = None
            self.ptz_idle_verified = False
            self.publish(
                "paused",
                command,
                message="Autofocus paused for manual camera movement; it will resume from a fresh frame.",
                manual_ptz_active=bool(command.get("manual_ptz_active")),
                manual_ptz_pause_until=manual_pause_until,
                pulse_count=self.pulse_count,
                **preserved_focus_geometry(self.last_state),
            )
            return
        request_id = str(command.get("request_id") or "")
        try:
            expires_at = float(command.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at > 0 and time.time() >= expires_at and not self.target_recovery_mode:
            self.finish_request(command, "timed_out", f"Could not focus on {label} before the request timed out.")
            return

        if (
            self.latest_detection_payload
            and time.monotonic() - self.latest_detection_received_monotonic <= 0.25
            and str(self.latest_detection_payload.get("source") or source).strip().lower() == source
        ):
            detections = self.latest_detection_payload
        else:
            root = read_json(self.args.detections_json)
            detections = source_payload(root, source)
        updated_at = detection_timestamp(detections)
        marker = detection_marker(detections)
        try:
            detection_monotonic_ns = int(detections.get("objects_updated_monotonic_ns") or 0)
        except (TypeError, ValueError):
            detection_monotonic_ns = 0
        age = time.time() - updated_at if updated_at > 0 else float("inf")
        if age > self.args.max_detection_age:
            self.publish("stale", command, detection_age_seconds=round(age, 3) if math.isfinite(age) else None, message="Waiting for fresh detections.")
            return
        if marker == self.detection_marker:
            self.publish(self.last_state.get("status", "focusing"), command, **{
                key: value for key, value in self.last_state.items()
                if key not in {"status", "enabled", "source", "target_label", "controller_pid", "updated_at"}
            })
            return
        self.detection_marker = marker

        if self.target_recovery_mode and time.monotonic() < self.cooldown_until:
            self.publish(
                "recovering_target",
                command,
                message=f"Waiting for the camera to settle before checking for {label} again.",
                pulse_count=self.pulse_count,
                target_recovery_mode=self.target_recovery_mode,
                target_recovery_steps=self.target_recovery_steps,
                target_recovery_backtrack_steps=self.target_recovery_backtrack_steps,
                target_random_search_steps=self.target_random_search_steps,
                movement_history_depth=len(self.movement_step_history),
            )
            return

        if (
            bool(getattr(self.args, "simple_closed_loop", False))
            and self.pending_pulse_distance is not None
            and detection_monotonic_ns > 0
            and self.pending_pulse_completed_monotonic_ns > 0
            and detection_monotonic_ns <= self.pending_pulse_completed_monotonic_ns
        ):
            self.publish(
                "settling",
                command,
                message="Waiting for a COCO frame captured after the PTZ pulse.",
                rejected_source_frame_id=marker[1] if marker[0] == "frame" else None,
                rejected_detection_monotonic_ns=detection_monotonic_ns,
                pulse_completed_monotonic_ns=self.pending_pulse_completed_monotonic_ns,
                **preserved_focus_geometry(self.last_state),
            )
            return

        if self.pending_pulse_direction and time.monotonic() < self.cooldown_until:
            preserved_geometry = preserved_focus_geometry(self.last_state)
            self.publish(
                "settling",
                command,
                pulse_count=self.pulse_count,
                pending_pulse_axis=self.pending_pulse_axis,
                pending_pulse_direction=self.pending_pulse_direction,
                pending_pulse_action_direction=self.pending_pulse_action_direction,
                pending_pulse_duration_ms=self.pending_pulse_duration_ms,
                pending_pulse_backend=self.pending_pulse_backend,
                pending_pulses=list(self.pending_pulses),
                vertical_direction_inverted=self.vertical_direction_inverted,
                vertical_direction_probe_count=self.vertical_direction_probe_count,
                message="Waiting for a post-motion DeepStream frame.",
                **preserved_geometry,
            )
            return

        objects = detections.get("objects") if isinstance(detections.get("objects"), list) else []
        target = choose_target(objects, label, self.previous_center, command.get("target_track_id"))
        if target is None or float(target["confidence"]) < self.args.minimum_confidence:
            self.stable_frames = 0
            self.reacquisition_confirmation_frames = 0
            self.missing_target_frames += 1
            now_monotonic = time.monotonic()
            if self.missing_target_since_monotonic <= 0.0:
                self.missing_target_since_monotonic = now_monotonic
            missing_seconds = max(0.0, now_monotonic - self.missing_target_since_monotonic)
            grace_seconds = max(0.0, float(getattr(self.args, "missing_target_grace_seconds", 0.0)))
            missing_target_limit = max(1, int(getattr(self.args, "missing_target_frames", 1)))
            if missing_seconds < grace_seconds:
                self.publish(
                    "awaiting_target",
                    command,
                    pulse_count=self.pulse_count,
                    missing_target_frames=self.missing_target_frames,
                    missing_target_seconds=round(missing_seconds, 3),
                    missing_target_grace_seconds=grace_seconds,
                    message=f"Holding camera movement briefly while waiting for {label} to reappear.",
                    **preserved_focus_geometry(self.last_state),
                )
                return
            if self.pending_pulse_direction:
                if self.missing_target_frames < missing_target_limit:
                    self.publish(
                        "verifying",
                        command,
                        pulse_count=self.pulse_count,
                        verification_snapshot=self.verification_snapshot,
                        message=f"Verifying whether {label} remains visible after camera movement.",
                    )
                    return
                self.finish_pulse_prediction(
                    "target_lost",
                    post_source_frame_id=marker[1] if marker[0] == "frame" else None,
                    missing_target_frames=self.missing_target_frames,
                )
                self.clear_pending_pulse_state()
                self.recover_lost_target(command, source, label)
                return
            if self.missing_target_frames < missing_target_limit:
                self.publish(
                    "awaiting_target",
                    command,
                    detection_age_seconds=round(age, 3),
                    message=f"Checking the next frame for {label} before stopping.",
                )
                return
            self.recover_lost_target(command, source, label)
            return

        had_missing_target = self.missing_target_since_monotonic > 0.0
        if had_missing_target:
            self.reacquisition_confirmation_frames += 1
            required_reacquisition_frames = max(
                1, int(getattr(self.args, "reacquisition_confirmation_frames", 1))
            )
            raw_center = (float(target["center_x"]), float(target["center_y"]))
            self.previous_center = raw_center
            if self.reacquisition_confirmation_frames < required_reacquisition_frames:
                self.publish(
                    "reacquiring_target",
                    command,
                    pulse_count=self.pulse_count,
                    reacquisition_confirmation_frames=self.reacquisition_confirmation_frames,
                    required_reacquisition_frames=required_reacquisition_frames,
                    message=f"Confirming that {label} has reappeared before resuming camera movement.",
                    **{
                        **preserved_focus_geometry(self.last_state),
                        "object_center": {"x": round(raw_center[0], 4), "y": round(raw_center[1], 4)},
                    },
                )
                return

        reacquired_target = bool(self.target_recovery_mode) or had_missing_target
        self.missing_target_frames = 0
        self.missing_target_since_monotonic = 0.0
        self.reacquisition_confirmation_frames = 0
        self.target_recovery_mode = ""

        raw_center = (float(target["center_x"]), float(target["center_y"]))
        simple_closed_loop = bool(getattr(self.args, "simple_closed_loop", False))
        self.center_samples.append(raw_center)
        center = raw_center if simple_closed_loop else (
            sorted(item[0] for item in self.center_samples)[len(self.center_samples) // 2],
            sorted(item[1] for item in self.center_samples)[len(self.center_samples) // 2],
        )
        target = {**target, "center_x": center[0], "center_y": center[1]}
        bbox = target.get("bbox") if isinstance(target.get("bbox"), list) else []
        frame_size = target.get("frame_size") if isinstance(target.get("frame_size"), list) else []
        if len(bbox) >= 4 and len(frame_size) >= 2:
            frame_width = max(1.0, finite_float(frame_size[0], 1.0))
            frame_height = max(1.0, finite_float(frame_size[1], 1.0))
            width = max(0.0, finite_float(bbox[2])) / frame_width
            height = max(0.0, finite_float(bbox[3])) / frame_height
            self.current_target_context = {
                "target_bbox": {
                    "width": width, "height": height, "area": width * height,
                    "aspect_ratio": width / max(1e-6, height),
                },
                "target_confidence": float(target["confidence"]),
            }
        if self.previous_center is not None and math.hypot(center[0] - self.previous_center[0], center[1] - self.previous_center[1]) <= self.args.stability_distance:
            self.stable_frames += 1
        else:
            self.stable_frames = 1
        self.previous_center = center
        # Keep the configured visual deadzone authoritative. A tiny fixed
        # vertical epsilon prevents detector rounding at the boundary (for
        # example 0.0508 vs 0.0500) from causing endless up/down corrections.
        effective_deadzone_x = effective_centering_deadzone(self.args.deadzone_x, "x", 0.0)
        effective_deadzone_y = effective_centering_deadzone(
            self.args.deadzone_y,
            "y",
            float(getattr(self.args, "vertical_completion_epsilon", 0.0)),
        )
        if simple_closed_loop:
            pulse_plan = fixed_pulse_batch(
                target,
                effective_deadzone_x,
                effective_deadzone_y,
                self.blocked_axes,
                3,
            )
            self.active_axis = "+".join(str(item.get("axis") or "") for item in pulse_plan)
            axis_preference = self.active_axis
            movement = pulse_plan[0] if pulse_plan else movement_for_target(
                target,
                effective_deadzone_x,
                effective_deadzone_y,
                self.blocked_axes,
            )
        else:
            axis_preference = self.fairness_axis_preference(target, effective_deadzone_x, effective_deadzone_y)
            pulse_plan = []
            movement = movement_for_target(
                target,
                effective_deadzone_x,
                effective_deadzone_y,
                self.blocked_axes,
                prefer_axis=axis_preference,
            )
        focus_distance = math.hypot(
            float(movement["offset"]["x"]),
            float(movement["offset"]["y"]),
        )
        common = {
            "confidence": round(float(target["confidence"]), 4),
            "object_center": {"x": round(center[0], 4), "y": round(center[1], 4)},
            "focus_point_mode": "bounding_box_midpoint",
            "goal_center": {"x": 0.5, "y": 0.5},
            "offset": {key: round(float(value), 4) for key, value in movement["offset"].items()},
            "distance_to_center": round(focus_distance, 4),
            "next_axis": "+".join(str(item.get("axis") or "") for item in pulse_plan) if simple_closed_loop else str(movement.get("axis") or ""),
            "next_direction": "+".join(str(item.get("direction") or "") for item in pulse_plan) if simple_closed_loop else str(movement.get("direction") or ""),
            "next_pulses": [
                {"axis": str(item.get("axis") or ""), "direction": str(item.get("direction") or "")}
                for item in pulse_plan
            ] if simple_closed_loop else [],
            "bbox": target["bbox"],
            "frame_size": target["frame_size"],
            "stable_frames": self.stable_frames,
            "detection_age_seconds": round(age, 3),
            "source_frame_id": marker[1] if marker[0] == "frame" else None,
            "verification_snapshot": self.verification_snapshot,
            "blocked_axes": sorted(self.blocked_axes),
            "ptz_recovery_stage_by_axis": dict(self.axis_recovery_stage),
            "effective_deadzone": {"x": round(effective_deadzone_x, 4), "y": round(effective_deadzone_y, 4)},
            "minimum_controllable_step": {key: round(value, 4) for key, value in self.axis_minimum_step.items()},
            "direction_gains": dict(self.direction_gains),
            "direction_gain_sample_counts": dict(self.direction_gain_sample_counts),
            "target_reacquired": reacquired_target,
            "target_track_id": target.get("track_id"),
            "target_track_age_frames": target.get("track_age_frames"),
            "target_recovery_steps": self.target_recovery_steps,
            "target_recovery_backtrack_steps": self.target_recovery_backtrack_steps,
            "target_random_search_steps": self.target_random_search_steps,
            "movement_history_depth": len(self.movement_step_history),
            "center_sample_count": len(self.center_samples),
            "verified_motion_count": self.verified_motion_count,
            "axis_preference": axis_preference,
            "active_axis": self.active_axis if simple_closed_loop else "",
            "control_mode": "coco_three_pulse_batch" if simple_closed_loop else "verified_adaptive",
            "same_axis_pulses": self.same_axis_pulses,
            "last_pulse_axis": self.last_pulse_axis,
            "vertical_direction_inverted": self.vertical_direction_inverted,
            "vertical_direction_probe_count": self.vertical_direction_probe_count,
            "pending_pulse_axis": self.pending_pulse_axis,
            "pending_pulse_direction": self.pending_pulse_direction,
            "pending_pulse_action_direction": self.pending_pulse_action_direction,
            "pending_pulse_duration_ms": self.pending_pulse_duration_ms,
            "pending_pulse_backend": self.pending_pulse_backend,
            "pending_pulses": list(self.pending_pulses),
            "latency_telemetry": dict(self.request_telemetry),
            "focus_response_models": self.configured_response_models(),
        }
        if self.pending_pulse_distance is not None and simple_closed_loop:
            current_preview_marker = motion_preview_marker(getattr(self.args, "motion_preview_path", ""))
            if current_preview_marker is not None and current_preview_marker != self.pending_motion_preview_marker:
                current_preview = load_motion_preview(getattr(self.args, "motion_preview_path", ""))
                global_motion = estimate_global_motion(self.pending_motion_preview, current_preview)
                self.last_global_motion = {**global_motion, "verified": bool(global_motion.get("available"))}
                common["global_motion"] = {
                    key: round(value, 5) if isinstance(value, float) else value
                    for key, value in self.last_global_motion.items()
                }
            pending_offsets = self.pending_pulse_offsets or {
                self.pending_pulse_axis or str(movement.get("axis") or ""): self.pending_pulse_distance,
            }
            progress_by_axis = {
                axis: target_axis_progress(previous_offset, float(movement["offset"].get(axis, 0.0)), 0.0)
                for axis, previous_offset in pending_offsets.items()
                if axis in {"x", "y"}
            }
            target_progress = next(iter(progress_by_axis.values()), target_axis_progress(0.0, 0.0, 0.0))
            common["target_axis_progress"] = {
                key: round(value, 5) if isinstance(value, float) else value
                for key, value in target_progress.items()
            }
            common["target_axis_progress_by_axis"] = {
                axis: {
                    key: round(value, 5) if isinstance(value, float) else value
                    for key, value in progress.items()
                }
                for axis, progress in progress_by_axis.items()
            }
            common["post_pulse_frame_received"] = True
            common["post_pulse_source_frame_id"] = marker[1] if marker[0] == "frame" else None
            acknowledged_ns = int(self.pending_prediction.get("dispatched_monotonic_ns") or 0)
            if acknowledged_ns > 0 and detection_monotonic_ns > acknowledged_ns:
                breakdown = dict(self.pending_prediction.get("latency_breakdown_ms") or {})
                breakdown.update({
                    "detection_update_after_ack": round((detection_monotonic_ns - acknowledged_ns) / 1_000_000, 3),
                    "controller_observed_after_ack": round((time.monotonic_ns() - acknowledged_ns) / 1_000_000, 3),
                    "controller_queue_after_detection": round(max(0.0, (time.monotonic_ns() - detection_monotonic_ns) / 1_000_000), 3),
                })
                self.pending_prediction["latency_breakdown_ms"] = breakdown
                common["step_latency_breakdown_ms"] = breakdown
            minimum_response = float(getattr(self.args, "minimum_observed_motion", 0.005))
            response_observed = bool(progress_by_axis) and all(
                float(progress.get("axis_motion", 0.0)) >= minimum_response
                for progress in progress_by_axis.values()
            )
            response_elapsed = max(0.0, time.monotonic() - self.pending_verification_started_at)
            response_timeout = self.pending_response_timeout()
            prediction_model = self.pending_prediction.get("prediction_model") if isinstance(self.pending_prediction.get("prediction_model"), dict) else {}
            no_response_probability = finite_float(prediction_model.get("no_response_probability"), float("nan"))
            required_response_frames = response_frames_required(
                int(getattr(self.args, "simple_response_frames", 2)),
                progress_by_axis,
                no_response_probability if math.isfinite(no_response_probability) else None,
            )
            if response_observed:
                self.verification_frames_seen += 1
            common["pulse_response_observed"] = response_observed
            common["pulse_response_elapsed"] = round(response_elapsed, 3)
            common["pulse_response_frames"] = self.verification_frames_seen
            common["adaptive_response_timeout_seconds"] = round(response_timeout, 3)
            common["adaptive_response_required_frames"] = required_response_frames
            common["predicted_response_seconds"] = prediction_model.get("expected_response_seconds")
            common["predicted_no_response_probability"] = prediction_model.get("no_response_probability")
            if not response_observed and response_elapsed < response_timeout:
                self.publish(
                    "settling",
                    command,
                    message=f"Waiting for the {label} box to reflect the completed PTZ step.",
                    **common,
                )
                return
            if not response_observed:
                self.finish_pulse_prediction(
                    "no_response",
                    center,
                    marker[1] if marker[0] == "frame" else None,
                    response_elapsed_seconds=round(response_elapsed, 3),
                    response_frames=self.verification_frames_seen,
                    progress_by_axis=common.get("target_axis_progress_by_axis"),
                )
                partial_response_observed = any(
                    float(progress.get("axis_motion", 0.0)) >= minimum_response
                    for progress in progress_by_axis.values()
                )
                if partial_response_observed:
                    self.verified_motion_count += 1
                self.pending_pulse_distance = None
                self.pending_pulse_axis = ""
                self.pending_pulse_offsets = {}
                self.pending_pulses = []
                self.pending_pulse_direction = ""
                self.pending_pulse_action_direction = ""
                self.pending_pulse_duration_ms = 0
                self.pending_pulse_backend = ""
                self.pending_pulse_completed_monotonic_ns = 0
                self.pending_motion_preview = None
                self.pending_motion_preview_marker = None
                self.last_verification_preview_marker = None
                self.pending_verification_started_at = 0.0
                self.verification_frames_seen = 0
                self.verification_best_motion = {}
                self.center_samples.clear()
                self.publish(
                    "observing",
                    command,
                    message=(
                        f"The completed {label} PTZ step undershot or only moved some axes; "
                        "continuing from the latest object position."
                    ),
                    ptz_motion_verified=partial_response_observed,
                    **common,
                )
                return
            if self.verification_frames_seen < required_response_frames:
                self.publish(
                    "settling",
                    command,
                    message=f"Confirming the {label} box position after the completed PTZ step.",
                    **common,
                )
                return
            if any(float(progress.get("center_progress", 0.0)) > 0.0 for progress in progress_by_axis.values()):
                self.verified_motion_count += 1
            self.finish_pulse_prediction(
                "observed",
                center,
                marker[1] if marker[0] == "frame" else None,
                response_elapsed_seconds=round(response_elapsed, 3),
                response_frames=self.verification_frames_seen,
                progress_by_axis=common.get("target_axis_progress_by_axis"),
            )
            self.pending_pulse_distance = None
            self.pending_pulse_axis = ""
            self.pending_pulse_offsets = {}
            self.pending_pulses = []
            self.pending_pulse_direction = ""
            self.pending_pulse_action_direction = ""
            self.pending_pulse_duration_ms = 0
            self.pending_pulse_backend = ""
            self.pending_pulse_completed_monotonic_ns = 0
            self.pending_motion_preview = None
            self.pending_motion_preview_marker = None
            self.last_verification_preview_marker = None
            self.pending_verification_started_at = 0.0
            self.verification_frames_seen = 0
            self.verification_best_motion = {}
            self.publish(
                "observing",
                command,
                message=f"Observed the completed PTZ step; recalculating from the next {label} box.",
                **common,
            )
            return
        elif self.pending_pulse_distance is not None:
            pulse_axis = self.pending_pulse_axis or str(movement.get("axis") or "")
            preview_path = getattr(self.args, "motion_preview_path", "")
            current_preview_marker = motion_preview_marker(preview_path)
            preview_advanced = bool(
                current_preview_marker is not None
                and current_preview_marker != self.pending_motion_preview_marker
                and current_preview_marker != self.last_verification_preview_marker
            )
            global_motion: dict = {}
            if preview_advanced:
                current_preview = load_motion_preview(preview_path)
                global_motion = estimate_global_motion(self.pending_motion_preview, current_preview)
                self.last_verification_preview_marker = current_preview_marker
                self.verification_frames_seen += 1
                candidate_score = (
                    float(global_motion.get("response", 0.0))
                    * abs(float(global_motion.get(pulse_axis, 0.0)))
                )
                best_score = (
                    float(self.verification_best_motion.get("response", 0.0))
                    * abs(float(self.verification_best_motion.get(pulse_axis, 0.0)))
                )
                if candidate_score >= best_score:
                    self.verification_best_motion = dict(global_motion)
            if self.verification_best_motion:
                global_motion = dict(self.verification_best_motion)
            current_axis_offset = float(movement["offset"].get(pulse_axis, 0.0))
            target_progress = target_axis_progress(
                self.pending_pulse_distance,
                current_axis_offset,
                float(getattr(self.args, "minimum_progress", 0.005)),
            )
            verification = verify_pulse_motion(
                global_motion,
                pulse_axis,
                target_progress,
                float(getattr(self.args, "minimum_phase_response", 0.08)),
                float(getattr(self.args, "minimum_global_motion", 0.008)),
            )
            axis_motion = float(verification["axis_motion"])
            motion_verified = bool(verification["verified"])
            verified_axis_motion = float(verification["verified_axis_motion"])
            self.last_global_motion = {
                **global_motion,
                "axis": pulse_axis,
                "axis_motion": axis_motion,
                "scene_verified": verification["scene_motion_verified"],
                "global_axis_verified": verification["global_axis_verified"],
                "global_centering_verified": verification["global_centering_verified"],
                "target_verified": verification["target_motion_verified"],
                "verified": motion_verified,
            }
            common["global_motion"] = {
                key: round(value, 5) if isinstance(value, float) else value
                for key, value in self.last_global_motion.items()
            }
            common["target_axis_progress"] = {
                key: round(value, 5) if isinstance(value, float) else value
                for key, value in target_progress.items()
            }
            verification_elapsed = max(0.0, time.monotonic() - self.pending_verification_started_at)
            verification_timeout = float(getattr(self.args, "motion_verification_timeout", 0.8))
            verification_min_seconds = float(getattr(self.args, "motion_verification_min_seconds", 0.15))
            verification_max_frames = max(1, int(getattr(self.args, "motion_verification_frames", 3)))
            common["motion_verification_elapsed"] = round(verification_elapsed, 3)
            common["motion_verification_frames"] = self.verification_frames_seen
            verification_exhausted = bool(
                verification_elapsed >= verification_timeout
                or (
                    self.verification_frames_seen >= verification_max_frames
                    and verification_elapsed >= verification_min_seconds
                )
            )
            if not motion_verified and not verification_exhausted:
                self.publish(
                    "verifying",
                    command,
                    message=(
                        f"Waiting for a new post-PTZ DeepStream frame on the {pulse_axis} axis."
                        if not preview_advanced
                        else f"Verifying post-PTZ motion on the {pulse_axis} axis."
                    ),
                    **common,
                )
                return
            if not motion_verified:
                self.no_progress_pulses += 1
                if pulse_axis in self.axis_no_progress:
                    self.axis_no_progress[pulse_axis] += 1
            else:
                self.no_progress_pulses = 0
                if pulse_axis in self.axis_no_progress:
                    self.axis_no_progress[pulse_axis] = 0
                    self.axis_recovery_stage[pulse_axis] = 0
                    self.axis_minimum_step[pulse_axis] = min(
                        self.axis_minimum_step[pulse_axis],
                        verified_axis_motion,
                    )
                    self.verified_motion_count += 1
                    if pulse_axis == "y":
                        self.vertical_direction_probe_count = 0
            self.finish_pulse_prediction(
                "observed" if motion_verified else "no_response",
                center,
                marker[1] if marker[0] == "frame" else None,
                motion_verified=motion_verified,
                verification_elapsed_seconds=round(verification_elapsed, 3),
                target_axis_progress=common.get("target_axis_progress"),
            )
            self.pending_pulse_distance = None
            self.pending_pulse_axis = ""
            self.pending_pulse_offsets = {}
            self.pending_pulses = []
            self.pending_pulse_direction = ""
            self.pending_pulse_action_direction = ""
            self.pending_motion_preview = None
            self.pending_motion_preview_marker = None
            self.last_verification_preview_marker = None
            self.pending_verification_started_at = 0.0
            self.verification_frames_seen = 0
            self.verification_best_motion = {}
            axis_no_progress_limit = int(getattr(self.args, "max_axis_no_progress_pulses", 3))
            vertical_probe_limit = int(getattr(self.args, "vertical_direction_probe_pulses", 2))
            if should_probe_vertical_direction(
                bool(getattr(self.args, "adaptive_tilt_direction_probe", False))
                and self.vertical_direction_probe_count == 0,
                pulse_axis,
                self.axis_no_progress.get("y", 0),
                vertical_probe_limit,
            ):
                self.vertical_direction_inverted = not self.vertical_direction_inverted
                self.vertical_direction_probe_count += 1
                self.axis_no_progress["y"] = 0
                self.no_progress_pulses = 0
                common["vertical_direction_inverted"] = self.vertical_direction_inverted
                common["vertical_direction_probe_count"] = self.vertical_direction_probe_count
                movement = movement_for_target(
                    target,
                    effective_deadzone_x,
                    effective_deadzone_y,
                    self.blocked_axes,
                    prefer_axis=self.fairness_axis_preference(target, effective_deadzone_x, effective_deadzone_y),
                )
            elif pulse_axis and self.axis_no_progress.get(pulse_axis, 0) >= axis_no_progress_limit:
                recovery_stage = self.axis_recovery_stage.get(pulse_axis, 0)
                if recovery_stage < 2:
                    self.axis_recovery_stage[pulse_axis] = recovery_stage + 1
                    self.axis_no_progress[pulse_axis] = 0
                else:
                    # Stay on the ONVIF recovery backend and keep trying until
                    # the user-configured overall step budget is exhausted.
                    # A short per-axis threshold must not silently turn a
                    # 100-step best-effort request into a six-step request.
                    self.axis_no_progress[pulse_axis] = 0
                movement = movement_for_target(
                    target,
                    effective_deadzone_x,
                    effective_deadzone_y,
                    self.blocked_axes,
                    prefer_axis=self.fairness_axis_preference(target, effective_deadzone_x, effective_deadzone_y),
                )
                common["blocked_axes"] = sorted(self.blocked_axes)
                common["ptz_recovery_stage_by_axis"] = dict(self.axis_recovery_stage)
            no_progress_limit = min(
                self.request_max_steps,
                int(getattr(self.args, "max_no_progress_pulses", self.request_max_steps)),
            )
            if self.no_progress_pulses >= no_progress_limit:
                self.finish_request(
                    command,
                    "failed",
                    (
                        f"PTZ commands were acknowledged, but {label} did not move toward center after "
                        f"{self.no_progress_pulses} configured steps."
                    ),
                    no_progress_pulses=self.no_progress_pulses,
                    **common,
                )
                return
        idle_trust_seconds = max(0.0, float(getattr(self.args, "ptz_idle_trust_seconds", 30.0)))
        idle_age = max(0.0, time.monotonic() - self.ptz_idle_verified_at) if self.ptz_idle_verified_at else float("inf")
        idle_trusted = bool(self.ptz_idle_verified and idle_age <= idle_trust_seconds)
        if self.ptz_idle_verified and not idle_trusted:
            self.ptz_idle_verified = False
        required_stable_frames = int(self.args.required_stable_frames)
        if movement["centered"] and not idle_trusted:
            required_stable_frames = max(
                required_stable_frames,
                int(getattr(self.args, "idle_confirmation_frames", 2)),
            )
        if self.stable_frames < required_stable_frames:
            self.publish("acquiring", command, message=f"Confirming the {label} detection before moving.", **common)
            return
        if movement["centered"]:
            # Multiple fresh, stable detector frames are a cheap confirmation
            # that an unowned camera is not still drifting. This avoids a slow
            # ONVIF Stop for the common already-centered case without blindly
            # trusting a single frame.
            self.ptz_idle_verified = True
            self.ptz_idle_verified_at = time.monotonic()
            self.finish_request(
                command,
                "complete",
                f"Centered the {label} bounding-box midpoint.",
                **common,
            )
            return
        if movement.get("exhausted"):
            blocked = ", ".join(sorted(self.blocked_axes)) or "camera"
            self.finish_request(
                command,
                "failed",
                (
                    f"Could not center {label}: no verified movement on the {blocked} axis after "
                    "restarting the native PTZ session and retrying through ONVIF."
                ),
                ptz_motion_verified=False,
                **common,
            )
            return
        if self.pulse_count >= self.request_max_steps:
            if self.verified_motion_count:
                failure = (
                    f"Camera moved, but could not place the {label} bounding-box midpoint inside the calibrated "
                    f"centering tolerance after {self.pulse_count} steps; the remaining offset is below the camera's reliable PTZ resolution."
                )
            else:
                failure = f"Could not center {label}: no physical camera movement was verified after {self.pulse_count} PTZ steps."
            self.finish_request(
                command,
                "failed",
                failure,
                ptz_motion_verified=bool(self.verified_motion_count),
                **common,
            )
            return
        if self.prepared_request_id != request_id:
            if not idle_trusted:
                self.publish(
                    "preparing",
                    command,
                    message="Clearing an unverified camera motion state before focusing.",
                    **common,
                )
                try:
                    clear_ptz_motion(
                        ptz_url(self.args.ptz_url, source),
                        self.args.ptz_timeout,
                        getattr(self.args, "ptz_socket", ""),
                        source,
                    )
                    self.ptz_idle_verified = True
                    self.ptz_idle_verified_at = time.monotonic()
                except Exception as exc:
                    self.ptz_idle_verified = False
                    self.publish(
                        "preparing",
                        command,
                        warning=f"Could not clear stale PTZ motion state: {str(exc)[:400]}",
                        message="PTZ cleanup timed out; continuing with bounded focus steps.",
                        **common,
                    )
            self.prepared_request_id = request_id
        now = time.monotonic()
        if now < self.cooldown_until:
            self.publish("settling", command, message="Waiting for camera motion to settle.", **common)
            return

        planned_movements = pulse_plan if simple_closed_loop else [movement]
        remaining_budget = max(1, self.request_max_steps - self.pulse_count)
        planned_movements = planned_movements[:remaining_budget]
        pulse_specs = budgeted_pulse_specs(
            planned_movements,
            self.args,
            effective_deadzone_x,
            effective_deadzone_y,
            self.axis_minimum_step,
            self.axis_recovery_stage,
            self.vertical_direction_inverted,
            self.direction_gains,
            simple_closed_loop,
        )
        pulse_specs, response_model_diagnostics = self.apply_selected_response_models(
            command,
            center,
            pulse_specs,
            effective_deadzone_x,
            effective_deadzone_y,
        )
        original_batch_count = len(pulse_specs)
        pulse_specs, batch_diagnostics = self.confidence_aware_batch(pulse_specs, response_model_diagnostics)
        if len(pulse_specs) != original_batch_count:
            pulse_specs, response_model_diagnostics = self.apply_selected_response_models(
                command,
                center,
                pulse_specs,
                effective_deadzone_x,
                effective_deadzone_y,
            )
        self.current_gru_planning = dict(response_model_diagnostics.get("axes", {}))
        common["focus_response_models"] = response_model_diagnostics.get("axis_models", {})
        common["gru_planning"] = dict(self.current_gru_planning)
        common["batch_policy"] = batch_diagnostics
        motion_preview_path = getattr(self.args, "motion_preview_path", "")
        motion_preview_before_marker = motion_preview_marker(motion_preview_path)
        motion_preview_before = load_motion_preview(motion_preview_path)
        ptz_submitted_monotonic_ns = time.monotonic_ns()
        if "first_ptz_submitted_monotonic_ns" not in self.request_telemetry:
            self.request_telemetry["first_ptz_submitted_monotonic_ns"] = ptz_submitted_monotonic_ns
            requested_ns = int(command.get("requested_monotonic_ns") or 0)
            if requested_ns > 0:
                self.request_telemetry["request_to_first_ptz_ms"] = round(
                    max(0.0, (ptz_submitted_monotonic_ns - requested_ns) / 1_000_000),
                    3,
                )
        attempted_specs = []
        results = []
        try:
            for spec in pulse_specs:
                attempted_specs.append(spec)
                results.append(post_pulse(
                    ptz_url(self.args.ptz_url, source),
                    spec["hardware_direction"],
                    spec["speed"],
                    spec["duration_ms"],
                    self.args.ptz_timeout,
                    spec["backend"],
                    spec["reset_native_session"],
                    getattr(self.args, "ptz_socket", ""),
                    source,
                ))
            result = results[-1] if results else {}
            ptz_acknowledged_monotonic_ns = time.monotonic_ns()
            if "first_ptz_acknowledged_monotonic_ns" not in self.request_telemetry:
                self.request_telemetry["first_ptz_acknowledged_monotonic_ns"] = ptz_acknowledged_monotonic_ns
                self.request_telemetry["first_ptz_round_trip_ms"] = round(
                    max(0.0, (ptz_acknowledged_monotonic_ns - ptz_submitted_monotonic_ns) / 1_000_000),
                    3,
                )
        except Exception as exc:
            spec = attempted_specs[-1]
            movement = spec["movement"]
            pulse_axis = spec["axis"]
            direction = spec["direction"]
            hardware_direction = spec["hardware_direction"]
            duration_ms = spec["duration_ms"]
            pulse_backend = spec["backend"]
            recovery_stage = spec["recovery_stage"]
            reset_native_session = spec["reset_native_session"]
            # The camera can accept a PTZ command and still time out while returning
            # its HTTP response. Count the attempt and verify the next detector frame
            # instead of exposing a premature terminal tool failure to the caller.
            self.cooldown_until = time.monotonic() + self.args.error_backoff
            pulse_count_before = self.pulse_count
            self.pulse_count += len(attempted_specs)
            self.record_dispatched_performance(attempted_specs)
            self.remember_movement_step(attempted_specs)
            self.begin_pulse_prediction(
                command,
                center,
                attempted_specs,
                marker[1] if marker[0] == "frame" else None,
                pulse_count_before,
                self.pulse_count,
                time.monotonic_ns(),
                submitted_monotonic_ns=ptz_submitted_monotonic_ns,
            )
            for attempted in attempted_specs:
                self.remember_pulse_axis(attempted["axis"])
            common["same_axis_pulses"] = self.same_axis_pulses
            common["last_pulse_axis"] = self.last_pulse_axis
            self.pending_pulse_offsets = {
                attempted["axis"]: float(attempted["movement"]["offset"][attempted["axis"]])
                for attempted in attempted_specs
            }
            self.pending_pulses = [
                {key: attempted[key] for key in ("axis", "direction", "hardware_direction", "duration_ms", "backend")}
                for attempted in attempted_specs
            ]
            self.pending_pulse_distance = next(iter(self.pending_pulse_offsets.values()))
            self.pending_pulse_axis = "+".join(self.pending_pulse_offsets)
            self.pending_pulse_direction = "+".join(item["hardware_direction"] for item in attempted_specs)
            self.pending_pulse_action_direction = "+".join(item["direction"] for item in attempted_specs)
            self.pending_pulse_duration_ms = sum(int(item["duration_ms"]) for item in attempted_specs)
            self.pending_pulse_backend = "+".join(item["backend"] for item in attempted_specs)
            self.pending_pulse_completed_monotonic_ns = time.monotonic_ns()
            common["pending_pulse_axis"] = self.pending_pulse_axis
            common["pending_pulse_direction"] = self.pending_pulse_direction
            common["pending_pulse_action_direction"] = self.pending_pulse_action_direction
            common["pending_pulse_duration_ms"] = self.pending_pulse_duration_ms
            common["pending_pulse_backend"] = self.pending_pulse_backend
            common["pending_pulses"] = list(self.pending_pulses)
            self.pending_motion_preview = motion_preview_before
            self.pending_motion_preview_marker = motion_preview_before_marker
            self.last_verification_preview_marker = None
            self.pending_verification_started_at = time.monotonic()
            self.verification_frames_seen = 0
            self.verification_best_motion = {}
            self.ptz_idle_verified = False
            self.center_samples.clear()
            self.publish(
                "verifying",
                command,
                warning=f"PTZ step response failed: {str(exc)[:400]}",
                message="PTZ response was not confirmed; checking the next camera frame before retrying.",
                direction=direction,
                hardware_direction=hardware_direction,
                duration_ms=duration_ms,
                speed=spec["speed"],
                pulse_count=self.pulse_count,
                backend=pulse_backend,
                recovery_stage=recovery_stage,
                reset_native_session=reset_native_session,
                ptz_submitted_monotonic_ns=ptz_submitted_monotonic_ns,
                ptz_response_failed_monotonic_ns=time.monotonic_ns(),
                **common,
            )
            return
        settle_seconds = float(self.args.settle_seconds)
        # Freshness is already enforced with the detector's monotonic timestamp,
        # so simple closed loop can consume the first genuinely post-pulse frame.
        self.cooldown_until = time.monotonic() + settle_seconds
        pulse_count_before = self.pulse_count
        self.pulse_count += len(pulse_specs)
        self.record_dispatched_performance(pulse_specs)
        self.remember_movement_step(pulse_specs)
        self.begin_pulse_prediction(
            command,
            center,
            pulse_specs,
            marker[1] if marker[0] == "frame" else None,
            pulse_count_before,
            self.pulse_count,
            ptz_acknowledged_monotonic_ns,
            submitted_monotonic_ns=ptz_submitted_monotonic_ns,
        )
        for spec in pulse_specs:
            self.remember_pulse_axis(spec["axis"])
        common["same_axis_pulses"] = self.same_axis_pulses
        common["last_pulse_axis"] = self.last_pulse_axis
        self.pending_pulse_offsets = {
            spec["axis"]: float(spec["movement"]["offset"][spec["axis"]])
            for spec in pulse_specs
        }
        self.pending_pulses = [
            {key: spec[key] for key in ("axis", "direction", "hardware_direction", "duration_ms", "backend")}
            for spec in pulse_specs
        ]
        self.pending_pulse_distance = next(iter(self.pending_pulse_offsets.values()))
        self.pending_pulse_axis = "+".join(self.pending_pulse_offsets)
        self.pending_pulse_direction = "+".join(spec["hardware_direction"] for spec in pulse_specs)
        self.pending_pulse_action_direction = "+".join(spec["direction"] for spec in pulse_specs)
        self.pending_pulse_duration_ms = sum(int(spec["duration_ms"]) for spec in pulse_specs)
        self.pending_pulse_backend = "+".join(spec["backend"] for spec in pulse_specs)
        self.pending_pulse_completed_monotonic_ns = ptz_acknowledged_monotonic_ns
        common["pending_pulse_axis"] = self.pending_pulse_axis
        common["pending_pulse_direction"] = self.pending_pulse_direction
        common["pending_pulse_action_direction"] = self.pending_pulse_action_direction
        common["pending_pulse_duration_ms"] = self.pending_pulse_duration_ms
        common["pending_pulse_backend"] = self.pending_pulse_backend
        common["pending_pulses"] = list(self.pending_pulses)
        self.pending_motion_preview = motion_preview_before
        self.pending_motion_preview_marker = motion_preview_before_marker
        self.last_verification_preview_marker = None
        self.pending_verification_started_at = time.monotonic()
        self.verification_frames_seen = 0
        self.verification_best_motion = {}
        self.ptz_idle_verified = bool(results) and all(ptz_result_confirms_idle(item) for item in results)
        if self.ptz_idle_verified:
            self.ptz_idle_verified_at = time.monotonic()
        self.center_samples.clear()
        self.publish(
            "focusing",
            command,
            message=f"Issued {' then '.join(spec['direction'] for spec in pulse_specs)} before resampling {label}.",
            direction="+".join(spec["direction"] for spec in pulse_specs),
            hardware_direction="+".join(spec["hardware_direction"] for spec in pulse_specs),
            duration_ms=sum(int(spec["duration_ms"]) for spec in pulse_specs),
            speed=max(int(spec["speed"]) for spec in pulse_specs),
            pulse_count=self.pulse_count,
            backend="+".join(str(item.get("backend") or spec["backend"]) for item, spec in zip(results, pulse_specs)),
            recovery_stage=max(int(spec["recovery_stage"]) for spec in pulse_specs),
            reset_native_session=any(bool(spec["reset_native_session"]) for spec in pulse_specs),
            ptz_submitted_monotonic_ns=ptz_submitted_monotonic_ns,
            ptz_acknowledged_monotonic_ns=ptz_acknowledged_monotonic_ns,
            ptz_round_trip_seconds=round(
                max(0.0, (ptz_acknowledged_monotonic_ns - ptz_submitted_monotonic_ns) / 1_000_000_000),
                4,
            ),
            **common,
        )

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.open_wake_socket()
        try:
            prewarm_motion_backend()
            self.configured_max_steps("wifi")
            readiness_deadline = time.monotonic() + max(0.0, float(getattr(self.args, "ptz_startup_ready_timeout", 3.0)))
            while time.monotonic() < readiness_deadline and not self.stop_event.is_set():
                readiness = query_ptz_idle(getattr(self.args, "ptz_socket", ""), "wifi", timeout=0.75)
                if readiness.get("ready") and readiness.get("idle_verified"):
                    self.ptz_idle_verified = True
                    self.ptz_idle_verified_at = time.monotonic()
                    break
                self.stop_event.wait(0.1)
            while not self.stop_event.is_set():
                try:
                    self.step()
                except Exception as exc:
                    command = read_json(self.args.command_json)
                    self.publish("error", command, error=str(exc)[:500])
                self.wait_for_wake(max(0.01, self.args.poll_interval))
            if self.pending_prediction:
                self.finish_pulse_prediction("controller_stopped")
            command = read_json(self.args.command_json)
            self.publish("stopped", command, message="Follow-object controller stopped.")
        finally:
            self.close_wake_socket()
        return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-json", default=str(PROJECT_ROOT / "webcam-focus-object-command.json"))
    parser.add_argument("--state-json", default=str(PROJECT_ROOT / "webcam-focus-object-state.json"))
    parser.add_argument("--wake-socket", default="/tmp/dgx-spark-focus-wake.sock")
    parser.add_argument("--detections-json", default=str(PROJECT_ROOT / "webcam-deepstream-yolo-coco.json"))
    parser.add_argument("--settings-json", default=str(PROJECT_ROOT / "webcam-deepstream-settings.json"))
    parser.add_argument(
        "--prediction-error-jsonl",
        default=str(PROJECT_ROOT / "webcam-focus-pulse-prediction-errors.jsonl"),
        help="Append-only persistent pulse prediction/observation event log",
    )
    parser.add_argument(
        "--direction-gain-json",
        default=str(PROJECT_ROOT / "webcam-focus-direction-gains.json"),
        help="Persistent learned left/right/up/down target-displacement gains",
    )
    parser.add_argument(
        "--gru-checkpoint",
        default=str(PROJECT_ROOT / "webcam-focus-gru-shadow.pt"),
        help="Trained shadow GRU checkpoint used when an axis selects GRU control",
    )
    parser.add_argument("--direction-gain-learning-rate", type=float, default=0.15)
    parser.add_argument("--prediction-max-aggregate-delta", type=float, default=0.35)
    parser.add_argument("--component-audio-settings-json", default=str(PROJECT_ROOT / "webcam-component-audio-settings.json"))
    parser.add_argument("--completion-chime-url", default="http://127.0.0.1:8090/voicechat-cue.wav?stage=focus_complete")
    parser.add_argument("--completion-chime-talk-url", default="http://127.0.0.1:8090/{source}-talk-audio")
    parser.add_argument("--completion-chime-timeout", type=float, default=6.0)
    parser.add_argument("--ptz-url", default="http://127.0.0.1:8090/{source}-ptz")
    parser.add_argument("--ptz-socket", default="/tmp/dgx-spark-focus-ptz.sock")
    parser.add_argument("--ptz-startup-ready-timeout", type=float, default=3.0)
    parser.add_argument("--snapshot-url", default="http://127.0.0.1:8090/{source}-snapshot.jpg")
    parser.add_argument("--snapshot-path", default=str(PROJECT_ROOT / "webcam-focus-object-verification.jpg"))
    parser.add_argument("--motion-preview-path", default=str(PROJECT_ROOT / "webcam-deepstream-synced-preview.jpg"))
    parser.add_argument("--snapshot-timeout", type=float, default=2.0)
    parser.add_argument("--poll-interval", type=float, default=0.10)
    parser.add_argument("--heartbeat-interval", type=float, default=1.0)
    parser.add_argument("--max-detection-age", type=float, default=2.0)
    parser.add_argument("--minimum-confidence", type=float, default=0.50)
    parser.add_argument("--deadzone-x", type=float, default=0.10)
    parser.add_argument("--deadzone-y", type=float, default=0.12)
    parser.add_argument("--required-stable-frames", type=int, default=2)
    parser.add_argument("--max-pulses", type=int, default=8)
    parser.add_argument("--max-no-progress-pulses", type=int, default=2)
    parser.add_argument("--max-axis-no-progress-pulses", type=int, default=3)
    parser.add_argument("--axis-fairness-pulses", type=int, default=2)
    parser.add_argument("--minimum-progress", type=float, default=0.015)
    parser.add_argument("--minimum-observed-motion", type=float, default=0.005)
    parser.add_argument("--minimum-global-motion", type=float, default=0.008)
    parser.add_argument("--minimum-phase-response", type=float, default=0.08)
    parser.add_argument("--motion-verification-timeout", type=float, default=0.8)
    parser.add_argument("--motion-verification-min-seconds", type=float, default=0.15)
    parser.add_argument("--motion-verification-frames", type=int, default=3)
    parser.add_argument("--minimum-controllable-x", type=float, default=0.16)
    parser.add_argument("--minimum-controllable-y", type=float, default=0.20)
    parser.add_argument("--motion-resolution-margin", type=float, default=0.01)
    parser.add_argument("--center-median-frames", type=int, default=3)
    parser.add_argument("--missing-target-frames", type=int, default=10)
    parser.add_argument("--missing-target-grace-seconds", type=float, default=0.0)
    parser.add_argument("--reacquisition-confirmation-frames", type=int, default=1)
    parser.add_argument("--max-target-random-search-moves", type=int, default=MAX_TARGET_RANDOM_SEARCH_MOVES)
    parser.add_argument("--idle-confirmation-frames", type=int, default=2)
    parser.add_argument("--ptz-idle-trust-seconds", type=float, default=30.0)
    parser.add_argument("--stability-distance", type=float, default=0.18)
    parser.add_argument("--min-pulse-ms", type=int, default=50)
    parser.add_argument("--max-pulse-ms", type=int, default=160)
    parser.add_argument("--speed", type=int, default=1)
    parser.add_argument("--vertical-speed", type=int, default=0)
    parser.add_argument(
        "--min-vertical-pulse-ms",
        type=int,
        default=0,
        help="Vertical pulse floor in milliseconds; 0 uses --min-pulse-ms",
    )
    parser.add_argument("--vertical-pulse-multiplier", type=float, default=1.0)
    parser.add_argument("--max-vertical-pulse-ms", type=int, default=0)
    parser.add_argument("--max-vertical-pulses-per-step", type=int, default=1)
    parser.add_argument("--vertical-completion-epsilon", type=float, default=0.002)
    parser.add_argument("--vertical-direction-probe-pulses", type=int, default=2)
    parser.add_argument("--adaptive-tilt-direction-probe", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--simple-closed-loop", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--post-pulse-view-settle-seconds", type=float, default=0.75)
    parser.add_argument("--simple-response-timeout", type=float, default=1.0)
    parser.add_argument("--simple-response-frames", type=int, default=2)
    parser.add_argument("--invert-tilt", action="store_true")
    parser.add_argument("--native-only-ptz", action="store_true")
    parser.add_argument("--settle-seconds", type=float, default=0.45)
    parser.add_argument("--error-backoff", type=float, default=2.0)
    parser.add_argument("--ptz-timeout", type=float, default=4.0)
    args = parser.parse_args()
    args.deadzone_x = max(0.01, min(0.45, args.deadzone_x))
    args.deadzone_y = max(0.01, min(0.45, args.deadzone_y))
    args.minimum_confidence = max(0.0, min(1.0, args.minimum_confidence))
    args.required_stable_frames = max(1, args.required_stable_frames)
    args.max_pulses = max(1, min(100, args.max_pulses))
    args.max_no_progress_pulses = max(1, min(100, args.max_no_progress_pulses))
    args.max_axis_no_progress_pulses = max(1, min(20, args.max_axis_no_progress_pulses))
    args.axis_fairness_pulses = max(1, min(10, args.axis_fairness_pulses))
    args.minimum_progress = max(0.001, min(0.25, args.minimum_progress))
    args.minimum_observed_motion = max(0.001, min(0.25, args.minimum_observed_motion))
    args.minimum_global_motion = max(0.001, min(0.25, args.minimum_global_motion))
    args.minimum_phase_response = max(0.0, min(1.0, args.minimum_phase_response))
    args.motion_verification_timeout = max(0.2, min(3.0, args.motion_verification_timeout))
    args.post_pulse_view_settle_seconds = max(0.0, min(5.0, args.post_pulse_view_settle_seconds))
    args.simple_response_timeout = max(0.2, min(10.0, args.simple_response_timeout))
    args.simple_response_frames = max(1, min(10, args.simple_response_frames))
    args.minimum_controllable_x = max(0.02, min(0.5, args.minimum_controllable_x))
    args.minimum_controllable_y = max(0.02, min(0.5, args.minimum_controllable_y))
    args.motion_resolution_margin = max(0.0, min(0.1, args.motion_resolution_margin))
    args.center_median_frames = max(1, min(9, args.center_median_frames))
    args.missing_target_frames = max(1, min(60, args.missing_target_frames))
    args.missing_target_grace_seconds = max(0.0, min(10.0, args.missing_target_grace_seconds))
    args.reacquisition_confirmation_frames = max(1, min(10, args.reacquisition_confirmation_frames))
    args.max_target_random_search_moves = max(0, min(10, args.max_target_random_search_moves))
    args.min_pulse_ms = max(30, min(1000, args.min_pulse_ms))
    args.max_pulse_ms = max(args.min_pulse_ms, min(1000, args.max_pulse_ms))
    args.speed = max(1, min(8, args.speed))
    args.vertical_speed = max(0, min(8, args.vertical_speed))
    args.vertical_pulse_multiplier = max(0.25, min(10.0, args.vertical_pulse_multiplier))
    args.max_vertical_pulse_ms = max(0, min(1000, args.max_vertical_pulse_ms))
    args.max_vertical_pulses_per_step = max(1, min(3, args.max_vertical_pulses_per_step))
    args.min_vertical_pulse_ms = max(0, min(args.min_pulse_ms, int(args.min_vertical_pulse_ms or 0)))
    args.vertical_completion_epsilon = max(0.0, min(0.02, args.vertical_completion_epsilon))
    args.vertical_direction_probe_pulses = max(1, min(20, args.vertical_direction_probe_pulses))
    args.direction_gain_learning_rate = max(0.01, min(0.5, args.direction_gain_learning_rate))
    args.prediction_max_aggregate_delta = max(0.02, min(0.75, args.prediction_max_aggregate_delta))
    args.snapshot_timeout = max(0.25, min(10.0, args.snapshot_timeout))
    return args


if __name__ == "__main__":
    raise SystemExit(FocusController(parse_args()).run())

#!/usr/bin/env python3
"""Exercise live preferred-object auto-centering after random UI-equivalent PTZ moves."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
import random
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


DIRECTIONS = ("left", "right", "up", "down")
TERMINAL_STATUSES = {"complete", "failed", "timed_out", "error", "cancelled", "controller_unavailable"}


def target_is_any(target: object) -> bool:
    return str(target or "").strip().lower() in {"", "any", "*"}


def target_matches(label: object, target: object) -> bool:
    return target_is_any(target) or str(label or "").strip().lower() == str(target or "").strip().lower()


def random_move_plan(seed: int, maximum_steps: int = 3,
                     directions: tuple[str, ...] = DIRECTIONS) -> list[str]:
    rng = random.Random(int(seed))
    step_count = rng.randint(1, max(1, int(maximum_steps)))
    allowed = tuple(direction for direction in directions if direction in DIRECTIONS) or DIRECTIONS
    return [rng.choice(allowed) for _ in range(step_count)]


def random_pulse_durations(seed: int, step_count: int, minimum_ms: int = 250,
                           maximum_ms: int = 500) -> list[int]:
    rng = random.Random(int(seed) ^ 0x5F3759DF)
    upper = max(30, min(1000, int(maximum_ms)))
    lower = max(30, min(upper, int(minimum_ms)))
    return [rng.randint(lower, upper) for _ in range(max(1, int(step_count)))]


def object_center(payload: dict, target_label: str) -> dict | None:
    wanted = " ".join(str(target_label or "").strip().lower().split())
    candidates = []
    for item in payload.get("objects", []):
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get("label") or item.get("class") or "").strip().lower().split())
        bbox = item.get("bbox")
        if (not target_is_any(wanted) and label != wanted) or not isinstance(bbox, list) or len(bbox) < 4:
            continue
        try:
            left, top, width, height = map(float, bbox[:4])
            frame_width = float(item.get("frame_width") or item.get("image_width") or 0)
            frame_height = float(item.get("frame_height") or item.get("image_height") or 0)
            confidence = float(item.get("confidence") or item.get("probability") or 0)
        except (TypeError, ValueError):
            continue
        if frame_width <= 0 or frame_height <= 0 or width <= 0 or height <= 0:
            continue
        candidate = {
            "x": (left + width / 2) / frame_width,
            "y": (top + height / 2) / frame_height,
            "confidence": confidence,
            "bbox": bbox[:4],
            "frame_size": [frame_width, frame_height],
        }
        if target_is_any(wanted):
            candidate.update({"label": label, "track_id": item.get("track_id")})
        candidates.append(candidate)
    return max(candidates, key=lambda item: item["confidence"], default=None)


def center_within_deadzone(center: dict | None, deadzone: dict | None) -> bool:
    if not center:
        return False
    tolerance = deadzone if isinstance(deadzone, dict) else {}
    try:
        dx = abs(float(center["x"]) - 0.5)
        dy = abs(float(center["y"]) - 0.5)
        return dx <= float(tolerance.get("x", 0.02)) and dy <= float(tolerance.get("y", 0.022))
    except (KeyError, TypeError, ValueError):
        return False


def focus_result_passed(status: object, *, centered: bool = True, target_matches: bool = True) -> bool:
    """Require both controller completion and independently observed centering accuracy."""
    return bool(
        str(status or "").strip().lower() == "complete"
        and centered
        and target_matches
    )


def iteration_numbers(iterations: int):
    """Yield one-based test numbers, forever only when iterations is -1."""
    if int(iterations) == -1:
        return itertools.count(1)
    return iter(range(1, max(1, int(iterations)) + 1))


def exception_details(error: Exception) -> dict:
    """Preserve useful failure details without allowing one test to end the suite."""
    message = str(error).strip() or repr(error)
    return {
        "error": message,
        "error_type": type(error).__name__,
    }


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


class LiveAutofocusClient:
    def __init__(self, base_url: str, command_path: Path, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.command_path = command_path
        self.timeout = float(timeout)

    def get_json(self, path: str) -> dict:
        with urlopen(f"{self.base_url}{path}", timeout=self.timeout) as response:
            value = json.load(response)
        return value if isinstance(value, dict) else {}

    def ui_pulse(self, direction: str, duration_ms: int, speed: int) -> dict:
        payload = json.dumps({
            "command": direction,
            "action": "pulse",
            "speed": int(speed),
            "duration_ms": int(duration_ms),
        }).encode("utf-8")
        request = Request(
            f"{self.base_url}/wifi-ptz",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(self.timeout, duration_ms / 1000 + 5)) as response:
                value = json.load(response)
        except HTTPError as error:
            try:
                value = json.loads(error.read().decode("utf-8"))
            except Exception:
                raise
            if isinstance(value, dict) and (value.get("dispatched") or value.get("ambiguous")):
                return {**value, "status": "ambiguous", "http_status": error.code}
            raise
        if not isinstance(value, dict) or value.get("status") == "error":
            raise RuntimeError(str(value.get("error") if isinstance(value, dict) else "invalid PTZ response"))
        return value

    def detections(self) -> dict:
        return self.get_json("/deepstream-detections.json?source=wifi")

    def focus_state(self) -> dict:
        return self.get_json("/focus-object-state.json")

    def focus_command(self) -> dict:
        return read_json(self.command_path)


def wait_until(description: str, timeout: float, poll_seconds: float, probe):
    deadline = time.monotonic() + max(0.1, float(timeout))
    last = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(max(0.02, float(poll_seconds)))
    raise TimeoutError(f"Timed out waiting for {description}; last observation: {last!r}")


def quiescent_focus_snapshot(command: dict, state: dict, center: dict | None, target: str) -> dict | None:
    status = str(state.get("status") or "").lower()
    command_request_id = str(command.get("request_id") or "")
    state_request_id = str(state.get("request_id") or "")
    deadzone = state.get("effective_deadzone") if isinstance(state.get("effective_deadzone"), dict) else {"x": 0.02, "y": 0.022}
    if command.get("enabled") or state.get("enabled"):
        return None
    if status not in TERMINAL_STATUSES:
        return None
    if command_request_id and state_request_id and command_request_id != state_request_id:
        return None
    actual_target = str(state.get("target_label") or (center or {}).get("label") or "").strip().lower()
    if not actual_target or not target_matches(actual_target, target):
        return None
    controller_center = state.get("object_center") if isinstance(state.get("object_center"), dict) else None
    if center is None or not center_within_deadzone(controller_center or center, deadzone):
        return None
    return {
        "request_id": state_request_id or command_request_id,
        "status": status,
        "center": controller_center or center,
        "deadzone": deadzone,
        "target": actual_target,
        "target_track_id": state.get("target_track_id") or (center or {}).get("track_id"),
    }


def wait_for_focus_quiescence(client: LiveAutofocusClient, target: str, *, timeout: float,
                              poll_seconds: float, quiet_seconds: float, phase: str = "inter_test") -> dict:
    """Require one unchanged, inactive, centered focus state for a continuous window."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    stable_since = 0.0
    stable_request_id = ""
    last = None
    while time.monotonic() < deadline:
        command = client.focus_command()
        state = client.focus_state()
        detections = client.detections()
        state_target = str(state.get("target_label") or target)
        center = object_center(detections, state_target if target_is_any(target) else target)
        current = quiescent_focus_snapshot(command, state, center, target)
        now = time.monotonic()
        if current is None:
            stable_since = 0.0
            stable_request_id = ""
        else:
            request_id = str(current.get("request_id") or "")
            if request_id != stable_request_id:
                stable_request_id = request_id
                stable_since = now
            elif stable_since and now - stable_since >= max(0.0, float(quiet_seconds)):
                return {**current, "quiet_seconds": round(now - stable_since, 3)}
        last = {
            "command_enabled": bool(command.get("enabled")),
            "command_request_id": command.get("request_id"),
            "state_enabled": bool(state.get("enabled")),
            "state_status": state.get("status"),
            "state_request_id": state.get("request_id"),
            "center": center,
        }
        time.sleep(max(0.02, float(poll_seconds)))
    raise TimeoutError(f"Timed out waiting for autofocus quiescence; last observation: {last!r}")


def wait_for_autofocus_idle(client: LiveAutofocusClient, *, timeout: float,
                            poll_seconds: float, quiet_seconds: float) -> dict:
    """Require all autofocus activity to remain unchanged and inactive before another test."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    stable_since = 0.0
    stable_signature = None
    last = None
    while time.monotonic() < deadline:
        command = client.focus_command()
        state = client.focus_state()
        now = time.monotonic()
        signature = (
            str(command.get("request_id") or ""),
            str(state.get("request_id") or ""),
            str(state.get("status") or "").lower(),
        )
        active = bool(command.get("enabled")) or bool(state.get("enabled"))
        if active:
            stable_since = 0.0
            stable_signature = None
        elif signature != stable_signature:
            stable_signature = signature
            stable_since = now
        elif now - stable_since >= max(0.0, float(quiet_seconds)):
            return {
                "command_request_id": signature[0],
                "state_request_id": signature[1],
                "state_status": signature[2],
                "quiet_seconds": round(now - stable_since, 3),
            }
        last = {
            "command_enabled": bool(command.get("enabled")),
            "command_request_id": command.get("request_id"),
            "state_enabled": bool(state.get("enabled")),
            "state_request_id": state.get("request_id"),
            "state_status": state.get("status"),
            "quiet_seconds": round(now - stable_since, 3) if stable_since else 0.0,
        }
        time.sleep(max(0.02, float(poll_seconds)))
    raise TimeoutError(f"Timed out waiting for autofocus to become idle between tests; last observation: {last!r}")


def wait_for_logical_autofocus(client: LiveAutofocusClient, target: str, *, baseline_request_id: str,
                               autofocus_armed_at: float, timeout: float, poll_seconds: float,
                               quiet_seconds: float) -> tuple[dict, list[str]]:
    """Follow replacement/follow-up request IDs until the whole autofocus chain is quiet."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    tracked_request_ids: list[str] = []
    terminal: dict | None = None
    quiet_since = 0.0
    last = None
    while time.monotonic() < deadline:
        command = client.focus_command()
        state = client.focus_state()
        now = time.monotonic()
        command_request_id = str(command.get("request_id") or "")
        command_matches = bool(
            command_request_id
            and command_request_id != baseline_request_id
            and target_matches(command.get("target_label"), target)
            and str(command.get("trigger") or "") == "realtime_deepstream_focus"
            and float(command.get("requested_at") or 0.0) >= autofocus_armed_at
        )
        if command_matches and command_request_id not in tracked_request_ids:
            tracked_request_ids.append(command_request_id)
            terminal = None
            quiet_since = 0.0

        state_request_id = str(state.get("request_id") or "")
        state_status = str(state.get("status") or "").lower()
        state_is_tracked = state_request_id in tracked_request_ids
        active = bool(command_matches and command.get("enabled")) or bool(state_is_tracked and state.get("enabled"))
        if state_is_tracked and state_status in TERMINAL_STATUSES and not state.get("enabled"):
            terminal = state
        if active or terminal is None:
            quiet_since = 0.0
        elif quiet_since <= 0.0:
            quiet_since = now
        elif now - quiet_since >= max(0.0, float(quiet_seconds)):
            return terminal, tracked_request_ids

        last = {
            "tracked_request_ids": list(tracked_request_ids),
            "command_request_id": command_request_id,
            "command_enabled": bool(command.get("enabled")),
            "state_request_id": state_request_id,
            "state_status": state_status,
            "state_enabled": bool(state.get("enabled")),
            "quiet_seconds": round(now - quiet_since, 3) if quiet_since else 0.0,
        }
        time.sleep(max(0.02, float(poll_seconds)))
    raise TimeoutError(f"Timed out waiting for the logical autofocus chain; last observation: {last!r}")


def wait_for_first_complete_event(client: LiveAutofocusClient, target: str, *, after_at: float,
                                  baseline_request_ids: set[str], timeout: float,
                                  poll_seconds: float,
                                  stop_event: threading.Event | None = None) -> tuple[dict, list[str]]:
    """Return the first terminal event for the requested target after pull-away begins."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    observed_request_ids: list[str] = []
    last = None
    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("Completion watcher stopped")
        state = client.focus_state()
        command = client.focus_command() if target_is_any(target) else {}
        history = state.get("history") if isinstance(state.get("history"), list) else []
        events = []
        for item in history:
            if not isinstance(item, dict):
                continue
            request_id = str(item.get("request_id") or "")
            try:
                completed_at = float(item.get("completed_at") or 0.0)
            except (TypeError, ValueError):
                completed_at = 0.0
            if (
                not request_id
                or request_id in baseline_request_ids
                or completed_at < float(after_at)
                or not target_matches(item.get("target_label"), target)
            ):
                continue
            events.append((completed_at, item))
        for _completed_at, item in sorted(events, key=lambda value: value[0]):
            request_id = str(item.get("request_id") or "")
            if request_id not in observed_request_ids:
                observed_request_ids.append(request_id)
            if str(item.get("status") or "").lower() in TERMINAL_STATUSES:
                return item, observed_request_ids
        if target_is_any(target) and time.time() >= float(after_at) + 0.25:
            state_target = str(state.get("target_label") or "").strip().lower()
            state_center = object_center(client.detections(), state_target) if state_target else None
            state_deadzone = (
                state.get("effective_deadzone")
                if isinstance(state.get("effective_deadzone"), dict)
                else {"x": 0.02, "y": 0.022}
            )
            try:
                manual_pause_until = float(command.get("manual_ptz_pause_until") or 0.0)
            except (TypeError, ValueError):
                manual_pause_until = 0.0
            if (
                str(state.get("status") or "").lower() == "complete"
                and not state.get("enabled")
                and not command.get("manual_ptz_active")
                and time.time() >= manual_pause_until + 0.1
                and state_target
                and center_within_deadzone(state_center, state_deadzone)
            ):
                request_id = str(state.get("request_id") or command.get("request_id") or "")
                if request_id and request_id not in observed_request_ids:
                    observed_request_ids.append(request_id)
                return {
                    **state,
                    "status": "complete",
                    "target_label": state_target,
                    "object_center": state_center,
                    "effective_deadzone": state_deadzone,
                    "message": "Selected preferred object remained centered after pull-away.",
                }, observed_request_ids
        current_request_id = str(state.get("request_id") or "")
        if (
            current_request_id
            and current_request_id not in baseline_request_ids
            and current_request_id not in observed_request_ids
        ):
            observed_request_ids.append(current_request_id)
        last = {
            "observed_request_ids": list(observed_request_ids),
            "state_request_id": current_request_id,
            "state_status": state.get("status"),
            "state_target": state.get("target_label"),
        }
        time.sleep(max(0.02, float(poll_seconds)))
    raise TimeoutError(f"Timed out waiting for the first complete autofocus event; last observation: {last!r}")


def run_iteration(client: LiveAutofocusClient, *, iteration: int, seed: int, target: str,
                  steps: int, minimum_duration_ms: int, maximum_duration_ms: int,
                  speed: int, autofocus_timeout: float,
                  poll_seconds: float, pull_away_settle_seconds: float,
                  total_iterations: int | str,
                  directions: tuple[str, ...] = DIRECTIONS) -> dict:
    plan = random_move_plan(seed, steps, directions)
    pulse_durations = random_pulse_durations(seed, len(plan), minimum_duration_ms, maximum_duration_ms)
    print(
        f"Test {iteration}/{total_iterations}: starting "
        f"(pull-away steps: {len(plan)}/{max(1, int(steps))})",
        flush=True,
    )
    baseline_payload = client.detections()
    if target_is_any(target):
        idle = wait_for_autofocus_idle(
            client,
            timeout=autofocus_timeout,
            poll_seconds=poll_seconds,
            quiet_seconds=min(0.2, max(0.05, pull_away_settle_seconds)),
        )
        selected_state = client.focus_state()
        selected_label = str(selected_state.get("target_label") or "").strip().lower()
        baseline = wait_until(
            "a detectable object from the realtime priority list",
            autofocus_timeout,
            poll_seconds,
            lambda: object_center(client.detections(), selected_label or target),
        )
        baseline_quiescence = {
            **idle,
            "target": baseline.get("label"),
            "target_track_id": baseline.get("track_id"),
            "center": baseline,
        }
    else:
        baseline_quiescence = wait_for_focus_quiescence(
            client,
            target,
            timeout=autofocus_timeout,
            poll_seconds=poll_seconds,
            quiet_seconds=min(0.2, max(0.05, pull_away_settle_seconds)),
            phase="pre_test",
        )
        baseline_payload = client.detections()
        baseline = object_center(baseline_payload, target)
    baseline_state = client.focus_state()
    baseline_history = baseline_state.get("history") if isinstance(baseline_state.get("history"), list) else []
    baseline_request_ids = {
        str(item.get("request_id") or "")
        for item in baseline_history
        if isinstance(item, dict) and item.get("request_id")
    }
    baseline_request_ids.update(
        request_id
        for request_id in (
            str(baseline_state.get("request_id") or ""),
            str(client.focus_command().get("request_id") or ""),
        )
        if request_id
    )
    started_at = time.time()
    first_pull_away_at = time.time()
    responses = []
    watcher_stop = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autofocus-completion")
    completion_future = executor.submit(
        wait_for_first_complete_event,
        client,
        target,
        after_at=first_pull_away_at,
        baseline_request_ids=baseline_request_ids,
        timeout=autofocus_timeout,
        poll_seconds=poll_seconds,
        stop_event=watcher_stop,
    )
    try:
        for index, (direction, duration_ms) in enumerate(zip(plan, pulse_durations), 1):
            if completion_future.done():
                break
            pulse_started_monotonic = time.monotonic()
            print(
                f"Test {iteration}/{total_iterations}: step {index}/{len(plan)} - "
                f"moving {direction} {duration_ms}ms",
                flush=True,
            )
            response = client.ui_pulse(direction, duration_ms, speed)
            pulse_runtime = round(time.monotonic() - pulse_started_monotonic, 3)
            responses.append({
                "step": index,
                "direction": direction,
                "backend": response.get("backend"),
                "duration_ms": response.get("duration_ms", duration_ms),
                "status": response.get("status"),
                "request_runtime_seconds": pulse_runtime,
            })
            settle_deadline = time.monotonic() + max(0.0, float(pull_away_settle_seconds))
            while not completion_future.done() and time.monotonic() < settle_deadline:
                time.sleep(min(0.05, max(0.0, settle_deadline - time.monotonic())))
            if completion_future.done() and index < len(plan):
                print(
                    f"Test {iteration}/{total_iterations}: complete event detected - stopping pull-away",
                    flush=True,
                )
                break
        print(f"Test {iteration}/{total_iterations}: waiting for auto-focus to complete", flush=True)
        completion = None
        request_chain = []
        if target_is_any(target) and not completion_future.done():
            current_state = client.focus_state()
            current_target = str(current_state.get("target_label") or "").strip().lower()
            current_center = object_center(client.detections(), current_target) if current_target else None
            current_deadzone = (
                current_state.get("effective_deadzone")
                if isinstance(current_state.get("effective_deadzone"), dict)
                else {"x": 0.02, "y": 0.022}
            )
            if (
                str(current_state.get("status") or "").lower() == "complete"
                and not current_state.get("enabled")
                and current_target
                and center_within_deadzone(current_center, current_deadzone)
            ):
                request_id = str(current_state.get("request_id") or "")
                completion = {
                    **current_state,
                    "status": "complete",
                    "target_label": current_target,
                    "message": "Selected preferred object remained centered after pull-away.",
                }
                request_chain = [request_id] if request_id else []
        if completion is None:
            completion, request_chain = completion_future.result()
    finally:
        watcher_stop.set()
        executor.shutdown(wait=True, cancel_futures=True)
    completion_target = str(completion.get("target_label") or "").strip().lower()
    selected_target = completion_target if target_is_any(target) else target
    displaced = object_center(client.detections(), selected_target)
    request_id = str(completion.get("request_id") or (request_chain[-1] if request_chain else ""))
    terminal = client.focus_state()
    final_payload = client.detections()
    final_center = (
        completion.get("object_center")
        if isinstance(completion.get("object_center"), dict)
        else object_center(final_payload, selected_target)
    )
    deadzone = (
        completion.get("effective_deadzone")
        if isinstance(completion.get("effective_deadzone"), dict)
        else terminal.get("effective_deadzone")
        if isinstance(terminal.get("effective_deadzone"), dict)
        else {"x": 0.02, "y": 0.022}
    )
    status = str(completion.get("status") or "").lower()
    centered = center_within_deadzone(final_center, deadzone)
    completion_matches = bool(completion_target) and target_matches(completion_target, target)
    result = {
        "iteration": iteration,
        "seed": seed,
        "target": selected_target,
        "requested_target": target or "any",
        "directions": [item["direction"] for item in responses],
        "pull_away_steps": len(responses),
        "pull_away_steps_sampled": len(plan),
        "pull_away_steps_max": max(1, int(steps)),
        "ui_pulse_ms": max(30, min(1000, int(maximum_duration_ms))),
        "ui_pulse_durations_ms": [int(item["duration_ms"]) for item in responses],
        "ui_pulse_ms_min": max(30, min(1000, int(minimum_duration_ms))),
        "ui_pulse_ms_max": max(30, min(1000, int(maximum_duration_ms))),
        "ui_speed": speed,
        "pull_away_settle_seconds": pull_away_settle_seconds,
        "realtime_focus_enabled_during_pull_away": True,
        "pull_away_stopped_on_complete": len(responses) < len(plan),
        "baseline_center": baseline,
        "baseline_quiescence": baseline_quiescence,
        "displaced_center": displaced,
        "final_center": final_center,
        "effective_deadzone": deadzone,
        "focus_request_id": request_id,
        "focus_request_chain": request_chain,
        "completion_target": completion_target,
        "completion_track_id": completion.get("target_track_id"),
        "focus_status": status,
        "controller_completed": status == "complete",
        "focus_steps": int(completion.get("pulse_count") or 0),
        "focus_runtime_seconds": completion.get("runtime_seconds"),
        "centered": centered,
        "target_matches": completion_matches,
        "passed": focus_result_passed(status, centered=centered, target_matches=completion_matches),
        "ptz_responses": responses,
        "message": completion.get("message"),
        "completion_event": completion,
        "started_at": started_at,
        "completed_at": time.time(),
    }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Required acknowledgement that this physically moves the camera")
    parser.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Number of tests to run; use -1 to run until interrupted (default: 1)",
    )
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument(
        "--pull-away-steps",
        "--steps",
        dest="pull_away_steps",
        type=int,
        default=3,
        help="Maximum random UI direction pulses; each test uniformly chooses 1..max (default: 3)",
    )
    parser.add_argument(
        "--target",
        default="any",
        help="Optional exact label override; default accepts the object selected from the realtime priority list.",
    )
    parser.add_argument(
        "--directions",
        default=",".join(DIRECTIONS),
        help="Comma-separated pull-away directions selected by the seeded plan (default: left,right,up,down)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--command-path", type=Path, default=Path("/dev/shm/dgx-spark-focus-command.json"))
    parser.add_argument(
        "--max-ui-pulse-ms",
        "--ui-pulse-ms",
        dest="max_ui_pulse_ms",
        type=int,
        default=500,
        help="Maximum random pull-away pulse duration, capped at 1000 ms (default: 500)",
    )
    parser.add_argument(
        "--min-ui-pulse-ms",
        type=int,
        default=250,
        help="Minimum random pull-away pulse duration (default: 250)",
    )
    parser.add_argument("--ui-speed", type=int, default=1)
    parser.add_argument(
        "--pull-away-settle-seconds",
        type=float,
        default=0.25,
        help="Additional settle time after each completed pull-away pulse (default: 0.25)",
    )
    parser.add_argument(
        "--test-timeout",
        "--autofocus-timeout",
        dest="autofocus_timeout",
        type=float,
        default=20.0,
        help="Maximum seconds to wait for each test's autofocus run to start and finish (default: 20)",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.1)
    parser.add_argument(
        "--inter-test-quiet-seconds",
        type=float,
        default=3.0,
        help="Continuous inactive time required before the next test starts (default: 3)",
    )
    parser.add_argument(
        "--delay-between-tests",
        type=float,
        default=0.0,
        help="Additional delay in seconds before starting the next test (default: 0)",
    )
    parser.add_argument("--results", type=Path, default=Path("autofocus-live-results.jsonl"))
    parser.add_argument("--append-results", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.execute:
        raise SystemExit("Refusing to move the camera without --execute")
    client = LiveAutofocusClient(args.base_url, args.command_path)
    requested_directions = tuple(
        item.strip().lower() for item in str(args.directions).split(",")
        if item.strip().lower() in DIRECTIONS
    )
    if not requested_directions:
        raise SystemExit("--directions must contain at least one of: left,right,up,down")
    args.results.parent.mkdir(parents=True, exist_ok=True)
    failures = 0
    run_indefinitely = args.iterations == -1
    requested_iterations = -1 if run_indefinitely else max(1, args.iterations)
    total_label: int | str = "∞" if run_indefinitely else requested_iterations
    with args.results.open("a" if args.append_results else "w", encoding="utf-8") as output:
        for iteration in iteration_numbers(args.iterations):
            iteration_seed = args.seed + iteration - 1
            test_started_monotonic = time.monotonic()
            try:
                result = run_iteration(
                    client,
                    iteration=iteration,
                    seed=iteration_seed,
                    target=args.target,
                    steps=max(1, args.pull_away_steps),
                    minimum_duration_ms=max(30, min(1000, args.min_ui_pulse_ms)),
                    maximum_duration_ms=max(
                        max(30, min(1000, args.min_ui_pulse_ms)),
                        min(1000, args.max_ui_pulse_ms),
                    ),
                    speed=max(1, min(8, args.ui_speed)),
                    autofocus_timeout=max(2.0, args.autofocus_timeout),
                    poll_seconds=max(0.02, args.poll_seconds),
                    pull_away_settle_seconds=max(0.0, args.pull_away_settle_seconds),
                    total_iterations=total_label,
                    directions=requested_directions,
                )
            except Exception as error:
                result = {
                    "iteration": iteration,
                    "seed": iteration_seed,
                    "target": args.target,
                    "passed": False,
                    **exception_details(error),
                    "completed_at": time.time(),
                }
            has_next_test = run_indefinitely or iteration < requested_iterations
            if has_next_test and not target_is_any(args.target):
                try:
                    result["inter_test_idle"] = wait_for_autofocus_idle(
                        client,
                        timeout=max(
                            2.0,
                            float(args.autofocus_timeout),
                            float(args.inter_test_quiet_seconds) + 1.0,
                        ),
                        poll_seconds=max(0.02, args.poll_seconds),
                        quiet_seconds=max(0.0, args.inter_test_quiet_seconds),
                    )
                except Exception as idle_error:
                    result["passed"] = False
                    idle_details = exception_details(idle_error)
                    result["inter_test_idle_error"] = idle_details["error"]
                    result["inter_test_idle_error_type"] = idle_details["error_type"]
                    result["error"] = (
                        f"{result.get('error')}; {idle_details['error']}"
                        if result.get("error") else idle_details["error"]
                    )
            elif has_next_test:
                result["inter_test_idle"] = {"deferred_to_next_targetless_baseline": True}
            test_runtime_seconds = round(time.monotonic() - test_started_monotonic, 3)
            result["test_runtime_seconds"] = test_runtime_seconds
            failures += 0 if result.get("passed") else 1
            output.write(json.dumps(result, sort_keys=True) + "\n")
            output.flush()
            if result.get("passed"):
                print(f"Test {iteration}/{total_label}: complete ({test_runtime_seconds:.3f}s)", flush=True)
            else:
                print(
                    f"Test {iteration}/{total_label}: failed ({test_runtime_seconds:.3f}s) - "
                    f"{result.get('error') or result.get('message') or 'auto-focus did not complete'}",
                    flush=True,
                )
            if has_next_test and args.delay_between_tests > 0:
                print(
                    f"Waiting {args.delay_between_tests:g}s before the next test",
                    flush=True,
                )
                time.sleep(args.delay_between_tests)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAutofocus live suite interrupted by user.", flush=True)
        raise SystemExit(130)

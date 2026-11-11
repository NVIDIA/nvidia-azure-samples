# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Scan pan walls by visual no-change detection and move the camera to center."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_ops.client import CameraPtzClient, PtzError, clamp_speed
from camera_ops.pan import build_payload, build_timed_payloads, ptz_url
from camera_ops.state import PanState, clear_walls, default_state_path, load_state, record_step, save_state, state_summary
from camera_ops.vision import fetch_snapshot, mean_absolute_luma_delta, snapshot_url


class CenteringError(RuntimeError):
    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


def opposite_direction(direction: str) -> str:
    if direction == "left":
        return "right"
    if direction == "right":
        return "left"
    raise ValueError("direction must be left or right")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan both pan walls and center the local IP camera.")
    parser.add_argument(
        "--max-steps-per-sweep",
        type=int,
        default=80,
        help="Safety cap for each wall scan. The scan stops earlier when no visual change is detected.",
    )
    parser.add_argument("--steps", type=int, default=None, help="Alias for --max-steps-per-sweep.")
    parser.add_argument("--first-direction", choices=("left", "right"), default="left", help="Direction to scan first.")
    parser.add_argument("--step-ms", type=int, default=1000, help="Motor-on duration for each pulse step in milliseconds.")
    parser.add_argument("--duration-ms", type=int, default=None, help="Alias for --step-ms.")
    parser.add_argument("--settle-ms", type=int, default=3000, help="Pause between movement steps in milliseconds. Defaults to 3000.")
    parser.add_argument("--snapshot-delay-ms", type=int, default=500, help="Delay after each pulse before comparing snapshots.")
    parser.add_argument("--wall-threshold", type=float, default=2.0, help="Mean luma delta at or below this value counts as no visual change.")
    parser.add_argument("--speed", type=int, default=1, help="PTZ speed, clamped to 1..8.")
    parser.add_argument("--timeout", type=float, default=8.0, help="HTTP timeout for PTZ and snapshot requests.")
    parser.add_argument(
        "--pulse-mode",
        choices=("timed", "server"),
        default="timed",
        help="Use explicit start/sleep/stop pulses or the server's pulse action. Defaults to timed.",
    )
    parser.add_argument("--source", choices=("wifi", "bulb"), default="wifi", help="Camera source endpoint.")
    parser.add_argument("--server-url", default="http://127.0.0.1:8090", help="Local webcam server base URL.")
    parser.add_argument("--url", default=None, help="Full PTZ endpoint URL. Overrides --server-url and --source.")
    parser.add_argument("--snapshot-url", default=None, help="Full snapshot URL for visual change detection.")
    parser.add_argument("--state-path", default=str(default_state_path()), help="Path to persisted pan wall state.")
    parser.add_argument("--keep-state", action="store_true", help="Start from existing tracked state instead of resetting to zero.")
    parser.add_argument("--dry-run", action="store_true", help="Print the scan plan without moving the camera.")
    parser.add_argument("--json", action="store_true", help="Print full JSON output.")
    return parser


def planned_payload(command: str, speed: int, duration_ms: int, pulse_mode: str) -> dict | list[dict]:
    if pulse_mode == "server":
        return build_payload(command, speed, max(30, min(1000, duration_ms)))
    return build_timed_payloads(command, speed)


def send_measured_step(
    *,
    client: CameraPtzClient,
    state: PanState,
    snap_url: str,
    direction: str,
    speed: int,
    duration_ms: int,
    timeout: float,
    pulse_mode: str,
    snapshot_delay_ms: int,
    wall_threshold: float,
) -> dict:
    before_position = state.position_steps
    before_snapshot = fetch_snapshot(snap_url, timeout=timeout)
    try:
        if pulse_mode == "server":
            response = client.pulse(command=direction, speed=speed, duration_ms=duration_ms)
        else:
            response = client.timed_pulse(command=direction, speed=speed, duration_ms=duration_ms)
    except PtzError as exc:
        raise CenteringError(str(exc)) from exc

    if snapshot_delay_ms > 0:
        time.sleep(snapshot_delay_ms / 1000.0)
    after_snapshot = fetch_snapshot(snap_url, timeout=timeout)
    delta = mean_absolute_luma_delta(before_snapshot, after_snapshot)
    changed = delta > wall_threshold
    if changed:
        record_step(state, direction)

    return {
        "direction": direction,
        "status": "changed" if changed else "no_change",
        "changed": changed,
        "pulse_mode": pulse_mode,
        "requested_duration_ms": duration_ms,
        "motor_on_ms": duration_ms,
        "effective_command_duration_ms": response.duration_ms,
        "post_stop_snapshot_delay_ms": snapshot_delay_ms,
        "visual_delta": round(delta, 3),
        "wall_threshold": wall_threshold,
        "position_before": before_position,
        "position_after": state.position_steps,
        "response": response.raw,
    }


def scan_until_no_change(
    *,
    client: CameraPtzClient,
    state: PanState,
    snap_url: str,
    direction: str,
    max_steps: int,
    speed: int,
    duration_ms: int,
    timeout: float,
    pulse_mode: str,
    snapshot_delay_ms: int,
    settle_ms: int,
    wall_threshold: float,
    min_changed_steps_before_wall: int = 0,
) -> dict:
    records = []
    changed_steps = 0
    ignored_no_change_steps = 0
    min_changed_steps_before_wall = max(0, int(min_changed_steps_before_wall))
    for index in range(max_steps):
        record = send_measured_step(
            client=client,
            state=state,
            snap_url=snap_url,
            direction=direction,
            speed=speed,
            duration_ms=duration_ms,
            timeout=timeout,
            pulse_mode=pulse_mode,
            snapshot_delay_ms=snapshot_delay_ms,
            wall_threshold=wall_threshold,
        )
        record["scan_step"] = index + 1
        if record["changed"]:
            changed_steps += 1
        else:
            if changed_steps >= min_changed_steps_before_wall:
                records.append(record)
                return {
                    "direction": direction,
                    "status": "wall_detected",
                    "requested_max_steps": max_steps,
                    "min_changed_steps_before_wall": min_changed_steps_before_wall,
                    "changed_steps": changed_steps,
                    "ignored_no_change_steps": ignored_no_change_steps,
                    "no_change_step": index + 1,
                    "wall_position": state.position_steps,
                    "records": records,
                }
            ignored_no_change_steps += 1
            record["status"] = "no_change_ignored_waiting_for_motion"
            record["ignored_reason"] = (
                f"waiting for at least {min_changed_steps_before_wall} changed step(s) before accepting a wall"
            )
        records.append(record)
        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)

    if changed_steps < min_changed_steps_before_wall:
        raise CenteringError(
            f"{direction} sweep did not observe movement within {max_steps} steps; "
            f"saw {changed_steps} changed step(s), need {min_changed_steps_before_wall}",
            {
                "direction": direction,
                "status": "no_motion_detected",
                "requested_max_steps": max_steps,
                "min_changed_steps_before_wall": min_changed_steps_before_wall,
                "changed_steps": changed_steps,
                "ignored_no_change_steps": ignored_no_change_steps,
                "wall_position": state.position_steps,
                "records": records,
            },
        )
    raise CenteringError(
        f"{direction} wall was not detected within {max_steps} steps",
        {
            "direction": direction,
            "status": "safety_cap_reached",
            "requested_max_steps": max_steps,
            "min_changed_steps_before_wall": min_changed_steps_before_wall,
            "changed_steps": changed_steps,
            "ignored_no_change_steps": ignored_no_change_steps,
            "wall_position": state.position_steps,
            "records": records,
        },
    )


def move_steps(
    *,
    client: CameraPtzClient,
    state: PanState,
    snap_url: str,
    direction: str,
    steps: int,
    speed: int,
    duration_ms: int,
    timeout: float,
    pulse_mode: str,
    snapshot_delay_ms: int,
    settle_ms: int,
    wall_threshold: float,
) -> list[dict]:
    records = []
    for index in range(steps):
        record = send_measured_step(
            client=client,
            state=state,
            snap_url=snap_url,
            direction=direction,
            speed=speed,
            duration_ms=duration_ms,
            timeout=timeout,
            pulse_mode=pulse_mode,
            snapshot_delay_ms=snapshot_delay_ms,
            wall_threshold=wall_threshold,
        )
        record["center_step"] = index + 1
        records.append(record)
        if not record["changed"]:
            raise CenteringError(f"no visual change while moving to center at position {record['position_before']}")
        if settle_ms > 0 and index < steps - 1:
            time.sleep(settle_ms / 1000.0)
    return records


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        max_steps = max(1, int(args.steps if args.steps is not None else args.max_steps_per_sweep))
        duration_ms_source = args.duration_ms if args.duration_ms is not None else args.step_ms
        duration_ms = max(1, min(1000, int(duration_ms_source)))
        settle_ms = max(0, min(10000, int(args.settle_ms)))
        snapshot_delay_ms = max(0, min(10000, int(args.snapshot_delay_ms)))
        wall_threshold = max(0.0, float(args.wall_threshold))
        speed = clamp_speed(args.speed)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))

    first_direction = args.first_direction
    reverse_direction = opposite_direction(first_direction)
    control_url = ptz_url(args.server_url, args.source, args.url)
    snap_url = snapshot_url(args.server_url, args.source, args.snapshot_url)
    state_path = Path(args.state_path).expanduser()
    state = load_state(state_path, args.source)
    if not args.keep_state:
        state.position_steps = 0
        clear_walls(state)

    scan_plan = {
        "first_scan": {
            "direction": first_direction,
            "max_steps": max_steps,
            "min_changed_steps_before_wall": 0,
        },
        "reverse_scan": {
            "direction": reverse_direction,
            "max_steps": max_steps,
            "min_changed_steps_before_wall": 1,
        },
        "center": {"method": "move half of changed reverse-scan steps back toward the first wall"},
    }

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "url": control_url,
                    "snapshot_url": snap_url,
                    "state_path": str(state_path),
                    "source": args.source,
                    "step_ms": duration_ms,
                    "motor_on_ms_per_step": duration_ms,
                    "server_pulse_step_ms": max(30, min(1000, duration_ms)),
                    "post_stop_snapshot_delay_ms": snapshot_delay_ms,
                    "settle_ms_between_steps": settle_ms,
                    "pulse_mode": args.pulse_mode,
                    "wall_threshold": wall_threshold,
                    "initial_state": state_summary(state),
                    "scan_plan": scan_plan,
                    "first_payload": planned_payload(first_direction, speed, duration_ms, args.pulse_mode),
                    "reverse_payload": planned_payload(reverse_direction, speed, duration_ms, args.pulse_mode),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    save_state(state_path, state)
    client = CameraPtzClient(url=control_url, timeout=args.timeout)
    result = {
        "status": "ok",
        "url": control_url,
        "snapshot_url": snap_url,
        "state_path": str(state_path),
        "source": args.source,
        "step_ms": duration_ms,
        "motor_on_ms_per_step": duration_ms,
        "server_pulse_step_ms": max(30, min(1000, duration_ms)),
        "post_stop_snapshot_delay_ms": snapshot_delay_ms,
        "settle_ms_between_steps": settle_ms,
        "pulse_mode": args.pulse_mode,
        "wall_threshold": wall_threshold,
        "scan_plan": scan_plan,
    }

    try:
        first_scan = scan_until_no_change(
            client=client,
            state=state,
            snap_url=snap_url,
            direction=first_direction,
            max_steps=max_steps,
            speed=speed,
            duration_ms=duration_ms,
            timeout=args.timeout,
            pulse_mode=args.pulse_mode,
            snapshot_delay_ms=snapshot_delay_ms,
            settle_ms=settle_ms,
            wall_threshold=wall_threshold,
            min_changed_steps_before_wall=0,
        )
        result["first_scan"] = first_scan
        first_wall_position = int(first_scan["wall_position"])

        reverse_scan = scan_until_no_change(
            client=client,
            state=state,
            snap_url=snap_url,
            direction=reverse_direction,
            max_steps=max_steps,
            speed=speed,
            duration_ms=duration_ms,
            timeout=args.timeout,
            pulse_mode=args.pulse_mode,
            snapshot_delay_ms=snapshot_delay_ms,
            settle_ms=settle_ms,
            wall_threshold=wall_threshold,
            min_changed_steps_before_wall=1,
        )
        result["reverse_scan"] = reverse_scan
        reverse_wall_position = int(reverse_scan["wall_position"])
        travel_steps = int(reverse_scan["changed_steps"])
        steps_back_to_center = math.ceil(travel_steps / 2)
        center_direction = first_direction
        center_moves = move_steps(
            client=client,
            state=state,
            snap_url=snap_url,
            direction=center_direction,
            steps=steps_back_to_center,
            speed=speed,
            duration_ms=duration_ms,
            timeout=args.timeout,
            pulse_mode=args.pulse_mode,
            snapshot_delay_ms=snapshot_delay_ms,
            settle_ms=settle_ms,
            wall_threshold=wall_threshold,
        )
        result["center_moves"] = center_moves

        left_wall = min(first_wall_position, reverse_wall_position)
        right_wall = max(first_wall_position, reverse_wall_position)
        state.left_wall_steps = left_wall
        state.right_wall_steps = right_wall
        save_state(state_path, state)
        result.update(
            {
                "first_scan": first_scan,
                "reverse_scan": reverse_scan,
                "wall_positions": {
                    "first": first_wall_position,
                    "reverse": reverse_wall_position,
                    "left": left_wall,
                    "right": right_wall,
                },
                "travel_steps_between_walls": travel_steps,
                "steps_back_to_center": steps_back_to_center,
                "center_direction": center_direction,
                "final_state": state_summary(state),
            }
        )
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        if isinstance(exc, CenteringError) and exc.details:
            result["error_details"] = exc.details
        result["final_state"] = state_summary(state)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 1

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"Scanned {first_direction} wall at {result['wall_positions']['first']}; "
            f"{reverse_direction} wall at {result['wall_positions']['reverse']}."
        )
        print(
            f"Travel between walls: {travel_steps} changed step(s); "
            f"moved {steps_back_to_center} step(s) {center_direction} to center."
        )
        print(f"Centered at position {state.position_steps}.")
        print(f"State saved to {state_path}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Send repeated up/down PTZ pulse steps to the local IP camera."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from camera_ops.client import CameraPtzClient, PtzError, clamp_speed, normalize_tilt_direction
from camera_ops.pan import build_payload, build_timed_payloads, ptz_url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send repeated up/down PTZ pulse steps to the local IP camera.")
    parser.add_argument("direction", help="Tilt direction: up or down.")
    parser.add_argument("--steps", type=int, default=1, help="Number of pulse steps to send. Defaults to 1.")
    parser.add_argument(
        "--step-ms",
        type=int,
        default=1000,
        help="Duration for each pulse step in milliseconds. Defaults to 1000.",
    )
    parser.add_argument("--duration-ms", type=int, default=None, help="Alias for --step-ms.")
    parser.add_argument(
        "--settle-ms",
        type=int,
        default=2000,
        help="Pause between steps in milliseconds. Defaults to 2000.",
    )
    parser.add_argument("--speed", type=int, default=1, help="PTZ speed, clamped to 1..8. Defaults to 1.")
    parser.add_argument("--timeout", type=float, default=8.0, help="HTTP timeout per step in seconds. Defaults to 8.")
    parser.add_argument("--source", choices=("wifi", "bulb"), default="wifi", help="Camera source endpoint.")
    parser.add_argument("--server-url", default="http://127.0.0.1:8090", help="Local webcam server base URL.")
    parser.add_argument("--url", default=None, help="Full PTZ endpoint URL. Overrides --server-url and --source.")
    parser.add_argument(
        "--pulse-mode",
        choices=("timed", "server"),
        default="timed",
        help="Use explicit start/sleep/stop pulses or the server's pulse action. Defaults to timed.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print requests without moving the camera.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON response.")
    parser.add_argument(
        "--stop-on-error",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stop sending remaining steps after a failed step. Defaults to true.",
    )
    return parser


def debug_log(message: str) -> None:
    print(f"[camera_ops.tilt] {message}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        command = normalize_tilt_direction(args.direction)
        steps = max(1, int(args.steps))
        step_ms_source = args.duration_ms if args.duration_ms is not None else args.step_ms
        step_ms = max(1, min(1000, int(step_ms_source)))
        settle_ms = max(0, min(10000, int(args.settle_ms)))
        speed = clamp_speed(args.speed)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))

    url = ptz_url(args.server_url, args.source, args.url)
    server_step_ms = max(30, min(1000, step_ms))
    payload = build_payload(command, speed, server_step_ms)
    timed_payloads = build_timed_payloads(command, speed)
    plan = [
        {
            "step": index + 1,
            "mode": args.pulse_mode,
            "duration_ms": step_ms,
            "payload": payload if args.pulse_mode == "server" else timed_payloads,
        }
        for index in range(steps)
    ]

    if args.dry_run:
        print(
            json.dumps(
                {
                    "url": url,
                    "steps": steps,
                    "step_ms": step_ms,
                    "server_pulse_step_ms": server_step_ms,
                    "settle_ms": settle_ms,
                    "pulse_mode": args.pulse_mode,
                    "plan": plan,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    client = CameraPtzClient(url=url, timeout=args.timeout)
    responses = []
    completed_steps = 0
    for index in range(steps):
        current_step = index + 1
        debug_log(
            f"step={current_step}/{steps} sending direction={command} "
            f"step_ms={step_ms} pulse_mode={args.pulse_mode}"
        )
        try:
            if args.pulse_mode == "server":
                result = client.pulse(command=command, speed=speed, duration_ms=step_ms)
            else:
                result = client.timed_pulse(command=command, speed=speed, duration_ms=step_ms)
        except PtzError as exc:
            debug_log(f"step={current_step}/{steps} status=error steps_taken={completed_steps}/{steps} error={exc}")
            error = {"step": current_step, "status": "error", "steps_taken": completed_steps, "error": str(exc)}
            responses.append(error)
            if args.stop_on_error:
                break
            continue

        completed_steps += 1
        debug_log(f"step={current_step}/{steps} status=ok steps_taken={completed_steps}/{steps}")
        responses.append({"step": current_step, "steps_taken": completed_steps, "response": result.raw})
        if index < steps - 1 and settle_ms > 0:
            debug_log(f"settling_ms={settle_ms} steps_taken={completed_steps}/{steps}")
            time.sleep(settle_ms / 1000.0)

    success = all(item.get("status") != "error" for item in responses)
    sent_steps = sum(1 for item in responses if "response" in item)
    if args.json:
        print(
            json.dumps(
                {
                    "status": "ok" if success else "error",
                    "command": command,
                    "steps_requested": steps,
                    "steps_sent": sent_steps,
                    "step_ms": step_ms,
                    "server_pulse_step_ms": server_step_ms,
                    "settle_ms": settle_ms,
                    "pulse_mode": args.pulse_mode,
                    "responses": responses,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(f"Sent {sent_steps} {command} step(s) at {step_ms} ms each, speed {speed}, mode {args.pulse_mode}.")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

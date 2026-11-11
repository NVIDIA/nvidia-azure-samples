# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client utilities for the local camera PTZ HTTP endpoint."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PAN_DIRECTION_ALIASES = {
    "left": "left",
    "l": "left",
    "pan_left": "left",
    "panleft": "left",
    "right": "right",
    "r": "right",
    "pan_right": "right",
    "panright": "right",
}

TILT_DIRECTION_ALIASES = {
    "up": "up",
    "u": "up",
    "tilt_up": "up",
    "tiltup": "up",
    "down": "down",
    "d": "down",
    "tilt_down": "down",
    "tiltdown": "down",
}


class PtzError(RuntimeError):
    """Raised when a PTZ request cannot be completed."""


@dataclass(frozen=True)
class PtzResponse:
    """Normalized view of a PTZ endpoint response."""

    status: str
    backend: str
    command: str
    action: str
    duration_ms: int
    speed: int
    raw: dict


def normalize_pan_direction(value: str) -> str:
    """Normalize user input into a supported pan direction."""

    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    direction = PAN_DIRECTION_ALIASES.get(key, "")
    if not direction:
        raise ValueError("direction must be left or right")
    return direction


def normalize_tilt_direction(value: str) -> str:
    """Normalize user input into a supported tilt direction."""

    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    direction = TILT_DIRECTION_ALIASES.get(key, "")
    if not direction:
        raise ValueError("direction must be up or down")
    return direction


def degrees_to_duration_ms(
    degrees: float,
    base_pulse_ms: int = 120,
    reference_degrees: float = 5.0,
) -> int:
    """Convert an approximate PTZ pan angle into the pulse duration used locally."""

    safe_degrees = max(1.0, min(45.0, float(degrees)))
    safe_base = max(30, min(1000, int(base_pulse_ms)))
    safe_reference = max(1.0, float(reference_degrees))
    return max(30, min(1000, int(round(safe_base * (safe_degrees / safe_reference)))))


def pan_duration_ms(
    direction: str,
    degrees: float,
    base_pulse_ms: int = 120,
    left_pulse_ms: int | None = None,
    right_pulse_ms: int | None = None,
    reference_degrees: float = 5.0,
) -> int:
    """Convert an approximate pan angle into a calibrated pulse duration."""

    command = normalize_pan_direction(direction)
    direction_pulse_ms = left_pulse_ms if command == "left" else right_pulse_ms
    pulse_ms = base_pulse_ms if direction_pulse_ms is None else direction_pulse_ms
    return degrees_to_duration_ms(
        degrees,
        base_pulse_ms=pulse_ms,
        reference_degrees=reference_degrees,
    )


def clamp_speed(speed: int) -> int:
    """Clamp camera PTZ speed to the range accepted by the local endpoint."""

    return max(1, min(8, int(speed)))


class CameraPtzClient:
    """Thin client for the stream server PTZ routes, such as /wifi-ptz."""

    def __init__(
        self,
        url: str = "http://127.0.0.1:8090/wifi-ptz",
        timeout: float = 8.0,
    ) -> None:
        self.url = str(url or "").strip()
        self.timeout = max(1.0, float(timeout))
        if not self.url:
            raise ValueError("PTZ URL is required")

    def pulse(self, command: str, speed: int = 1, duration_ms: int = 120) -> PtzResponse:
        """Send one bounded PTZ pulse."""

        safe_speed = clamp_speed(speed)
        safe_duration = max(30, min(1000, int(duration_ms)))
        payload = {
            "command": command,
            "action": "pulse",
            "speed": safe_speed,
            "duration_ms": safe_duration,
        }
        result = self._post_json(payload)
        return PtzResponse(
            status=str(result.get("status") or "ok"),
            backend=str(result.get("backend") or ""),
            command=str(result.get("command") or command),
            action=str(result.get("action") or "pulse"),
            duration_ms=int(result.get("duration_ms") or safe_duration),
            speed=safe_speed,
            raw=result,
        )

    def timed_pulse(self, command: str, speed: int = 1, duration_ms: int = 120) -> PtzResponse:
        """Send one PTZ pulse as explicit start/sleep/stop commands."""

        safe_duration = max(1, min(1000, int(duration_ms)))
        started_at = time.monotonic()
        start = self.control(command=command, action="start", speed=speed)
        time.sleep(safe_duration / 1000.0)
        stop = self.control(command=command, action="stop", speed=speed)
        elapsed_ms = int(round((time.monotonic() - started_at) * 1000))
        return PtzResponse(
            status="ok" if start.status != "error" and stop.status != "error" else "error",
            backend=stop.backend or start.backend,
            command=command,
            action="timed_pulse",
            duration_ms=safe_duration,
            speed=clamp_speed(speed),
            raw={
                "status": "ok",
                "backend": stop.backend or start.backend,
                "command": command,
                "action": "timed_pulse",
                "duration_ms": safe_duration,
                "elapsed_ms": elapsed_ms,
                "start_response": start.raw,
                "stop_response": stop.raw,
            },
        )

    def control(self, command: str, action: str, speed: int = 1) -> PtzResponse:
        """Send a PTZ start or stop command."""

        safe_speed = clamp_speed(speed)
        safe_action = str(action or "").strip().lower()
        if safe_action not in {"start", "stop"}:
            raise ValueError("PTZ action must be start or stop")
        payload = {
            "command": command,
            "action": safe_action,
            "speed": safe_speed,
        }
        result = self._post_json(payload)
        return PtzResponse(
            status=str(result.get("status") or "ok"),
            backend=str(result.get("backend") or ""),
            command=str(result.get("command") or command),
            action=str(result.get("action") or safe_action),
            duration_ms=int(result.get("duration_ms") or 0),
            speed=safe_speed,
            raw=result,
        )

    def pan(
        self,
        direction: str,
        degrees: float = 5.0,
        speed: int = 1,
        base_pulse_ms: int = 120,
        left_pulse_ms: int | None = None,
        right_pulse_ms: int | None = None,
    ) -> PtzResponse:
        """Pan the camera left or right by sending a short pulse."""

        command = normalize_pan_direction(direction)
        duration_ms = pan_duration_ms(
            command,
            degrees,
            base_pulse_ms=base_pulse_ms,
            left_pulse_ms=left_pulse_ms,
            right_pulse_ms=right_pulse_ms,
        )
        return self.pulse(command=command, speed=speed, duration_ms=duration_ms)

    def _post_json(self, payload: dict) -> dict:
        request = Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(8192).decode("utf-8", "replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            raise PtzError(f"camera PTZ HTTP {exc.code}: {body}") from exc
        except (OSError, URLError) as exc:
            raise PtzError(f"camera PTZ request failed: {exc}") from exc

        if not raw.strip():
            return {}
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PtzError(f"camera PTZ returned invalid JSON: {raw[:500]}") from exc
        if not isinstance(result, dict):
            raise PtzError(f"camera PTZ returned unexpected JSON: {raw[:500]}")
        if result.get("status") == "error":
            raise PtzError(str(result.get("error") or result))
        return result

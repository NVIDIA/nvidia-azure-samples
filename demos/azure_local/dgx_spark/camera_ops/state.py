# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Persisted pan state for conservative wall-aware PTZ steps."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from typing import Any


STATE_VERSION = 1


@dataclass
class PanState:
    source: str
    position_steps: int = 0
    left_wall_steps: int | None = None
    right_wall_steps: int | None = None
    updated_at: float = 0.0


def default_state_path() -> Path:
    return Path(__file__).with_name("pan_state.json")


def load_state(path: Path, source: str) -> PanState:
    payload = _read_payload(path)
    source_payload = (payload.get("sources") or {}).get(source, {})
    if not isinstance(source_payload, dict):
        source_payload = {}
    return PanState(
        source=source,
        position_steps=_int_value(source_payload.get("position_steps"), 0),
        left_wall_steps=_optional_int(source_payload.get("left_wall_steps")),
        right_wall_steps=_optional_int(source_payload.get("right_wall_steps")),
        updated_at=float(source_payload.get("updated_at") or 0.0),
    )


def save_state(path: Path, state: PanState) -> None:
    payload = _read_payload(path)
    sources = payload.setdefault("sources", {})
    if not isinstance(sources, dict):
        sources = {}
        payload["sources"] = sources
    state.updated_at = time.time()
    sources[state.source] = asdict(state)
    payload["version"] = STATE_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_walls(state: PanState) -> PanState:
    state.left_wall_steps = None
    state.right_wall_steps = None
    return state


def mark_wall(state: PanState, direction: str) -> PanState:
    if direction == "left":
        state.left_wall_steps = state.position_steps
    elif direction == "right":
        state.right_wall_steps = state.position_steps
    else:
        raise ValueError("wall direction must be left or right")
    return state


def can_step(state: PanState, direction: str) -> tuple[bool, str]:
    if direction == "left" and state.left_wall_steps is not None and state.position_steps <= state.left_wall_steps:
        return False, f"left wall already known at position {state.left_wall_steps}"
    if direction == "right" and state.right_wall_steps is not None and state.position_steps >= state.right_wall_steps:
        return False, f"right wall already known at position {state.right_wall_steps}"
    return True, ""


def record_step(state: PanState, direction: str) -> PanState:
    if direction == "left":
        state.position_steps -= 1
    elif direction == "right":
        state.position_steps += 1
    else:
        raise ValueError("step direction must be left or right")
    return state


def state_summary(state: PanState) -> dict[str, Any]:
    return {
        "source": state.source,
        "position_steps": state.position_steps,
        "left_wall_steps": state.left_wall_steps,
        "right_wall_steps": state.right_wall_steps,
        "updated_at": state.updated_at,
    }


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": STATE_VERSION, "sources": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "sources": {}}
    if not isinstance(payload, dict):
        return {"version": STATE_VERSION, "sources": {}}
    payload.setdefault("version", STATE_VERSION)
    payload.setdefault("sources", {})
    return payload


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

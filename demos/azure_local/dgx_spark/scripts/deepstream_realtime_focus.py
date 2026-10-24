#!/usr/bin/env python3
"""Deterministic low-latency DeepStream-to-focus command dispatcher."""

from __future__ import annotations

import fcntl
import json
import math
import os
import threading
import time
from pathlib import Path


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    temporary.replace(path)


class AsyncStateWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.condition = threading.Condition()
        self.pending: dict | None = None
        self.stopped = False
        self.thread = threading.Thread(target=self.run, name="realtime-focus-state", daemon=True)
        self.thread.start()

    def submit(self, payload: dict) -> None:
        with self.condition:
            self.pending = payload
            self.condition.notify()

    def run(self) -> None:
        while True:
            with self.condition:
                while self.pending is None and not self.stopped:
                    self.condition.wait()
                if self.stopped and self.pending is None:
                    return
                payload = self.pending
                self.pending = None
            if payload is not None:
                write_json(self.path, payload)

    def close(self) -> None:
        with self.condition:
            self.stopped = True
            self.condition.notify()
        self.thread.join(timeout=1.0)


def clean_label(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def confidence(item: dict) -> float:
    try:
        value = float(item.get("confidence", item.get("probability", item.get("score", 0.0))) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, value / 100.0 if value > 1.0 else value))


def center(item: dict) -> tuple[float, float] | None:
    bbox = item.get("bbox")
    if not isinstance(bbox, list) or len(bbox) < 4:
        return None
    try:
        left, top, width, height = map(float, bbox[:4])
        frame_width = float(item.get("frame_width") or item.get("image_width") or 0)
        frame_height = float(item.get("frame_height") or item.get("image_height") or 0)
    except (TypeError, ValueError):
        return None
    if frame_width <= 0 or frame_height <= 0 or width <= 0 or height <= 0:
        return None
    return ((left + width / 2) / frame_width, (top + height / 2) / frame_height)


def preferred_labels(settings: dict) -> list[str]:
    value = settings.get("preferred_objects")
    if isinstance(value, str):
        raw = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    labels = []
    seen = set()
    for item in raw:
        label = clean_label(item)
        if label and label not in seen:
            labels.append(label)
            seen.add(label)
    return labels


def minimum_confidence(settings: dict) -> float:
    try:
        value = float(
            settings.get(
                "realtime_focus_min_confidence_percent",
                settings.get("minimum_object_confidence_percent", 25.0),
            )
        ) / 100.0
    except (TypeError, ValueError):
        value = 0.25
    return max(0.0, min(1.0, value))


def select_target(objects: list[dict], settings: dict) -> dict | None:
    minimum = minimum_confidence(settings)
    preferred = preferred_labels(settings)
    if not preferred:
        return None
    ranks = {label: rank for rank, label in enumerate(preferred)}
    candidates = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        label = clean_label(item.get("label") or item.get("class") or item.get("name"))
        probability = confidence(item)
        object_center = center(item)
        if not label or probability < minimum or object_center is None:
            continue
        rank = ranks.get(label)
        if rank is None:
            continue
        candidates.append(
            {
                "label": label,
                "confidence": probability,
                "center": object_center,
                "track_id": item.get("track_id"),
                "preferred_rank": rank,
                "raw": item,
            }
        )
    if not candidates:
        return None
    best_rank = min(int(item["preferred_rank"]) for item in candidates)
    return max(
        (item for item in candidates if item["preferred_rank"] == best_rank),
        key=lambda item: item["confidence"],
    )


class RealtimeFocusDispatcher:
    """Select and dispatch one-shot focus requests directly from detector frames."""

    def __init__(
        self,
        *,
        source: str,
        settings_path: str | Path,
        command_path: str | Path,
        state_path: str | Path,
        timeout_seconds: float = 180.0,
    ) -> None:
        self.source = clean_label(source) or "wifi"
        self.settings_path = Path(settings_path)
        self.command_path = Path(command_path)
        self.state_path = Path(state_path)
        self.state_writer = AsyncStateWriter(self.state_path)
        self.timeout_seconds = max(2.0, min(300.0, float(timeout_seconds)))
        self.settings_stamp: tuple[int, int] | None = None
        self.settings: dict = {}
        persisted = read_json(self.state_path)
        self.latched_label = clean_label(persisted.get("latched_label"))
        self.last_center = tuple(persisted.get("last_center", ())) if isinstance(persisted.get("last_center"), list) else ()
        self.last_dispatch_monotonic = 0.0
        self.absent_frames = 0
        self.absent_since = 0.0
        self.candidate_label = ""
        self.candidate_frames = 0

    def load_settings(self) -> dict:
        try:
            stat = self.settings_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        if stamp != self.settings_stamp:
            self.settings = read_json(self.settings_path)
            self.settings_stamp = stamp
        return self.settings

    def persist(self, **extra: object) -> None:
        payload = {
            "status": "active",
            "source": self.source,
            "latched_label": self.latched_label,
            "last_center": list(self.last_center) if len(self.last_center) == 2 else [],
            "absent_frames": self.absent_frames,
            "updated_at": time.time(),
            **extra,
        }
        self.state_writer.submit(payload)

    def close(self) -> None:
        self.state_writer.close()

    def consider(
        self,
        objects: list[dict],
        *,
        frame_id: int,
        objects_updated_at: float,
        objects_updated_monotonic_ns: int,
    ) -> dict:
        settings = self.load_settings()
        if settings.get("realtime_focus_enabled", True) is False:
            return {}
        selected = select_target(objects, settings)
        now_monotonic = time.monotonic()
        now = time.time()
        try:
            rearm_frames = max(1, int(settings.get("realtime_focus_rearm_absent_frames", 20)))
        except (TypeError, ValueError):
            rearm_frames = 20
        try:
            rearm_seconds = max(0.0, float(settings.get("realtime_focus_rearm_absent_seconds", 0.75)))
        except (TypeError, ValueError):
            rearm_seconds = 0.75
        try:
            stable_frames = max(1, int(settings.get("realtime_focus_stable_frames", 1)))
        except (TypeError, ValueError):
            stable_frames = 1
        try:
            cooldown = max(0.0, float(settings.get("realtime_focus_cooldown_seconds", 0.25)))
        except (TypeError, ValueError):
            cooldown = 0.25
        try:
            reacquire_distance = max(0.02, float(settings.get("realtime_focus_reacquire_distance", 0.12)))
        except (TypeError, ValueError):
            reacquire_distance = 0.12

        latched_visible = bool(
            self.latched_label
            and any(
                isinstance(item, dict)
                and clean_label(item.get("label") or item.get("class") or item.get("name")) == self.latched_label
                and confidence(item) >= minimum_confidence(settings)
                for item in objects
            )
        )
        if self.latched_label and not latched_visible:
            selected_rank = preferred_labels(settings).index(selected["label"]) if selected and selected["label"] in preferred_labels(settings) else 10_000
            latched_rank = preferred_labels(settings).index(self.latched_label) if self.latched_label in preferred_labels(settings) else 10_000
            if not selected or selected_rank >= latched_rank:
                self.absent_frames += 1
                if not self.absent_since:
                    self.absent_since = now_monotonic
                if self.absent_frames >= rearm_frames and now_monotonic - self.absent_since >= rearm_seconds:
                    self.latched_label = ""
                    self.last_center = ()
                    self.candidate_label = ""
                    self.candidate_frames = 0
                    self.persist(event="rearmed", frame_id=frame_id)
                else:
                    return {}

        if selected is None:
            return {}

        label = selected["label"]
        object_center = selected["center"]
        self.absent_frames = 0
        self.absent_since = 0.0
        if label == self.candidate_label:
            self.candidate_frames += 1
        else:
            self.candidate_label = label
            self.candidate_frames = 1
        if self.candidate_frames < stable_frames:
            return {}

        preferred = preferred_labels(settings)
        ranks = {item: index for index, item in enumerate(preferred)}
        selected_rank = ranks.get(label, len(preferred))
        latched_rank = ranks.get(self.latched_label, len(preferred))
        higher_priority = bool(self.latched_label and label != self.latched_label and selected_rank < latched_rank)
        moved_far = bool(
            self.latched_label == label
            and len(self.last_center) == 2
            and math.hypot(object_center[0] - self.last_center[0], object_center[1] - self.last_center[1]) >= reacquire_distance
            and (abs(object_center[0] - 0.5) > 0.08 or abs(object_center[1] - 0.5) > 0.08)
        )
        newly_armed = not self.latched_label
        if not (newly_armed or higher_priority or moved_far):
            return {}
        if now_monotonic - self.last_dispatch_monotonic < cooldown:
            return {}

        lock_path = self.command_path.with_suffix(self.command_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            current = read_json(self.command_path)
            if current.get("enabled"):
                current_label = clean_label(current.get("target_label"))
                current_rank = ranks.get(current_label, len(preferred))
                can_preempt = bool(
                    current.get("trigger") == "realtime_deepstream_focus"
                    and label != current_label
                    and selected_rank < current_rank
                )
                if not can_preempt:
                    return {}
            request_id = f"focus_rt_{time.time_ns()}"
            request = {
                "enabled": True,
                "source": self.source,
                "target_label": label,
                "requested_at": now,
                "requested_monotonic_ns": time.monotonic_ns(),
                "expires_at": now + self.timeout_seconds,
                "request_id": request_id,
                "trigger": "realtime_deepstream_focus",
                "preferred_rank": selected_rank,
                "priority": selected_rank + 1,
                "source_frame_id": int(frame_id),
                "objects_updated_at": float(objects_updated_at),
                "objects_updated_monotonic_ns": int(objects_updated_monotonic_ns),
                "target_confidence": round(float(selected["confidence"]), 6),
                "target_center": {"x": round(object_center[0], 6), "y": round(object_center[1], 6)},
            }
            if selected.get("track_id") is not None:
                request["target_track_id"] = selected["track_id"]
            if current.get("enabled"):
                request["preempted_request_id"] = str(current.get("request_id") or "")
                request["preempted_target_label"] = clean_label(current.get("target_label"))
            write_json(self.command_path, request)

        self.latched_label = label
        self.last_center = object_center
        self.last_dispatch_monotonic = now_monotonic
        self.persist(
            event="dispatched",
            request_id=request_id,
            frame_id=frame_id,
            confidence=request["target_confidence"],
            dispatched_at=now,
        )
        return request

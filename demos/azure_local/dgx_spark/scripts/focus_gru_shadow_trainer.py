#!/usr/bin/env python3
"""Continuously train a causal GRU shadow model for autofocus pulse response.

The existing per-direction linear model remains authoritative.  This process only
reads its event log and writes independent checkpoints, health state, and shadow
predictions so the recurrent model can be evaluated safely before deployment.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import random
import signal
import time
from typing import Iterable

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F


DIRECTIONS = ("left", "right", "up", "down")
BOUNDARY_OUTCOMES = {"target_lost", "superseded", "manual_yield", "cancelled"}
TRAINING_OUTCOMES = {"observed", "no_response", "target_lost"}
FEATURE_NAMES = (
    "pre_x", "pre_y", "pre_offset_x", "pre_offset_y",
    "left_seconds", "right_seconds", "up_seconds", "down_seconds",
    "left_count", "right_count", "up_count", "down_count",
    "max_speed_x", "max_speed_y",
    "native_x", "native_y", "auto_x", "auto_y",
    "batch_size", "recovery_stage_x", "recovery_stage_y", "pulse_count_before",
    "gain_left", "gain_right", "gain_up", "gain_down",
    "previous_dx", "previous_dy", "previous_motion_valid",
    "previous_visible", "previous_no_response",
    "previous_latency", "previous_latency_valid", "seconds_since_previous",
    "reversal_x", "reversal_y", "same_direction_streak_x", "same_direction_streak_y",
    "bbox_width", "bbox_height", "bbox_area", "bbox_aspect", "bbox_valid", "target_confidence",
    "global_motion_x", "global_motion_y", "global_motion_magnitude", "global_motion_response", "global_motion_valid",
    "ptz_session_age", "ptz_session_age_valid",
    "last_reversal_response_x", "last_reversal_gain_x", "last_reversal_valid_x",
    "last_reversal_response_y", "last_reversal_gain_y", "last_reversal_valid_y",
    "recent_gain_mean_left", "recent_gain_std_left", "recent_gain_last_left", "recent_gain_count_left",
    "recent_gain_mean_right", "recent_gain_std_right", "recent_gain_last_right", "recent_gain_count_right",
    "recent_gain_mean_up", "recent_gain_std_up", "recent_gain_last_up", "recent_gain_count_up",
    "recent_gain_mean_down", "recent_gain_std_down", "recent_gain_last_down", "recent_gain_count_down",
)
FEATURE_DIM = len(FEATURE_NAMES)


def finite_float(value: object, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def atomic_write_json(path: str | Path, payload: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(output)


def append_jsonl(path: str | Path, payload: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, separators=(",", ":")) + "\n")
        stream.flush()


def record_order(record: dict) -> tuple[int, str]:
    return (
        int(finite_float(record.get("dispatched_monotonic_ns"), 0.0)),
        str(record.get("recorded_at") or ""),
    )


def read_observed_records(path: str | Path) -> list[dict]:
    """Read completed, deduplicated transitions; tolerate malformed tail lines."""
    selected: dict[str, dict] = {}
    try:
        stream = Path(path).open("r", encoding="utf-8")
    except OSError:
        return []
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(value, dict) or value.get("event") != "pulse_observed":
                continue
            step_id = str(value.get("step_id") or "").strip()
            request_id = str(value.get("request_id") or "").strip()
            if step_id and request_id:
                selected[step_id] = value
    return sorted(selected.values(), key=record_order)


def pulse_summary(record: dict) -> tuple[dict[str, float], dict[str, float], dict[str, float], dict[str, float]]:
    durations = {name: 0.0 for name in DIRECTIONS}
    counts = {name: 0.0 for name in DIRECTIONS}
    speeds = {"x": 0.0, "y": 0.0}
    backend = {"native_x": 0.0, "native_y": 0.0, "auto_x": 0.0, "auto_y": 0.0}
    pulses = record.get("pulses") if isinstance(record.get("pulses"), list) else []
    for pulse in pulses:
        if not isinstance(pulse, dict):
            continue
        direction = str(pulse.get("direction") or "").lower()
        axis = str(pulse.get("axis") or ("x" if direction in {"left", "right"} else "y"))
        if direction in durations:
            durations[direction] += max(0.0, finite_float(pulse.get("duration_ms"))) / 1000.0
            counts[direction] += 1.0
        if axis in speeds:
            speeds[axis] = max(speeds[axis], max(0.0, finite_float(pulse.get("speed"))) / 8.0)
        pulse_backend = str(pulse.get("backend") or record.get("control_mode") or "auto").lower()
        if axis in {"x", "y"}:
            if "native" in pulse_backend:
                backend[f"native_{axis}"] = 1.0
            if "auto" in pulse_backend or not pulse_backend:
                backend[f"auto_{axis}"] = 1.0
    return durations, counts, speeds, backend


def action_sign(record: dict, axis: str) -> int:
    durations, _, _, _ = pulse_summary(record)
    value = durations["left"] - durations["right"] if axis == "x" else durations["up"] - durations["down"]
    return 1 if value > 0 else (-1 if value < 0 else 0)


def outcome_targets(record: dict) -> dict[str, float]:
    outcome = str(record.get("outcome") or "")
    actual = record.get("actual_delta") if isinstance(record.get("actual_delta"), dict) else {}
    dx = finite_float(actual.get("x"), float("nan"))
    dy = finite_float(actual.get("y"), float("nan"))
    movement_valid = outcome in {"observed", "no_response"} and math.isfinite(dx) and math.isfinite(dy)
    visible_valid = outcome in TRAINING_OUTCOMES
    no_response_valid = outcome in {"observed", "no_response"}
    latency = finite_float(record.get("response_elapsed_seconds"), float("nan"))
    return {
        "dx": dx if movement_valid else 0.0,
        "dy": dy if movement_valid else 0.0,
        "movement_mask": float(movement_valid),
        "visible": float(outcome != "target_lost") if visible_valid else 0.0,
        "visible_mask": float(visible_valid),
        "no_response": float(outcome == "no_response") if no_response_valid else 0.0,
        "no_response_mask": float(no_response_valid),
        "latency": math.log1p(max(0.0, latency)) if math.isfinite(latency) else 0.0,
        "latency_mask": float(math.isfinite(latency)),
    }


def new_history_context() -> dict:
    return {
        "previous_sign": {"x": 0, "y": 0},
        "last_reversal": {"x": {}, "y": {}},
        "gain_samples": {direction: [] for direction in DIRECTIONS},
    }


def update_history_context(history: dict, record: dict) -> None:
    targets = outcome_targets(record)
    for axis in ("x", "y"):
        sign = action_sign(record, axis)
        prior_sign = int(history["previous_sign"].get(axis, 0))
        if sign and prior_sign and sign != prior_sign and targets["movement_mask"]:
            actual = float(targets["dx" if axis == "x" else "dy"])
            duration = sum(pulse_summary(record)[0][name] for name in (("left", "right") if axis == "x" else ("up", "down")))
            projected = actual * sign
            history["last_reversal"][axis] = {
                "response": projected,
                "gain": projected / max(0.001, duration * 1000.0),
            }
        if sign:
            history["previous_sign"][axis] = sign
    updates = record.get("direction_gain_updates") if isinstance(record.get("direction_gain_updates"), dict) else {}
    for direction in DIRECTIONS:
        update = updates.get(direction) if isinstance(updates.get(direction), dict) else {}
        sample = finite_float(update.get("raw_sample"), float("nan"))
        if math.isfinite(sample):
            history["gain_samples"][direction] = (history["gain_samples"][direction] + [sample])[-8:]


def causal_feature(record: dict, previous: dict | None, streaks: dict[str, int], history: dict | None = None) -> np.ndarray:
    """Create features available at dispatch time; current outcomes are excluded."""
    pre = record.get("pre_center") if isinstance(record.get("pre_center"), dict) else {}
    pre_x, pre_y = finite_float(pre.get("x"), 0.5), finite_float(pre.get("y"), 0.5)
    durations, counts, speeds, backend = pulse_summary(record)
    pulses = record.get("pulses") if isinstance(record.get("pulses"), list) else []
    stages = {"x": 0.0, "y": 0.0}
    for pulse in pulses:
        if isinstance(pulse, dict):
            axis = str(pulse.get("axis") or "")
            if axis in stages:
                stages[axis] = max(stages[axis], finite_float(pulse.get("recovery_stage")))
    model = record.get("prediction_model") if isinstance(record.get("prediction_model"), dict) else {}
    gains = model.get("direction_gains") if isinstance(model.get("direction_gains"), dict) else {}
    previous_targets = outcome_targets(previous) if previous else {}
    previous_dx = finite_float(previous_targets.get("dx"))
    previous_dy = finite_float(previous_targets.get("dy"))
    previous_motion_valid = finite_float(previous_targets.get("movement_mask"))
    previous_visible = finite_float(previous_targets.get("visible")) if previous_targets.get("visible_mask") else 0.0
    previous_no_response = finite_float(previous_targets.get("no_response")) if previous_targets.get("no_response_mask") else 0.0
    previous_latency = finite_float(previous_targets.get("latency"))
    previous_latency_valid = finite_float(previous_targets.get("latency_mask"))
    history = history or new_history_context()
    context = record.get("context") if isinstance(record.get("context"), dict) else {}
    bbox = context.get("target_bbox") if isinstance(context.get("target_bbox"), dict) else {}
    bbox_width = max(0.0, finite_float(bbox.get("width")))
    bbox_height = max(0.0, finite_float(bbox.get("height")))
    bbox_valid = float(bbox_width > 0 and bbox_height > 0)
    global_motion = context.get("previous_global_motion") if isinstance(context.get("previous_global_motion"), dict) else {}
    global_valid = float(bool(global_motion.get("available") or global_motion.get("verified")))
    session_age_raw = finite_float(context.get("ptz_session_age_seconds"), float("nan"))
    session_valid = float(math.isfinite(session_age_raw))
    reversal_values = []
    for axis in ("x", "y"):
        reversal = history["last_reversal"].get(axis) or {}
        valid = float(bool(reversal))
        reversal_values += [finite_float(reversal.get("response")), finite_float(reversal.get("gain")) / 0.001, valid]
    gain_values = []
    for direction in DIRECTIONS:
        samples = history["gain_samples"].get(direction) or []
        gain_values += [
            (sum(samples) / len(samples) / 0.001) if samples else 0.0,
            (float(np.std(samples)) / 0.001) if samples else 0.0,
            (samples[-1] / 0.001) if samples else 0.0,
            len(samples) / 8.0,
        ]
    elapsed = 0.0
    if previous:
        now_ns = finite_float(record.get("dispatched_monotonic_ns"))
        old_ns = finite_float(previous.get("dispatched_monotonic_ns"))
        elapsed = min(10.0, max(0.0, (now_ns - old_ns) / 1e9)) / 10.0
    reversal = {}
    for axis in ("x", "y"):
        current_sign = action_sign(record, axis)
        previous_sign = action_sign(previous, axis) if previous else 0
        reversal[axis] = float(current_sign != 0 and previous_sign != 0 and current_sign != previous_sign)
    values = [
        pre_x, pre_y, pre_x - 0.5, pre_y - 0.5,
        *(durations[name] for name in DIRECTIONS), *(counts[name] / 3.0 for name in DIRECTIONS),
        speeds["x"], speeds["y"], backend["native_x"], backend["native_y"], backend["auto_x"], backend["auto_y"],
        len(pulses) / 3.0, stages["x"] / 2.0, stages["y"] / 2.0,
        finite_float(record.get("pulse_count_before")) / 100.0,
        *(finite_float(gains.get(name)) / 0.001 for name in DIRECTIONS),
        previous_dx, previous_dy, previous_motion_valid, previous_visible, previous_no_response,
        previous_latency, previous_latency_valid, elapsed,
        reversal["x"], reversal["y"], min(10, streaks.get("x", 0)) / 10.0, min(10, streaks.get("y", 0)) / 10.0,
        bbox_width, bbox_height, bbox_width * bbox_height,
        min(5.0, max(0.0, finite_float(bbox.get("aspect_ratio"), bbox_width / max(1e-6, bbox_height)))) / 5.0,
        bbox_valid, max(0.0, min(1.0, finite_float(context.get("target_confidence")))),
        finite_float(global_motion.get("x")), finite_float(global_motion.get("y")),
        finite_float(global_motion.get("magnitude")), finite_float(global_motion.get("response")), global_valid,
        math.log1p(min(300.0, max(0.0, session_age_raw))) / math.log1p(300.0) if session_valid else 0.0,
        session_valid,
        *reversal_values, *gain_values,
    ]
    assert len(values) == FEATURE_DIM
    return np.asarray(values, dtype=np.float32)


@dataclass
class SequenceSample:
    features: np.ndarray
    mask: np.ndarray
    targets: dict[str, float]
    request_id: str
    step_id: str
    order: tuple[int, str]
    outcome: str
    linear_delta: dict[str, float]


def _request_segments(records: Iterable[dict]) -> Iterable[list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        grouped.setdefault(str(record.get("request_id")), []).append(record)
    for request_records in grouped.values():
        segment: list[dict] = []
        for record in sorted(request_records, key=record_order):
            segment.append(record)
            if str(record.get("outcome") or "") in BOUNDARY_OUTCOMES:
                yield segment
                segment = []
        if segment:
            yield segment


def build_samples(records: Iterable[dict], sequence_length: int = 12) -> list[SequenceSample]:
    samples: list[SequenceSample] = []
    for segment in _request_segments(records):
        vectors: list[np.ndarray] = []
        previous: dict | None = None
        history_context = new_history_context()
        streaks = {"x": 0, "y": 0}
        previous_sign = {"x": 0, "y": 0}
        for record in segment:
            for axis in ("x", "y"):
                sign = action_sign(record, axis)
                streaks[axis] = streaks[axis] + 1 if sign and sign == previous_sign[axis] else (1 if sign else 0)
                previous_sign[axis] = sign
            vectors.append(causal_feature(record, previous, streaks, history_context))
            outcome = str(record.get("outcome") or "")
            if outcome in TRAINING_OUTCOMES:
                usable = vectors[-sequence_length:]
                padded = np.zeros((sequence_length, FEATURE_DIM), dtype=np.float32)
                mask = np.zeros(sequence_length, dtype=np.float32)
                padded[-len(usable):] = np.stack(usable)
                mask[-len(usable):] = 1.0
                samples.append(SequenceSample(
                    padded, mask, outcome_targets(record), str(record.get("request_id")),
                    str(record.get("step_id")), record_order(record), outcome,
                    {
                        axis: finite_float(
                            (record.get("linear_predicted_delta") if isinstance(record.get("linear_predicted_delta"), dict) else record.get("predicted_delta") if isinstance(record.get("predicted_delta"), dict) else {}).get(axis),
                            float("nan"),
                        )
                        for axis in ("x", "y")
                    },
                ))
            previous = record
            update_history_context(history_context, record)
    return sorted(samples, key=lambda sample: sample.order)


def query_sequence(records: list[dict], pending: dict, sequence_length: int) -> tuple[np.ndarray, np.ndarray]:
    request_id = str(pending.get("request_id") or "")
    history = [record for record in records if str(record.get("request_id") or "") == request_id]
    history.sort(key=record_order)
    start = 0
    for index, record in enumerate(history):
        if str(record.get("outcome") or "") in BOUNDARY_OUTCOMES:
            start = index + 1
    history = history[start:] + [pending]
    vectors, previous = [], None
    streaks, previous_sign = {"x": 0, "y": 0}, {"x": 0, "y": 0}
    history_context = new_history_context()
    for record in history:
        for axis in ("x", "y"):
            sign = action_sign(record, axis)
            streaks[axis] = streaks[axis] + 1 if sign and sign == previous_sign[axis] else (1 if sign else 0)
            previous_sign[axis] = sign
        vectors.append(causal_feature(record, previous, streaks, history_context))
        update_history_context(history_context, record)
        previous = record
    usable = vectors[-sequence_length:]
    features = np.zeros((sequence_length, FEATURE_DIM), dtype=np.float32)
    mask = np.zeros(sequence_length, dtype=np.float32)
    features[-len(usable):], mask[-len(usable):] = np.stack(usable), 1.0
    return features, mask


class FocusGRU(nn.Module):
    def __init__(self, hidden_size: int = 64, mixtures: int = 3):
        super().__init__()
        self.hidden_size, self.mixtures = hidden_size, mixtures
        self.gru = nn.GRU(FEATURE_DIM, hidden_size, batch_first=True)
        self.mixture_logits = nn.Linear(hidden_size, mixtures)
        self.means = nn.Linear(hidden_size, mixtures * 2)
        self.raw_scales = nn.Linear(hidden_size, mixtures * 2)
        self.visibility = nn.Linear(hidden_size, 1)
        self.no_response = nn.Linear(hidden_size, 1)
        self.latency = nn.Linear(hidden_size, 1)

    def forward(self, features: torch.Tensor, mask: torch.Tensor) -> dict[str, torch.Tensor]:
        # Sequences are left-padded so their causal current action is always at
        # the final index. Padding is normalized back to zero by batches().
        recurrent, _ = self.gru(features)
        state = recurrent[:, -1]
        return {
            "logits": self.mixture_logits(state),
            "means": self.means(state).reshape(-1, self.mixtures, 2),
            "scales": F.softplus(self.raw_scales(state).reshape(-1, self.mixtures, 2)) + 1e-4,
            "visibility_logit": self.visibility(state).squeeze(-1),
            "no_response_logit": self.no_response(state).squeeze(-1),
            "latency": self.latency(state).squeeze(-1),
        }


def masked_mean(values: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    return (values * mask).sum() / mask.sum().clamp(min=1.0)


def model_loss(outputs: dict[str, torch.Tensor], targets: dict[str, torch.Tensor]) -> tuple[torch.Tensor, dict[str, float]]:
    movement = torch.stack((targets["dx"], targets["dy"]), dim=-1)
    log_component = -0.5 * (((movement[:, None, :] - outputs["means"]) / outputs["scales"]) ** 2).sum(-1)
    log_component -= torch.log(outputs["scales"]).sum(-1) + math.log(2.0 * math.pi)
    nll = -torch.logsumexp(F.log_softmax(outputs["logits"], -1) + log_component, -1)
    movement_loss = masked_mean(nll, targets["movement_mask"])
    visible_loss = masked_mean(F.binary_cross_entropy_with_logits(outputs["visibility_logit"], targets["visible"], reduction="none"), targets["visible_mask"])
    response_loss = masked_mean(F.binary_cross_entropy_with_logits(outputs["no_response_logit"], targets["no_response"], reduction="none"), targets["no_response_mask"])
    latency_loss = masked_mean(F.smooth_l1_loss(outputs["latency"], targets["latency"], reduction="none"), targets["latency_mask"])
    total = movement_loss + 0.25 * visible_loss + 0.25 * response_loss + 0.1 * latency_loss
    return total, {"movement_nll": float(movement_loss.detach()), "visibility_bce": float(visible_loss.detach()), "no_response_bce": float(response_loss.detach()), "latency_huber": float(latency_loss.detach())}


def replay_subset(samples: list[SequenceSample], maximum: int, seed: int = 17) -> list[SequenceSample]:
    if len(samples) <= maximum:
        return list(samples)
    rng = random.Random(seed)
    recent_count = min(len(samples), max(1, int(maximum * 0.4)))
    recent = samples[-recent_count:]
    older = samples[:-recent_count]
    rare = [sample for sample in older if sample.outcome in {"no_response", "target_lost"}]
    rare_count = min(len(rare), max(1, int(maximum * 0.2)))
    chosen_rare = rng.sample(rare, rare_count) if len(rare) > rare_count else rare
    used = {sample.step_id for sample in recent + chosen_rare}
    candidates = [sample for sample in older if sample.step_id not in used]
    remaining = maximum - len(recent) - len(chosen_rare)
    random_part = rng.sample(candidates, min(remaining, len(candidates)))
    return sorted(recent + chosen_rare + random_part, key=lambda sample: sample.order)


def normalizer(samples: list[SequenceSample]) -> tuple[np.ndarray, np.ndarray]:
    valid = np.concatenate([sample.features[sample.mask.astype(bool)] for sample in samples], axis=0)
    mean, std = valid.mean(axis=0), valid.std(axis=0)
    std[std < 1e-5] = 1.0
    return mean.astype(np.float32), std.astype(np.float32)


def batches(samples: list[SequenceSample], mean: np.ndarray, std: np.ndarray, batch_size: int, device: str, shuffle: bool) -> Iterable[tuple[torch.Tensor, torch.Tensor, dict[str, torch.Tensor]]]:
    indices = list(range(len(samples)))
    if shuffle:
        random.shuffle(indices)
    for offset in range(0, len(indices), batch_size):
        selected = [samples[index] for index in indices[offset:offset + batch_size]]
        features = np.stack([((sample.features - mean) / std) * sample.mask[:, None] for sample in selected])
        masks = np.stack([sample.mask for sample in selected])
        target_names = selected[0].targets.keys()
        targets = {name: torch.tensor([sample.targets[name] for sample in selected], dtype=torch.float32, device=device) for name in target_names}
        yield torch.tensor(features, dtype=torch.float32, device=device), torch.tensor(masks, dtype=torch.float32, device=device), targets


def evaluate(model: FocusGRU, samples: list[SequenceSample], mean: np.ndarray, std: np.ndarray, batch_size: int, device: str) -> float:
    model.eval()
    weighted, count = 0.0, 0
    with torch.no_grad():
        for features, masks, targets in batches(samples, mean, std, batch_size, device, False):
            loss, _ = model_loss(model(features, masks), targets)
            weighted += float(loss) * len(features)
            count += len(features)
    return weighted / max(1, count)


def sample_axis(sample: SequenceSample) -> str:
    current = sample.features[-1]
    horizontal_seconds = float(current[4] + current[5])
    vertical_seconds = float(current[6] + current[7])
    return "horizontal" if horizontal_seconds >= vertical_seconds else "vertical"


def evaluate_axis_movement_nll(
    model: FocusGRU,
    samples: list[SequenceSample],
    mean: np.ndarray,
    std: np.ndarray,
    batch_size: int,
    device: str,
    axis: str,
) -> float | None:
    axis_index = 0 if axis == "horizontal" else 1
    selected = [sample for sample in samples if sample_axis(sample) == axis and sample.targets["movement_mask"]]
    if not selected:
        return None
    model.eval()
    weighted, count = 0.0, 0
    with torch.no_grad():
        for features, masks, targets in batches(selected, mean, std, batch_size, device, False):
            outputs = model(features, masks)
            target = targets["dx" if axis_index == 0 else "dy"]
            means = outputs["means"][:, :, axis_index]
            scales = outputs["scales"][:, :, axis_index]
            log_component = -0.5 * ((target[:, None] - means) / scales) ** 2
            log_component -= torch.log(scales) + 0.5 * math.log(2.0 * math.pi)
            loss = -torch.logsumexp(F.log_softmax(outputs["logits"], -1) + log_component, -1)
            weighted += float(loss.sum())
            count += len(features)
    return weighted / max(1, count)


def evaluate_axis_prediction_accuracy(
    model: FocusGRU,
    samples: list[SequenceSample],
    mean: np.ndarray,
    std: np.ndarray,
    batch_size: int,
    device: str,
    axis: str,
) -> dict[str, float | int | None]:
    axis_index = 0 if axis == "horizontal" else 1
    axis_name = "x" if axis_index == 0 else "y"
    selected = [
        sample for sample in samples
        if sample_axis(sample) == axis
        and sample.targets["movement_mask"]
        and math.isfinite(sample.linear_delta.get(axis_name, float("nan")))
    ]
    if not selected:
        return {"linear_accuracy": None, "gru_accuracy": None, "samples": 0}
    linear_correct = 0
    gru_correct = 0
    count = 0
    model.eval()
    with torch.no_grad():
        for offset in range(0, len(selected), batch_size):
            group = selected[offset:offset + batch_size]
            features = np.stack([((sample.features - mean) / std) * sample.mask[:, None] for sample in group])
            masks = np.stack([sample.mask for sample in group])
            outputs = model(
                torch.tensor(features, dtype=torch.float32, device=device),
                torch.tensor(masks, dtype=torch.float32, device=device),
            )
            weights = F.softmax(outputs["logits"], -1)
            gru_values = (weights * outputs["means"][:, :, axis_index]).sum(-1).cpu().tolist()
            for sample, gru_value in zip(group, gru_values):
                actual = float(sample.targets["dx" if axis_name == "x" else "dy"])
                tolerance = max(0.01, abs(actual) * 0.25)
                linear_correct += abs(float(sample.linear_delta[axis_name]) - actual) <= tolerance
                gru_correct += abs(float(gru_value) - actual) <= tolerance
                count += 1
    return {
        "linear_accuracy": 100.0 * linear_correct / max(1, count),
        "gru_accuracy": 100.0 * gru_correct / max(1, count),
        "samples": count,
    }


def load_checkpoint(path: str | Path, device: str) -> tuple[FocusGRU, np.ndarray, np.ndarray, dict] | None:
    try:
        checkpoint = torch.load(path, map_location=device, weights_only=True)
        if checkpoint.get("feature_names") != list(FEATURE_NAMES):
            return None
        model = FocusGRU(int(checkpoint["hidden_size"]), int(checkpoint["mixtures"])).to(device)
        model.load_state_dict(checkpoint["model_state"])
        return model, np.asarray(checkpoint["feature_mean"], dtype=np.float32), np.asarray(checkpoint["feature_std"], dtype=np.float32), checkpoint
    except (OSError, KeyError, RuntimeError, ValueError, TypeError):
        return None


def save_checkpoint(path: str | Path, model: FocusGRU, mean: np.ndarray, std: np.ndarray, metadata: dict) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.{time.time_ns()}.tmp")
    torch.save({
        "schema_version": 2, "feature_names": list(FEATURE_NAMES), "feature_mean": mean.tolist(),
        "feature_std": std.tolist(), "hidden_size": model.hidden_size, "mixtures": model.mixtures,
        "model_state": model.state_dict(), **metadata,
    }, temporary)
    temporary.replace(output)


def train_challenger(
    samples: list[SequenceSample],
    args: argparse.Namespace,
    champion: tuple | None = None,
    loss_history: list[dict] | None = None,
) -> dict:
    selected = replay_subset(samples, args.replay_size, args.seed)
    split = min(len(selected) - 1, max(1, int(len(selected) * (1.0 - args.validation_fraction))))
    train_samples, validation = selected[:split], selected[split:]
    mean, std = normalizer(train_samples)
    model = FocusGRU(args.hidden_size, args.mixtures).to(args.device)
    if champion and champion[0].hidden_size == args.hidden_size and champion[0].mixtures == args.mixtures:
        model.load_state_dict(champion[0].state_dict())
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.learning_rate, weight_decay=1e-4)
    epoch_curve: list[dict] = []
    for epoch_index in range(args.epochs):
        model.train()
        for features, masks, targets in batches(train_samples, mean, std, args.batch_size, args.device, True):
            optimizer.zero_grad(set_to_none=True)
            loss, _ = model_loss(model(features, masks), targets)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 2.0)
            optimizer.step()
        horizontal_accuracy = evaluate_axis_prediction_accuracy(
            model, validation, mean, std, args.batch_size, args.device, "horizontal"
        )
        vertical_accuracy = evaluate_axis_prediction_accuracy(
            model, validation, mean, std, args.batch_size, args.device, "vertical"
        )
        epoch_curve.append({
            "trained_at": time.time(),
            "total_samples": len(samples),
            "epoch": epoch_index + 1,
            "epochs": args.epochs,
            "horizontal_left_right_nll": evaluate_axis_movement_nll(
                model, validation, mean, std, args.batch_size, args.device, "horizontal"
            ),
            "vertical_up_down_nll": evaluate_axis_movement_nll(
                model, validation, mean, std, args.batch_size, args.device, "vertical"
            ),
            "horizontal_linear_accuracy": horizontal_accuracy["linear_accuracy"],
            "horizontal_gru_accuracy": horizontal_accuracy["gru_accuracy"],
            "horizontal_accuracy_samples": horizontal_accuracy["samples"],
            "vertical_linear_accuracy": vertical_accuracy["linear_accuracy"],
            "vertical_gru_accuracy": vertical_accuracy["gru_accuracy"],
            "vertical_accuracy_samples": vertical_accuracy["samples"],
            "promoted": False,
        })
    validation_loss = evaluate(model, validation, mean, std, args.batch_size, args.device)
    horizontal_loss = epoch_curve[-1]["horizontal_left_right_nll"]
    vertical_loss = epoch_curve[-1]["vertical_up_down_nll"]
    champion_loss = float(champion[3].get("validation_loss", float("inf"))) if champion else float("inf")
    required_gain = args.minimum_improvement * max(abs(champion_loss), 1e-6)
    promoted = champion is None or (champion_loss - validation_loss) > required_gain
    curve_entry = epoch_curve[-1]
    curve_entry["validation_loss"] = validation_loss
    curve_entry["promoted"] = promoted
    updated_history = [item for item in (loss_history or []) if isinstance(item, dict)] + epoch_curve
    updated_history = updated_history[-100:]
    metadata = {
        "trained_at": curve_entry["trained_at"], "total_samples": len(samples),
        "sequence_length": int(getattr(args, "sequence_length", 12)),
        "training_samples": len(train_samples), "validation_samples": len(validation),
        "validation_loss": validation_loss,
        "horizontal_left_right_nll": horizontal_loss,
        "vertical_up_down_nll": vertical_loss,
        "previous_validation_loss": champion_loss if math.isfinite(champion_loss) else None,
        "promoted": promoted, "loss_history": updated_history,
    }
    if promoted:
        save_checkpoint(args.checkpoint, model, mean, std, metadata)
    return {**metadata, "model": model, "mean": mean, "std": std}


def predict(model: FocusGRU, mean: np.ndarray, std: np.ndarray, features: np.ndarray, mask: np.ndarray, device: str) -> dict:
    model.eval()
    normalized = ((features - mean) / std) * mask[:, None]
    x = torch.tensor(normalized[None], dtype=torch.float32, device=device)
    m = torch.tensor(mask[None], dtype=torch.float32, device=device)
    with torch.no_grad():
        output = model(x, m)
        weights = F.softmax(output["logits"], -1)[0]
        means = output["means"][0]
        expected = (weights[:, None] * means).sum(0)
        variance = (weights[:, None] * (output["scales"][0] ** 2 + means ** 2)).sum(0) - expected ** 2
    return {
        "predicted_delta": {"x": float(expected[0]), "y": float(expected[1])},
        "predicted_std": {"x": float(variance[0].clamp(min=0).sqrt()), "y": float(variance[1].clamp(min=0).sqrt())},
        "visibility_probability": float(torch.sigmoid(output["visibility_logit"])[0]),
        "no_response_probability": float(torch.sigmoid(output["no_response_logit"])[0]),
        "response_seconds": float(torch.expm1(output["latency"][0]).clamp(min=0)),
        "mixture_weights": [float(value) for value in weights],
    }


def tail_records(path: Path, offset: int, remainder: bytes) -> tuple[list[dict], int, bytes]:
    try:
        size = path.stat().st_size
        if size < offset:
            offset, remainder = 0, b""
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read()
            offset = stream.tell()
    except OSError:
        return [], offset, remainder
    parts = (remainder + chunk).split(b"\n")
    remainder = parts.pop() if parts else b""
    records = []
    for raw in parts:
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            records.append(value)
    return records, offset, remainder


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-jsonl", default="webcam-focus-pulse-prediction-errors.jsonl")
    parser.add_argument("--checkpoint", default="webcam-focus-gru-shadow.pt")
    parser.add_argument("--state-json", default="webcam-focus-gru-shadow-state.json")
    parser.add_argument("--shadow-jsonl", default="webcam-focus-gru-shadow-predictions.jsonl")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--sequence-length", type=int, default=12)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--mixtures", type=int, default=3)
    parser.add_argument("--minimum-training-samples", type=int, default=500)
    parser.add_argument("--train-every", type=int, default=250)
    parser.add_argument("--replay-size", type=int, default=100000)
    parser.add_argument("--validation-fraction", type=float, default=0.2)
    parser.add_argument("--minimum-improvement", type=float, default=0.005)
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--learning-rate", type=float, default=0.001)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=17)
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    if args.device != "cpu" and not torch.cuda.is_available():
        raise SystemExit(f"requested unavailable device: {args.device}")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    event_path = Path(args.event_jsonl)
    stop = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    completed = read_observed_records(event_path)
    samples = build_samples(completed, args.sequence_length)
    champion = load_checkpoint(args.checkpoint, args.device)
    prior_state = {}
    try:
        prior_state = json.loads(Path(args.state_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    loss_history = prior_state.get("loss_history") if isinstance(prior_state.get("loss_history"), list) else []
    if not loss_history and champion and isinstance(champion[3].get("loss_history"), list):
        loss_history = champion[3]["loss_history"]
    last_training: dict = {}
    checkpoint_sample_count = int(champion[3].get("total_samples", 0)) if champion else 0
    has_accuracy_history = any(
        isinstance(item, dict)
        and isinstance(item.get("horizontal_gru_accuracy"), (int, float))
        and isinstance(item.get("vertical_gru_accuracy"), (int, float))
        for item in loss_history
    )
    needs_initial_training = (
        champion is None
        or len(samples) - checkpoint_sample_count >= args.train_every
        or not has_accuracy_history
    )
    if len(samples) >= args.minimum_training_samples and needs_initial_training:
        print(f"Training GRU challenger from {len(samples)} causal sequences", flush=True)
        last_training = train_challenger(samples, args, champion, loss_history)
        loss_history = last_training["loss_history"]
        if last_training["promoted"]:
            champion = load_checkpoint(args.checkpoint, args.device)
        print(f"GRU validation_loss={last_training['validation_loss']:.5f} promoted={last_training['promoted']}", flush=True)
    offset = event_path.stat().st_size if event_path.exists() else 0
    remainder = b""
    new_training_samples = 0
    pending_predictions: dict[str, dict] = {}
    last_prediction: dict = {}

    while not stop:
        incoming, offset, remainder = tail_records(event_path, offset, remainder)
        for record in incoming:
            event = str(record.get("event") or "")
            step_id = str(record.get("step_id") or "")
            if event == "pulse_dispatched" and step_id and champion:
                features, mask = query_sequence(completed, record, args.sequence_length)
                prediction = predict(champion[0], champion[1], champion[2], features, mask, args.device)
                prediction.update({
                    "event": "shadow_prediction", "recorded_at": time.time(), "step_id": step_id,
                    "request_id": record.get("request_id"), "model_trained_at": champion[3].get("trained_at"),
                })
                pending_predictions[step_id] = prediction
                last_prediction = prediction
                append_jsonl(args.shadow_jsonl, prediction)
            elif event == "pulse_observed" and step_id:
                completed = [old for old in completed if str(old.get("step_id")) != step_id]
                completed.append(record)
                completed.sort(key=record_order)
                if str(record.get("outcome") or "") in TRAINING_OUTCOMES:
                    new_training_samples += 1
                prior = pending_predictions.pop(step_id, None)
                if prior:
                    observation = {
                        "event": "shadow_observation", "recorded_at": time.time(), "step_id": step_id,
                        "request_id": record.get("request_id"), "outcome": record.get("outcome"),
                        "actual_delta": record.get("actual_delta"), "prediction": prior,
                    }
                    append_jsonl(args.shadow_jsonl, observation)
        if new_training_samples >= args.train_every:
            samples = build_samples(completed, args.sequence_length)
            if len(samples) >= args.minimum_training_samples:
                print(f"Training GRU challenger after {new_training_samples} new sequences", flush=True)
                last_training = train_challenger(samples, args, champion, loss_history)
                loss_history = last_training["loss_history"]
                if last_training["promoted"]:
                    champion = load_checkpoint(args.checkpoint, args.device)
                print(f"GRU validation_loss={last_training['validation_loss']:.5f} promoted={last_training['promoted']}", flush=True)
            new_training_samples = 0
        atomic_write_json(args.state_json, {
            "status": "running", "pid": os.getpid(), "updated_at": time.time(), "device": args.device,
            "linear_controller_authoritative": True, "completed_transitions": len(completed),
            "training_sequences": len(samples), "new_sequences_since_training": new_training_samples,
            "checkpoint_available": champion is not None, "checkpoint": str(args.checkpoint),
            "champion_validation_loss": champion[3].get("validation_loss") if champion else None,
            "champion_trained_at": champion[3].get("trained_at") if champion else None,
            "loss_history": loss_history[-100:],
            "last_training": {key: value for key, value in last_training.items() if key not in {"model", "mean", "std", "loss_history"}},
            "last_shadow_prediction": last_prediction,
        })
        time.sleep(max(0.05, args.poll_seconds))


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()

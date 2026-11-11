import argparse
import json
from pathlib import Path

import numpy as np
import pytest
import torch

from scripts import focus_gru_shadow_trainer as trainer


def transition(step, request="run-1", outcome="observed", direction="left", actual=None, order=None):
    axis = "x" if direction in {"left", "right"} else "y"
    return {
        "event": "pulse_observed",
        "step_id": step,
        "request_id": request,
        "recorded_at": f"2026-01-01T00:00:{step[-2:]}.000Z",
        "dispatched_monotonic_ns": order if order is not None else int(step.split("-")[-1]) * 1_000_000_000,
        "outcome": outcome,
        "pre_center": {"x": 0.7, "y": 0.4},
        "pulses": [{"axis": axis, "direction": direction, "duration_ms": 100, "speed": 1, "backend": "native"}],
        "actual_delta": actual if actual is not None else {"x": 0.03 if axis == "x" else 0.0, "y": 0.02 if axis == "y" else 0.0},
        "predicted_delta": {"x": 0.025 if axis == "x" else 0.0, "y": 0.015 if axis == "y" else 0.0},
        "response_elapsed_seconds": 0.4,
        "prediction_model": {"direction_gains": {name: 0.0003 for name in trainer.DIRECTIONS}},
    }


def test_parser_uses_observations_deduplicates_and_sorts(tmp_path):
    path = tmp_path / "events.jsonl"
    observed = transition("step-2", order=2)
    newer_duplicate = {**observed, "actual_delta": {"x": 0.09, "y": 0.0}}
    lines = [
        json.dumps({**transition("step-1", order=1), "event": "pulse_dispatched"}),
        json.dumps(observed),
        "{partial",
        json.dumps(transition("step-1", order=1)),
        json.dumps(newer_duplicate),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    records = trainer.read_observed_records(path)
    assert [record["step_id"] for record in records] == ["step-1", "step-2"]
    assert records[-1]["actual_delta"]["x"] == 0.09


def test_sequences_are_left_padded_and_request_local():
    records = [
        transition("step-1", "a", order=1), transition("step-2", "b", order=2),
        transition("step-3", "a", order=3),
    ]
    samples = trainer.build_samples(records, sequence_length=4)
    by_step = {sample.step_id: sample for sample in samples}
    assert by_step["step-1"].mask.tolist() == [0, 0, 0, 1]
    assert by_step["step-3"].mask.tolist() == [0, 0, 1, 1]
    assert by_step["step-2"].mask.tolist() == [0, 0, 0, 1]


def test_current_outcome_and_actual_delta_cannot_leak_into_features():
    previous = transition("step-1", order=1)
    current = transition("step-2", order=2)
    changed = {**current, "outcome": "target_lost", "actual_delta": {"x": 999, "y": -999}, "response_elapsed_seconds": 99}
    first = trainer.causal_feature(current, previous, {"x": 1, "y": 0})
    second = trainer.causal_feature(changed, previous, {"x": 1, "y": 0})
    np.testing.assert_array_equal(first, second)


def test_context_features_and_history_are_causal():
    previous = transition("step-1", direction="left", order=1)
    previous["direction_gain_updates"] = {"left": {"raw_sample": 0.0004}}
    current = transition("step-2", direction="right", order=2)
    current["context"] = {
        "target_bbox": {"width": 0.2, "height": 0.3, "area": 0.06, "aspect_ratio": 2 / 3},
        "target_confidence": 0.91,
        "previous_global_motion": {"available": True, "x": 0.03, "y": -0.02, "magnitude": 0.036, "response": 0.8},
        "ptz_session_age_seconds": 12.0,
    }
    history = trainer.new_history_context()
    trainer.update_history_context(history, previous)
    feature = trainer.causal_feature(current, previous, {"x": 1, "y": 0}, history)
    values = dict(zip(trainer.FEATURE_NAMES, feature))

    assert values["bbox_width"] == pytest.approx(0.2)
    assert values["bbox_height"] == pytest.approx(0.3)
    assert values["target_confidence"] == pytest.approx(0.91)
    assert values["global_motion_x"] == pytest.approx(0.03)
    assert values["global_motion_valid"] == 1
    assert values["ptz_session_age_valid"] == 1
    assert values["recent_gain_last_left"] == pytest.approx(0.4)
    changed = {**current, "actual_delta": {"x": 99, "y": 99}, "direction_gain_updates": {"right": {"raw_sample": 0.003}}}
    np.testing.assert_array_equal(feature, trainer.causal_feature(changed, previous, {"x": 1, "y": 0}, history))


def test_outcome_masks():
    observed = trainer.outcome_targets(transition("step-1", outcome="observed"))
    no_response = trainer.outcome_targets(transition("step-2", outcome="no_response"))
    lost = trainer.outcome_targets(transition("step-3", outcome="target_lost", actual=None))
    cancelled = trainer.outcome_targets(transition("step-4", outcome="cancelled"))
    assert (observed["movement_mask"], observed["visible"], observed["no_response"]) == (1, 1, 0)
    assert (no_response["movement_mask"], no_response["visible"], no_response["no_response"]) == (1, 1, 1)
    assert (lost["movement_mask"], lost["visible"], lost["visible_mask"]) == (0, 0, 1)
    assert cancelled["movement_mask"] == cancelled["visible_mask"] == cancelled["no_response_mask"] == 0


def test_model_shapes_probabilities_and_fully_masked_loss_are_finite():
    model = trainer.FocusGRU(hidden_size=8, mixtures=3)
    output = model(torch.zeros(2, 4, trainer.FEATURE_DIM), torch.tensor([[0, 0, 0, 1], [0, 0, 1, 1.0]]))
    assert output["means"].shape == (2, 3, 2)
    assert torch.all(output["scales"] > 0)
    assert torch.allclose(torch.softmax(output["logits"], -1).sum(-1), torch.ones(2))
    targets = {name: torch.zeros(2) for name in ("dx", "dy", "movement_mask", "visible", "visible_mask", "no_response", "no_response_mask", "latency", "latency_mask")}
    loss, _ = trainer.model_loss(output, targets)
    assert torch.isfinite(loss)


def test_optimizer_cycle_saves_and_reloads_checkpoint(tmp_path):
    records = [transition(f"step-{index}", request=f"run-{index // 5}", direction=("left", "right", "up", "down")[index % 4], order=index) for index in range(1, 25)]
    samples = trainer.build_samples(records, sequence_length=4)
    checkpoint = tmp_path / "shadow.pt"
    args = argparse.Namespace(
        replay_size=100, seed=3, validation_fraction=0.2, hidden_size=8, mixtures=2,
        device="cpu", learning_rate=0.002, epochs=2, batch_size=8,
        minimum_improvement=0.0, checkpoint=str(checkpoint),
    )
    result = trainer.train_challenger(samples, args)
    loaded = trainer.load_checkpoint(checkpoint, "cpu")
    assert result["promoted"] is True
    assert np.isfinite(result["horizontal_left_right_nll"])
    assert np.isfinite(result["vertical_up_down_nll"])
    assert np.isfinite(result["loss_history"][-1]["horizontal_linear_accuracy"])
    assert np.isfinite(result["loss_history"][-1]["horizontal_gru_accuracy"])
    assert np.isfinite(result["loss_history"][-1]["vertical_linear_accuracy"])
    assert np.isfinite(result["loss_history"][-1]["vertical_gru_accuracy"])
    assert result["loss_history"][-1]["total_samples"] == len(samples)
    assert checkpoint.exists() and loaded is not None
    assert len(result["loss_history"]) == 2
    assert [point["epoch"] for point in result["loss_history"]] == [1, 2]
    assert len(loaded[3]["loss_history"]) == 2
    features, mask = samples[-1].features, samples[-1].mask
    first = trainer.predict(result["model"], result["mean"], result["std"], features, mask, "cpu")
    second = trainer.predict(loaded[0], loaded[1], loaded[2], features, mask, "cpu")
    assert first["predicted_delta"] == second["predicted_delta"]

import io
import itertools
import json
from urllib.error import HTTPError

from scripts.autofocus_live_suite import (
    LiveAutofocusClient,
    center_within_deadzone,
    exception_details,
    focus_result_passed,
    iteration_numbers,
    main,
    object_center,
    parse_args,
    quiescent_focus_snapshot,
    random_move_plan,
    random_pulse_durations,
    wait_for_autofocus_idle,
    wait_for_first_complete_event,
    wait_for_logical_autofocus,
)


def test_random_move_plan_is_seeded_and_uses_ui_directions():
    assert random_move_plan(1, 3) == random_move_plan(1, 3)
    assert 1 <= len(random_move_plan(1, 3)) <= 3
    assert set(random_move_plan(1, 3)).issubset({"left", "right", "up", "down"})


def test_random_move_plan_uniformly_varies_count_up_to_configured_maximum():
    counts = {len(random_move_plan(seed, 4)) for seed in range(100)}

    assert counts == {1, 2, 3, 4}
    assert len(random_move_plan(5, 1)) == 1


def test_random_move_plan_can_limit_trials_to_one_axis():
    vertical = [direction for seed in range(20) for direction in random_move_plan(seed, 3, ("up", "down"))]

    assert set(vertical) == {"up", "down"}


def test_random_pulse_durations_are_seeded_and_uniformly_bounded():
    assert random_pulse_durations(7, 4, 500, 1000) == random_pulse_durations(7, 4, 500, 1000)
    values = [value for seed in range(100) for value in random_pulse_durations(seed, 3, 500, 1000)]

    assert all(500 <= value <= 1000 for value in values)
    assert min(values) < 520
    assert max(values) > 950
    assert random_pulse_durations(1, 2, 500, 200) == [200, 200]


def test_live_suite_test_timeout_defaults_to_twenty_seconds_and_keeps_old_alias(monkeypatch):
    monkeypatch.setattr("sys.argv", ["autofocus_live_suite.py"])
    assert parse_args().autofocus_timeout == 20.0
    assert parse_args().target == "any"
    assert parse_args().delay_between_tests == 0.0
    monkeypatch.setattr("sys.argv", ["autofocus_live_suite.py", "--test-timeout", "25"])
    assert parse_args().autofocus_timeout == 25.0
    monkeypatch.setattr("sys.argv", ["autofocus_live_suite.py", "--autofocus-timeout", "30"])
    assert parse_args().autofocus_timeout == 30.0


def test_live_suite_delay_between_tests_is_configurable(monkeypatch):
    monkeypatch.setattr("sys.argv", ["autofocus_live_suite.py", "--delay-between-tests", "1.5"])

    assert parse_args().delay_between_tests == 1.5


def test_iteration_numbers_runs_forever_only_for_minus_one():
    assert list(iteration_numbers(3)) == [1, 2, 3]
    assert list(iteration_numbers(0)) == [1]
    assert list(itertools.islice(iteration_numbers(-1), 4)) == [1, 2, 3, 4]


def test_exception_details_keeps_type_and_nonempty_message():
    assert exception_details(TimeoutError("too slow")) == {
        "error": "too slow",
        "error_type": "TimeoutError",
    }
    assert exception_details(RuntimeError())["error"] == "RuntimeError()"


def test_main_continues_after_test_and_inter_test_timeouts(monkeypatch, tmp_path):
    calls = []

    def fail_iteration(_client, **kwargs):
        calls.append(kwargs["iteration"])
        raise TimeoutError(f"test {kwargs['iteration']} timeout")

    def fail_idle(*_args, **_kwargs):
        raise TimeoutError("idle timeout")

    results = tmp_path / "results.jsonl"
    monkeypatch.setattr("scripts.autofocus_live_suite.run_iteration", fail_iteration)
    monkeypatch.setattr("scripts.autofocus_live_suite.wait_for_autofocus_idle", fail_idle)
    monkeypatch.setattr(
        "sys.argv",
        [
            "autofocus_live_suite.py",
            "--execute",
            "--iterations",
            "3",
            "--target",
            "clock",
            "--results",
            str(results),
        ],
    )

    assert main() == 1
    assert calls == [1, 2, 3]
    records = [json.loads(line) for line in results.read_text().splitlines()]
    assert len(records) == 3
    assert records[0]["error_type"] == "TimeoutError"
    assert records[0]["inter_test_idle_error_type"] == "TimeoutError"
    assert "idle timeout" in records[0]["error"]
    assert "inter_test_idle_error" not in records[-1]


def test_main_delays_only_between_tests(monkeypatch, tmp_path):
    sleeps = []

    def pass_iteration(_client, **kwargs):
        return {"iteration": kwargs["iteration"], "passed": True}

    monkeypatch.setattr("scripts.autofocus_live_suite.run_iteration", pass_iteration)
    monkeypatch.setattr("scripts.autofocus_live_suite.time.sleep", sleeps.append)
    monkeypatch.setattr(
        "sys.argv",
        [
            "autofocus_live_suite.py",
            "--execute",
            "--iterations",
            "3",
            "--delay-between-tests",
            "1.5",
            "--results",
            str(tmp_path / "results.jsonl"),
        ],
    )

    assert main() == 0
    assert sleeps == [1.5, 1.5]


def test_logical_autofocus_follows_replacement_request_before_releasing_next_test():
    steps = [
        (
            {"request_id": "focus-1", "enabled": True, "target_label": "clock", "trigger": "realtime_deepstream_focus", "requested_at": 101.0},
            {"request_id": "focus-1", "enabled": True, "status": "focusing"},
        ),
        (
            {"request_id": "focus-1", "enabled": False, "target_label": "clock", "trigger": "realtime_deepstream_focus", "requested_at": 101.0},
            {"request_id": "focus-1", "enabled": False, "status": "failed"},
        ),
        (
            {"request_id": "focus-2", "enabled": True, "target_label": "clock", "trigger": "realtime_deepstream_focus", "requested_at": 102.0},
            {"request_id": "focus-2", "enabled": True, "status": "focusing"},
        ),
        (
            {"request_id": "focus-2", "enabled": False, "target_label": "clock", "trigger": "realtime_deepstream_focus", "requested_at": 102.0},
            {"request_id": "focus-2", "enabled": False, "status": "complete"},
        ),
    ]

    class FakeClient:
        index = 0

        def focus_command(self):
            return steps[min(self.index, len(steps) - 1)][0]

        def focus_state(self):
            value = steps[min(self.index, len(steps) - 1)][1]
            self.index += 1
            return value

    terminal, request_chain = wait_for_logical_autofocus(
        FakeClient(),
        "clock",
        baseline_request_id="focus-0",
        autofocus_armed_at=100.0,
        timeout=1.0,
        poll_seconds=0.02,
        quiet_seconds=0.0,
    )

    assert request_chain == ["focus-1", "focus-2"]
    assert terminal["request_id"] == "focus-2"


def test_first_complete_event_requires_requested_target_and_returns_its_first_terminal_result():
    class FakeClient:
        def focus_state(self):
            return {
                "history": [
                    {"request_id": "old", "target_label": "clock", "status": "complete", "completed_at": 90.0},
                    {"request_id": "other", "target_label": "bed", "status": "complete", "completed_at": 101.0},
                    {"request_id": "failed", "target_label": "clock", "status": "failed", "completed_at": 102.0},
                    {"request_id": "winner", "target_label": "clock", "status": "complete", "completed_at": 103.0,
                     "runtime_seconds": 2.5, "pulse_count": 6},
                ],
                "request_id": "winner",
                "target_label": "clock",
                "status": "complete",
            }

    event, request_ids = wait_for_first_complete_event(
        FakeClient(),
        "clock",
        after_at=100.0,
        baseline_request_ids={"old"},
        timeout=1.0,
        poll_seconds=0.02,
    )

    assert event["request_id"] == "failed"
    assert event["target_label"] == "clock"
    assert request_ids == ["failed"]


def test_inter_test_idle_wait_resets_when_a_followup_request_starts():
    samples = [
        ({"request_id": "one", "enabled": False}, {"request_id": "one", "enabled": False, "status": "complete"}),
        ({"request_id": "two", "enabled": True}, {"request_id": "two", "enabled": True, "status": "focusing"}),
        ({"request_id": "two", "enabled": False}, {"request_id": "two", "enabled": False, "status": "complete"}),
    ]

    class FakeClient:
        index = 0

        def focus_command(self):
            return samples[min(self.index, len(samples) - 1)][0]

        def focus_state(self):
            value = samples[min(self.index, len(samples) - 1)][1]
            self.index += 1
            return value

    idle = wait_for_autofocus_idle(
        FakeClient(), timeout=1.0, poll_seconds=0.02, quiet_seconds=0.04,
    )

    assert idle["state_request_id"] == "two"
    assert idle["state_status"] == "complete"
    assert idle["quiet_seconds"] >= 0.04


def test_object_center_selects_highest_confidence_matching_target():
    payload = {"objects": [
        {"label": "person", "confidence": 0.99, "bbox": [0, 0, 10, 10], "frame_width": 100, "frame_height": 100},
        {"label": "clock", "confidence": 0.75, "bbox": [40, 30, 20, 40], "frame_width": 100, "frame_height": 100},
        {"label": "clock", "confidence": 0.90, "bbox": [45, 40, 10, 20], "frame_width": 100, "frame_height": 100},
    ]}

    assert object_center(payload, "clock") == {
        "x": 0.5,
        "y": 0.5,
        "confidence": 0.9,
        "bbox": [45, 40, 10, 20],
        "frame_size": [100.0, 100.0],
    }
    assert object_center(payload, "any")["label"] == "person"


def test_center_within_deadzone_checks_both_axes():
    deadzone = {"x": 0.02, "y": 0.022}
    assert center_within_deadzone({"x": 0.519, "y": 0.479}, deadzone) is True
    assert center_within_deadzone({"x": 0.521, "y": 0.5}, deadzone) is False
    assert center_within_deadzone(None, deadzone) is False


def test_live_result_requires_controller_completion_observed_center_and_matching_target():
    assert focus_result_passed("complete") is True
    assert focus_result_passed("complete", centered=False) is False
    assert focus_result_passed("complete", target_matches=False) is False
    assert focus_result_passed("failed") is False


def test_ui_pulse_treats_dispatched_504_as_ambiguous_motion(monkeypatch, tmp_path):
    body = io.BytesIO(json.dumps({
        "status": "error", "ambiguous": True, "dispatched": True, "backend": "native",
    }).encode())
    error = HTTPError("http://camera/wifi-ptz", 504, "timeout", {}, body)
    monkeypatch.setattr("scripts.autofocus_live_suite.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))
    client = LiveAutofocusClient("http://camera", tmp_path / "command.json")

    result = client.ui_pulse("right", 1000, 1)

    assert result["status"] == "ambiguous"
    assert result["dispatched"] is True
    assert result["http_status"] == 504


def test_quiescent_snapshot_requires_matching_inactive_centered_run():
    command = {"enabled": False, "request_id": "focus-1"}
    state = {
        "enabled": False,
        "request_id": "focus-1",
        "status": "complete",
        "target_label": "clock",
        "object_center": {"x": 0.51, "y": 0.49},
        "effective_deadzone": {"x": 0.02, "y": 0.022},
    }
    center = {"x": 0.51, "y": 0.49}

    assert quiescent_focus_snapshot(command, state, center, "clock")["request_id"] == "focus-1"
    assert quiescent_focus_snapshot({**command, "enabled": True}, state, center, "clock") is None
    assert quiescent_focus_snapshot(command, {**state, "request_id": "focus-2"}, center, "clock") is None
    assert quiescent_focus_snapshot(command, {**state, "object_center": {"x": 0.54, "y": 0.49}}, center, "clock") is None
    assert quiescent_focus_snapshot(command, state, None, "clock") is None

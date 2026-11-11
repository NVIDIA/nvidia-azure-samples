from pathlib import Path


def dashboard_source() -> str:
    return (Path(__file__).resolve().parents[1] / "scripts/webcam_stream_server.py").read_text(encoding="utf-8")


def test_focus_tool_has_collapsed_completion_time_panel_below_history():
    source = dashboard_source()

    assert "function renderFocusCompletionTimes" in source
    assert "details.className = 'focus-completion-time'" in source
    assert "historyPanel.insertAdjacentElement('afterend', details)" in source
    assert "renderFocusCompletionTimes(panel, state, panelSource)" in source
    function = source[source.index("function renderFocusCompletionTimes"):source.index("function renderFocusModelControls")]
    assert "details.open" not in function


def test_completion_time_curve_is_rolling_average_of_successful_runs():
    source = dashboard_source()

    assert "function focusCompletionTimeSeries(history, windowSize = 10)" in source
    assert "String(item?.status || '').toLowerCase() === 'complete'" in source
    assert "Number(item.runtime_seconds) >= 0" in source
    assert "completed.slice(Math.max(0, index - windowSize + 1), index + 1)" in source
    assert "10-run average" in source
    assert "Average successful autofocus completion time over completed runs" in source
    assert "manual-input pauses excluded" in source

from pathlib import Path


def test_focus_tool_has_collapsed_axis_loss_curves_under_history():
    source = (Path(__file__).resolve().parents[1] / "scripts/webcam_stream_server.py").read_text(encoding="utf-8")

    assert "function renderFocusGruLoss" in source
    assert "historyPanel.insertAdjacentElement('afterend', details)" in source
    assert "details.className = 'focus-gru-loss'" in source
    assert "details.open" not in source[source.index("function renderFocusGruLoss"):source.index("function focusStepActionText")]
    assert "focusGruLossCard('LEFT / RIGHT'" in source
    assert "focusGruLossCard('UP / DOWN'" in source
    assert "horizontal_left_right_nll" in source
    assert "vertical_up_down_nll" in source
    assert "height: clamp(20rem, 42vh, 28rem)" in source
    assert "scroll-padding-bottom: 2rem" in source
    assert "overflow-y: scroll !important" in source
    assert "overscroll-behavior: contain" in source
    assert ".focus-gru-loss-content::-webkit-scrollbar { width: 12px; }" in source
    assert "scrollbar-color: #71849a #121a24" in source
    assert "function focusPredictionAccuracyChart" in source
    assert "Linear ${points[points.length - 1].linear.toFixed(1)}%" in source
    assert "GRU ${points[points.length - 1].gru.toFixed(1)}%" in source
    assert "Within max(0.01 frame units, 25% of observed displacement)" in source


def test_focus_tool_compares_actual_steps_and_overshoots_by_axis_model():
    source = (Path(__file__).resolve().parents[1] / "scripts/webcam_stream_server.py").read_text(encoding="utf-8")

    assert "function focusPerformanceSeries(history, axis, metric)" in source
    assert "function focusActualPerformanceChart(history, axis, metric)" in source
    assert "ACTUAL STEPS TO FOCUS" in source
    assert "ACTUAL OVERSHOOT RATE" in source
    assert "item?.axis_metrics?.[axis]" in source
    assert "axisMetric.effective_model" in source
    assert "String(item?.status || '').toLowerCase() === 'complete'" in source
    assert "Number(axisMetric.steps) > 0" in source
    assert "sum.count += Number(axisMetric.overshoot_count)" in source
    assert "sum.batches += Number(axisMetric.observed_batches)" in source
    assert "Rolling mean over 10 successful runs" in source
    assert "Rolling rate over 20 observed correction batches" in source


def test_focus_performance_curves_use_focus_state_and_tolerate_missing_history():
    source = (Path(__file__).resolve().parents[1] / "scripts/webcam_stream_server.py").read_text(encoding="utf-8")

    assert "focusState = latestFocusObjectState" in source
    assert "Array.isArray(focusState?.performance_history) ? focusState.performance_history : []" in source
    assert "renderFocusGruLoss(panel, latestFocusGruState, state)" in source
    assert "performanceHistory, 'x'" in source
    assert "performanceHistory, 'y'" in source
    assert "Linear and GRU actual autofocus steps to focus curves" in source
    assert "Linear and GRU actual autofocus overshoot rate curves" in source


def test_focus_gru_state_has_dashboard_endpoint_and_path_argument():
    source = (Path(__file__).resolve().parents[1] / "scripts/webcam_stream_server.py").read_text(encoding="utf-8")

    assert 'elif path == "/focus-gru-state.json"' in source
    assert 'parser.add_argument("--focus-gru-state-path"' in source


def test_focus_settings_offer_independent_axis_model_selectors():
    source = (Path(__file__).resolve().parents[1] / "scripts/webcam_stream_server.py").read_text(encoding="utf-8")

    assert "buildModelRow('horizontal', 'Left / right model')" in source
    assert "buildModelRow('vertical', 'Up / down model')" in source
    assert "GRU recurrent model" in source
    assert "focus_horizontal_model" in source
    assert "focus_vertical_model" in source
    assert "function renderFocusModelControls" in source
    assert "renderFocusModelControls(panel)" in source

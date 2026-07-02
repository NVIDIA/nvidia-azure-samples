import argparse
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import nemotron_voice_responder as responder
from scripts import webcam_stream_server as server
from scripts.tool_planner_config import (
    DEFAULT_TOOL_PLANNER_TEMPLATE,
    read_tool_planner_template,
    write_tool_planner_template,
)


def test_planner_prompt_separates_system_policy_from_current_input():
    prompt = responder.tool_planner_prompt(
        {
            "source": "server",
            "text": "Current timestamp: 2026-06-30T12:44:51-04:00",
            "system_response_policy": "For timestamps, search the web for current news.",
        },
        {},
        {},
        DEFAULT_TOOL_PLANNER_TEMPLATE,
    )

    assert "ACTIVE SYSTEM RESPONSE POLICY:\nFor timestamps, search the web for current news." in prompt
    assert "CURRENT INPUT:\nCurrent timestamp: 2026-06-30T12:44:51-04:00" in prompt
    assert "You only select tools" in prompt
    assert "never invent tool results" in prompt
    assert "If it requires web search, online lookup, or current news, select web_search" in prompt
    assert 'web_search requires {"query":"specific non-empty search phrase"}' in prompt
    assert 'never call it with {}' in prompt
    assert '{"query":"recent news YYYY-MM-DD"}' in prompt
    assert "complete supplied calendar date exactly" in prompt
    assert "year-only or partial date is invalid" in prompt


def test_planner_template_persists_and_requires_critical_placeholders(tmp_path):
    path = tmp_path / "planner-template.json"
    template = "Policy: {{SYSTEM_RESPONSE_POLICY}}\nInput: {{CURRENT_INPUT}}"

    payload = write_tool_planner_template(path, {"template": template})

    assert payload["config_file"] == str(path.resolve())
    assert read_tool_planner_template(path) == template
    with pytest.raises(ValueError, match="CURRENT_INPUT"):
        write_tool_planner_template(path, {"template": "Policy: {{SYSTEM_RESPONSE_POLICY}}"})


def test_plan_tools_uses_persisted_template_and_accepts_timestamp_web_search(monkeypatch, tmp_path):
    template_path = tmp_path / "planner-template.json"
    write_tool_planner_template(template_path, {"template": DEFAULT_TOOL_PLANNER_TEMPLATE})
    captured = {}

    def fake_ollama_json(_url, _path, payload, **_kwargs):
        captured["prompt"] = payload["prompt"]
        return {
            "response": (
                '{"needs_tools":true,"calls":[{"name":"web_search",'
                '"args":{"query":"top news June 30 2026"}}],"reason":"policy requires news"}'
            )
        }

    monkeypatch.setattr(responder, "ollama_json", fake_ollama_json)
    args = argparse.Namespace(
        enable_tools=True,
        camera_tools=True,
        camera_ptz_default_source="wifi",
        tool_planner_model="nemotron-mini:latest",
        enable_environment_tools=False,
        tool_planner_template_json=str(template_path),
        tool_planner_queue_json=str(tmp_path / "queue.json"),
        tool_planner_url="http://planner",
        ollama_url="http://ollama",
        max_tool_calls=3,
        tool_planner_num_predict=96,
        tool_planner_num_ctx=768,
        nemotron_keep_alive="60m",
        timeout=30.0,
        tool_planner_timeout=8.0,
    )

    plan, _raw = responder.plan_tools(
        args,
        "fallback-model",
        {
            "source": "server",
            "text": "Current timestamp: 2026-06-30T12:44:51-04:00",
            "system_response_policy": "Search the web for current news for this date.",
        },
        {},
        {},
    )

    assert plan["calls"] == [
        {"name": "web_search", "args": {"query": "top news June 30 2026"}}
    ]
    assert "ACTIVE SYSTEM RESPONSE POLICY:\nSearch the web for current news for this date." in captured["prompt"]
    assert "CURRENT INPUT:\nCurrent timestamp: 2026-06-30T12:44:51-04:00" in captured["prompt"]


def test_dashboard_exposes_planner_details_editor_and_endpoints():
    html = server.render_dashboard_html(
        "test",
        "device",
        8443,
        planner_template="Route {{CURRENT_INPUT}} & inspect <camera>.",
        planner_template_file="/tmp/planner-template.json",
    )

    assert "Planner Details" in html
    assert 'id="planner-template"' in html
    assert 'id="planner-template-save"' in html
    assert "fetchJson('/tool-planner-template.json')" in html
    assert "endpoint('/tool-planner-template')" in html
    assert "plannerTemplateLoadRetryTimer" in html
    assert "initializePlannerTemplate();" in html
    assert "Route {{CURRENT_INPUT}} &amp; inspect &lt;camera&gt;." in html
    assert "/tmp/planner-template.json" in html
    assert 'id="planner-template-status">ready</span>' in html


def test_stream_server_parser_has_shared_planner_template_default(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["webcam_stream_server.py"])
    args = server.parse_args()

    assert Path(args.tool_planner_template_path).name == "webcam-tool-planner-template.json"

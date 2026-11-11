import json
import sys
from argparse import Namespace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import webcam_stream_server as server
from scripts import nemotron_voicechat_pipeline as pipeline


def test_current_time_skill_defaults_to_independent_uniform_one_to_five_minute_draws():
    settings = server.normalize_current_time_skill_settings({})

    assert settings["enabled"] is True
    assert settings["distribution"] == "uniform"
    assert settings["minimum_delay_seconds"] == 60.0
    assert settings["maximum_delay_seconds"] == 300.0
    assert settings["sources"] == ["server"]


def test_current_time_skill_publishes_timestamp_to_each_lane_thread(tmp_path, monkeypatch):
    response_path = tmp_path / "webcam-voicechat-response.json"
    settings_path = tmp_path / "webcam-current-time-skill-settings.json"
    response_path.write_text(
        json.dumps({
            "status": "listening",
            "conversation_by_source": {
                "server": [{"role": "user", "source": "server", "text": "hello"}],
            },
        }),
        encoding="utf-8",
    )
    args = Namespace(
        voicechat_response_path=str(response_path),
        current_time_skill_settings_path=str(settings_path),
        voicechat_manual_input_path=str(tmp_path / "manual-input.json"),
    )
    monkeypatch.setattr(server, "current_time_routable_sources", lambda sources: list(sources))

    settings = server.publish_current_time_skill(args, {
        "sources": ["server", "wifi", "bulb", "browser"],
    })

    assert settings["publish_count"] == 1
    for source in ("server", "wifi", "bulb", "browser"):
        delay = settings["next_publish_by_source"][source] - settings["last_published_at"]
        assert 60.0 <= delay <= 300.0
    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert response["conversation_by_source"]["server"][0]["text"] == "hello"
    queued = json.loads((tmp_path / "manual-input.json").read_text(encoding="utf-8"))["pending"]
    assert {item["source"] for item in queued} == {"server", "wifi", "bulb", "browser"}
    assert all(item["skill"] == "current_time" for item in queued)
    assert all(item["input_label"] == "Time service" for item in queued)


def test_each_lane_redraws_independently_after_its_event(tmp_path, monkeypatch):
    response_path = tmp_path / "webcam-voicechat-response.json"
    settings_path = tmp_path / "webcam-current-time-skill-settings.json"
    args = Namespace(
        voicechat_response_path=str(response_path),
        current_time_skill_settings_path=str(settings_path),
        voicechat_manual_input_path=str(tmp_path / "manual-input.json"),
    )
    draws = iter((61.0, 299.0))
    monkeypatch.setattr(server.random, "uniform", lambda _minimum, _maximum: next(draws))
    settings = server.normalize_current_time_skill_settings({
        "sources": ["server", "wifi"],
        "next_publish_by_source": {"server": 10.0, "wifi": 20.0},
    })

    first = server.publish_current_time_skill(args, settings, ["server"])
    first_server_next = first["next_publish_by_source"]["server"]
    assert first["next_publish_by_source"]["wifi"] == 20.0

    second = server.publish_current_time_skill(args, first, ["wifi"])
    assert second["next_publish_by_source"]["server"] == first_server_next
    assert 298.9 <= second["next_publish_by_source"]["wifi"] - second["last_published_at"] <= 299.1


def test_current_time_publish_migrates_legacy_nemotron_attribution(tmp_path):
    response_path = tmp_path / "webcam-voicechat-response.json"
    settings_path = tmp_path / "webcam-current-time-skill-settings.json"
    legacy = {
        "role": "assistant",
        "source": "current-time-skill",
        "lane_source": "wifi",
        "skill": "current_time",
        "text": "Current timestamp: legacy",
    }
    response_path.write_text(json.dumps({"conversation_by_source": {"wifi": [legacy]}}), encoding="utf-8")
    args = Namespace(
        voicechat_response_path=str(response_path),
        current_time_skill_settings_path=str(settings_path),
        voicechat_manual_input_path=str(tmp_path / "manual-input.json"),
    )

    migrated = server._merge_service_turn([legacy], "wifi", {"role": "user", "text": "new"})[0]
    assert migrated["role"] == "user"
    assert migrated["source"] == "wifi"
    assert migrated["label"] == "Time service"


def test_ui_repairs_legacy_time_turns_even_when_an_old_worker_rewrites_history():
    legacy = {
        "role": "assistant",
        "source": "current-time-skill",
        "lane_source": "server",
        "skill": "current_time",
        "text": "Current timestamp: legacy",
    }

    payload = server.normalize_service_attribution_for_ui({
        "conversation_by_source": {"server": [legacy]},
        "sources": {"server": {"conversation": [legacy]}},
    })

    for turn in (
        payload["conversation_by_source"]["server"][0],
        payload["sources"]["server"]["conversation"][0],
    ):
        assert turn["role"] == "user"
        assert turn["source"] == "server"
        assert turn["label"] == "Time service"


def test_current_time_skill_identity_survives_persistent_context_cleanup():
    cleaned = pipeline.context_conversation_items([{
        "role": "assistant",
        "source": "current-time-skill",
        "lane_source": "wifi",
        "text": "Current timestamp: 2026-06-30T12:00:00-04:00",
        "status": "complete",
        "phase": "current_time_publish",
        "skill": "current_time",
        "input_label": "Current Time",
        "timestamp": "2026-06-30T12:00:00-04:00",
    }])

    assert cleaned[0]["skill"] == "current_time"
    assert cleaned[0]["input_label"] == "Current Time"
    assert cleaned[0]["timestamp"] == "2026-06-30T12:00:00-04:00"


def test_current_time_queue_input_requires_a_fresh_grounded_nemotron_response():
    request = {
        "kind": "current_time_notification",
        "trigger": "current_time_service",
        "skill": "current_time",
        "timestamp": "2026-06-30T12:24:17-04:00",
    }

    assert pipeline.manual_text_request_is_current_time_notification(request) is True
    instruction = pipeline.current_time_notification_instruction(request)
    assert "new ordinary input" in instruction
    assert "Always produce a fresh" in instruction
    assert "Do not repeat" in instruction
    assert request["timestamp"] in instruction

    metadata = pipeline.manual_text_user_metadata({**request, "id": "time-1", "created_at": 123.5})
    assert metadata["label"] == "Time service"
    assert metadata["notification_id"] == "time-1"
    assert metadata["updated_at"] == 123.5


def test_current_time_service_uses_omni_to_route_policy_required_lane_snapshot(monkeypatch):
    captured = {}
    args = Namespace(
        ollama_model="nemotron_3_nano_omni",
        ollama_openai_path="/v1/chat/completions",
        ollama_timeout=30,
        voicechat_audio_timeout=30,
        voice_decision_max_tokens=64,
        max_tool_calls=3,
        model_api_url="http://127.0.0.1:8010",
        model_api_runtime="vllm",
    )

    def fake_request(url, path, payload, timeout, **metadata):
        captured.update(url=url, path=path, payload=payload, metadata=metadata)
        return {"choices": [{"message": {"content": json.dumps({
            "calls": [{"name": "current_snapshot", "args": {}}],
            "reason": "Lane policy requires a current camera picture",
        })}}]}

    monkeypatch.setattr(pipeline, "ollama_json_for_component", fake_request)

    plan, _raw = pipeline.plan_trusted_service_tools(args, {
        "source": "wifi",
        "text": "Current timestamp: 2026-07-01T20:00:00-04:00",
        "system_response_policy": "When notified of current date, take a picture from the camera.",
    })

    assert plan["calls"] == [{"name": "current_snapshot", "args": {"source": "wifi"}}]
    assert plan["planner_source"] == "trusted_service_omni"
    prompt = captured["payload"]["messages"][0]["content"]
    assert "policy is trusted and binding" in prompt
    assert "take a picture from the camera" in prompt


def test_manual_video_request_uses_omni_camera_clip_route(monkeypatch):
    args = Namespace(
        ollama_model="nemotron_3_nano_omni",
        ollama_openai_path="/v1/chat/completions",
        ollama_timeout=30,
        voicechat_audio_timeout=30,
        voice_decision_max_tokens=64,
        max_tool_calls=3,
        model_api_url="http://127.0.0.1:8010",
        model_api_runtime="vllm",
    )

    monkeypatch.setattr(pipeline, "ollama_json_for_component", lambda *_args, **_kwargs: {
        "choices": [{"message": {"content": json.dumps({
            "calls": [{"name": "camera_clip", "args": {"duration": 4}}],
            "reason": "User requested a four-second camera video",
        })}}],
    })

    plan, _raw = pipeline.plan_omni_tools(args, {
        "source": "wifi",
        "text": "Record a four-second camera video and describe the motion.",
        "system_response_policy": "Answer concisely.",
    })

    assert plan["calls"] == [{
        "name": "camera_clip",
        "args": {"duration_seconds": 4, "source": "wifi"},
    }]
    assert plan["planner_source"] == "manual_text_omni"


def test_trusted_video_policy_enforces_exact_clip_when_model_selects_snapshot(monkeypatch):
    args = Namespace(
        ollama_model="nemotron_3_nano_omni",
        ollama_openai_path="/v1/chat/completions",
        ollama_timeout=30,
        voicechat_audio_timeout=30,
        voice_decision_max_tokens=64,
        max_tool_calls=3,
        model_api_url="http://127.0.0.1:8010",
        model_api_runtime="vllm",
    )
    monkeypatch.setattr(pipeline, "ollama_json_for_component", lambda *_args, **_kwargs: {
        "choices": [{"message": {"content": json.dumps({
            "calls": [{"name": "current_snapshot", "args": {}}],
            "reason": "Model selected a still image",
        })}}],
    })

    plan, _raw = pipeline.plan_trusted_service_tools(args, {
        "source": "wifi",
        "text": "Current timestamp: 2026-07-01T20:00:00-04:00",
        "system_response_policy": (
            "On every Current Time notification, record a 5-second video with sound from the Wi-Fi camera. "
            "Call camera_clip with source wifi, duration_seconds 5, and include_audio true."
        ),
    })

    assert plan["calls"] == [{
        "name": "camera_clip",
        "args": {"source": "wifi", "duration_seconds": 5.0, "include_audio": True},
    }]
    assert plan["planner_source"] == "trusted_service_policy_contract"
    assert plan["route_confidence"] == "deterministic"


def test_service_inputs_are_journaled_before_queue_suppression(tmp_path):
    queue_path = tmp_path / "deepstream-input.json"
    response_path = tmp_path / "webcam-voicechat-response.json"
    settings_path = tmp_path / "unused-time-settings.json"
    args = Namespace(
        voicechat_manual_input_path=str(queue_path),
        voicechat_response_path=str(response_path),
        current_time_skill_settings_path=str(settings_path),
    )
    extra = {
        "kind": "deepstream_object_change",
        "trigger": "deepstream_yolo_coco",
        "input_label": "Live Stream Processor",
        "deepstream_event": {"event_id": "event-1", "objects": [{"label": "person"}]},
    }

    first = server.append_manual_voicechat_input(args, "wifi", "first raw event", extra)
    second = server.append_manual_voicechat_input(args, "wifi", "second raw event", extra)

    assert first.get("suppressed") is not True
    assert second["suppressed"] is True
    history = json.loads((tmp_path / "webcam-voicechat-history.json").read_text(encoding="utf-8"))
    turns = history["conversation_by_source"]["wifi"]
    assert [turn["text"] for turn in turns] == ["first raw event", "second raw event"]
    assert all(turn["label"] == "Live Stream service" for turn in turns)
    assert all(turn["raw_notification"] is True for turn in turns)
    assert turns[0]["service_payload"]["event_id"] == "event-1"


def test_dashboard_does_not_filter_service_or_technical_notifications():
    html = server.render_dashboard_html("test", "device", 0)

    assert "entries = entries.filter((entry) => !entry.technical_note)" not in html
    assert "entry.temporary && transientPhases.has" not in html
    assert "Raw service payload" in html
    assert "rawServicePayload ? 'details' : 'div'" in html
    assert "if (rawServicePayload) item.open = false" in html
    assert "Lane 1 · server" in html
    assert ".find((candidate) => isRenderableImageSrc(candidate))" in html
    assert "Camera Clip" in html
    assert "document.createElement('video')" in html
    assert "Lane 2 · Wi-Fi" in html

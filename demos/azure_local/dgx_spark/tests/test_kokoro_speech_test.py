import importlib.util
import random
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kokoro_speech_test.py"
SPEC = importlib.util.spec_from_file_location("kokoro_speech_test", SCRIPT)
assert SPEC and SPEC.loader
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def test_seed_produces_repeatable_trials():
    first = random.Random(42)
    second = random.Random(42)

    assert [harness.generate_question(first) for _ in range(5)] == [
        harness.generate_question(second) for _ in range(5)
    ]


def test_question_generation_avoids_recent_duplicate():
    rng = random.Random(7)
    first = harness.generate_question(rng)

    assert harness.generate_question(rng, {first}) != first


def test_resolve_jbl_sink_accepts_dynamic_profile_name(monkeypatch):
    dynamic_sink = "bluez_output.E8_D0_3C_4C_A3_7E.a2dp-sink"
    monkeypatch.setattr(harness, "pulse_sinks", lambda: [dynamic_sink])

    assert harness.resolve_jbl_sink(harness.JBL_SINK, harness.JBL_MAC) == dynamic_sink


def test_resolve_jbl_sink_refuses_another_audio_device(monkeypatch):
    monkeypatch.setattr(harness, "pulse_sinks", lambda: ["alsa_output.pci-hdmi"])

    with pytest.raises(RuntimeError, match="JBL PulseAudio sink"):
        harness.resolve_jbl_sink(harness.JBL_SINK, harness.JBL_MAC)


def test_voice_pool_rejects_unknown_voice():
    with pytest.raises(Exception, match="unknown voice"):
        harness.parse_voice_pool("af_heart,not_a_voice")


def test_enqueue_and_read_service_result(tmp_path):
    queue = tmp_path / "queue.json"
    queue.write_text("{}")
    request = {"id": "speech-test-1", "source": "server", "text": "Hello?"}

    harness.enqueue_service_request(queue, request)
    queued = harness.read_json(queue)

    assert queued["pending"] == [request]
    assert queued["latest_request_id"] == "speech-test-1"

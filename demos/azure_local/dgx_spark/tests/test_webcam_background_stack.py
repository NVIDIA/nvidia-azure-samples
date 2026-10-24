import json
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts import webcam_background_stack as stack


def test_nemotron_startup_warmup_validates_two_passes_and_publishes_live_state(monkeypatch, tmp_path):
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"ready":true}'}}]}).encode()

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return Response()

    clock = iter((10.0, 10.4, 11.0, 11.2))
    monkeypatch.setattr(stack, "urlopen", fake_urlopen)
    monkeypatch.setattr(stack.time, "perf_counter", lambda: next(clock))
    args = SimpleNamespace(
        runtime_root=str(tmp_path),
        nemotron_omni_port=8010,
        nemotron_omni_model="nemotron_3_nano_omni",
    )

    assert stack.warm_nemotron_omni(args) is True
    assert len(requests) == 2
    state = json.loads((tmp_path / "webcam-nemotron-runtime-state.json").read_text())
    assert state["status"] == "ready"
    assert state["warmup_complete"] is True
    assert state["cold_pass_seconds"] == 0.4
    assert state["warm_validation_seconds"] == 0.2
    assert state["speculative_decoding"] is False


def test_focus_shadow_service_is_runtime_scoped_and_follows_controller(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr(sys, "argv", [
        "webcam_background_stack.py", "status", "--runtime-root", str(tmp_path),
        "--script-root", str(root), "--stream-python", sys.executable,
    ])
    services = stack.build_services(stack.parse_args())
    names = [service.name for service in services]
    assert names.count("deepstream-coco-server") == 1
    assert names.count("deepstream-coco-wifi") == 1
    server_deepstream = services[names.index("deepstream-coco-server")]
    wifi_deepstream = services[names.index("deepstream-coco-wifi")]
    assert server_deepstream.command[server_deepstream.command.index("--source") + 1] == "server"
    assert server_deepstream.command[server_deepstream.command.index("--stream-url") + 1].endswith("/stream.mjpg?fps=5")
    assert "--drop-frame-interval" not in server_deepstream.command
    assert server_deepstream.command[server_deepstream.command.index("--focus-command-json") + 1] == ""
    assert server_deepstream.command[server_deepstream.command.index("--synced-preview-output") + 1].endswith("-server.jpg")
    assert wifi_deepstream.command[wifi_deepstream.command.index("--source") + 1] == "wifi"
    server_voice = services[names.index("voicechat-server")]
    wifi_voice = services[names.index("voicechat-wifi")]
    assert "--no-camera-tools" in server_voice.command
    assert "--no-camera-tools" not in wifi_voice.command
    assert wifi_voice.command[wifi_voice.command.index("--output-target-mode") + 1] == "wifi_camera"
    assert server_voice.command.count("--piper-voice-pool") == 1
    assert wifi_voice.command.count("--piper-voice-pool") == 1
    assert any("en_US-hfc_male-medium.onnx" in item for item in server_voice.command)
    assert not any("en_US-hfc_female-medium.onnx" in item for item in wifi_voice.command)
    assert names.index("focus-gru-shadow") == names.index("focus-object") + 1
    focus = services[names.index("focus-object")]
    shadow = services[names.index("focus-gru-shadow")]
    stream = services[names.index("stream")]
    assert stream.command[stream.command.index("--wifi-talk-method") + 1] == "native"
    assert "--no-wifi-native-persistent-speaker" in stream.command
    assert "--wifi-native-persistent-speaker" not in stream.command
    assert str(tmp_path / "webcam-focus-pulse-prediction-errors.jsonl") in focus.command
    assert str(tmp_path / "webcam-focus-direction-gains.json") in focus.command
    assert str(tmp_path / "webcam-focus-gru-shadow.pt") in focus.command
    assert "--simple-closed-loop" not in focus.command
    assert focus.command[focus.command.index("--missing-target-grace-seconds") + 1] == "0.8"
    assert focus.command[focus.command.index("--reacquisition-confirmation-frames") + 1] == "2"
    assert focus.command[focus.command.index("--max-target-random-search-moves") + 1] == "0"
    assert focus.command[focus.command.index("--required-stable-frames") + 1] == "2"
    assert focus.command[focus.command.index("--center-median-frames") + 1] == "3"
    assert focus.command[focus.command.index("--deadzone-x") + 1] == "0.04"
    assert focus.command[focus.command.index("--deadzone-y") + 1] == "0.06"
    assert focus.command[focus.command.index("--max-no-progress-pulses") + 1] == "6"
    assert focus.command[focus.command.index("--max-axis-no-progress-pulses") + 1] == "2"
    assert focus.command[focus.command.index("--max-vertical-pulse-ms") + 1] == "160"
    assert "--adaptive-tilt-direction-probe" not in focus.command
    assert str(tmp_path / "webcam-focus-gru-shadow-state.json") in stream.command
    assert str(tmp_path / "webcam-deepstream-settings.json") in stream.command
    assert shadow.command[0] == sys.executable
    assert str(tmp_path / "webcam-focus-pulse-prediction-errors.jsonl") in shadow.command
    assert str(tmp_path / "webcam-focus-gru-shadow.pt") in shadow.command
    assert str(tmp_path / "webcam-focus-gru-shadow-state.json") in shadow.command
    assert str(tmp_path / "webcam-focus-gru-shadow-predictions.jsonl") in shadow.command
    assert shadow.command[-2:] == ["--device", "cpu"]
    assert (shadow.log_name, shadow.pid_name) == ("webcam-focus-gru-shadow.log", "webcam-focus-gru-shadow.pid")

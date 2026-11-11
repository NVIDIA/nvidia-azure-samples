import argparse
import io
import json
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import focus_object_controller as controller
from scripts import webcam_stream_server as server


class FakeStdin:
    def __init__(self, worker):
        self.worker = worker
        self.commands = []
        self.pending = b""

    def write(self, payload):
        self.pending += payload
        return len(payload)

    def flush(self):
        command, action, speed, duration = self.pending.decode("ascii").strip().split()
        self.commands.append((command, action, int(speed), int(duration)))
        self.pending = b""
        self.worker.lines.put(json.dumps({
            "status": "ok",
            "event": "completed",
            "backend": "x64_netsdk_qemu_persistent",
            "command": command,
            "action": action,
            "speed": int(speed),
            "duration_ms": int(duration),
            "pulse_stop_ok": True,
        }))

    def close(self):
        pass


class FakeProcess:
    def __init__(self, worker):
        self.pid = 4321
        self.stdin = FakeStdin(worker)
        self.stdout = None
        self.stderr = None
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def ptz_args(**overrides):
    values = {
        "wifi_camera_host": "192.0.2.10",
        "wifi_camera_native_port": 37777,
        "wifi_rtsp_user": "admin",
        "wifi_rtsp_password_env": "CAMERA_PASSWORD",
        "wifi_ptz_channel": 0,
        "wifi_ptz_pulse_ms": 100,
        "wifi_native_ptz_timeout": 1,
        "wifi_native_persistent_ptz": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_persistent_worker_reuses_one_process_and_serializes_commands(monkeypatch):
    worker = server.PersistentX64NativePTZ(ptz_args())
    process = FakeProcess(worker)
    worker.process = process
    monkeypatch.setattr(worker, "_start_locked", lambda: {
        "status": "ok", "event": "already_running", "pid": process.pid,
    })

    first = worker.send("right", "pulse", 8, 100, 1)
    second = worker.send("down", "pulse", 7, 80, 1)

    assert first["pid"] == second["pid"] == process.pid
    assert first["pulse_stop_ok"] is True
    assert process.stdin.commands == [
        ("right", "pulse", 8, 100),
        ("down", "pulse", 7, 80),
    ]


def test_persistent_worker_validates_before_dispatch(monkeypatch):
    worker = server.PersistentX64NativePTZ(ptz_args())
    monkeypatch.setattr(worker, "_start_locked", lambda: pytest.fail("must not start"))

    with pytest.raises(ValueError, match="unsupported persistent PTZ command"):
        worker.send("diagonal", "pulse", 8, 100, 1)


def test_ambiguous_persistent_failure_is_not_replayed_one_shot(monkeypatch):
    class AmbiguousWorker:
        def send(self, *_args):
            raise server.AmbiguousNativePTZError("dispatched but unconfirmed")

    monkeypatch.setattr(server, "persistent_x64_native_ptz", lambda _args: AmbiguousWorker())
    monkeypatch.setattr(
        server,
        "post_ptz_to_native_camera_one_shot",
        lambda *_args: pytest.fail("ambiguous command must not be replayed"),
    )

    with pytest.raises(server.AmbiguousNativePTZError, match="dispatched but unconfirmed"):
        server.post_ptz_to_native_camera(ptz_args(), "right", "pulse", 8, 100)


def test_pre_dispatch_failure_falls_back_to_one_shot(monkeypatch):
    class UnavailableWorker:
        def send(self, *_args):
            raise RuntimeError("worker unavailable")

    monkeypatch.setattr(server, "persistent_x64_native_ptz", lambda _args: UnavailableWorker())
    monkeypatch.setattr(
        server,
        "post_ptz_to_native_camera_one_shot",
        lambda *_args: {"status": "ok", "backend": "x64_netsdk_qemu"},
    )

    result = server.post_ptz_to_native_camera(ptz_args(), "right", "pulse", 8, 100)

    assert result["backend"] == "x64_netsdk_qemu"
    assert result["persistent_fallback_error"] == "worker unavailable"


def test_auto_ptz_prefers_native_with_onvif_fallback():
    assert server.ptz_backend_order("up", "auto") == ("native", "onvif")
    assert server.ptz_backend_order("down", "auto") == ("native", "onvif")
    assert server.ptz_backend_order("left", "auto") == ("native", "onvif")
    assert server.ptz_backend_order("zoom_in", "auto") == ("native", "onvif")
    assert server.ptz_backend_order("up", "native") == ("native",)
    assert server.ptz_backend_order("left", "onvif") == ("onvif",)


def test_native_pulse_uses_distinct_start_and_stop_commands(monkeypatch):
    calls = []
    sleeps = []

    def send(_args, command, action, speed, duration_ms=None):
        calls.append((command, action, speed, duration_ms))
        return {"status": "ok", "backend": "x64_netsdk_qemu_persistent", "action": action}

    monkeypatch.setattr(server, "post_ptz_to_native_camera", send)
    monkeypatch.setattr(server.time, "sleep", sleeps.append)

    result = server.post_ptz_pulse_to_native_camera(ptz_args(), "right", 1, 30)

    assert calls == [("right", "start", 1, None), ("right", "stop", 1, None)]
    assert sleeps == [0.03]
    assert result["action"] == "pulse"
    assert result["duration_ms"] == 30
    assert result["pulse_stop_ok"] is True


def test_focus_controller_trusts_confirmed_persistent_pulse_stop(monkeypatch):
    payload = json.dumps({
        "status": "ok",
        "backend": "x64_netsdk_qemu_persistent",
        "pulse_stop_ok": True,
    }).encode()
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return payload

    monkeypatch.setattr(controller, "urlopen", lambda request, timeout: calls.append(request) or Response())
    monkeypatch.setattr(controller, "post_stop", lambda *_args: pytest.fail("confirmed persistent stop is sufficient"))

    result = controller.post_pulse("http://localhost/wifi-ptz", "right", 8, 100, 1, "native")

    assert result["pulse_stop_ok"] is True
    assert len(calls) == 1


def test_persistent_workers_are_isolated_by_camera_identity():
    wifi = ptz_args(wifi_camera_host="192.0.2.10")
    bulb = ptz_args(wifi_camera_host="192.0.2.11")

    assert server.persistent_x64_native_ptz_key(wifi) != server.persistent_x64_native_ptz_key(bulb)

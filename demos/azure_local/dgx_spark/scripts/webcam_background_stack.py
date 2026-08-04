#!/usr/bin/env python3
"""Run the webcam monitoring agents as a browser-independent background stack."""

from __future__ import annotations

import argparse
import json
import os
import signal
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_ROOT = SCRIPT_ROOT
DEFAULT_AGENT_PYTHON = Path("/home/anslutsky/venvs/nemo-asr-gpu/bin/python")
DEFAULT_LOCAL_PYTHON = DEFAULT_RUNTIME_ROOT / ".venv/bin/python"
DEFAULT_AUX_PYTHON = Path("/home/anslutsky/jupyterlab/.venv/bin/python")
DEFAULT_COSMOS_PYTHON = DEFAULT_LOCAL_PYTHON if DEFAULT_LOCAL_PYTHON.exists() else DEFAULT_AUX_PYTHON
DEFAULT_STREAM_PYTHON = DEFAULT_LOCAL_PYTHON if DEFAULT_LOCAL_PYTHON.exists() else DEFAULT_AUX_PYTHON
DEFAULT_GO2RTC_BINARY = DEFAULT_RUNTIME_ROOT / "bin/go2rtc"
DEFAULT_SERVER_SINK = "bluez_output.E8_D0_3C_4C_A3_7E.1"
DEFAULT_CAMERA_ENV_FILE = Path.home() / ".config/dgx-spark/secrets.env"
DEFAULT_NEMOTRON_OMNI_WEIGHTS = Path(
    "/home/anslutsky/Dev/NIMs/Weights/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4"
)
DEFAULT_NEMOTRON_OMNI_MODEL = "nemotron_3_nano_omni"
DEFAULT_NEMOTRON_OMNI_PORT = 8010


@dataclass(frozen=True)
class Service:
    name: str
    command: list[str]
    log_name: str
    pid_name: str
    env: dict[str, str] | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    parser.add_argument("--runtime-root", default=str(DEFAULT_RUNTIME_ROOT))
    parser.add_argument("--script-root", default=str(SCRIPT_ROOT))
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--https-port", type=int, default=8443)
    parser.add_argument("--video-size", default="1280x720")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--agent-python", default=str(DEFAULT_AGENT_PYTHON))
    parser.add_argument("--cosmos-python", default=str(DEFAULT_COSMOS_PYTHON))
    parser.add_argument("--stream-python", default=str(DEFAULT_STREAM_PYTHON))
    parser.add_argument("--server-audio-sink", default=DEFAULT_SERVER_SINK)
    parser.add_argument("--camera-env-file", default=str(DEFAULT_CAMERA_ENV_FILE))
    parser.add_argument(
        "--nemotron-omni-service",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run the official Nemotron 3 Nano Omni NVFP4 vLLM service used by understanding and final answers.",
    )
    parser.add_argument("--nemotron-omni-weights", default=str(DEFAULT_NEMOTRON_OMNI_WEIGHTS))
    parser.add_argument("--nemotron-omni-model", default=DEFAULT_NEMOTRON_OMNI_MODEL)
    parser.add_argument("--nemotron-omni-port", type=int, default=DEFAULT_NEMOTRON_OMNI_PORT)
    parser.add_argument("--nemotron-omni-startup-timeout", type=float, default=600.0)
    parser.add_argument("--dedicated-asr-service", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--dedicated-asr-port", type=int, default=8012)
    parser.add_argument("--dedicated-asr-startup-timeout", type=float, default=180.0)
    parser.add_argument("--dedicated-asr-primary-model", default="nvidia/canary-1b-flash")
    parser.add_argument("--dedicated-asr-fast-model", default="nvidia/parakeet-tdt-0.6b-v2")
    parser.add_argument(
        "--dedicated-asr-parallel-models",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--env-interval", type=float, default=60.0)
    parser.add_argument("--cosmos-timeout", type=float, default=240.0)
    parser.add_argument("--wifi-label", default="Wi-Fi Amcrest camera")
    parser.add_argument("--wifi-rtsp-url", default="rtsp://192.168.1.188:554/cam/realmonitor?channel=1&subtype=1")
    parser.add_argument("--wifi-rtsp-user", default="admin")
    parser.add_argument("--wifi-rtsp-password-env", default="AMCREST_PASSWORD")
    parser.add_argument("--wifi-fps", type=int, default=5)
    parser.add_argument("--wifi-camera-native-port", type=int, default=37777)
    parser.add_argument("--wifi-native-speaker-sdk-lib", default=os.environ.get("DAHUA_NETSDK_LIB", ""))
    parser.add_argument("--wifi-native-speaker-timeout", type=float, default=30.0)
    parser.add_argument("--wifi-native-speaker-login-retries", type=int, default=2)
    parser.add_argument("--wifi-native-speaker-login-retry-delay", type=float, default=0.35)
    parser.add_argument("--bulb-label", default="Light bulb ONVIF camera")
    parser.add_argument("--bulb-rtsp-url", default="rtsp://192.168.1.190:10554/tcp/av0_0")
    parser.add_argument("--bulb-rtsp-user", default="admin")
    parser.add_argument("--bulb-rtsp-password-env", default="AMCREST_PASSWORD")
    parser.add_argument("--bulb-fps", type=int, default=5)
    parser.add_argument("--bulb-camera-host", default="192.168.1.190")
    parser.add_argument("--bulb-camera-http-port", type=int, default=10080)
    parser.add_argument("--bulb-camera-native-port", type=int, default=0)
    parser.add_argument("--bulb-onvif-port", type=int, default=10080)
    parser.add_argument("--go2rtc-binary", default=str(DEFAULT_GO2RTC_BINARY))
    parser.add_argument("--go2rtc-api", default="http://127.0.0.1:1984")
    parser.add_argument("--go2rtc-config", default="")
    parser.add_argument("--go2rtc-wifi-stream", default="wifi_camera")
    parser.add_argument("--go2rtc-wifi-speaker-stream", default="wifi_camera_speaker")
    parser.add_argument("--go2rtc-bulb-stream", default="bulb_camera")
    parser.add_argument("--go2rtc-bulb-speaker-stream", default="bulb_camera_speaker")
    parser.add_argument("--go2rtc-onvif-port", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Restart running services during start")
    parser.add_argument(
        "--only-service",
        action="append",
        default=[],
        help="Limit start, stop, restart, or status to a named service; may be repeated.",
    )
    parser.add_argument("--no-wifi-lane", action="store_true", help="Do not run the Wi-Fi RTSP camera lane")
    parser.add_argument("--enable-bulb-lane", action="store_true", help="Run the experimental light bulb ONVIF camera lane")
    parser.add_argument("--no-bulb-lane", action="store_true", help="Do not run the light bulb ONVIF camera lane")
    parser.add_argument(
        "--enable-wifi-audio-channel",
        action="store_true",
        help="Decode one shared Wi-Fi microphone channel inside the stream server. Consumers read from this buffer instead of opening camera audio sessions.",
    )
    parser.add_argument(
        "--enable-wifi-speech-worker",
        action="store_true",
        default=True,
        help="Run the Wi-Fi microphone/speech agent using the shared audio channel.",
    )
    parser.add_argument("--no-wifi-speech-worker", action="store_true", help="Do not run the Wi-Fi microphone/speech agent")
    parser.add_argument("--no-bulb-speech-worker", action="store_true", help="Do not run the light bulb microphone/speech agent")
    parser.add_argument("--enable-go2rtc", action="store_true", help="Run go2rtc for camera talkback/backchannel experiments.")
    parser.add_argument("--no-go2rtc", action="store_true", help="Compatibility flag; go2rtc is already off unless --enable-go2rtc is passed.")
    parser.add_argument("--no-browser-lane", action="store_true", help="Do not run the idle browser-input environment lane")
    parser.add_argument("--include-legacy-alert", action="store_true", help="Also run the legacy global change-monitor service")
    parser.add_argument(
        "--enable-environment-agents",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run the old per-lane environment agents. Disabled by default while the stack is being redesigned.",
    )
    return parser.parse_args(argv)


def runtime_path(args: argparse.Namespace, *parts: str) -> str:
    return str(Path(args.runtime_root, *parts))


def wifi_lane_enabled(args: argparse.Namespace) -> bool:
    return bool(str(getattr(args, "wifi_rtsp_url", "") or "").strip()) and not bool(getattr(args, "no_wifi_lane", False))


def bulb_lane_enabled(args: argparse.Namespace) -> bool:
    return (
        bool(getattr(args, "enable_bulb_lane", False))
        and bool(str(getattr(args, "bulb_rtsp_url", "") or "").strip())
        and not bool(getattr(args, "no_bulb_lane", False))
    )


def camera_lane_enabled(args: argparse.Namespace, source: str) -> bool:
    return bulb_lane_enabled(args) if source == "bulb" else wifi_lane_enabled(args)


def executable(path: str, fallback: str = sys.executable) -> str:
    candidate = Path(path)
    if candidate.exists():
        return str(candidate)
    return fallback


def script_supports_flag(script_path: Path, flag: str) -> bool:
    try:
        return flag in script_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def load_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return {}
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        try:
            parts = shlex.split(line, comments=False, posix=True)
        except ValueError:
            parts = [line]
        if not parts:
            continue
        line = parts[0]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = value
    return values


def service_pid_path(args: argparse.Namespace, service: Service) -> Path:
    return Path(args.runtime_root) / service.pid_name


def read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip().splitlines()[0]
        return int(text)
    except Exception:
        return None


def process_state(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        after_name = stat.rsplit(")", 1)[1].strip()
        return after_name.split()[0] if after_name else ""
    except Exception:
        return ""


def process_parent_pid(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("PPid:"):
                return int(line.split()[1])
    except Exception:
        return None
    return None


def is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return process_state(pid) != "Z"
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pid_cmdline(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


def unlink_pid_file(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def signal_process_group(pid: int, sig: int) -> None:
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    except PermissionError:
        pgid = 0
    sent = False
    if pgid > 1 and pgid != os.getpgrp():
        try:
            os.killpg(pgid, sig)
            sent = True
        except ProcessLookupError:
            return
        except PermissionError:
            pass
    if not sent:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


def stop_pid(pid: int, timeout: float = 20.0) -> None:
    if not is_running(pid):
        return
    signal_process_group(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_running(pid):
            return
        time.sleep(0.25)
    signal_process_group(pid, signal.SIGKILL)


def wait_http(url: str, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2.0) as response:
                if response.status < 500:
                    return True
        except (OSError, URLError):
            pass
        time.sleep(0.5)
    return False


def write_runtime_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def warm_nemotron_omni(args: argparse.Namespace) -> bool:
    """Hide first-request processor/JIT work before speech workers use the model."""
    state_path = Path(runtime_path(args, "webcam-nemotron-runtime-state.json"))
    base_url = f"http://127.0.0.1:{args.nemotron_omni_port}"
    state = {
        "status": "warming",
        "model": str(args.nemotron_omni_model),
        "runtime_policy": "vllm_startup_warmup_v1",
        "warmup_policy": "two_pass_text_json_before_voice_workers",
        "warmup_complete": False,
        "speculative_decoding": False,
        "physical_wave_audio_only": True,
        "audio_path_changed": False,
        "updated_at": time.time(),
    }
    write_runtime_json(state_path, state)
    durations: list[float] = []
    outputs: list[str] = []
    try:
        for pass_index in range(2):
            payload = {
                "model": str(args.nemotron_omni_model),
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Startup health check for the local voice pipeline. "
                            "Return exactly one JSON object and do not reason aloud. /no_think"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Validate text-only JSON generation for the acoustic guard, dialog decision, "
                            f"and tool-answer runtime. Pass {pass_index + 1}. Return {json.dumps({'ready': True})}."
                        ),
                    },
                ],
                "temperature": 0,
                "top_k": 1,
                "max_tokens": 12,
                "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False},
            }
            request = Request(
                base_url + "/v1/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            started = time.perf_counter()
            with urlopen(request, timeout=90.0) as response:
                result = json.loads(response.read().decode("utf-8"))
            durations.append(time.perf_counter() - started)
            content = str((((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""))
            outputs.append(content)
            parsed = json.loads(content)
            if parsed.get("ready") is not True:
                raise RuntimeError(f"warmup pass {pass_index + 1} returned {content!r}")
        state.update(
            {
                "status": "ready",
                "warmup_complete": True,
                "warmup_passes": 2,
                "cold_pass_seconds": round(durations[0], 4),
                "warm_validation_seconds": round(durations[1], 4),
                "warm_validation_under_two_seconds": durations[1] < 2.0,
                "outputs_validated": len(outputs),
                "updated_at": time.time(),
            }
        )
        write_runtime_json(state_path, state)
        print(
            "nemotron-omni: startup warmup complete "
            f"cold={durations[0]:.3f}s warm={durations[1]:.3f}s"
        )
        return durations[1] < 2.0
    except Exception as exc:
        state.update({"status": "error", "error": str(exc), "updated_at": time.time()})
        write_runtime_json(state_path, state)
        print(f"nemotron-omni: startup warmup failed: {exc}", file=sys.stderr)
        return False


def rtsp_url_with_credentials(url: str, user: str, password: str) -> str:
    raw_url = str(url or "").strip()
    if not raw_url:
        return raw_url
    parts = urlsplit(raw_url)
    if parts.scheme != "rtsp" or "@" in parts.netloc or not user:
        return raw_url
    credentials = quote(user, safe="")
    if password:
        credentials += f":{quote(password, safe='')}"
    hostname = parts.hostname or ""
    netloc = f"{credentials}@{hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def rtsp_url_for_go2rtc(args: argparse.Namespace, source: str = "wifi", backchannel: str = "1") -> str:
    env_values = getattr(args, "_env_file_values", {})
    password_env = str(getattr(args, f"{source}_rtsp_password_env", "AMCREST_PASSWORD") or "AMCREST_PASSWORD")
    password = env_values.get(password_env) or os.environ.get(password_env, "")
    raw_url = rtsp_url_with_credentials(
        str(getattr(args, f"{source}_rtsp_url", "") or ""),
        str(getattr(args, f"{source}_rtsp_user", "") or ""),
        password,
    )
    parts = urlsplit(raw_url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query_keys = {key.lower() for key, _value in query_items}
    if source == "wifi" and "unicast" not in query_keys:
        query_items.append(("unicast", "true"))
    if source == "wifi" and "proto" not in query_keys:
        query_items.append(("proto", "Onvif"))
    fragment = parts.fragment or f"backchannel={backchannel}"
    if "backchannel=" not in fragment:
        fragment = f"{fragment}#backchannel={backchannel}" if fragment else f"backchannel={backchannel}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), fragment))


def onvif_url_for_go2rtc(args: argparse.Namespace, source: str = "wifi") -> str:
    env_values = getattr(args, "_env_file_values", {})
    password_env = str(getattr(args, f"{source}_rtsp_password_env", "AMCREST_PASSWORD") or "AMCREST_PASSWORD")
    password = env_values.get(password_env) or os.environ.get(password_env, "")
    parts = urlsplit(str(getattr(args, f"{source}_rtsp_url", "") or ""))
    host = str(getattr(args, f"{source}_camera_host", "") or "").strip() or (parts.hostname or "")
    user = str(getattr(args, f"{source}_rtsp_user", "") or "").strip()
    if not host or not user:
        return ""
    credentials = quote(user, safe="")
    if password:
        credentials += f":{quote(password, safe='')}"
    netloc = f"{credentials}@{host}"
    onvif_port = int(getattr(args, "go2rtc_onvif_port", 0) or 0) if source == "wifi" else int(getattr(args, f"{source}_onvif_port", 0) or 0)
    if onvif_port > 0:
        netloc += f":{onvif_port}"
    return urlunsplit(("onvif", netloc, "", "", ""))


def go2rtc_config_path(args: argparse.Namespace) -> Path:
    configured = str(args.go2rtc_config or "").strip()
    return Path(configured) if configured else Path(args.runtime_root) / "go2rtc-webcam.yaml"


def go2rtc_api_listen(args: argparse.Namespace) -> str:
    parts = urlsplit(str(args.go2rtc_api or "http://127.0.0.1:1984"))
    return parts.netloc or "127.0.0.1:1984"


def write_go2rtc_config(args: argparse.Namespace) -> Path:
    config_path = go2rtc_config_path(args)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "api:",
        f"  listen: {json.dumps(go2rtc_api_listen(args))}",
        "rtsp:",
        '  listen: "127.0.0.1:8554"',
        "webrtc:",
        '  listen: "127.0.0.1:8555"',
        "streams:",
    ]
    for source, stream_name, speaker_name in (
        ("wifi", args.go2rtc_wifi_stream, args.go2rtc_wifi_speaker_stream),
        ("bulb", args.go2rtc_bulb_stream, args.go2rtc_bulb_speaker_stream),
    ):
        if not camera_lane_enabled(args, source):
            continue
        onvif_url = onvif_url_for_go2rtc(args, source)
        sources = []
        if onvif_url:
            sources.append(onvif_url)
        sources.append(rtsp_url_for_go2rtc(args, source, backchannel="0" if onvif_url else "1"))
        speaker_sources = []
        if onvif_url:
            speaker_sources.append(onvif_url)
        speaker_sources.append(rtsp_url_for_go2rtc(args, source, backchannel="1"))
        lines.append(f"  {stream_name}:")
        lines.extend(f"    - {json.dumps(item)}" for item in sources)
        lines.append(f"  {speaker_name}:")
        lines.extend(f"    - {json.dumps(item)}" for item in speaker_sources)
    lines.append("")
    content = "\n".join(lines)
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(config_path)
    config_path.chmod(0o600)
    return config_path


def maybe_go2rtc_service(args: argparse.Namespace) -> Service | None:
    if args.no_go2rtc or not (args.enable_go2rtc or bulb_lane_enabled(args)) or not (wifi_lane_enabled(args) or bulb_lane_enabled(args)):
        return None
    binary = Path(args.go2rtc_binary)
    if not binary.exists():
        return None
    config_path = write_go2rtc_config(args)
    return Service(
        "go2rtc",
        [str(binary), "--config", str(config_path)],
        "webcam-go2rtc.log",
        "webcam-go2rtc.pid",
    )


def maybe_nemotron_omni_service(args: argparse.Namespace) -> Service | None:
    if not bool(getattr(args, "nemotron_omni_service", True)):
        return None
    script = Path(args.script_root) / "scripts/run_nemotron_omni_vllm.sh"
    return Service(
        "nemotron-omni",
        ["bash", str(script)],
        "webcam-nemotron-omni.log",
        "webcam-nemotron-omni.pid",
        {
            "WEIGHTS": str(Path(args.nemotron_omni_weights).expanduser()),
            "PORT": str(args.nemotron_omni_port),
            "SERVED_MODEL_NAME": str(args.nemotron_omni_model),
        },
    )


def maybe_dedicated_asr_service(args: argparse.Namespace) -> Service | None:
    if not bool(getattr(args, "dedicated_asr_service", True)):
        return None
    return Service(
        "dedicated-asr",
        [
            executable(args.agent_python),
            str(Path(args.script_root) / "scripts/local_audio_asr_service.py"),
            "--host", "127.0.0.1",
            "--port", str(args.dedicated_asr_port),
            "--device", "cuda",
            "--primary-model", str(args.dedicated_asr_primary_model),
            "--fast-model", str(args.dedicated_asr_fast_model),
            "--parallel-models" if args.dedicated_asr_parallel_models else "--no-parallel-models",
        ],
        "webcam-dedicated-asr.log",
        "webcam-dedicated-asr.pid",
    )


def server_microphone_adapter_service(args: argparse.Namespace) -> Service:
    return Service(
        "server-microphone-adapter",
        [
            executable(args.agent_python),
            str(Path(args.script_root) / "scripts/server_microphone_adapter.py"),
            "--source-name", "dgx_nexigo_70",
            "--hardware-percent", "70",
            "--software-percent", "70",
            "--state-json", runtime_path(args, "webcam-server-microphone-adapter.json"),
        ],
        "webcam-server-microphone-adapter.log",
        "webcam-server-microphone-adapter.pid",
    )


def build_services(args: argparse.Namespace) -> list[Service]:
    runtime = Path(args.runtime_root)
    script_root = Path(args.script_root)
    agent_python = executable(args.agent_python)
    cosmos_python = executable(args.cosmos_python)
    stream_python = executable(args.stream_python)
    stream_script = script_root / "scripts/webcam_stream_server.py"
    tls_cert = script_root / "webcam-local.crt"
    tls_key = script_root / "webcam-local.key"
    stream_cmd = [
        stream_python,
        str(stream_script),
        "--device",
        args.device,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--video-size",
        args.video_size,
        "--fps",
        str(args.fps),
        "--analysis-path",
        runtime_path(args, "webcam-analysis.json"),
        "--alert-path",
        runtime_path(args, "webcam-alert.json"),
        "--agent-state-path",
        runtime_path(args, "webcam-agent-state.json"),
        "--server-agent-state-path",
        runtime_path(args, "webcam-server-agent-state.json"),
        "--wifi-agent-state-path",
        runtime_path(args, "webcam-wifi-agent-state.json"),
        "--bulb-agent-state-path",
        runtime_path(args, "webcam-bulb-agent-state.json"),
        "--browser-agent-state-path",
        runtime_path(args, "webcam-browser-agent-state.json"),
        "--transcript-path",
        runtime_path(args, "webcam-transcript.json"),
        "--voice-response-path",
        runtime_path(args, "webcam-voice-response.json"),
        "--voicechat-response-path",
        runtime_path(args, "webcam-voicechat-response.json"),
        "--voicechat-audio-dir",
        runtime_path(args, "webcam-voicechat-audio"),
        "--voice-session-reset-path",
        runtime_path(args, "webcam-voice-session-reset.json"),
        "--speech-pipeline-mode-path",
        runtime_path(args, "webcam-speech-pipeline-mode.json"),
        "--voice-output-target-path",
        runtime_path(args, "webcam-voice-output-target.json"),
        "--speech-playback-lock-path",
        runtime_path(args, "webcam-speech-playback-lock.json"),
        "--browser-audio-dir",
        runtime_path(args, "browser-audio-chunks"),
        "--browser-audio-max-age",
        "10",
        "--audio-buffer-control-path",
        runtime_path(args, "webcam-audio-buffer-control.json"),
        "--asr-settings-path",
        runtime_path(args, "webcam-asr-settings.json"),
        "--runtime-stats-path",
        runtime_path(args, "webcam-runtime-stats.json"),
        "--nemotron-runtime-state-path",
        runtime_path(args, "webcam-nemotron-runtime-state.json"),
        "--llm-token-usage-db",
        runtime_path(args, "webcam-llm-token-usage.sqlite3"),
        "--focus-object-command-path",
        "/dev/shm/dgx-spark-focus-command.json",
        "--realtime-focus-state-path",
        runtime_path(args, "webcam-realtime-focus-state.json"),
        "--focus-object-state-path",
        "/dev/shm/dgx-spark-focus-state.json",
        "--focus-gru-state-path",
        runtime_path(args, "webcam-focus-gru-shadow-state.json"),
        "--focus-object-wake-socket",
        "/tmp/dgx-spark-focus-wake.sock",
        "--focus-ptz-socket",
        "/tmp/dgx-spark-focus-ptz.sock",
        "--voicechat-manual-input-path",
        runtime_path(args, "webcam-voicechat-manual-input.json"),
        "--deepstream-nemotron-input-path",
        runtime_path(args, "webcam-deepstream-nemotron-input.json"),
        "--deepstream-synced-preview-path",
        "/dev/shm/dgx-spark-deepstream-preview.jpg",
        "--deepstream-detections-path",
        "/dev/shm/dgx-spark-detections.json",
        "--deepstream-settings-path",
        runtime_path(args, "webcam-deepstream-settings.json"),
        "--deepstream-notification-wake-socket",
        "/tmp/dgx-spark-detection-wake.sock",
    ]
    if script_supports_flag(stream_script, "--text-prompt-queue-path"):
        stream_cmd += [
            "--text-prompt-queue-path",
            runtime_path(args, "webcam-text-prompt-queue.json"),
        ]
    if script_supports_flag(stream_script, "--secrets-env-file"):
        stream_cmd += [
            "--secrets-env-file",
            str(Path(args.camera_env_file).expanduser()),
        ]
    if args.enable_environment_agents:
        stream_cmd.append("--enable-environment-agents")
    if wifi_lane_enabled(args):
        stream_cmd += [
            "--wifi-label",
            args.wifi_label,
            "--wifi-rtsp-url",
            args.wifi_rtsp_url,
            "--wifi-rtsp-user",
            args.wifi_rtsp_user,
            "--wifi-rtsp-password-env",
            args.wifi_rtsp_password_env,
            "--wifi-rtsp-backend",
            "ffmpeg",
            "--wifi-camera-native-port",
            str(args.wifi_camera_native_port),
            "--wifi-fps",
            str(args.wifi_fps),
            "--wifi-audio-gain-db",
            "18",
            "--go2rtc-api-url",
            args.go2rtc_api,
            "--wifi-talk-method",
            "native",
            "--wifi-native-speaker-timeout",
            str(args.wifi_native_speaker_timeout),
            "--wifi-native-speaker-login-retries",
            str(args.wifi_native_speaker_login_retries),
            "--wifi-native-speaker-login-retry-delay",
            str(args.wifi_native_speaker_login_retry_delay),
            # The helper's stdin-server mode overfeeds this camera's talkback
            # buffer. One-shot NetSDK playback retains real-time packet pacing.
            "--no-wifi-native-persistent-speaker",
            "--wifi-talk-go2rtc-stream",
            args.go2rtc_wifi_speaker_stream,
            "--wifi-talk-go2rtc-codec",
            "pcma",
        ]
        if args.wifi_native_speaker_sdk_lib:
            stream_cmd += [
                "--wifi-native-speaker-sdk-lib",
                args.wifi_native_speaker_sdk_lib,
            ]
        if (
            script_supports_flag(stream_script, "--enable-wifi-shared-audio")
            and (
                args.enable_wifi_audio_channel
                or (args.enable_wifi_speech_worker and not args.no_wifi_speech_worker)
            )
        ):
            stream_cmd.append("--enable-wifi-shared-audio")
    if bulb_lane_enabled(args):
        stream_cmd += [
            "--bulb-label",
            args.bulb_label,
            "--bulb-rtsp-url",
            args.bulb_rtsp_url,
            "--bulb-rtsp-user",
            args.bulb_rtsp_user,
            "--bulb-rtsp-password-env",
            args.bulb_rtsp_password_env,
            "--bulb-rtsp-backend",
            "ffmpeg",
            "--bulb-camera-host",
            args.bulb_camera_host,
            "--bulb-camera-http-port",
            str(args.bulb_camera_http_port),
            "--bulb-camera-native-port",
            str(args.bulb_camera_native_port),
            "--bulb-fps",
            str(args.bulb_fps),
            "--bulb-audio-gain-db",
            "18",
            "--go2rtc-api-url",
            args.go2rtc_api,
            "--bulb-talk-method",
            "go2rtc",
            "--bulb-talk-go2rtc-stream",
            args.go2rtc_bulb_speaker_stream,
            "--bulb-talk-go2rtc-codec",
            "pcma",
        ]
        if (
            script_supports_flag(stream_script, "--enable-bulb-shared-audio")
            and not args.no_bulb_speech_worker
        ):
            stream_cmd.append("--enable-bulb-shared-audio")
    if args.https_port and tls_cert.exists() and tls_key.exists():
        stream_cmd += [
            "--https-port",
            str(args.https_port),
            "--tls-cert",
            str(tls_cert),
            "--tls-key",
            str(tls_key),
        ]

    services = []
    go2rtc = maybe_go2rtc_service(args)
    if go2rtc is not None:
        services.append(go2rtc)
    services.append(Service("stream", stream_cmd, "webcam-stream.log", "webcam-stream.pid"))
    services.append(server_microphone_adapter_service(args))
    nemotron_omni = maybe_nemotron_omni_service(args)
    if nemotron_omni is not None:
        services.append(nemotron_omni)
    dedicated_asr = maybe_dedicated_asr_service(args)
    if dedicated_asr is not None:
        services.append(dedicated_asr)
    deepstream_lanes = [
        {
            "source": "server",
            "service": "deepstream-coco-server",
            "runtime_config": ".runtime_dashboard_yolo_coco_server.txt",
            "runtime_infer_config": ".runtime_config_infer_primary_yolo11_coco_server.txt",
            "bbox_dir": "/dev/shm/dgx-spark-deepstream-kitti-server",
            "preview": "/dev/shm/dgx-spark-deepstream-preview-server.jpg",
            "preview_dir": "/dev/shm/dgx-spark-deepstream-preview-frames-server",
            "focus_command": "",
            "log": "webcam-deepstream-yolo-coco-server.log",
            "pid": "webcam-deepstream-yolo-coco-server.pid",
            "extra": [
                "--stream-url", f"http://127.0.0.1:{args.port}/stream.mjpg?fps=5",
            ],
        }
    ]
    if wifi_lane_enabled(args):
        deepstream_lanes.append(
            {
                "source": "wifi",
                "service": "deepstream-coco-wifi",
                "runtime_config": ".runtime_dashboard_yolo_coco_wifi.txt",
                "runtime_infer_config": ".runtime_config_infer_primary_yolo11_coco_wifi.txt",
                "bbox_dir": "/dev/shm/dgx-spark-deepstream-kitti-wifi",
                "preview": "/dev/shm/dgx-spark-deepstream-preview.jpg",
                "preview_dir": "/dev/shm/dgx-spark-deepstream-preview-frames-wifi",
                "focus_command": "/dev/shm/dgx-spark-focus-command.json",
                "log": "webcam-deepstream-yolo-coco.log",
                "pid": "webcam-deepstream-yolo-coco.pid",
                "extra": [
                    "--wifi-rtsp-url", args.wifi_rtsp_url,
                    "--wifi-rtsp-user", args.wifi_rtsp_user,
                    "--wifi-rtsp-password-env", args.wifi_rtsp_password_env,
                ],
            }
        )
    for lane in deepstream_lanes:
        services.append(
            Service(
                lane["service"],
                [
                    stream_python,
                    str(script_root / "scripts/deepstream_yolo_coco_bridge.py"),
                    "--source",
                    lane["source"],
                    "--work-dir",
                    str(script_root / "deepstream-yolo-coco/DeepStream-Yolo"),
                    "--runtime-config",
                    lane["runtime_config"],
                    "--runtime-infer-config",
                    lane["runtime_infer_config"],
                    "--output-json",
                    "/dev/shm/dgx-spark-detections.json",
                    "--focus-settings-json",
                    runtime_path(args, "webcam-deepstream-settings.json"),
                    "--focus-command-json",
                    lane["focus_command"],
                    "--realtime-focus-state-json",
                    runtime_path(args, "webcam-realtime-focus-state.json"),
                    "--bbox-dir-name",
                    lane["bbox_dir"],
                    "--focus-wake-socket",
                    "/tmp/dgx-spark-focus-wake.sock",
                    "--notification-wake-socket",
                    "/tmp/dgx-spark-detection-wake.sock",
                    "--synced-preview-output",
                    lane["preview"],
                    "--synced-preview-dir-name",
                    lane["preview_dir"],
                    "--secrets-env-file",
                    str(Path(args.camera_env_file).expanduser()),
                    *lane["extra"],
                    "--metadata-publish-hz",
                    "60",
                    "--rtsp-latency-ms",
                    "50",
                    "--streammux-width",
                    "640",
                    "--streammux-height",
                    "480",
                    "--motion-frame-height",
                    "120",
                    "--kitti-prune-interval",
                    "5",
                ],
                lane["log"],
                lane["pid"],
            )
        )
    services += [
        Service(
            "focus-object",
            [
                stream_python,
                str(script_root / "scripts/focus_object_controller.py"),
                "--command-json",
                "/dev/shm/dgx-spark-focus-command.json",
                "--state-json",
                "/dev/shm/dgx-spark-focus-state.json",
                "--wake-socket",
                "/tmp/dgx-spark-focus-wake.sock",
                "--detections-json",
                "/dev/shm/dgx-spark-detections.json",
                "--settings-json",
                runtime_path(args, "webcam-deepstream-settings.json"),
                "--component-audio-settings-json",
                runtime_path(args, "webcam-component-audio-settings.json"),
                "--completion-chime-url",
                f"http://127.0.0.1:{args.port}/voicechat-cue.wav?stage=focus_complete",
                "--completion-chime-talk-url",
                f"http://127.0.0.1:{args.port}/{{source}}-talk-audio",
                "--ptz-url",
                f"http://127.0.0.1:{args.port}/{{source}}-ptz",
                "--ptz-socket",
                "/tmp/dgx-spark-focus-ptz.sock",
                "--snapshot-url",
                f"http://127.0.0.1:{args.port}/{{source}}-snapshot.jpg",
                "--snapshot-path",
                runtime_path(args, "webcam-focus-object-verification.jpg"),
                "--motion-preview-path",
                "/dev/shm/dgx-spark-deepstream-preview-motion.pgm",
                "--prediction-error-jsonl",
                runtime_path(args, "webcam-focus-pulse-prediction-errors.jsonl"),
                "--direction-gain-json",
                runtime_path(args, "webcam-focus-direction-gains.json"),
                "--gru-checkpoint",
                runtime_path(args, "webcam-focus-gru-shadow.pt"),
                "--min-pulse-ms",
                "50",
                "--max-pulse-ms",
                "220",
                "--speed",
                "1",
                "--required-stable-frames",
                "2",
                "--minimum-confidence",
                "0.25",
                "--max-pulses",
                "100",
                "--max-no-progress-pulses",
                "6",
                "--max-axis-no-progress-pulses",
                "2",
                "--deadzone-x",
                "0.04",
                "--deadzone-y",
                "0.06",
                "--post-pulse-view-settle-seconds",
                "0.8",
                "--simple-response-timeout",
                "1.0",
                "--simple-response-frames",
                "2",
                "--center-median-frames",
                "3",
                "--minimum-progress",
                "0.0005",
                "--minimum-global-motion",
                "0.0005",
                "--direction-gain-learning-rate",
                "0.03",
                "--vertical-speed",
                "6",
                "--min-vertical-pulse-ms",
                "30",
                "--vertical-pulse-multiplier",
                "1",
                "--max-vertical-pulse-ms",
                "160",
                "--vertical-completion-epsilon",
                "0.002",
                "--vertical-direction-probe-pulses",
                "2",
                "--missing-target-frames",
                "1",
                "--missing-target-grace-seconds",
                "0.8",
                "--reacquisition-confirmation-frames",
                "2",
                "--max-target-random-search-moves",
                "0",
                "--settle-seconds",
                "0.05",
                "--motion-verification-timeout",
                "0.45",
                "--motion-verification-min-seconds",
                "0.08",
                "--motion-verification-frames",
                "2",
                "--ptz-idle-trust-seconds",
                "3600",
            ],
            "webcam-focus-object.log",
            "webcam-focus-object.pid",
        ),
        Service(
            "focus-gru-shadow",
            [
                stream_python,
                str(script_root / "scripts/focus_gru_shadow_trainer.py"),
                "--event-jsonl",
                runtime_path(args, "webcam-focus-pulse-prediction-errors.jsonl"),
                "--checkpoint",
                runtime_path(args, "webcam-focus-gru-shadow.pt"),
                "--state-json",
                runtime_path(args, "webcam-focus-gru-shadow-state.json"),
                "--shadow-jsonl",
                runtime_path(args, "webcam-focus-gru-shadow-predictions.jsonl"),
                "--device",
                "cpu",
            ],
            "webcam-focus-gru-shadow.log",
            "webcam-focus-gru-shadow.pid",
        ),
        Service(
            "cosmos",
            [
                cosmos_python,
                str(script_root / "scripts/cosmos_webcam_analyzer.py"),
                "--backend",
                "vllm_omni",
                "--model",
                str(args.nemotron_omni_model),
                "--ollama-url",
                f"http://127.0.0.1:{args.nemotron_omni_port}",
                "--model-api-runtime",
                "vllm",
                "--ollama-image-max-width",
                "960",
                "--ollama-image-jpeg-quality",
                "72",
                "--device-map",
                "cuda",
                "--clip-seconds",
                "1",
                "--sample-fps",
                "1",
                "--model-video-fps",
                "1",
                "--max-pixels",
                "230400",
                "--max-new-tokens",
                "220",
                "--loop-delay",
                "1",
                "--output-json",
                runtime_path(args, "webcam-analysis.json"),
                "--clips-dir",
                runtime_path(args, "webcam-analysis-clips"),
                "--trigger-json",
                runtime_path(args, "webcam-cosmos-trigger.json"),
                "--trigger-only",
            ],
            "webcam-analysis.log",
            "webcam-analysis.pid",
            {
                "HF_HOME": runtime_path(args, ".cache/huggingface"),
                "TRANSFORMERS_OFFLINE": "1",
            },
        ),
    ]
    if args.enable_environment_agents:
        services.append(environment_service(args, "server", "Server USB input", "server-snapshot.jpg"))
    if args.enable_environment_agents and wifi_lane_enabled(args):
        services.append(environment_service(args, "wifi", args.wifi_label, "wifi-snapshot.jpg"))
    if args.enable_environment_agents and bulb_lane_enabled(args):
        services.append(environment_service(args, "bulb", args.bulb_label, "bulb-snapshot.jpg"))
    if args.enable_environment_agents and not args.no_browser_lane:
        services.append(environment_service(args, "browser", "Browser laptop input", "browser-snapshot.jpg"))
    if args.include_legacy_alert:
        services.append(
            Service(
                "alert",
                [
                    agent_python,
                    str(script_root / "scripts/nemotron_change_monitor.py"),
                    "--model",
                    "nemotron3:33b",
                    "--analysis-json",
                    runtime_path(args, "webcam-analysis.json"),
                    "--alert-json",
                    runtime_path(args, "webcam-alert.json"),
                    "--database",
                    runtime_path(args, "webcam-analysis-history.sqlite3"),
                    "--interval",
                    "4",
                    "--history-limit",
                    "8",
                    "--min-history",
                    "2",
                    "--timeout",
                    "240",
                ],
                "webcam-alert.log",
                "webcam-alert.pid",
            )
        )
    for source_id in voicechat_sources(args):
        services.append(voicechat_service(args, source_id))
    return services


def retired_environment_services() -> list[Service]:
    return [
        Service("server-environment", [], "webcam-server-agent.log", "webcam-server-agent.pid"),
        Service("wifi-environment", [], "webcam-wifi-agent.log", "webcam-wifi-agent.pid"),
        Service("bulb-environment", [], "webcam-bulb-agent.log", "webcam-bulb-agent.pid"),
        Service("browser-environment", [], "webcam-browser-agent.log", "webcam-browser-agent.pid"),
    ]


def retired_speech_services() -> list[Service]:
    return [
        Service("asr", [], "webcam-asr.log", "webcam-asr.pid"),
        Service("voice", [], "webcam-voice.log", "webcam-voice.pid"),
        Service("deepstream-nemotron", [], "webcam-deepstream-nemotron.log", "webcam-deepstream-nemotron.pid"),
        Service("voicechat", [], "webcam-voicechat.log", "webcam-voicechat.pid"),
        Service("voicechat-server", [], "webcam-voicechat-server.log", "webcam-voicechat-server.pid"),
        Service("voicechat-wifi", [], "webcam-voicechat-wifi.log", "webcam-voicechat-wifi.pid"),
        Service("voicechat-bulb", [], "webcam-voicechat-bulb.log", "webcam-voicechat-bulb.pid"),
        Service("voicechat-browser", [], "webcam-voicechat-browser.log", "webcam-voicechat-browser.pid"),
        Service("go2rtc", [], "webcam-go2rtc.log", "webcam-go2rtc.pid"),
    ]


def expected_process_token(service: Service) -> str:
    for part in service.command:
        name = Path(str(part)).name
        if name.endswith(".py") or name == "go2rtc":
            return name
    if service.name.endswith("-environment"):
        return "nemotron_environment_agent.py"
    if service.name == "asr":
        return "nemotron_asr_monitor.py"
    if service.name == "voice":
        return "nemotron_voice_responder.py"
    return ""


def camera_rtsp_hosts(args: argparse.Namespace) -> set[str]:
    hosts: set[str] = set()
    for source in ("wifi", "bulb"):
        if not camera_lane_enabled(args, source):
            continue
        raw_url = str(getattr(args, f"{source}_rtsp_url", "") or "").strip()
        if not raw_url:
            continue
        parts = urlsplit(raw_url)
        if parts.hostname:
            hosts.add(parts.hostname)
    return hosts


def iter_process_ids() -> list[int]:
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            pids.append(int(entry.name))
    return pids


def cleanup_orphaned_camera_ffmpeg(args: argparse.Namespace) -> None:
    hosts = camera_rtsp_hosts(args)
    if not hosts:
        return
    cleaned: list[int] = []
    for pid in iter_process_ids():
        if pid == os.getpid() or not is_running(pid):
            continue
        if process_parent_pid(pid) != 1:
            continue
        try:
            raw_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except Exception:
            continue
        executable_name = Path(raw_cmdline[0].decode("utf-8", "replace")).name if raw_cmdline and raw_cmdline[0] else ""
        cmdline = b" ".join(raw_cmdline).decode("utf-8", "replace")
        if executable_name != "ffmpeg" or "rtsp://" not in cmdline:
            continue
        if not any(host in cmdline for host in hosts):
            continue
        stop_pid(pid, timeout=3.0)
        cleaned.append(pid)
    if cleaned:
        print(f"camera media cleanup: stopped orphaned ffmpeg pid(s) {', '.join(str(pid) for pid in cleaned)}")


def write_voicechat_pipeline_mode(args: argparse.Namespace) -> None:
    payload = {
        "mode": "voicechat",
        "updated_at": time.time(),
        "message": "Canary ASR, Parakeet telemetry, Nemotron decisions, and Piper TTS are the enabled speech pipeline.",
    }
    path = Path(runtime_path(args, "webcam-speech-pipeline-mode.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def environment_service(args: argparse.Namespace, source_id: str, label: str, snapshot_name: str) -> Service:
    agent_python = executable(args.agent_python)
    script_root = Path(args.script_root)
    return Service(
        f"{source_id}-environment",
        [
            agent_python,
            str(script_root / "scripts/nemotron_environment_agent.py"),
            "--source-id",
            source_id,
            "--source-label",
            label,
            "--model",
            str(args.nemotron_omni_model),
            "--agent-state-json",
            runtime_path(args, f"webcam-{source_id}-agent-state.json"),
            "--analysis-json",
            runtime_path(args, "webcam-analysis.json"),
            "--cosmos-trigger-json",
            runtime_path(args, "webcam-cosmos-trigger.json"),
            "--cosmos-lock-file",
            runtime_path(args, "webcam-cosmos-trigger.lock"),
            "--alert-json",
            runtime_path(args, "webcam-alert.json"),
            "--transcript-json",
            runtime_path(args, "webcam-transcript.json"),
            "--voice-response-json",
            runtime_path(args, "webcam-voice-response.json"),
            "--wake-json",
            runtime_path(args, "webcam-environment-wake.json"),
            "--database",
            runtime_path(args, f"webcam-{source_id}-environment-history.sqlite3"),
            "--screenshot-dir",
            runtime_path(args, f"webcam-{source_id}-agent-screenshots"),
            "--snapshot-url",
            f"http://127.0.0.1:{args.port}/{snapshot_name}",
            "--interval",
            f"{args.env_interval:g}",
            "--cosmos-timeout",
            f"{args.cosmos_timeout:g}",
        ],
        f"webcam-{source_id}-agent.log",
        f"webcam-{source_id}-agent.pid",
    )


def voice_service(args: argparse.Namespace) -> Service:
    agent_python = executable(args.agent_python)
    script_root = Path(args.script_root)
    return Service(
        "voice",
        [
            agent_python,
            str(script_root / "scripts/nemotron_voice_responder.py"),
            "--model",
            "nemotron-mini:latest",
            "--transcript-json",
            runtime_path(args, "webcam-transcript.json"),
            "--analysis-json",
            runtime_path(args, "webcam-analysis.json"),
            "--cosmos-trigger-json",
            runtime_path(args, "webcam-cosmos-trigger.json"),
            "--cosmos-lock-file",
            runtime_path(args, "webcam-cosmos-trigger.lock"),
            "--visual-cache-json",
            runtime_path(args, "webcam-voice-visual-cache.json"),
            "--environment-wake-json",
            runtime_path(args, "webcam-environment-wake.json"),
            "--no-force-visual-update",
            "--force-visual-update-timeout",
            "60",
            "--force-visual-update-max-age-seconds",
            "20",
            "--force-visual-update-poll-seconds",
            "0.5",
            "--alert-json",
            runtime_path(args, "webcam-alert.json"),
            "--database",
            runtime_path(args, "webcam-analysis-history.sqlite3"),
            "--server-agent-state-json",
            runtime_path(args, "webcam-server-agent-state.json"),
            "--browser-agent-state-json",
            runtime_path(args, "webcam-browser-agent-state.json"),
            "--wifi-agent-state-json",
            runtime_path(args, "webcam-wifi-agent-state.json"),
            "--bulb-agent-state-json",
            runtime_path(args, "webcam-bulb-agent-state.json"),
            "--server-database",
            runtime_path(args, "webcam-server-environment-history.sqlite3"),
            "--browser-database",
            runtime_path(args, "webcam-browser-environment-history.sqlite3"),
            "--wifi-database",
            runtime_path(args, "webcam-wifi-environment-history.sqlite3"),
            "--bulb-database",
            runtime_path(args, "webcam-bulb-environment-history.sqlite3"),
            "--voice-response-json",
            runtime_path(args, "webcam-voice-response.json"),
            "--voice-session-reset-json",
            runtime_path(args, "webcam-voice-session-reset.json"),
            "--pipeline-mode-json",
            runtime_path(args, "webcam-speech-pipeline-mode.json"),
            "--voice-output-target-json",
            runtime_path(args, "webcam-voice-output-target.json"),
            "--notification-stats-db",
            runtime_path(args, "webcam-notification-stats.sqlite3"),
            "--audio-dir",
            runtime_path(args, "webcam-voice-audio"),
            "--screenshot-dir",
            runtime_path(args, "webcam-voice-screenshots"),
            "--snapshot-url",
            f"http://127.0.0.1:{args.port}/snapshot.jpg",
            "--server-snapshot-url",
            f"http://127.0.0.1:{args.port}/server-snapshot.jpg",
            "--browser-snapshot-url",
            f"http://127.0.0.1:{args.port}/browser-snapshot.jpg",
            "--wifi-snapshot-url",
            f"http://127.0.0.1:{args.port}/wifi-snapshot.jpg",
            "--bulb-snapshot-url",
            f"http://127.0.0.1:{args.port}/bulb-snapshot.jpg",
            "--server-audio-sink",
            args.server_audio_sink,
            "--history-limit",
            "4",
            "--voice-context-history-limit",
            "2",
            "--voice-visual-context-chars",
            "520",
            "--voice-alert-context-chars",
            "220",
            "--voice-history-item-chars",
            "120",
            "--nemotron-num-predict",
            "192",
            "--nemotron-num-ctx",
            "2048",
            "--nemotron-temperature",
            "0",
            "--nemotron-keep-alive",
            "60m",
            "--nemotron-warmup",
            "--enable-tools",
            "--max-tool-calls",
            "3",
            "--tool-timeout",
            "8",
            "--tool-result-chars",
            "2200",
            "--tool-planner-model",
            "nemotron-mini:latest",
            "--tool-planner-timeout",
            "15",
            "--tool-planner-num-predict",
            "160",
            "--tool-planner-num-ctx",
            "768",
            "--tool-planner-queue-json",
            runtime_path(args, "webcam-tool-planner-queue.json"),
            "--enable-system-tools",
            "--shell-tool-mode",
            "read_only",
            "--shell-timeout",
            "8",
            "--shell-output-chars",
            "2400",
            "--shell-cwd",
            str(Path(args.runtime_root)),
            "--response-candidates",
            "1",
            "--response-max-words",
            "55",
            "--response-timeout",
            "18",
            "--response-fallback-model",
            "nemotron-mini:latest",
            "--screenshot-count",
            "0",
            "--screenshot-delay",
            "0",
            "--no-send-screenshots",
            "--no-send-images-to-nemotron",
            "--tts-backend",
            "none",
            "--no-magpie-warmup",
            "--output-target-mode",
            "auto",
            "--utterance-idle-seconds",
            "1.1",
            "--utterance-gap-seconds",
            "2",
            "--max-utterance-seconds",
            "16",
            "--min-utterance-words",
            "2",
            "--dialog-activation-mode",
            "any",
        ],
        "webcam-voice.log",
        "webcam-voice.pid",
    )


def voicechat_sources(args: argparse.Namespace) -> list[str]:
    sources = ["server"]
    if wifi_lane_enabled(args) and args.enable_wifi_speech_worker and not args.no_wifi_speech_worker:
        sources.append("wifi")
    if bulb_lane_enabled(args) and not args.no_bulb_speech_worker:
        sources.append("bulb")
    if not args.no_browser_lane:
        sources.append("browser")
    return sources


def voicechat_output_target(source_mode: str) -> str:
    return {
        "server": "server",
        "wifi": "wifi_camera",
        "bulb": "bulb_camera",
        "browser": "browser",
    }.get(source_mode, "browser")


def voicechat_service(args: argparse.Namespace, source_mode: str) -> Service:
    agent_python = executable(args.agent_python)
    script_root = Path(args.script_root)
    voicechat_script = script_root / "scripts/nemotron_voicechat_pipeline.py"
    source_mode = str(source_mode or "server").strip().lower()
    default_camera_source = "wifi" if wifi_lane_enabled(args) else ("bulb" if bulb_lane_enabled(args) else "wifi")
    camera_source = source_mode if source_mode in {"wifi", "bulb"} else default_camera_source
    camera_audio_url = f"http://127.0.0.1:{args.port}/{camera_source}-audio.wav"
    camera_talk_audio_url = f"http://127.0.0.1:{args.port}/{camera_source}-talk-audio"
    camera_ptz_url = f"http://127.0.0.1:{args.port}/{camera_source}-ptz"
    camera_rtsp_url = str(getattr(args, f"{camera_source}_rtsp_url", "") or "")
    camera_rtsp_user = str(getattr(args, f"{camera_source}_rtsp_user", "") or "")
    camera_rtsp_password_env = str(getattr(args, f"{camera_source}_rtsp_password_env", "AMCREST_PASSWORD") or "AMCREST_PASSWORD")
    camera_ptz_pulse_ms = "180" if camera_source == "bulb" else "120"
    camera_ptz_speed = "2" if camera_source == "bulb" else "1"
    command = [
            agent_python,
            str(voicechat_script),
            "--pipeline-mode-json",
            runtime_path(args, "webcam-speech-pipeline-mode.json"),
            "--transcript-json",
            runtime_path(args, "webcam-transcript.json"),
            "--voicechat-response-json",
            runtime_path(args, "webcam-voicechat-response.json"),
            "--voicechat-session-reset-json",
            runtime_path(args, "webcam-voice-session-reset.json"),
            "--voicechat-manual-input-json",
            runtime_path(args, "webcam-voicechat-manual-input.json"),
            "--voice-output-target-json",
            runtime_path(args, "webcam-voice-output-target.json"),
            "--speech-playback-lock-json",
            runtime_path(args, "webcam-speech-playback-lock.json"),
            "--browser-audio-dir",
            runtime_path(args, "browser-audio-chunks"),
            "--audio-buffer-control-json",
            runtime_path(args, "webcam-audio-buffer-control.json"),
            "--asr-settings-json",
            runtime_path(args, "webcam-asr-settings.json"),
            "--component-audio-settings-json",
            runtime_path(args, "webcam-component-audio-settings.json"),
            "--server-microphone-adapter-state-json",
            runtime_path(args, "webcam-server-microphone-adapter.json"),
            "--notification-stats-db",
            runtime_path(args, "webcam-notification-stats.sqlite3"),
            "--server-agent-state-json",
            runtime_path(args, "webcam-server-agent-state.json"),
            "--browser-agent-state-json",
            runtime_path(args, "webcam-browser-agent-state.json"),
            "--wifi-agent-state-json",
            runtime_path(args, "webcam-wifi-agent-state.json"),
            "--bulb-agent-state-json",
            runtime_path(args, "webcam-bulb-agent-state.json"),
            "--environment-wake-json",
            runtime_path(args, "webcam-environment-wake.json"),
            "--server-snapshot-url",
            f"http://127.0.0.1:{args.port}/server-snapshot.jpg",
            "--browser-snapshot-url",
            f"http://127.0.0.1:{args.port}/browser-snapshot.jpg",
            "--wifi-snapshot-url",
            f"http://127.0.0.1:{args.port}/wifi-snapshot.jpg",
            "--bulb-snapshot-url",
            f"http://127.0.0.1:{args.port}/bulb-snapshot.jpg",
            "--omni-snapshot-count",
            "1",
            "--omni-snapshot-interval",
            "0",
            "--omni-snapshot-max-width",
            "256",
            "--omni-snapshot-jpeg-quality",
            "42",
            "--source-mode",
            source_mode,
            "--wifi-audio-url",
            camera_audio_url,
            "--wifi-rtsp-url",
            camera_rtsp_url,
            "--wifi-rtsp-user",
            camera_rtsp_user,
            "--wifi-rtsp-password-env",
            camera_rtsp_password_env,
            "--wifi-rtsp-transport",
            "tcp",
            "--wifi-audio-gain-db",
            "18",
            "--startup-audio-drop-seconds",
            "3",
            "--post-playback-listen-cooldown-seconds",
            "0.35",
            "--playback-lock-stale-seconds",
            "35",
            "--chunk-seconds",
            "0.125",
            "--browser-chunk-min-age",
            "0.08",
            "--browser-chunk-max-age",
            "10",
            "--utterance-gap-seconds",
            "0.65",
            "--utterance-final-silence-seconds",
            "0.65",
            "--utterance-preroll-seconds",
            "1.2",
            "--utterance-max-seconds",
            "18",
            "--max-conversation-turns",
            "24",
            "--wifi-speech-rms-multiplier",
            "0.75",
            "--wifi-speech-peak-multiplier",
            "0.8",
            "--wifi-utterance-min-voiced-chunks",
            "1",
            "--wifi-utterance-min-voiced-seconds",
            "0.25",
            "--marblenet-vad-device",
            "cpu",
            "--marblenet-vad-min-speech-ratio",
            "0.03",
            "--marblenet-vad-min-speech-seconds",
            "0.08",
            "--loop-delay",
            "0.08",
            "--backend",
            "vllm",
            "--model-api-url",
            f"http://127.0.0.1:{args.nemotron_omni_port}",
            "--model-api-runtime",
            "vllm",
            "--dedicated-asr-url",
            f"http://127.0.0.1:{args.dedicated_asr_port}",
            "--dedicated-asr-timeout",
            "15",
            "--ollama-model",
            str(args.nemotron_omni_model),
            "--voicechat-audio-max-tokens",
            "112",
            "--voice-decision-max-tokens",
            "64",
            "--voicechat-audio-timeout",
            "60",
            "--voicechat-keep-alive",
            "60m",
            "--voicechat-warmup-timeout",
            "75",
            "--no-voicechat-warmup",
            "--voicechat-keep-alive-refresh-seconds",
            "2400",
            "--answer-model",
            str(args.nemotron_omni_model),
            "--answer-timeout",
            "180",
            "--answer-max-tokens",
            "320",
            "--answer-num-ctx",
            "32768",
            "--answer-keep-alive",
            "-1",
            "--tool-planner-model",
            "nemotron-mini:latest",
            "--tool-planner-url",
            "http://127.0.0.1:11434",
            "--tool-planner-timeout",
            "15",
            "--tool-planner-num-predict",
            "160",
            "--tool-planner-num-ctx",
            "768",
            "--tool-planner-queue-json",
            runtime_path(args, "webcam-tool-planner-queue.json"),
            "--enable-system-tools",
            "--shell-tool-mode",
            "read_only",
            "--environment-tool-timeout",
            "90",
            "--camera-ptz-url",
            camera_ptz_url,
            "--camera-ptz-default-source",
            camera_source,
            "--camera-ptz-degrees",
            "5",
            "--camera-ptz-pulse-ms",
            camera_ptz_pulse_ms,
            "--camera-ptz-speed",
            camera_ptz_speed,
            "--focus-object-command-json",
            "/dev/shm/dgx-spark-focus-command.json",
            "--focus-object-state-json",
            "/dev/shm/dgx-spark-focus-state.json",
            "--focus-object-timeout",
            "180",
            "--audio-dir",
            runtime_path(args, "webcam-voicechat-audio"),
            "--no-request-native-audio",
            "--no-native-audio-required",
            "--tts-backend",
            "piper",
            "--piper-model-path",
            str(Path.home() / ".cache/dgx-spark/piper/en_US-lessac-medium.onnx"),
            "--piper-voice-pool",
            str(Path.home() / ".cache/dgx-spark/piper/en_US-hfc_male-medium.onnx"),
            "--kokoro-voice",
            "af_heart",
            "--kokoro-device",
            "cpu",
            "--kokoro-warmup" if source_mode in {"server", "wifi", "bulb"} else "--no-kokoro-warmup",
            "--no-magpie-warmup",
            "--native-audio-voice",
            "Sofia",
            "--magpie-voice",
            "Sofia",
            "--magpie-speaker-index",
            "1",
            "--server-audio-sink",
            args.server_audio_sink,
            "--server-playback-lead-silence-seconds",
            "0.65",
            "--wifi-talk-audio-url",
            camera_talk_audio_url,
            "--wifi-talkback-timeout",
            "30",
            "--output-target-mode",
            voicechat_output_target(source_mode),
    ]
    if source_mode == "server":
        command += [
            "--server-audio-format", "pulse",
            "--server-audio-source", "dgx_nexigo_70",
        ]
    if script_supports_flag(voicechat_script, "--text-prompt-queue-json"):
        insertion = command.index("--asr-settings-json")
        command[insertion:insertion] = [
            "--text-prompt-queue-json",
            runtime_path(args, "webcam-text-prompt-queue.json"),
            "--text-prompt-magpie-voice",
            "male",
            "--text-prompt-magpie-speaker-index",
            "0",
            "--text-prompt-volume-peak",
            "0.98",
            "--text-prompt-max-gain-db",
            "30",
        ]
    if script_supports_flag(voicechat_script, "--secrets-env-file"):
        insertion = command.index("--enable-system-tools")
        command[insertion:insertion] = [
            "--secrets-env-file",
            str(Path(args.camera_env_file).expanduser()),
        ]
    if script_supports_flag(voicechat_script, "--wifi-audio-capture-mode"):
        insertion = command.index("--wifi-rtsp-url")
        command[insertion:insertion] = [
            "--wifi-audio-capture-mode",
            "shared",
        ]
    if script_supports_flag(voicechat_script, "--wifi-utterance-final-silence-seconds"):
        insertion = command.index("--utterance-preroll-seconds")
        command[insertion:insertion] = [
            "--wifi-utterance-final-silence-seconds",
            "2.35",
            "--browser-utterance-final-silence-seconds",
            "1.75",
        ]
    if script_supports_flag(voicechat_script, "--wifi-utterance-min-seconds"):
        insertion = command.index("--loop-delay")
        command[insertion:insertion] = [
            "--wifi-utterance-min-seconds",
            "1.4",
            "--wifi-utterance-max-active-seconds",
            "7.5",
        ]
    if script_supports_flag(voicechat_script, "--wifi-marblenet-vad-min-speech-ratio"):
        insertion = command.index("--loop-delay")
        command[insertion:insertion] = [
            "--wifi-marblenet-vad-min-speech-ratio",
            "0.03",
            "--wifi-marblenet-vad-min-speech-seconds",
            "0.08",
        ]
    if script_supports_flag(voicechat_script, "--listening-beep-duration"):
        insertion = command.index("--backend")
        command[insertion:insertion] = [
            "--listening-beep-duration",
            "0.35",
        ]
    if script_supports_flag(voicechat_script, "--stage-chimes"):
        insertion = command.index("--backend")
        command[insertion:insertion] = ["--no-stage-chimes", "--no-listening-beep"]
    if script_supports_flag(voicechat_script, "--voicechat-snapshot"):
        command.insert(command.index("--backend"), "--no-voicechat-snapshot")
    if source_mode == "server" and script_supports_flag(voicechat_script, "--camera-tools"):
        command.insert(command.index("--enable-system-tools"), "--no-camera-tools")
    return Service(
        f"voicechat-{source_mode}",
        command,
        f"webcam-voicechat-{source_mode}.log",
        f"webcam-voicechat-{source_mode}.pid",
    )


def start_service(args: argparse.Namespace, service: Service) -> None:
    pid_path = service_pid_path(args, service)
    existing_pid = read_pid(pid_path)
    if is_running(existing_pid):
        expected_token = expected_process_token(service)
        if expected_token and expected_token not in pid_cmdline(existing_pid):
            unlink_pid_file(pid_path)
            print(f"{service.name}: ignored stale pid={existing_pid}")
            existing_pid = None
    if is_running(existing_pid):
        if not args.force:
            print(f"{service.name}: already running pid={existing_pid}")
            return
        stop_pid(existing_pid)
    log_path = Path(args.runtime_root) / service.log_name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("LLM_TOKEN_USAGE_DB", runtime_path(args, "webcam-llm-token-usage.sqlite3"))
    if service.env:
        env.update(service.env)
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            service.command,
            cwd=args.runtime_root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    print(f"{service.name}: started pid={process.pid}")


def stop_service(args: argparse.Namespace, service: Service) -> None:
    pid_path = service_pid_path(args, service)
    pid = read_pid(pid_path)
    if not is_running(pid):
        unlink_pid_file(pid_path)
        print(f"{service.name}: stopped")
        return
    expected_token = expected_process_token(service)
    if expected_token and expected_token not in pid_cmdline(pid):
        unlink_pid_file(pid_path)
        print(f"{service.name}: stopped stale pid={pid}")
        return
    stop_pid(pid)
    unlink_pid_file(pid_path)
    print(f"{service.name}: stopped pid={pid}")


def print_status(args: argparse.Namespace, services: list[Service]) -> int:
    failed = 0
    for service in services:
        pid = read_pid(service_pid_path(args, service))
        running = is_running(pid)
        failed += 0 if running else 1
        state = "running" if running else "stopped"
        print(f"{service.name:20} {state:8} pid={pid or '-'}")
    return 1 if failed else 0


def main() -> int:
    args = parse_args()
    Path(args.runtime_root).mkdir(parents=True, exist_ok=True)
    args._env_file_values = load_env_file(args.camera_env_file)
    if args.action in {"start", "restart"}:
        write_voicechat_pipeline_mode(args)
    services = build_services(args)
    selective = bool(args.only_service)
    if selective:
        requested = {str(name or "").strip() for name in args.only_service if str(name or "").strip()}
        available = {service.name for service in services}
        unknown = sorted(requested - available)
        if unknown:
            print(f"Unknown service name(s): {', '.join(unknown)}", file=sys.stderr)
            print(f"Available services: {', '.join(sorted(available))}", file=sys.stderr)
            return 2
        services = [service for service in services if service.name in requested]
    if args.action == "status":
        return print_status(args, services)
    if args.action == "stop":
        if not selective:
            for service in retired_environment_services():
                stop_service(args, service)
            for service in retired_speech_services():
                stop_service(args, service)
        for service in reversed(services):
            stop_service(args, service)
        if not selective:
            cleanup_orphaned_camera_ffmpeg(args)
        return 0
    if args.action == "restart":
        if not selective:
            for service in retired_environment_services():
                stop_service(args, service)
            for service in retired_speech_services():
                stop_service(args, service)
        for service in reversed(services):
            stop_service(args, service)
        args.force = True
    elif not selective:
        for service in retired_speech_services():
            stop_service(args, service)
    if not selective and not args.enable_environment_agents:
        for service in retired_environment_services():
            stop_service(args, service)
    if not selective:
        cleanup_orphaned_camera_ffmpeg(args)
    for index, service in enumerate(services):
        start_service(args, service)
        if service.name == "stream":
            wait_http(f"http://127.0.0.1:{args.port}/", timeout=15.0)
        if service.name == "nemotron-omni":
            ready = wait_http(
                f"http://127.0.0.1:{args.nemotron_omni_port}/v1/models",
                timeout=max(30.0, float(args.nemotron_omni_startup_timeout)),
            )
            if not ready:
                print("nemotron-omni: failed readiness check; downstream voice workers were not started")
                stop_service(args, service)
                return 1
            if not warm_nemotron_omni(args):
                print("nemotron-omni: failed startup warmup; downstream voice workers were not started")
                stop_service(args, service)
                return 1
        if service.name == "dedicated-asr":
            ready = wait_http(
                f"http://127.0.0.1:{args.dedicated_asr_port}/health",
                timeout=max(30.0, float(args.dedicated_asr_startup_timeout)),
            )
            if not ready:
                print("dedicated-asr: failed readiness check; downstream voice workers were not started")
                stop_service(args, service)
                return 1
        if service.name.startswith("voicechat-") and index < len(services) - 1:
            # Kokoro and its CUDA kernels initialize per worker. Starting every
            # lane simultaneously can make the warmups contend and fall back to
            # Piper, so give each physical lane a short head start.
            time.sleep(4.0)
    return print_status(args, services)


if __name__ == "__main__":
    raise SystemExit(main())

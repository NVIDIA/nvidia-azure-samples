#!/usr/bin/env python3
"""Run deepstream-yolo-coco and publish latest detections as dashboard JSON."""

from __future__ import annotations

import argparse
import configparser
import ctypes
import fcntl
import json
import math
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import select
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

try:
    from .deepstream_realtime_focus import RealtimeFocusDispatcher
except ImportError:
    from deepstream_realtime_focus import RealtimeFocusDispatcher

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DEEPSTREAM_ROOT = PROJECT_ROOT / "deepstream-yolo-coco"
DEFAULT_WORK_DIR = DEFAULT_DEEPSTREAM_ROOT / "DeepStream-Yolo"
DEFAULT_SECRETS_ENV_FILE = Path.home() / ".config/dgx-spark/secrets.env"
SECRETS_ENV_CACHE: dict[str, str] | None = None
PERSON_ANIMAL_CLASS_IDS = {
    "person": 0,
    "bird": 14,
    "cat": 15,
    "dog": 16,
    "horse": 17,
    "sheep": 18,
    "cow": 19,
    "elephant": 20,
    "bear": 21,
    "zebra": 22,
    "giraffe": 23,
}


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp_path.write_bytes(content)
    tmp_path.replace(path)


def write_json(path: Path, payload: dict) -> None:
    # This file is replaced for every detector frame. Compact JSON cuts the
    # metadata write volume roughly in half without changing its schema.
    atomic_write_bytes(
        path,
        (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"),
    )


def write_source_detection_json(path: Path, source: str, payload: dict) -> dict:
    """Publish one detector lane without discarding concurrent lane state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                previous = {}
            previous_sources = previous.get("sources") if isinstance(previous, dict) else {}
            sources = dict(previous_sources) if isinstance(previous_sources, dict) else {}
            current_sources = payload.get("sources") if isinstance(payload.get("sources"), dict) else {}
            source_key = str(source or "").strip().lower()
            source_payload = current_sources.get(source_key)
            if source_key and isinstance(source_payload, dict):
                sources[source_key] = source_payload
            merged = {**payload, "sources": sources}
            write_json(path, merged)
            return merged
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def preview_frame_id(path: Path) -> int:
    try:
        return int(path.stem.rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return -1


class PreviewMotionPublisher:
    """Coalesce preview frames into a tiny same-timeline grayscale artifact."""

    def __init__(
        self,
        output: Path,
        *,
        width: int = 160,
        height: int = 90,
        max_hz: float = 10.0,
    ) -> None:
        self.output = Path(output)
        self.metadata_output = self.output.with_suffix(self.output.suffix + ".json")
        self.width = max(16, int(width))
        self.height = max(16, int(height))
        self.minimum_interval_ns = int(1_000_000_000 / max_hz) if max_hz > 0 else 0
        self._condition = threading.Condition()
        self._pending: tuple[bytes, dict] | None = None
        self._latest: dict = {}
        self._stopped = False
        self._last_written_monotonic_ns = 0
        self._thread = threading.Thread(target=self._run, name="deepstream-preview-motion", daemon=True)
        self._thread.start()

    def submit(self, jpeg: bytes, metadata: dict) -> None:
        # Keep only the newest frame when conversion is slower than preview
        # publication. The detector/control loop never waits for this worker.
        with self._condition:
            self._pending = (jpeg, dict(metadata))
            self._condition.notify()

    def latest(self) -> dict:
        with self._condition:
            return dict(self._latest)

    def stop(self, timeout: float = 1.0) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify()
        self._thread.join(timeout=max(0.0, timeout))

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                remaining_ns = self.minimum_interval_ns - (
                    time.monotonic_ns() - self._last_written_monotonic_ns
                )
                if remaining_ns > 0:
                    # Wait on the condition so newer submissions replace the
                    # pending frame while honoring the low-churn rate limit.
                    self._condition.wait(timeout=remaining_ns / 1_000_000_000)
                    continue
                jpeg, metadata = self._pending
                self._pending = None
            try:
                image = Image.open(BytesIO(jpeg)).convert("L")
                image.thumbnail((self.width, self.height), Image.Resampling.BILINEAR)
                canvas = Image.new("L", (self.width, self.height))
                canvas.paste(image, ((self.width - image.width) // 2, (self.height - image.height) // 2))
                artifact = f"P5\n{self.width} {self.height}\n255\n".encode("ascii") + canvas.tobytes()
                written_monotonic_ns = time.monotonic_ns()
                artifact_metadata = {
                    **metadata,
                    "motion_frame_path": str(self.output),
                    "motion_frame_width": self.width,
                    "motion_frame_height": self.height,
                    "motion_frame_bytes": len(artifact),
                    "motion_frame_written_monotonic_ns": written_monotonic_ns,
                }
                atomic_write_bytes(self.output, artifact)
                write_json(self.metadata_output, artifact_metadata)
                with self._condition:
                    self._last_written_monotonic_ns = written_monotonic_ns
                    self._latest = artifact_metadata
            except (OSError, ValueError):
                # Preview publication remains independent of this optional
                # control artifact.
                continue


def default_image() -> str:
    local_image = "deepstream-yolo-coco:8.0-samples-sbsa"
    result = subprocess.run(
        ["docker", "image", "inspect", local_image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        return local_image
    return "nvcr.io/nvidia/deepstream:8.0-samples-multiarch"


def default_container_name(source: str) -> str:
    safe_source = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(source or "wifi").lower())
    return f"deepstream-yolo-coco-dashboard-{safe_source or 'wifi'}"


def cleanup_container(name: str) -> None:
    if not name:
        return
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def load_labels(path: Path) -> dict[str, int]:
    labels: dict[str, int] = {}
    try:
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            label = line.strip()
            if label:
                labels[label] = index
    except OSError:
        pass
    return labels


def default_snapshot_url(args: argparse.Namespace) -> str:
    source = str(args.source or "wifi").strip().lower()
    path = {
        "server": "/server-snapshot.jpg",
        "wifi": "/wifi-snapshot.jpg",
        "bulb": "/bulb-snapshot.jpg",
        "browser": "/browser-snapshot.jpg",
    }.get(source, "/wifi-snapshot.jpg")
    return f"{args.dashboard_url.rstrip('/')}{path}"


def default_stream_url(args: argparse.Namespace) -> str:
    source = str(args.source or "wifi").strip().lower()
    path = {
        "server": "/stream.mjpg",
        "wifi": "/wifi-stream.mjpg",
        "bulb": "/bulb-stream.mjpg",
        "browser": "/browser-stream.mjpg",
    }.get(source, "/wifi-stream.mjpg")
    return f"{args.dashboard_url.rstrip('/')}{path}"


def mask_uri(uri: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(uri)
    except ValueError:
        return uri
    if not parsed.username and not parsed.password:
        return uri
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    netloc = f"{parsed.username or 'user'}:***@{host}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def load_secrets_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return {}
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    values: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        try:
            parts = shlex.split(line, comments=False, posix=True)
        except ValueError:
            parts = [line]
        if not parts or "=" not in parts[0]:
            continue
        key, value = parts[0].split("=", 1)
        key = key.strip()
        if key:
            values[key] = value
    return values


def secrets_env_values(args: argparse.Namespace) -> dict[str, str]:
    global SECRETS_ENV_CACHE
    if SECRETS_ENV_CACHE is None:
        SECRETS_ENV_CACHE = load_secrets_env_file(getattr(args, "secrets_env_file", DEFAULT_SECRETS_ENV_FILE))
    return SECRETS_ENV_CACHE


def rtsp_password(args: argparse.Namespace, source: str) -> str:
    env_name = str(getattr(args, f"{source}_rtsp_password_env", "") or "").strip()
    if not env_name:
        return ""
    return os.environ.get(env_name, "") or secrets_env_values(args).get(env_name, "")


def rtsp_url_with_credentials(url: str, user: str, password: str) -> str:
    value = str(url or "").strip()
    if not value or not user or not password:
        return value
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or "@" in parts.netloc:
        return value
    credentials = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(password, safe='')}"
    return urllib.parse.urlunsplit((parts.scheme, f"{credentials}@{parts.netloc}", parts.path, parts.query, parts.fragment))


def direct_rtsp_stream_url(args: argparse.Namespace, source: str) -> str:
    source_key = str(source or "").strip().lower()
    url = str(getattr(args, f"{source_key}_rtsp_url", "") or "").strip()
    if not url:
        return ""
    user = str(getattr(args, f"{source_key}_rtsp_user", "") or "").strip()
    return rtsp_url_with_credentials(url, user, rtsp_password(args, source_key))


def resolve_stream_url(args: argparse.Namespace) -> str:
    explicit_url = str(args.stream_url or os.environ.get("DEEPSTREAM_STREAM_URL") or "").strip()
    if explicit_url:
        return explicit_url
    direct_url = direct_rtsp_stream_url(args, args.source)
    if direct_url:
        return direct_url
    return default_stream_url(args)


def fetch_snapshot_jpeg(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "deepstream-yolo-coco/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def write_runtime_config(args: argparse.Namespace, source_uri: str) -> Path:
    base_path = Path(args.work_dir) / args.base_config
    base_infer_path = Path(args.work_dir) / args.base_infer_config
    runtime_infer_path = Path(args.work_dir) / args.runtime_infer_config
    infer_parser = configparser.ConfigParser(interpolation=None)
    infer_parser.optionxform = str
    with base_infer_path.open("r", encoding="utf-8") as infer_file:
        infer_parser.read_file(infer_file)
    if not infer_parser.has_section("class-attrs-all"):
        infer_parser.add_section("class-attrs-all")
    infer_parser.set("class-attrs-all", "pre-cluster-threshold", str(args.detection_threshold))
    with runtime_infer_path.open("w", encoding="utf-8") as output:
        infer_parser.write(output, space_around_delimiters=False)
    try:
        runtime_infer_path.chmod(0o600)
    except OSError:
        pass

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    with base_path.open("r", encoding="utf-8") as config_file:
        parser.read_file(config_file)
    for section in ("application", "source0", "streammux", "sink0"):
        if not parser.has_section(section):
            parser.add_section(section)
    parser.set("application", "gie-kitti-output-dir", args.bbox_dir_name)
    parser.set("source0", "enable", "1")
    is_rtsp = urllib.parse.urlsplit(source_uri).scheme.lower() == "rtsp"
    parser.set("source0", "type", "4" if is_rtsp else "3")
    parser.set("source0", "uri", source_uri)
    if is_rtsp:
        parser.set("source0", "latency", str(max(1, int(getattr(args, "rtsp_latency_ms", 50)))))
        parser.set("source0", "drop-on-latency", "1")
        parser.set("source0", "select-rtp-protocol", "4")
        parser.set("source0", "rtsp-reconnect-interval-sec", "10")
        parser.set("source0", "rtsp-reconnect-attempts", "-1")
    else:
        for option in (
            "latency",
            "drop-on-latency",
            "select-rtp-protocol",
            "rtsp-reconnect-interval-sec",
            "rtsp-reconnect-attempts",
        ):
            parser.remove_option("source0", option)
    drop_frame_interval = max(0, int(getattr(args, "drop_frame_interval", 0) or 0))
    if drop_frame_interval:
        parser.set("source0", "drop-frame-interval", str(drop_frame_interval))
    else:
        parser.remove_option("source0", "drop-frame-interval")
    parser.set("streammux", "live-source", "1" if args.live_source else "0")
    parser.set("streammux", "width", str(args.streammux_width))
    parser.set("streammux", "height", str(args.streammux_height))
    parser.set("streammux", "batched-push-timeout", "10000")
    parser.set("sink0", "enable", "1")
    parser.set("sink0", "type", "1")
    parser.set("sink0", "sync", "0")
    if not parser.has_section("primary-gie"):
        parser.add_section("primary-gie")
    parser.set("primary-gie", "config-file", runtime_infer_path.name)

    runtime_path = Path(args.work_dir) / args.runtime_config
    with runtime_path.open("w", encoding="utf-8") as output:
        parser.write(output, space_around_delimiters=False)
    try:
        runtime_path.chmod(0o600)
    except OSError:
        pass
    return runtime_path


def docker_command(args: argparse.Namespace, runtime_config: Path) -> list[str]:
    root_dir = Path(args.deepstream_root)
    work_dir = Path(args.work_dir)
    image = args.image or default_image()
    ld_prefix = ""
    extra_mounts: list[str] = []
    runtime_lib_dir = root_dir / "runtime-libs/deepstream"
    if runtime_lib_dir.is_dir():
        extra_mounts += ["-v", f"{runtime_lib_dir}:/opt/ds-sbsa-libs:ro"]
        ld_prefix = "/opt/ds-sbsa-libs:"
    bbox_path = Path(args.bbox_dir_name)
    if bbox_path.is_absolute():
        # DeepStream's KITTI sink is retained for compatibility, but placing
        # its per-frame files on tmpfs removes high-rate physical disk writes.
        extra_mounts += ["-v", f"{bbox_path}:{bbox_path}"]
    preview_dir = str(getattr(args, "synced_preview_dir_name", ".dashboard-synced-preview") or ".dashboard-synced-preview")
    preview_path = Path(preview_dir)
    if preview_path.is_absolute():
        extra_mounts += ["-v", f"{preview_path}:{preview_path}"]
    preview_pipeline = ""
    if str(getattr(args, "synced_preview_output", "") or "").strip():
        if str(getattr(args, "source", "wifi") or "").strip().lower() == "server":
            preview_source = "uridecodebin uri=\"$uri\" ! videoconvert ! "
        else:
            preview_source = (
                f"rtspsrc location=\"$uri\" protocols=tcp latency={max(1, int(getattr(args, 'rtsp_latency_ms', 50)))} "
                "drop-on-latency=true ! rtph264depay ! h264parse ! decodebin ! "
            )
        preview_pipeline = (
            f"mkdir -p {shlex.quote(preview_dir)}; "
            f"rm -f {shlex.quote(preview_dir)}/*.jpg; "
            f"uri=$(sed -n 's/^uri=//p' {shlex.quote(runtime_config.name)} | head -n1); "
            "( while true; do "
            f"gst-launch-1.0 -q {preview_source}nvvideoconvert ! "
            "'video/x-raw(memory:NVMM),format=NV12' ! mux.sink_0 "
            f"nvstreammux name=mux batch-size=1 width={int(args.streammux_width)} height={int(args.streammux_height)} "
            "live-source=true batched-push-timeout=10000 ! "
            f"nvinfer config-file-path={shlex.quote(args.runtime_infer_config)} batch-size=1 ! "
            "nvvideoconvert ! video/x-raw,format=I420 ! "
            "jpegenc quality=82 ! "
            f"multifilesink location={shlex.quote(preview_dir + '/frame-%010d.jpg')} max-files=3; "
            "preview_status=$?; echo \"boxed preview pipeline exited with code $preview_status; restarting\" >&2; sleep 1; "
            "done ) & "
            "preview_pid=$!; "
        )
    shell = (
        f"mkdir -p {shlex.quote(args.bbox_dir_name)}; "
        f"{preview_pipeline}"
        f"deepstream-app -c {shlex.quote(runtime_config.name)}; status=$?; "
        "if [ -n \"${preview_pid:-}\" ]; then kill \"$preview_pid\" 2>/dev/null || true; wait \"$preview_pid\" 2>/dev/null || true; fi; "
        "exit $status"
    )
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        args.container_name,
        "--runtime=nvidia",
        "--gpus",
        "all",
        "--network=host",
        "--privileged",
        "--entrypoint",
        "/bin/bash",
        "-e",
        "LD_LIBRARY_PATH=/opt/ds-codec-libs:"
        f"{ld_prefix}/opt/nvidia/deepstream/deepstream/lib:"
        "/opt/nvidia/deepstream/deepstream-8.0/lib:"
        "/usr/lib/aarch64-linux-gnu/tegra:"
        "/usr/local/nvidia/lib:/usr/local/nvidia/lib64:"
        "/usr/local/cuda/lib64:/usr/local/cuda-13.0/lib64",
        "-e",
        "GST_PLUGIN_PATH=/usr/lib/aarch64-linux-gnu/gstreamer-1.0:"
        "/usr/lib/aarch64-linux-gnu/gstreamer-1.0/deepstream",
        *extra_mounts,
        "-v",
        f"{work_dir}:/workspace",
        "-w",
        "/workspace",
        image,
        "-lc",
        shell,
    ]


def parse_frame_id(path: Path) -> int:
    try:
        return int(path.stem.rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        return 0


def parse_kitti_file(path: Path, labels: dict[str, int], frame_width: int, frame_height: int) -> list[dict]:
    objects: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return objects
    for line in lines:
        parts = line.split()
        if len(parts) < 16:
            continue
        label = parts[0]
        try:
            left, top, right, bottom = (float(parts[4]), float(parts[5]), float(parts[6]), float(parts[7]))
            confidence = float(parts[-1])
        except ValueError:
            continue
        width = max(0.0, right - left)
        height = max(0.0, bottom - top)
        objects.append(
            {
                "label": label,
                "class_id": labels.get(label, PERSON_ANIMAL_CLASS_IDS.get(label)),
                "confidence": confidence,
                "bbox": [left, top, width, height],
                "frame_width": frame_width,
                "frame_height": frame_height,
            }
        )
    return objects


def bbox_iou(first: list[float], second: list[float]) -> float:
    if len(first) < 4 or len(second) < 4:
        return 0.0
    ax1, ay1, aw, ah = map(float, first[:4])
    bx1, by1, bw, bh = map(float, second[:4])
    ax2, ay2 = ax1 + max(0.0, aw), ay1 + max(0.0, ah)
    bx2, by2 = bx1 + max(0.0, bw), by1 + max(0.0, bh)
    overlap = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(0.0, min(ay2, by2) - max(ay1, by1))
    union = max(0.0, aw * ah) + max(0.0, bw * bh) - overlap
    return overlap / union if union > 0.0 else 0.0


class DetectionTracker:
    """Assign stable local IDs to detector boxes using class-aware frame association."""

    def __init__(self, frame_width: int, frame_height: int, max_missed_frames: int = 60) -> None:
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))
        self.max_missed_frames = max(1, int(max_missed_frames))
        self.next_id = 1
        self.tracks: dict[int, dict] = {}

    def update(self, objects: list[dict]) -> list[dict]:
        detections = [dict(item) for item in objects if isinstance(item, dict)]
        pairs = []
        for track_id, track in self.tracks.items():
            for index, item in enumerate(detections):
                if str(item.get("label") or "") != track["label"]:
                    continue
                bbox = item.get("bbox") if isinstance(item.get("bbox"), list) else []
                if len(bbox) < 4:
                    continue
                iou = bbox_iou(track["bbox"], bbox)
                previous_center = track["center"]
                current_center = (
                    (float(bbox[0]) + float(bbox[2]) / 2.0) / self.frame_width,
                    (float(bbox[1]) + float(bbox[3]) / 2.0) / self.frame_height,
                )
                distance = math.hypot(current_center[0] - previous_center[0], current_center[1] - previous_center[1])
                if iou >= 0.02 or distance <= 0.35:
                    pairs.append((iou * 2.0 + max(0.0, 0.35 - distance), track_id, index, current_center))

        assigned_tracks = set()
        assigned_detections = set()
        for _score, track_id, index, current_center in sorted(pairs, reverse=True):
            if track_id in assigned_tracks or index in assigned_detections:
                continue
            item = detections[index]
            track = self.tracks[track_id]
            track.update({
                "bbox": list(item["bbox"]),
                "center": current_center,
                "missed": 0,
                "hits": int(track.get("hits", 0)) + 1,
            })
            item["track_id"] = track_id
            item["track_age_frames"] = track["hits"]
            assigned_tracks.add(track_id)
            assigned_detections.add(index)

        for index, item in enumerate(detections):
            if index in assigned_detections:
                continue
            bbox = item.get("bbox") if isinstance(item.get("bbox"), list) else []
            if len(bbox) < 4:
                continue
            track_id = self.next_id
            self.next_id += 1
            center = (
                (float(bbox[0]) + float(bbox[2]) / 2.0) / self.frame_width,
                (float(bbox[1]) + float(bbox[3]) / 2.0) / self.frame_height,
            )
            self.tracks[track_id] = {
                "label": str(item.get("label") or ""), "bbox": list(bbox),
                "center": center, "missed": 0, "hits": 1,
            }
            item["track_id"] = track_id
            item["track_age_frames"] = 1
            assigned_tracks.add(track_id)

        for track_id in list(self.tracks):
            if track_id not in assigned_tracks:
                self.tracks[track_id]["missed"] = int(self.tracks[track_id].get("missed", 0)) + 1
                if self.tracks[track_id]["missed"] > self.max_missed_frames:
                    del self.tracks[track_id]
        return detections


def object_confidence_probability(item: dict) -> float | None:
    try:
        raw = float(item.get("confidence", item.get("score", item.get("probability"))))
    except (TypeError, ValueError):
        return None
    if raw > 1.0:
        raw /= 100.0
    return max(0.0, min(1.0, raw))


def load_settings_json(path: str | Path) -> dict:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def minimum_object_confidence_probability(settings: dict | None, default_percent: float = 25.0) -> float:
    payload = settings if isinstance(settings, dict) else {}
    raw_percent = payload.get(
        "minimum_object_confidence_percent",
        payload.get(
            "object_confidence_percent",
            payload.get("min_object_confidence_percent"),
        ),
    )
    if raw_percent is not None:
        try:
            percent = float(raw_percent)
        except (TypeError, ValueError):
            percent = default_percent
        return max(0.0, min(1.0, percent / 100.0))

    raw_threshold = payload.get(
        "minimum_object_confidence_threshold",
        payload.get(
            "object_confidence_threshold",
            payload.get("min_object_confidence_threshold"),
        ),
    )
    if raw_threshold is not None:
        try:
            threshold = float(raw_threshold)
        except (TypeError, ValueError):
            threshold = default_percent / 100.0
        if threshold > 1.0:
            threshold /= 100.0
        return max(0.0, min(1.0, threshold))

    return max(0.0, min(1.0, float(default_percent) / 100.0))


def filter_objects_by_confidence(objects: list[dict], minimum_probability: float) -> list[dict]:
    try:
        threshold = float(minimum_probability)
    except (TypeError, ValueError):
        threshold = 0.25
    threshold = max(0.0, min(1.0, threshold))
    return [
        dict(item)
        for item in objects
        if isinstance(item, dict) and (object_confidence_probability(item) or 0.0) >= threshold
    ]


def annotate_preview_percentages(jpeg: bytes, objects: list[dict]) -> bytes:
    if not objects:
        return jpeg
    try:
        image = Image.open(BytesIO(jpeg)).convert("RGB")
    except (OSError, ValueError):
        return jpeg
    draw = ImageDraw.Draw(image)
    image_width, image_height = image.size
    if image_width <= 1 or image_height <= 1:
        return jpeg
    color = (247, 199, 107)
    background = (8, 11, 15)
    text_color = (255, 245, 214)
    line_width = max(2, round(min(image_width, image_height) / 180))
    pad_x = max(4, round(image_width / 180))
    pad_y = max(2, round(image_height / 220))
    label_rects: list[tuple[int, int, int, int]] = []

    def intersects_existing(rect: tuple[int, int, int, int]) -> bool:
        left, top, right, bottom = rect
        for other_left, other_top, other_right, other_bottom in label_rects:
            if left <= other_right and right >= other_left and top <= other_bottom and bottom >= other_top:
                return True
        return False

    for item in objects[:32]:
        if not isinstance(item, dict):
            continue
        bbox = item.get("bbox") or item.get("box") or item.get("rect")
        if not isinstance(bbox, list) or len(bbox) < 4:
            continue
        try:
            left, top, width, height = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
            frame_width = float(item.get("frame_width") or item.get("image_width") or image_width)
            frame_height = float(item.get("frame_height") or item.get("image_height") or image_height)
        except (TypeError, ValueError):
            continue
        if frame_width <= 0 or frame_height <= 0 or width <= 0 or height <= 0:
            continue
        x1 = max(0, min(image_width - 1, left / frame_width * image_width))
        y1 = max(0, min(image_height - 1, top / frame_height * image_height))
        x2 = max(0, min(image_width - 1, (left + width) / frame_width * image_width))
        y2 = max(0, min(image_height - 1, (top + height) / frame_height * image_height))
        if x2 <= x1 or y2 <= y1:
            continue
        label = str(item.get("label") or item.get("class") or item.get("class_name") or item.get("name") or "object")
        probability = object_confidence_probability(item)
        text = f"{label} {probability * 100:.0f}%" if probability is not None else label
        if not text.strip():
            continue
        draw.rectangle((x1, y1, x2, y2), outline=color, width=line_width)
        text_bbox = draw.textbbox((0, 0), text)
        text_width = max(24, text_bbox[2] - text_bbox[0])
        text_height = max(8, text_bbox[3] - text_bbox[1])
        label_width = min(image_width - 2, text_width + pad_x * 2)
        label_height = text_height + pad_y * 2
        label_x = max(0, min(image_width - label_width - 1, round(x1)))
        label_y = round(y1) - label_height - 2 if y1 > label_height + 4 else round(y1) + 2
        label_y = max(0, min(image_height - label_height - 1, label_y))
        label_rect = (label_x, label_y, label_x + label_width, label_y + label_height)
        for step in range(1, 8):
            if not intersects_existing(label_rect):
                break
            shifted_y = min(image_height - label_height - 1, label_y + step * (label_height + 2))
            label_rect = (label_x, shifted_y, label_x + label_width, shifted_y + label_height)
        label_x, label_y = label_rect[0], label_rect[1]
        draw.rectangle(
            label_rect,
            fill=background,
            outline=color,
            width=1,
        )
        draw.text((label_x + pad_x, label_y + pad_y), text, fill=text_color)
        label_rects.append(label_rect)
    output = BytesIO()
    image.save(output, format="JPEG", quality=82)
    return output.getvalue()


def latest_kitti_file(path: Path, *, require_nonempty: bool = False) -> Path | None:
    candidates = [
        item
        for item in path.glob("*.txt")
        if item.is_file() and (not require_nonempty or item.stat().st_size > 0)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item.stat().st_mtime, parse_frame_id(item), item.name))


class SequentialKittiTracker:
    """Track DeepStream's sequential KITTI filenames without rescanning per frame."""

    def __init__(self, directory: Path, *, resync_seconds: float = 1.0, max_advance: int = 512) -> None:
        self.directory = Path(directory)
        self.resync_seconds = max(0.1, float(resync_seconds))
        self.max_advance = max(1, int(max_advance))
        self.current: Path | None = None
        self.prefix = ""
        self.number_width = 0
        self.frame_id = -1
        self.last_resync = 0.0

    def reset(self) -> None:
        self.current = None
        self.prefix = ""
        self.number_width = 0
        self.frame_id = -1
        self.last_resync = 0.0

    def _adopt(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        token = path.stem.rsplit("_", 1)
        if len(token) != 2 or not token[1].isdigit():
            return None
        self.current = path
        self.prefix = token[0]
        self.number_width = len(token[1])
        self.frame_id = int(token[1])
        return path

    def _discover(self, now: float) -> Path | None:
        self.last_resync = now
        return self._adopt(latest_kitti_file(self.directory))

    def newest(self, now: float | None = None) -> Path | None:
        checked_at = time.monotonic() if now is None else float(now)
        if self.current is None or self.frame_id < 0 or not self.prefix:
            return self._discover(checked_at)

        advanced = False
        for _ in range(self.max_advance):
            next_frame = self.frame_id + 1
            candidate = self.directory / f"{self.prefix}_{next_frame:0{self.number_width}d}.txt"
            if not candidate.is_file():
                break
            self.current = candidate
            self.frame_id = next_frame
            advanced = True

        if not advanced and checked_at - self.last_resync >= self.resync_seconds:
            discovered = latest_kitti_file(self.directory)
            self.last_resync = checked_at
            if discovered is not None and discovered != self.current:
                return self._adopt(discovered)
        return self.current


class DirectoryEventWake:
    """Wake immediately when DeepStream closes or creates a KITTI frame file."""

    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_TO = 0x00000080
    IN_CREATE = 0x00000100

    def __init__(self, directory: Path) -> None:
        self.fd = -1
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fd = int(libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC))
            if fd < 0:
                return
            encoded = os.fsencode(str(directory))
            watch = int(libc.inotify_add_watch(fd, encoded, self.IN_CLOSE_WRITE | self.IN_MOVED_TO | self.IN_CREATE))
            if watch < 0:
                os.close(fd)
                return
            self.fd = fd
        except Exception:
            self.fd = -1

    def wait(self, timeout: float, stop_event: threading.Event) -> None:
        if self.fd < 0:
            stop_event.wait(timeout)
            return
        readable, _, _ = select.select([self.fd], [], [], max(0.001, float(timeout)))
        if readable:
            try:
                os.read(self.fd, 65536)
            except BlockingIOError:
                pass

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def detection_message(objects: list[dict]) -> str:
    if not objects:
        return "Live Stream Processor is running; no COCO objects detected in the latest frame."
    counts: dict[str, int] = {}
    for item in objects:
        label = str(item.get("label") or "object")
        counts[label] = counts.get(label, 0) + 1
    detected = ", ".join(f"{label}{f' x{count}' if count > 1 else ''}" for label, count in sorted(counts.items()))
    return f"Live Stream Processor detected {detected}."


def publish_detection_json(
    path: Path,
    source: str,
    status: str,
    message: str,
    frame_id: int | None = None,
    objects: list[dict] | None = None,
    error: str = "",
    fps: float | None = None,
    source_frame_id: int | None = None,
    object_frame_id: int | None = None,
    objects_held: bool = False,
    objects_updated_at: float | None = None,
    objects_updated_monotonic_ns: int | None = None,
    processor: dict | None = None,
) -> dict:
    now = time.time()
    published_monotonic_ns = time.monotonic_ns()
    published_objects = objects or []
    published_frame_id = object_frame_id if object_frame_id is not None else frame_id
    source_frame = source_frame_id if source_frame_id is not None else frame_id
    source_payload = {
        "status": status,
        "updated_at": now,
        "published_monotonic_ns": published_monotonic_ns,
        "source": source,
        "model": "YOLO11 COCO",
        "runtime": "deepstream-yolo-coco",
        "message": message,
        "objects": published_objects,
    }
    if objects_held:
        source_payload["objects_held"] = True
    if published_frame_id is not None:
        source_payload["frame_id"] = published_frame_id
        source_payload["object_frame_id"] = published_frame_id
    if source_frame is not None:
        source_payload["source_frame_id"] = source_frame
    if objects_updated_at is not None:
        source_payload["objects_updated_at"] = objects_updated_at
    if objects_updated_monotonic_ns is not None:
        source_payload["objects_updated_monotonic_ns"] = int(objects_updated_monotonic_ns)
    if processor is not None:
        source_payload["processor"] = processor
        for key, value in processor.items():
            if key not in {"source", "status", "message", "model", "runtime", "objects"}:
                source_payload[key] = value
    if error:
        source_payload["error"] = error
    if fps is not None:
        source_payload["fps"] = fps
    payload = {
        "status": status,
        "updated_at": now,
        "published_monotonic_ns": published_monotonic_ns,
        "model": "YOLO11 COCO",
        "runtime": "deepstream-yolo-coco",
        "message": message,
        "source": source,
        "objects": published_objects,
        "sources": {source: source_payload},
    }
    if objects_held:
        payload["objects_held"] = True
    if published_frame_id is not None:
        payload["frame_id"] = published_frame_id
        payload["object_frame_id"] = published_frame_id
    if source_frame is not None:
        payload["source_frame_id"] = source_frame
    if objects_updated_at is not None:
        payload["objects_updated_at"] = objects_updated_at
    if objects_updated_monotonic_ns is not None:
        payload["objects_updated_monotonic_ns"] = int(objects_updated_monotonic_ns)
    if processor is not None:
        payload["processor"] = processor
        for key, value in processor.items():
            if key not in {"source", "status", "message", "model", "runtime", "objects"}:
                payload[key] = value
    if error:
        payload["error"] = error
    if fps is not None:
        payload["fps"] = fps
    return write_source_detection_json(path, source, payload)


def load_prior_detection_state(path: Path, source: str) -> tuple[list[dict], int, float | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], -1, None
    if not isinstance(payload, dict):
        return [], -1, None
    source_key = str(source or "").strip().lower()
    source_payload = payload
    sources = payload.get("sources")
    if isinstance(sources, dict) and isinstance(sources.get(source_key), dict):
        source_payload = sources[source_key]
    objects = source_payload.get("objects")
    if not isinstance(objects, list):
        return [], -1, None
    clean_objects = [dict(item) for item in objects if isinstance(item, dict)]
    if not clean_objects:
        return [], -1, None
    frame_id = source_payload.get("object_frame_id", source_payload.get("frame_id", -1))
    try:
        object_frame_id = int(frame_id)
    except (TypeError, ValueError):
        object_frame_id = -1
    updated_at = source_payload.get("objects_updated_at", source_payload.get("updated_at"))
    try:
        objects_updated_at = float(updated_at) if updated_at is not None else None
    except (TypeError, ValueError):
        objects_updated_at = None
    return clean_objects, object_frame_id, objects_updated_at


def int_from_values(*values: object) -> int:
    candidates: list[int] = []
    for value in values:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            continue
        candidates.append(number)
    return max(candidates) if candidates else 0


def load_prior_processor_counters(path: Path, source: str) -> tuple[int, int, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, 0, 0
    if not isinstance(payload, dict):
        return 0, 0, 0
    source_key = str(source or "").strip().lower()
    source_payload = payload
    sources = payload.get("sources")
    if isinstance(sources, dict) and isinstance(sources.get(source_key), dict):
        source_payload = sources[source_key]
    processor = source_payload.get("processor")
    if not isinstance(processor, dict):
        processor = payload.get("processor") if isinstance(payload.get("processor"), dict) else {}
    sampled_frames = int_from_values(
        source_payload.get("sampled_frames"),
        source_payload.get("source_frame_id"),
        processor.get("sampled_frames"),
        processor.get("source_frame_id"),
    )
    emitted_events = int_from_values(
        source_payload.get("emitted_frames"),
        source_payload.get("emitted_frame_id"),
        processor.get("emitted_frames"),
        processor.get("emitted_frame_id"),
    )
    deepstream_runs = int_from_values(
        source_payload.get("deepstream_runs"),
        processor.get("deepstream_runs"),
        emitted_events,
    )
    return sampled_frames, emitted_events, deepstream_runs


def write_event_video(args: argparse.Namespace, jpeg: bytes, event_id: int) -> Path:
    event_dir = Path(args.work_dir) / args.motion_event_dir
    event_dir.mkdir(parents=True, exist_ok=True)
    stem = f"event_{event_id:06d}"
    jpg_path = event_dir / f"{stem}.jpg"
    video_path = event_dir / f"{stem}.mp4"
    jpg_path.write_bytes(jpeg)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(args.motion_event_fps),
        "-t",
        str(args.motion_event_seconds),
        "-i",
        str(jpg_path),
        "-vf",
        (
            f"scale={args.streammux_width}:{args.streammux_height}:force_original_aspect_ratio=decrease,"
            f"pad={args.streammux_width}:{args.streammux_height}:(ow-iw)/2:(oh-ih)/2"
        ),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(video_path),
    ]
    subprocess.run(cmd, cwd=PROJECT_ROOT, check=True, timeout=args.event_video_timeout)
    return video_path


def run_subprocess_with_watchdog(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: float,
    heartbeat=None,
    heartbeat_interval: float = 1.0,
    on_timeout=None,
) -> tuple[subprocess.CompletedProcess, float]:
    started = time.monotonic()
    process = subprocess.Popen(cmd, cwd=cwd)
    timeout_seconds = max(0.1, float(timeout))
    heartbeat_interval = max(0.1, float(heartbeat_interval))
    last_heartbeat = started
    while True:
        return_code = process.poll()
        now = time.monotonic()
        elapsed = now - started
        if return_code is not None:
            return subprocess.CompletedProcess(cmd, return_code), elapsed
        if heartbeat and now - last_heartbeat >= heartbeat_interval:
            heartbeat(elapsed, timeout_seconds)
            last_heartbeat = now
        if elapsed >= timeout_seconds:
            if on_timeout:
                on_timeout()
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                if on_timeout:
                    on_timeout()
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            raise subprocess.TimeoutExpired(cmd, timeout_seconds)
        time.sleep(0.05)


def run_deepstream_event(
    args: argparse.Namespace,
    labels: dict[str, int],
    bbox_dir: Path,
    jpeg: bytes,
    event_id: int,
    heartbeat=None,
) -> tuple[list[dict], dict]:
    for old_file in bbox_dir.glob("*.txt"):
        old_file.unlink(missing_ok=True)
    video_path = write_event_video(args, jpeg, event_id)
    relative_video = video_path.relative_to(Path(args.work_dir))
    original_live_source = args.live_source
    args.live_source = False
    try:
        runtime_config = write_runtime_config(args, f"file:///workspace/{relative_video.as_posix()}")
    finally:
        args.live_source = original_live_source
    cleanup_container(args.container_name)
    cmd = docker_command(args, runtime_config)
    result, duration = run_subprocess_with_watchdog(
        cmd,
        cwd=PROJECT_ROOT,
        timeout=args.deepstream_event_timeout,
        heartbeat=heartbeat,
        heartbeat_interval=args.processor_heartbeat_interval,
        on_timeout=lambda: cleanup_container(args.container_name),
    )
    latest = latest_kitti_file(bbox_dir, require_nonempty=True) or latest_kitti_file(bbox_dir)
    objects = parse_kitti_file(latest, labels, args.streammux_width, args.streammux_height) if latest else []
    return objects, {
        "deepstream_return_code": result.returncode,
        "deepstream_duration_seconds": duration,
        "deepstream_event_video": str(video_path),
        "deepstream_kitti_file": str(latest) if latest else "",
        "deepstream_frames": len(list(bbox_dir.glob("*.txt"))),
    }


def run(args: argparse.Namespace) -> int:
    args.work_dir = str(Path(args.work_dir).resolve())
    args.deepstream_root = str(Path(args.deepstream_root).resolve())
    args.container_name = args.container_name or default_container_name(args.source)
    bbox_dir = Path(args.work_dir) / args.bbox_dir_name
    bbox_dir.mkdir(parents=True, exist_ok=True)
    output_json = Path(args.output_json).resolve()
    synced_preview_output = Path(args.synced_preview_output).resolve() if args.synced_preview_output else None
    synced_preview_raw_output = (
        synced_preview_output.with_name(f"{synced_preview_output.stem}-raw{synced_preview_output.suffix}")
        if synced_preview_output is not None
        else None
    )
    synced_preview_dir = Path(args.work_dir) / args.synced_preview_dir_name
    synced_preview_dir.mkdir(parents=True, exist_ok=True)
    synced_preview_signature: tuple[str, int, int] | None = None
    preview_state: dict = {}
    motion_output = (
        Path(args.motion_frame_output).resolve()
        if str(args.motion_frame_output or "").strip()
        else synced_preview_output.with_name(f"{synced_preview_output.stem}-motion.pgm")
        if synced_preview_output is not None
        else None
    )
    motion_publisher = (
        PreviewMotionPublisher(
            motion_output,
            width=args.motion_frame_width,
            height=args.motion_frame_height,
            max_hz=args.motion_frame_hz,
        )
        if motion_output is not None
        else None
    )
    last_objects: list[dict] = []
    last_objects_updated_at: float | None = None
    last_objects_updated_monotonic_ns: int | None = None
    last_notification_summary: dict[str, list[float]] | None = None
    labels = load_labels(Path(args.work_dir) / "labels.txt")
    stream_url = resolve_stream_url(args)
    stream_label = mask_uri(stream_url)
    snapshot_url = args.motion_snapshot_url or default_snapshot_url(args)
    stop_event = threading.Event()
    process: subprocess.Popen | None = None
    stream_started_at = 0.0
    latest_kitti_signature: tuple[str, int, int] | None = None
    latest_kitti_frame_id: int | None = None
    kitti_tracker = SequentialKittiTracker(bbox_dir)
    detection_tracker = DetectionTracker(args.streammux_width, args.streammux_height)
    kitti_wake = DirectoryEventWake(bbox_dir)
    realtime_dispatcher = RealtimeFocusDispatcher(
        source=args.source,
        settings_path=args.focus_settings_json,
        command_path=args.focus_command_json,
        state_path=args.realtime_focus_state_json,
        timeout_seconds=args.realtime_focus_timeout,
    ) if str(args.focus_command_json or "").strip() else None

    def make_processor_state(
        processor_status: str,
        message: str,
        *,
        error: str = "",
        extra: dict | None = None,
    ) -> dict:
        state = {
            "component": "deepstream_yolo_coco",
            "processor_status": processor_status,
            "deepstream_status": processor_status,
            "message": message,
            "source": args.source,
            "stream_url": stream_label,
            "snapshot_url": snapshot_url,
            "updated_at": time.time(),
        }
        if stream_started_at:
            state["stream_uptime_seconds"] = round(max(0.0, time.time() - stream_started_at), 2)
        if last_objects_updated_at is not None:
            state["objects_age_seconds"] = round(max(0.0, time.time() - last_objects_updated_at), 2)
        if error:
            state["deepstream_error"] = error
        if extra:
            state.update(extra)
        return state

    def publish(
        status: str,
        processor_status: str,
        message: str,
        *,
        objects: list[dict] | None = None,
        error: str = "",
        extra: dict | None = None,
        objects_updated_at: float | None = None,
    ) -> None:
        nonlocal last_notification_summary
        published_objects = objects if objects is not None else last_objects
        detection_payload = publish_detection_json(
            output_json,
            args.source,
            status,
            message,
            latest_kitti_frame_id,
            published_objects,
            error=error,
            objects_held=False,
            objects_updated_at=objects_updated_at if objects_updated_at is not None else last_objects_updated_at,
            objects_updated_monotonic_ns=last_objects_updated_monotonic_ns,
            processor=make_processor_state(
                processor_status,
                message,
                error=error,
                extra={**preview_state, **(motion_publisher.latest() if motion_publisher else {}), **(extra or {})},
            ),
        )
        if objects_updated_at is not None:
            realtime_request = realtime_dispatcher.consider(
                published_objects,
                frame_id=int(latest_kitti_frame_id or 0),
                objects_updated_at=float(objects_updated_at),
                objects_updated_monotonic_ns=int(last_objects_updated_monotonic_ns or time.monotonic_ns()),
            ) if realtime_dispatcher is not None else {}
            focus_wake_path = str(getattr(args, "focus_wake_socket", "") or "").strip()
            if focus_wake_path:
                source_payload = detection_payload.get("sources", {}).get(args.source, detection_payload)
                envelope = {
                    "v": 1,
                    "type": "focus_frame",
                    "source": args.source,
                    "frame_id": int(latest_kitti_frame_id or 0),
                    "monotonic_ns": int(last_objects_updated_monotonic_ns or time.monotonic_ns()),
                    "detections": source_payload,
                    "command": realtime_request,
                }
                client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    client.sendto(json.dumps(envelope, separators=(",", ":")).encode("utf-8"), focus_wake_path)
                except OSError:
                    pass
                finally:
                    client.close()
            wake_paths: set[str] = set()
            notification_summary: dict[str, list[float]] = {}
            for item in published_objects:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "")
                notification_summary.setdefault(label, []).append(float(item.get("confidence") or 0.0))
            for values in notification_summary.values():
                values.sort(reverse=True)
            notification_changed = last_notification_summary is None or set(notification_summary) != set(last_notification_summary)
            if not notification_changed and last_notification_summary is not None:
                for label, values in notification_summary.items():
                    previous_values = last_notification_summary.get(label, [])
                    if len(values) != len(previous_values) or any(
                        abs(current - previous) >= 0.08
                        for current, previous in zip(values, previous_values)
                    ):
                        notification_changed = True
                        break
            if notification_changed:
                last_notification_summary = notification_summary
                wake_paths.add(str(getattr(args, "notification_wake_socket", "") or "").strip())
            for wake_path in wake_paths - {""}:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    client.sendto(str(latest_kitti_frame_id or 0).encode("ascii"), wake_path)
                except OSError:
                    # The controller retains its short polling fallback.
                    pass
                finally:
                    client.close()

    def remove_old_kitti_files(keep: int = 90) -> None:
        files = sorted(
            [item for item in bbox_dir.glob("*.txt") if item.is_file()],
            key=lambda item: (item.stat().st_mtime_ns, item.name),
            reverse=True,
        )
        for old_file in files[keep:]:
            old_file.unlink(missing_ok=True)

    def promote_synced_preview() -> bool:
        nonlocal synced_preview_signature, preview_state
        if synced_preview_output is None:
            return False
        try:
            candidates = [item for item in synced_preview_dir.glob("*.jpg") if item.is_file()]
            latest_preview = max(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name))
            stat = latest_preview.stat()
            signature = (latest_preview.name, stat.st_mtime_ns, stat.st_size)
        except (OSError, ValueError):
            return False
        if signature == synced_preview_signature:
            return False
        try:
            jpeg = latest_preview.read_bytes()
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                return False
            annotation_objects = last_objects
            latest_annotations = latest_kitti_file(bbox_dir)
            if latest_annotations is not None:
                annotation_objects = parse_kitti_file(
                    latest_annotations,
                    labels,
                    args.streammux_width,
                    args.streammux_height,
                )
            raw_annotation_object_count = len(annotation_objects)
            settings = load_settings_json(args.focus_settings_json)
            minimum_probability = minimum_object_confidence_probability(
                settings,
                default_percent=max(0.0, min(100.0, float(args.detection_threshold) * 100.0)),
            )
            annotation_objects = filter_objects_by_confidence(annotation_objects, minimum_probability)
            promoted_jpeg = annotate_preview_percentages(jpeg, annotation_objects)
            promoted_monotonic_ns = time.monotonic_ns()
            current_preview_state = {
                "preview_frame_id": preview_frame_id(latest_preview),
                "preview_source_file": latest_preview.name,
                "preview_source_mtime_ns": stat.st_mtime_ns,
                "preview_promoted_at": time.time(),
                "preview_promoted_monotonic_ns": promoted_monotonic_ns,
                "preview_path": str(synced_preview_output),
                "preview_annotation_object_count": len(annotation_objects),
                "preview_raw_annotation_object_count": raw_annotation_object_count,
                "preview_minimum_object_confidence_percent": round(minimum_probability * 100.0, 3),
                "preview_percentages_annotated": bool(annotation_objects),
            }
            if synced_preview_raw_output is not None:
                atomic_write_bytes(synced_preview_raw_output, jpeg)
            atomic_write_bytes(synced_preview_output, promoted_jpeg)
        except OSError:
            return False
        synced_preview_signature = signature
        preview_state = current_preview_state
        if motion_publisher is not None:
            motion_publisher.submit(promoted_jpeg, current_preview_state)
        return True

    def start_deepstream() -> subprocess.Popen:
        nonlocal stream_started_at, latest_kitti_signature, latest_kitti_frame_id, synced_preview_signature
        cleanup_container(args.container_name)
        for old_file in bbox_dir.glob("*.txt"):
            old_file.unlink(missing_ok=True)
        latest_kitti_signature = None
        latest_kitti_frame_id = None
        kitti_tracker.reset()
        synced_preview_signature = None
        if synced_preview_output is not None:
            synced_preview_output.unlink(missing_ok=True)
        if synced_preview_raw_output is not None:
            synced_preview_raw_output.unlink(missing_ok=True)
        args.live_source = True
        runtime_config = write_runtime_config(args, stream_url)
        cmd = docker_command(args, runtime_config)
        stream_started_at = time.time()
        print("Starting deepstream-yolo-coco live stream processor", flush=True)
        print("Stream source:", stream_label, flush=True)
        return subprocess.Popen(cmd, cwd=PROJECT_ROOT)

    startup_message = f"Starting Live Stream Processor on {stream_label}."
    publish("starting", "starting", startup_message, objects=last_objects)

    def stop_process(_signum: int, _frame: object) -> None:
        stop_event.set()
        cleanup_container(args.container_name)

    signal.signal(signal.SIGTERM, stop_process)
    signal.signal(signal.SIGINT, stop_process)

    last_publish = time.monotonic()
    last_kitti_seen_at = 0.0
    last_kitti_prune = 0.0
    metadata_poll_seconds = 1.0 / max(1.0, min(60.0, float(args.metadata_publish_hz)))
    process = start_deepstream()
    publish(
        "active",
        "running",
        "Live Stream Processor is reading the stream; waiting for DeepStream object output.",
        objects=last_objects,
        extra={"deepstream_started_at": stream_started_at},
    )

    while not stop_event.is_set():
        now = time.monotonic()
        promote_synced_preview()
        return_code = process.poll() if process else None
        if return_code is not None:
            message = f"Live Stream Processor exited with code {return_code}; restarting stream reader."
            publish(
                "error",
                "restarting",
                message,
                objects=last_objects,
                error=message,
                extra={"deepstream_return_code": return_code, "deepstream_completed_at": time.time()},
            )
            stop_event.wait(max(0.1, float(args.stream_restart_delay)))
            if stop_event.is_set():
                break
            process = start_deepstream()
            publish(
                "active",
                "running",
                "Live Stream Processor restarted and is reading the stream.",
                objects=last_objects,
                extra={"deepstream_started_at": stream_started_at},
            )
            last_publish = time.monotonic()
            continue

        latest = kitti_tracker.newest(now)
        if latest is not None:
            try:
                stat = latest.stat()
                signature = (latest.name, stat.st_mtime_ns, stat.st_size)
            except OSError:
                signature = None
            if signature and signature != latest_kitti_signature:
                objects = detection_tracker.update(
                    parse_kitti_file(latest, labels, args.streammux_width, args.streammux_height)
                )
                last_objects = [dict(item) for item in objects]
                last_objects_updated_at = time.time()
                last_objects_updated_monotonic_ns = time.monotonic_ns()
                latest_kitti_signature = signature
                latest_kitti_frame_id = parse_frame_id(latest)
                last_kitti_seen_at = time.monotonic()
                message = detection_message(last_objects)
                publish(
                    "active",
                    "running",
                    message,
                    objects=last_objects,
                    objects_updated_at=last_objects_updated_at,
                    extra={
                        "deepstream_started_at": stream_started_at,
                        "deepstream_kitti_file": str(latest),
                        "deepstream_frame_id": latest_kitti_frame_id,
                        "deepstream_output_updated_at": last_objects_updated_at,
                    },
                )
                last_publish = time.monotonic()

        if now - last_kitti_prune >= max(1.0, float(args.kitti_prune_interval)):
            remove_old_kitti_files()
            last_kitti_prune = now

        if (time.monotonic() - last_publish) >= max(0.25, float(args.publish_interval)):
            if last_kitti_seen_at:
                message = detection_message(last_objects)
                extra = {"deepstream_started_at": stream_started_at}
                if (time.monotonic() - last_kitti_seen_at) > max(1.0, float(args.stream_stall_timeout)):
                    extra["deepstream_output_stale_seconds"] = round(time.monotonic() - last_kitti_seen_at, 2)
                    message = f"{message} Waiting for newer stream metadata."
                publish(
                    "active",
                    "running",
                    message,
                    objects=last_objects,
                    objects_updated_at=last_objects_updated_at,
                    extra=extra,
                )
            else:
                publish(
                    "active",
                    "running",
                    "Live Stream Processor is reading the stream; waiting for DeepStream object output.",
                    objects=last_objects,
                    extra={"deepstream_started_at": stream_started_at},
                )
            last_publish = time.monotonic()

        kitti_wake.wait(metadata_poll_seconds, stop_event)

    cleanup_container(args.container_name)
    kitti_wake.close()
    if realtime_dispatcher is not None:
        realtime_dispatcher.close()
    if motion_publisher is not None:
        motion_publisher.stop()
    stopped_message = "Live Stream Processor stopped."
    publish("stopped", "stopped", stopped_message, objects=last_objects)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="wifi", choices=("server", "wifi", "bulb", "browser"))
    parser.add_argument("--deepstream-root", default=str(DEFAULT_DEEPSTREAM_ROOT))
    parser.add_argument("--work-dir", default=str(DEFAULT_WORK_DIR))
    parser.add_argument("--base-config", default="deepstream_app_config_person_animals_headless.txt")
    parser.add_argument("--runtime-config", default=".runtime_dashboard_yolo_coco.txt")
    parser.add_argument("--base-infer-config", default="config_infer_primary_yolo11_person_animals.txt")
    parser.add_argument("--runtime-infer-config", default=".runtime_config_infer_primary_yolo11_coco.txt")
    parser.add_argument("--detection-threshold", type=float, default=0.25)
    parser.add_argument("--bbox-dir-name", default=".dashboard-kitti-output")
    parser.add_argument("--output-json", default=str(PROJECT_ROOT / "webcam-deepstream-yolo-coco.json"))
    parser.add_argument("--focus-settings-json", default=str(PROJECT_ROOT / "webcam-deepstream-settings.json"))
    parser.add_argument("--focus-command-json", default=str(PROJECT_ROOT / "webcam-focus-object-command.json"))
    parser.add_argument("--realtime-focus-state-json", default=str(PROJECT_ROOT / "webcam-realtime-focus-state.json"))
    parser.add_argument("--realtime-focus-timeout", type=float, default=180.0)
    parser.add_argument(
        "--focus-wake-socket",
        default="/tmp/dgx-spark-focus-wake.sock",
        help="Unix datagram socket notified after each fresh detection publication.",
    )
    parser.add_argument(
        "--notification-wake-socket",
        default="/tmp/dgx-spark-detection-wake.sock",
        help="Unix datagram socket notified after each fresh detection publication.",
    )
    parser.add_argument(
        "--synced-preview-output",
        default="",
        help="Atomic JPEG written from DeepStream's post-inference preview buffer.",
    )
    parser.add_argument("--synced-preview-dir-name", default=".dashboard-synced-preview")
    parser.add_argument(
        "--motion-frame-output",
        default="",
        help="Optional atomic low-resolution grayscale PGM derived asynchronously from the synchronized preview.",
    )
    parser.add_argument("--motion-frame-width", type=int, default=160)
    parser.add_argument("--motion-frame-height", type=int, default=90)
    parser.add_argument(
        "--motion-frame-hz",
        type=float,
        default=10.0,
        help="Maximum grayscale artifact publication rate; newer frames are coalesced.",
    )
    parser.add_argument("--settings-json", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--image", default=os.environ.get("DEEPSTREAM_IMAGE", ""))
    parser.add_argument("--container-name", default="", help="Docker container name used for cleanup across bridge restarts.")
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8090")
    parser.add_argument("--stream-url", default="", help="Stream URI consumed directly by the live stream processor.")
    parser.add_argument("--stream-stall-timeout", type=float, default=6.0, help="Seconds without new DeepStream output before marking metadata stale.")
    parser.add_argument("--rtsp-latency-ms", type=int, default=50, help="RTSP jitter buffer used by detector and preview pipelines.")
    parser.add_argument(
        "--drop-frame-interval",
        type=int,
        default=0,
        help="Decode one frame, then drop this many input frames to keep a live source at the live edge.",
    )
    parser.add_argument("--stream-restart-delay", type=float, default=2.0, help="Seconds to wait before restarting the live stream processor after exit.")
    parser.add_argument("--secrets-env-file", default=str(DEFAULT_SECRETS_ENV_FILE), help="Local KEY=value secrets file used when camera password env vars are not present.")
    parser.add_argument("--wifi-rtsp-url", default="rtsp://192.168.1.188:554/cam/realmonitor?channel=1&subtype=1", help="Direct Wi-Fi camera RTSP URL for DeepStream.")
    parser.add_argument("--wifi-rtsp-user", default="admin", help="Wi-Fi RTSP username; password is read from --wifi-rtsp-password-env.")
    parser.add_argument("--wifi-rtsp-password-env", default="AMCREST_PASSWORD", help="Environment or secrets-file key containing the Wi-Fi RTSP password.")
    parser.add_argument("--snapshot-url", dest="motion_snapshot_url", default="", help="JPEG snapshot URL sampled by the live stream processor.")
    parser.add_argument("--motion-snapshot-url", dest="motion_snapshot_url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--motion-change-percent", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--motion-pixel-threshold", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--motion-downsample-width", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--motion-downsample-height", type=int, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--sample-fps", dest="motion_sample_fps", type=float, default=5.0, help="Snapshot sample rate for live stream processor runs.")
    parser.add_argument("--motion-sample-fps", dest="motion_sample_fps", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--event-dir", dest="motion_event_dir", default=".live-stream-processor-events", help="Directory for generated single-frame event videos.")
    parser.add_argument("--motion-event-dir", dest="motion_event_dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--event-fps", dest="motion_event_fps", type=float, default=5.0, help="Frame rate for generated event videos.")
    parser.add_argument("--motion-event-fps", dest="motion_event_fps", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--event-seconds", dest="motion_event_seconds", type=float, default=0.4, help="Duration for generated event videos.")
    parser.add_argument("--motion-event-seconds", dest="motion_event_seconds", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--deepstream-event-timeout", type=float, default=10.0)
    parser.add_argument("--event-video-timeout", type=float, default=5.0, help="Seconds to wait for ffmpeg event-video creation.")
    parser.add_argument("--processor-heartbeat-interval", type=float, default=0.75, help="Seconds between running-state heartbeat publishes during a DeepStream run.")
    parser.add_argument("--snapshot-timeout", dest="motion_snapshot_timeout", type=float, default=2.0, help="Seconds to wait for a dashboard snapshot.")
    parser.add_argument("--motion-snapshot-timeout", dest="motion_snapshot_timeout", type=float, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--publish-interval", type=float, default=1.0)
    parser.add_argument("--metadata-publish-hz", type=float, default=60.0, help="Maximum live KITTI metadata publication rate (1-60 Hz).")
    parser.add_argument("--kitti-prune-interval", type=float, default=5.0, help="Seconds between retained KITTI file pruning passes.")
    parser.add_argument("--streammux-width", type=int, default=1920)
    parser.add_argument("--streammux-height", type=int, default=1080)
    parser.add_argument("--live-source", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def main() -> int:
    try:
        return run(parse_args())
    except Exception as exc:
        output_json = PROJECT_ROOT / "webcam-deepstream-yolo-coco.json"
        try:
            publish_detection_json(output_json, "wifi", "error", f"deepstream-yolo-coco bridge failed: {exc}", error=str(exc))
        except Exception:
            pass
        print(f"deepstream-yolo-coco bridge failed: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

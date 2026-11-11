from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.deepstream_yolo_coco_bridge import (  # noqa: E402
    docker_command,
    filter_objects_by_confidence,
    minimum_object_confidence_probability,
)
from scripts.webcam_stream_server import (  # noqa: E402
    deepstream_synced_preview_path,
    deepstream_filter_payload_objects,
    normalize_deepstream_settings,
)

from argparse import Namespace


def test_deepstream_preview_path_isolated_for_stationary_server_lane(tmp_path):
    configured = tmp_path / "deepstream-preview.jpg"
    assert deepstream_synced_preview_path(configured, "wifi") == configured
    assert deepstream_synced_preview_path(configured, "server") == tmp_path / "deepstream-preview-server.jpg"


def test_preview_confidence_filter_uses_minimum_percent():
    threshold = minimum_object_confidence_probability({"minimum_object_confidence_percent": 87})
    assert threshold == 0.87

    objects = [
        {"label": "person", "confidence": 0.47},
        {"label": "cat", "confidence": 0.91},
        {"label": "chair", "confidence": 91.5},
    ]

    assert [item["label"] for item in filter_objects_by_confidence(objects, threshold)] == ["cat", "chair"]


def test_preview_confidence_filter_accepts_threshold_alias():
    threshold = minimum_object_confidence_probability({"minimum_object_confidence_threshold": 0.52})

    objects = [
        {"label": "low", "confidence": 51},
        {"label": "high", "confidence": 0.53},
    ]

    assert [item["label"] for item in filter_objects_by_confidence(objects, threshold)] == ["high"]


def test_boxed_view_keeps_non_preferred_detections():
    settings = {
        "preferred_objects": ["cat"],
        "minimum_object_confidence_percent": 25,
    }
    payload = {
        "objects": [
            {"label": "cat", "confidence": 0.91},
            {"label": "person", "confidence": 0.88},
            {"label": "keyboard", "confidence": 0.79},
        ]
    }

    visible = deepstream_filter_payload_objects(payload, settings)["objects"]

    assert [item["label"] for item in visible] == ["cat", "person", "keyboard"]


def test_boxed_preview_pipeline_restarts_after_rtsp_failure(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    args = Namespace(
        deepstream_root=str(tmp_path / "deepstream"),
        work_dir=str(work_dir),
        image="test-image",
        bbox_dir_name="/dev/shm/test-kitti",
        synced_preview_dir_name="/dev/shm/test-preview",
        synced_preview_output="/dev/shm/test-preview.jpg",
        rtsp_latency_ms=50,
        streammux_width=640,
        streammux_height=480,
        runtime_infer_config="runtime-infer.txt",
        container_name="test-container",
        source="wifi",
    )

    command = docker_command(args, work_dir / "runtime.txt")
    shell = command[-1]

    assert "( while true; do gst-launch-1.0" in shell
    assert "boxed preview pipeline exited with code $preview_status; restarting" in shell
    assert "sleep 1; done ) & preview_pid=$!" in shell


def test_dashboard_reopens_mjpeg_connections_that_stop_without_an_error():
    source = (PROJECT_ROOT / "scripts" / "webcam_stream_server.py").read_text(encoding="utf-8")

    assert "const MJPEG_PREVIEW_RECONNECT_MS = 8000" in source
    assert "now - lastSet >= MJPEG_PREVIEW_RECONNECT_MS" in source
    assert "broken || connectionExpired || img.dataset.mjpegStarted" in source


def test_dashboard_gives_each_mjpeg_preview_a_unique_connection_url():
    source = (PROJECT_ROOT / "scripts" / "webcam_stream_server.py").read_text(encoding="utf-8")

    assert "let mjpegPreviewConnectionSequence = 0" in source
    assert "stream_client: clientId" in source
    assert "stream_seq: String(mjpegPreviewConnectionSequence)" in source
    assert "img.src = mjpegUrl(path, clientId)" in source


def test_wifi_lane_previews_poll_finite_snapshots_instead_of_mjpeg():
    source = (PROJECT_ROOT / "scripts" / "webcam_stream_server.py").read_text(encoding="utf-8")

    assert 'id="wifi-picker-video-preview" src="/wifi-snapshot.jpg"' in source
    assert 'id="wifi-detail-video-preview" src="/wifi-snapshot.jpg"' in source
    assert "setInterval(refreshLaneSnapshotPreviews, 400)" in source
    assert "img.getClientRects().length === 0" in source
    assert "now - requestStarted < 2000" in source


def test_wifi_boxed_preview_uses_continuous_overlay_stream_without_panel_replacement():
    source = (PROJECT_ROOT / "scripts" / "webcam_stream_server.py").read_text(encoding="utf-8")

    assert "wifi: '/deepstream-overlay.mjpg?source=wifi'" in source
    assert "function isMjpegPreviewPath(path)" in source
    assert "liveImage?.dataset?.mjpegPath || streamPath" in source
    assert "const wantsContinuousOverlay = isMjpegPreviewPath(configuredPreviewPath)" in source
    assert "const wantsLiveStream = wantsContinuousOverlay" in source


def test_focus_axis_models_are_normalized_independently():
    settings = normalize_deepstream_settings({
        "focus_horizontal_model": "GRU",
        "focus_vertical_model": "unsupported",
    })

    assert settings["focus_horizontal_model"] == "gru"
    assert settings["focus_vertical_model"] == "linear"

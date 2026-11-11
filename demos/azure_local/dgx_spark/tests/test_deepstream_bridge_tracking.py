import json
import os
from pathlib import Path
import sys
import time
import threading

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import deepstream_yolo_coco_bridge as bridge
from scripts import focus_object_controller as controller


def write_frame(directory: Path, frame_id: int, timestamp: float) -> Path:
    path = directory / f"00_000_{frame_id:07d}.txt"
    path.write_text("")
    os.utime(path, (timestamp, timestamp))
    return path


def test_sequential_tracker_advances_without_directory_rescan(tmp_path, monkeypatch):
    first = write_frame(tmp_path, 10, 10.0)
    tracker = bridge.SequentialKittiTracker(tmp_path, resync_seconds=1.0)

    assert tracker.newest(now=0.0) == first
    second = write_frame(tmp_path, 11, 11.0)
    third = write_frame(tmp_path, 12, 12.0)
    monkeypatch.setattr(bridge, "latest_kitti_file", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("sequential advance must not rescan")
    ))

    assert tracker.newest(now=0.05) == third
    assert tracker.frame_id == 12
    assert second.is_file()


def test_sequential_tracker_periodically_resynchronizes_across_gap(tmp_path):
    first = write_frame(tmp_path, 20, 20.0)
    tracker = bridge.SequentialKittiTracker(tmp_path, resync_seconds=1.0)
    assert tracker.newest(now=0.0) == first

    gap_target = write_frame(tmp_path, 22, 22.0)

    assert tracker.newest(now=0.5) == first
    assert tracker.newest(now=1.1) == gap_target
    assert tracker.frame_id == 22


def test_detection_tracker_keeps_instance_ids_across_motion_and_short_dropout():
    tracker = bridge.DetectionTracker(1000, 1000, max_missed_frames=60)
    first = tracker.update([
        {"label": "keyboard", "bbox": [100, 400, 200, 100]},
        {"label": "keyboard", "bbox": [700, 400, 200, 100]},
    ])
    left_id, right_id = first[0]["track_id"], first[1]["track_id"]

    second = tracker.update([
        {"label": "keyboard", "bbox": [680, 400, 200, 100]},
        {"label": "keyboard", "bbox": [120, 400, 200, 100]},
    ])
    for _ in range(30):
        tracker.update([])
    returned = tracker.update([{"label": "keyboard", "bbox": [140, 400, 200, 100]}])

    assert second[0]["track_id"] == right_id
    assert second[1]["track_id"] == left_id
    assert returned[0]["track_id"] == left_id
    assert returned[0]["track_age_frames"] == 3


def test_directory_event_wake_observes_new_kitti_file(tmp_path):
    wake = bridge.DirectoryEventWake(tmp_path)
    stop = threading.Event()
    writer = threading.Thread(target=lambda: (time.sleep(0.03), write_frame(tmp_path, 1, time.time())))
    writer.start()
    started = time.monotonic()
    try:
        wake.wait(0.5, stop)
    finally:
        writer.join(timeout=1)
        wake.close()
    assert time.monotonic() - started < 0.4


def test_detection_publication_includes_source_frame_identity(tmp_path):
    output = tmp_path / "detections.json"
    bridge.publish_detection_json(
        output,
        "wifi",
        "active",
        "detected cat",
        frame_id=1234,
        objects=[{"label": "cat", "confidence": 0.9}],
        objects_updated_at=100.0,
        objects_updated_monotonic_ns=987654321,
        processor={"deepstream_status": "running"},
    )

    payload = json.loads(output.read_text())
    assert payload["source_frame_id"] == 1234
    assert payload["object_frame_id"] == 1234
    assert payload["sources"]["wifi"]["source_frame_id"] == 1234
    assert payload["objects_updated_monotonic_ns"] == 987654321
    assert payload["sources"]["wifi"]["objects_updated_monotonic_ns"] == 987654321
    assert payload["published_monotonic_ns"] > 0
    assert controller.detection_marker(payload["sources"]["wifi"]) == ("frame", 1234)


def test_detection_publication_preserves_other_lane_state(tmp_path):
    output = tmp_path / "detections.json"
    bridge.publish_detection_json(output, "wifi", "active", "wifi frame", frame_id=10)
    bridge.publish_detection_json(output, "server", "active", "server frame", frame_id=20)

    payload = json.loads(output.read_text())
    assert set(payload["sources"]) == {"server", "wifi"}
    assert payload["sources"]["wifi"]["source_frame_id"] == 10
    assert payload["sources"]["server"]["source_frame_id"] == 20


def test_preview_motion_publisher_coalesces_same_timeline_artifact(tmp_path):
    jpeg_path = tmp_path / "input.jpg"
    Image.new("RGB", (320, 180), (20, 40, 60)).save(jpeg_path, format="JPEG")
    output = tmp_path / "motion.pgm"
    publisher = bridge.PreviewMotionPublisher(output, width=80, height=45, max_hz=0)
    try:
        publisher.submit(
            jpeg_path.read_bytes(),
            {
                "preview_frame_id": 42,
                "preview_promoted_monotonic_ns": 123456,
            },
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not output.exists():
            time.sleep(0.01)
        metadata = json.loads(output.with_suffix(".pgm.json").read_text())
    finally:
        publisher.stop()

    assert output.read_bytes().startswith(b"P5\n80 45\n255\n")
    assert metadata["preview_frame_id"] == 42
    assert metadata["preview_promoted_monotonic_ns"] == 123456
    assert metadata["motion_frame_width"] == 80
    assert metadata["motion_frame_height"] == 45


def test_preview_frame_id_uses_multifilesink_sequence():
    assert bridge.preview_frame_id(Path("frame-0000001234.jpg")) == 1234
    assert bridge.preview_frame_id(Path("preview.jpg")) == -1

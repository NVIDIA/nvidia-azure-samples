import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import webcam_stream_server as server


def test_stream_start_audio_cleanup_preserves_voice_session_files(tmp_path):
    audio_dir = tmp_path / "browser-audio"
    metadata_dir = audio_dir / "metadata"
    metadata_dir.mkdir(parents=True)
    (audio_dir / "browser_audio_1.webm").write_bytes(b"queued audio")
    (metadata_dir / "browser_audio_1.json").write_text("{}", encoding="utf-8")

    protected_paths = {
        "voice_session_reset_path": tmp_path / "voice-session-reset.json",
        "voice_response_path": tmp_path / "voice-response.json",
        "voicechat_response_path": tmp_path / "voicechat-response.json",
        "speech_playback_lock_path": tmp_path / "speech-playback-lock.json",
        "transcript_path": tmp_path / "transcript.json",
    }
    sentinel = {"conversation_by_source": {"wifi": [{"role": "assistant", "text": "kept"}]}}
    for path in protected_paths.values():
        path.write_text(json.dumps(sentinel), encoding="utf-8")

    args = argparse.Namespace(
        browser_audio_dir=str(audio_dir),
        audio_buffer_control_path=str(tmp_path / "audio-buffer-control.json"),
        **{name: str(path) for name, path in protected_paths.items()},
    )

    result = server.clear_browser_audio_buffers(
        args,
        "stream_server_start",
        preserve_session=True,
    )

    assert result["deleted_browser_audio_files"] == 1
    assert result["reason"] == "stream_server_start"
    assert not (audio_dir / "browser_audio_1.webm").exists()
    assert not (metadata_dir / "browser_audio_1.json").exists()
    assert json.loads((tmp_path / "audio-buffer-control.json").read_text())["reason"] == "stream_server_start"
    for path in protected_paths.values():
        assert json.loads(path.read_text(encoding="utf-8")) == sentinel

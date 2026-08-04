import argparse
import time

from scripts import nemotron_voicechat_pipeline as pipeline


def playback_args(tmp_path):
    return argparse.Namespace(
        speech_playback_lock_json=str(tmp_path / "playback-lock.json"),
        playback_lock_stale_seconds=90.0,
        post_playback_listen_cooldown_seconds=2.5,
        listening_beep_capture_suppression_seconds=2.0,
        stage_chime_capture_suppression_seconds=1.25,
    )


def test_active_playback_only_pauses_the_speaking_lane(tmp_path):
    args = playback_args(tmp_path)
    pipeline.write_playback_lock(args, True, "server", "playback", "audio-1", "Lane 1 is speaking.")

    assert pipeline.playback_lock_active(args, "server") == (True, "Lane 1 is speaking.")
    assert pipeline.playback_lock_active(args, "wifi") == (False, "")


def test_playback_cooldown_only_pauses_the_speaking_lane(tmp_path):
    args = playback_args(tmp_path)
    pipeline.write_playback_lock(args, False, "server", "complete", "audio-1", "Playback complete.")

    assert pipeline.playback_lock_active(args, "server") == (True, "audio output cooldown")
    assert pipeline.playback_lock_active(args, "wifi") == (False, "")


def test_other_lane_chunk_is_not_discarded_during_playback(tmp_path):
    args = playback_args(tmp_path)
    started_at = time.time()
    pipeline.write_playback_lock(args, True, "server", "playback", "audio-1", "Lane 1 is speaking.")

    assert pipeline.captured_chunk_overlaps_output_suppression(
        args, "wifi", started_at, time.time()
    ) == (False, "")
    assert pipeline.captured_chunk_overlaps_output_suppression(
        args, "server", started_at, time.time()
    ) == (True, "Lane 1 is speaking.")


def test_unscoped_legacy_lock_still_pauses_all_lanes(tmp_path):
    args = playback_args(tmp_path)
    pipeline.write_json(
        args.speech_playback_lock_json,
        {
            "active": True,
            "source": "",
            "phase": "playback",
            "message": "Legacy output lock.",
            "expires_at": time.time() + 30,
        },
    )

    assert pipeline.playback_lock_active(args, "server") == (True, "Legacy output lock.")
    assert pipeline.playback_lock_active(args, "wifi") == (True, "Legacy output lock.")


def test_worker_start_clears_only_its_own_stale_lock(tmp_path):
    args = playback_args(tmp_path)
    pipeline.write_playback_lock(args, True, "server", "playback", "server-audio", "Server speaking")
    pipeline.write_playback_lock(args, True, "wifi", "playback", "wifi-audio", "Wi-Fi speaking")

    pipeline.reset_source_playback_lock_on_start(args, "wifi")

    assert pipeline.playback_lock_active(args, "wifi") == (False, "")
    assert pipeline.playback_lock_active(args, "server") == (True, "Server speaking")

import time
from io import BytesIO
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import wave

from scripts import nemotron_voicechat_pipeline as pipeline
from scripts import nemotron_voice_responder as responder
from scripts import local_audio_asr_service as local_asr


def pcm_wav_bytes(samples: bytes, sample_rate: int = 16000) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples)
    return output.getvalue()


def test_current_snapshot_tool_persists_frozen_dialog_image(monkeypatch, tmp_path):
    jpeg = b"\xff\xd8" + b"snapshot" + b"\xff\xd9"

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return jpeg

    monkeypatch.setattr(responder, "urlopen", lambda *_args, **_kwargs: Response())
    args = SimpleNamespace(
        audio_dir=str(tmp_path),
        wifi_snapshot_url="http://camera/wifi-snapshot.jpg",
        snapshot_url="http://camera/snapshot.jpg",
        tool_snapshot_timeout=2.0,
        tool_snapshot_max_bytes=1000,
    )

    result = responder.current_snapshot_tool(args, "wifi", "inspect build")

    assert result["image_url"].startswith("/voicechat-tool-snapshot.jpg?id=wifi_snapshot_")
    assert result["image_file"].endswith(".jpg")
    assert (tmp_path / "tool-snapshots" / result["image_file"]).read_bytes() == jpeg


def test_camera_clip_tool_clamps_duration_and_persists_mp4(monkeypatch, tmp_path):
    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"mp4-video")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(responder.subprocess, "run", fake_run)
    args = SimpleNamespace(
        audio_dir=str(tmp_path),
        wifi_snapshot_url="http://127.0.0.1:8090/wifi-snapshot.jpg",
        snapshot_url="http://127.0.0.1:8090/snapshot.jpg",
        camera_clip_min_seconds=1.0,
        camera_clip_max_seconds=10.0,
        camera_clip_default_seconds=3.0,
        camera_clip_max_bytes=1000,
    )

    result = responder.camera_clip_tool(args, "wifi", 60, "inspect motion")

    assert result["duration_seconds"] == 10.0
    assert result["video_url"].startswith("/voicechat-tool-clip.mp4?id=wifi_clip_")
    assert (tmp_path / "tool-clips" / result["video_file"]).read_bytes() == b"mp4-video"


def test_camera_clip_uses_live_stream_rate_and_optional_audio(monkeypatch, tmp_path):
    captured = {}

    def fake_run(command, **_kwargs):
        captured["command"] = command
        Path(command[-1]).write_bytes(b"mp4-video-with-audio")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(responder.subprocess, "run", fake_run)
    args = SimpleNamespace(
        audio_dir=str(tmp_path),
        wifi_snapshot_url="http://127.0.0.1:8090/wifi-snapshot.jpg",
        snapshot_url="http://127.0.0.1:8090/snapshot.jpg",
        camera_clip_min_seconds=1.0,
        camera_clip_max_seconds=10.0,
        camera_clip_default_seconds=3.0,
        camera_clip_max_bytes=1000,
    )

    result = responder.camera_clip_tool(args, "wifi", 5, "inspect build", True)

    command = captured["command"]
    assert command[command.index("-r") + 1] == "5"
    assert "http://127.0.0.1:8090/wifi-audio.wav" in command
    assert result["duration_seconds"] == 5.0
    assert result["audio_requested"] is True
    assert result["audio_included"] is True


def test_camera_clip_becomes_video_url_model_evidence(tmp_path):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(b"short-mp4")

    items, infos = pipeline.tool_result_video_content([{
        "name": "camera_clip",
        "result": {
            "source": "wifi",
            "duration_seconds": 3.0,
            "video_path": str(clip_path),
            "video_url": "/voicechat-tool-clip.mp4?id=wifi_clip_1.mp4",
        },
    }])

    assert items[0]["type"] == "video_url"
    assert items[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
    assert infos[0]["duration_seconds"] == 3.0


def test_local_asr_prefers_canary_when_nonempty():
    selected = local_asr.select_transcript("Canary words", "Parakeet words", "canary", "parakeet")

    assert selected == {
        "text": "Canary words",
        "selected_model": "canary",
        "selection_reason": "primary_nonempty",
        "used_fast_fallback": False,
    }


def test_local_asr_uses_unchanged_fast_hypothesis_only_when_canary_is_empty():
    selected = local_asr.select_transcript("", "May two tell me the exact local time now.", "canary", "parakeet")

    assert selected == {
        "text": "May two tell me the exact local time now.",
        "selected_model": "parakeet",
        "selection_reason": "primary_empty_fast_nonempty",
        "used_fast_fallback": True,
    }


def test_local_asr_exposes_normalized_hypothesis_score_without_selecting_by_it():
    hypothesis = SimpleNamespace(
        text="Copper bridges.",
        score=-12.0,
        y_sequence=[1, 2, 3, 4],
        word_confidence=[0.8, 0.9],
    )

    result = local_asr.transcript_hypothesis(([hypothesis], None))

    assert result["text"] == "Copper bridges."
    assert result["score"] == -12.0
    assert result["token_count"] == 4
    assert result["score_per_token"] == -3.0
    assert round(result["mean_word_confidence"], 6) == 0.85


def test_model_optional_bool_accepts_json_and_common_text_values():
    assert pipeline.model_optional_bool(True) is True
    assert pipeline.model_optional_bool("false") is False
    assert pipeline.model_optional_bool(1) is True
    assert pipeline.model_optional_bool("unknown") is None


def test_configured_system_prompt_defaults_to_the_worker_lane_file(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "PROJECT_ROOT", tmp_path)
    (tmp_path / "webcam-nemotron-server-system-prompt.json").write_text(
        json.dumps({"system_prompt": "Server-only policy\n\n  Keep this indentation."}), encoding="utf-8"
    )
    (tmp_path / "webcam-nemotron-wifi-system-prompt.json").write_text(
        json.dumps({"system_prompt": "Wi-Fi-only policy"}), encoding="utf-8"
    )

    assert pipeline.configured_nemotron_system_prompt(
        SimpleNamespace(source_mode="server", nemotron_system_prompt_json="")
    ) == "Server-only policy\n\n  Keep this indentation."
    assert pipeline.configured_nemotron_system_prompt(
        SimpleNamespace(source_mode="wifi", nemotron_system_prompt_json="")
    ) == "Wi-Fi-only policy"


def test_complete_no_tool_model_response_reuses_initial_output_without_content_matcher():
    assert pipeline.reusable_initial_model_response(
        "Noted: Opal surveyors compare forty two bronze calipers beneath the eastern balcony.",
        {"needs_tools": False, "calls": []},
        [],
        request_native_audio=False,
    ) is True


def test_initial_response_reuse_still_rejects_structural_starters_and_tool_turns():
    assert pipeline.reusable_initial_model_response(
        "Sure, here is the answer.",
        {"needs_tools": False, "calls": []},
        [],
        request_native_audio=False,
    ) is False


def test_trusted_time_tool_direct_response_requires_one_ai_selected_local_result_and_default_policy():
    answer, tool = pipeline.trusted_tool_direct_response(
        [{"name": "current_time", "result": {"direct_answer": "The local date and time is 2026-06-30 06:45:00 EDT."}}],
        pipeline.DEFAULT_NEMOTRON_SYSTEM_PROMPT,
    )

    assert answer == "The local date and time is 2026-06-30 06:45:00 EDT."
    assert tool == "current_time"
    assert pipeline.trusted_tool_direct_response(
        [{"name": "current_time", "result": {"direct_answer": answer}}],
        "Answer every request as a limerick.",
    ) == ("", "")
    assert pipeline.trusted_tool_direct_response(
        [
            {"name": "current_time", "result": {"direct_answer": answer}},
            {"name": "runtime_stats", "result": {}},
        ],
        pipeline.DEFAULT_NEMOTRON_SYSTEM_PROMPT,
    ) == ("", "")
    assert pipeline.reusable_initial_model_response(
        "The current time is seven thirty.",
        {"needs_tools": True, "calls": [{"name": "current_time", "args": {}}]},
        [{"name": "current_time", "result": {"local_time": "07:30"}}],
        request_native_audio=False,
    ) is False


def test_camera_playback_retains_native_helper_timing_payload(tmp_path, monkeypatch):
    audio = tmp_path / "response.wav"
    audio.write_bytes(b"RIFF-test")
    response_payload = {
        "status": "ok",
        "backend": "x64_netsdk_qemu_persistent",
        "helper_handoff_completed_at": 123.5,
        "helper_sent_event_at": 127.0,
        "helper_handoff_seconds": 0.001,
        "helper_send_seconds": 3.5,
        "first_packet_timing_available": False,
    }

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return json.dumps(response_payload).encode("utf-8")

    monkeypatch.setattr(pipeline, "urlopen", lambda *_args, **_kwargs: Response())
    played, error, telemetry = pipeline.play_audio_on_wifi_camera_with_telemetry(
        audio,
        "http://127.0.0.1:8090/wifi-talk-audio",
        5.0,
    )

    assert played is True
    assert error == ""
    assert telemetry["helper_handoff_completed_at"] == 123.5
    assert telemetry["helper_send_seconds"] == 3.5
    assert telemetry["worker_request_seconds"] >= 0


def test_camera_transport_is_a_live_process_flow_component(monkeypatch):
    monkeypatch.setattr(pipeline, "ACTIVE_SOURCE_MODE", "wifi")
    stages = pipeline.voicechat_stages(
        "complete",
        "complete",
        "complete",
        "complete",
        "complete",
        "complete",
        output_target="wifi_camera",
        payloads={
            "playback": {
                "transport": {
                    "backend": "x64_netsdk_qemu_persistent",
                    "volume_percent": 70,
                    "scaling_seconds": 0.02,
                    "transcode_seconds": 0.03,
                    "helper_handoff_seconds": 0.001,
                    "helper_send_seconds": 3.4,
                }
            }
        },
    )
    by_id = {stage["id"]: stage for stage in stages}

    assert by_id["camera_output_conditioning"]["payload"]["volume_percent"] == 70
    transport = by_id["camera_native_speaker_transport"]["payload"]
    assert transport["helper_handoff_seconds"] == 0.001
    assert transport["physical_wave_audio_only"] is True
    assert transport["cross_lane_backend_content"] is False
    playback = by_id["playback"]["payload"]
    assert playback["lead_silence_seconds"] == 0.50
    assert playback["physical_onset_qualified"] is True
    assert playback["physical_onset_preserved"] is True
    assert playback["onset_qualification_trials"] == 5
    assert playback["rejected_shorter_lead_seconds"] == 0.45
    assert playback["physical_first_packet_measured"] is False
    assert playback["fast_asr_exact_trials"] == 5
    assert playback["authoritative_asr_phonetic_variation"] is True


def test_session_flush_reopens_microphone_without_startup_mute_window():
    args = SimpleNamespace(startup_audio_drop_seconds=3.0, chunk_seconds=0.125)

    assert pipeline.session_reset_audio_drop_seconds(args, "session_flush") == 0.0
    assert pipeline.session_reset_audio_drop_seconds(args, "voice_response_clear") == 0.0
    assert pipeline.session_reset_audio_drop_seconds(args, "other_reset") == 0.25


def test_entry_audio_concatenates_compatible_wav_chunks_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "combine_wavs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compatible PCM must use fast path")),
    )
    first = b"\x01\x00" * 160
    second = b"\x02\x00" * 240
    with tempfile.TemporaryDirectory() as directory:
        output = pipeline.write_entry_audio(
            {"chunks": [pcm_wav_bytes(first), pcm_wav_bytes(second)]},
            Path(directory),
            "wifi",
            "test",
        )
        with wave.open(str(output), "rb") as wav_file:
            assert wav_file.getframerate() == 16000
            assert wav_file.getnframes() == 400
            assert wav_file.readframes(400) == first + second


def test_speech_gate_audio_can_exclude_preroll_without_removing_final_asr_audio():
    silence = b"\x00\x00" * 160
    onset = b"\x02\x00" * 240
    entry = {"chunks": [pcm_wav_bytes(silence), pcm_wav_bytes(onset)], "preroll_chunks": 1}
    with tempfile.TemporaryDirectory() as directory:
        output = pipeline.write_entry_audio(
            entry,
            Path(directory),
            "wifi",
            "speech_gate_candidate",
            skip_chunks=entry["preroll_chunks"],
        )
        with wave.open(str(output), "rb") as wav_file:
            assert wav_file.getnframes() == 240
            assert wav_file.readframes(240) == onset
    assert len(entry["chunks"]) == 2


def test_sample_level_endpoint_clock_counts_silence_inside_last_chunk(tmp_path):
    tone = int(4000).to_bytes(2, "little", signed=True) * int(16000 * 0.2)
    silence = b"\x00\x00" * int(16000 * 0.3)
    wav_path = tmp_path / "trailing-silence.wav"
    wav_path.write_bytes(pcm_wav_bytes(tone + silence))

    trailing = pipeline.trailing_silence_seconds_for_wav(
        wav_path,
        rms_threshold=0.004,
        peak_threshold=0.018,
    )

    assert 0.29 <= trailing <= 0.31


def test_boundary_buffer_exposes_live_capture_quantum():
    class Args:
        chunk_seconds = 0.125
        utterance_gap_seconds = 0.65
        utterance_final_silence_seconds = 0.65
        utterance_max_seconds = 18.0
        voicechat_audio_max_tokens = 112

    payload = pipeline.utterance_nemotron_buffer(Args(), {"duration": 1.5, "speech_detected": True})

    assert payload["capture_chunk_seconds"] == 0.125
    assert payload["boundary_check_quantum_seconds"] == 0.125
    assert payload["final_pause_seconds"] == 0.65


def test_configured_tts_model_label_matches_the_active_backend():
    class PiperArgs:
        tts_backend = "piper"
        piper_model_path = "/models/en_US-lessac-medium.onnx"
        magpie_direct_tts = True
        magpie_max_decoder_steps = 420
        magpie_maskgit_steps = 1
        magpie_speaker_index = 1

    assert pipeline.configured_tts_model_label(PiperArgs()) == "en_US-lessac-medium"


def test_piper_voice_pool_is_physically_qualified_and_rotates_by_storyline(tmp_path, monkeypatch):
    models = [tmp_path / name for name in ("en_US-lessac-medium.onnx", "en_US-hfc_male-medium.onnx", "en_US-hfc_female-medium.onnx")]
    for model in models:
        model.touch()
    reset = tmp_path / "reset.json"
    pipeline.write_json(reset, {"session_id": "story-one"})

    class Args:
        tts_backend = "piper"
        piper_model_path = str(models[0])
        piper_voice_pool = [str(models[1]), str(models[2])]
        voicechat_session_reset_json = str(reset)
        source_mode = "wifi"

    pipeline._PIPER_ASSIGNMENTS.clear()
    pipeline._PIPER_LAST_BY_SOURCE.clear()
    first, first_story = pipeline.select_piper_voice(Args(), "wifi")
    again, again_story = pipeline.select_piper_voice(Args(), "wifi")
    pipeline.write_json(reset, {"session_id": "story-two"})
    second, second_story = pipeline.select_piper_voice(Args(), "wifi")

    assert pipeline.piper_voice_pool(Args()) == [str(model) for model in models]
    assert first == again
    assert first_story == again_story == "story-one"
    assert second_story == "story-two"
    assert second != first
    payload = pipeline.native_audio_payload(Args())
    assert payload["onnx_intra_op_threads"] == 8
    assert payload["onnx_inter_op_threads"] == 1
    assert payload["runtime_policy"] == "piper_cpu_threads_v1"
    assert payload["audio_level_changed"] is False


def test_server_lane_stages_include_live_microphone_adapter(tmp_path, monkeypatch):
    state_path = tmp_path / "server-microphone-adapter.json"
    pipeline.write_json(state_path, {
        "status": "active",
        "output_source": "dgx_nexigo_70",
        "hardware_capture_percent": 70,
        "software_capture_percent": 70,
        "physical_wave_audio_only": True,
    })
    monkeypatch.setattr(pipeline, "ACTIVE_SOURCE_MODE", "server")
    monkeypatch.setattr(pipeline, "ACTIVE_SERVER_MICROPHONE_ADAPTER_STATE_PATH", str(state_path))
    monkeypatch.setattr(pipeline, "SERVER_CAPTURE_STATE", {
        "status": "active",
        "backend": "persistent_ffmpeg_pcm",
        "continuous_capture": True,
        "dropped_gap_seconds": 0.0,
    })

    stages = pipeline.voicechat_stages("active", "waiting", "waiting", "waiting", "waiting", "listening")
    adapter = next(stage for stage in stages if stage["id"] == "server_microphone_adapter")
    continuous = next(stage for stage in stages if stage["id"] == "server_continuous_capture")
    drain = next(stage for stage in stages if stage["id"] == "server_playback_backlog_drain")

    assert adapter["status"] == "active"
    assert adapter["payload"]["output_source"] == "dgx_nexigo_70"
    assert adapter["payload"]["physical_wave_audio_only"] is True
    assert continuous["status"] == "active"
    assert continuous["payload"]["continuous_capture"] is True
    assert continuous["payload"]["dropped_gap_seconds"] == 0.0
    assert drain["payload"]["content_agnostic"] is True
    assert drain["payload"]["deterministic_phrase_matching"] is False


def test_persistent_server_reader_writes_gap_free_analysis_chunk(tmp_path, monkeypatch):
    args = SimpleNamespace(chunk_seconds=0.25)
    reader = pipeline.ServerAudioReader(args, "pulse", "dgx_nexigo_70")
    reader.process = SimpleNamespace(pid=1234)
    expected_bytes = int(16000 * 2 * args.chunk_seconds)
    monkeypatch.setattr(reader, "_read_exact", lambda needed, _timeout: b"\x01\x00" * (needed // 2))
    pipeline.SERVER_CAPTURE_STATE.clear()

    output = tmp_path / "chunk.wav"
    reader.capture(output)

    with wave.open(str(output), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getnframes() == 4000
    assert output.stat().st_size == expected_bytes + 44
    assert pipeline.SERVER_CAPTURE_STATE["continuous_capture"] is True
    assert pipeline.SERVER_CAPTURE_STATE["dropped_gap_seconds"] == 0.0
    assert pipeline.SERVER_CAPTURE_STATE["backend"] == "persistent_ffmpeg_pcm"


def test_server_reader_drains_buffered_self_playback_before_resuming(tmp_path, monkeypatch):
    lock_path = tmp_path / "playback-lock.json"
    started_at = time.time() - 1
    pipeline.write_json(lock_path, {
        "sources": {
            "server": {
                "active": False,
                "phase": "complete",
                "audio_id": "voice-1",
                "message": "Audible speech playback complete.",
                "updated_at": time.time(),
            }
        }
    })
    drained = []
    reader = SimpleNamespace(drain=lambda seconds: drained.append(seconds))
    args = SimpleNamespace(
        speech_playback_lock_json=str(lock_path),
        post_playback_listen_cooldown_seconds=0.35,
    )
    monkeypatch.setattr(pipeline, "SERVER_CAPTURE_STATE", {})

    assert pipeline.drain_server_reader_after_output(args, "server", reader, started_at) is True
    assert drained == [0.5]
    assert pipeline.SERVER_CAPTURE_STATE["post_playback_backlog_drained"] is True


def test_server_reader_drains_manual_playback_without_matching_message_text(tmp_path, monkeypatch):
    lock_path = tmp_path / "playback-lock.json"
    started_at = time.time() - 1
    pipeline.write_json(lock_path, {
        "sources": {
            "server": {
                "active": False,
                "phase": "complete",
                "audio_id": "manual_voicechat_1",
                "message": "Manual input response complete.",
                "updated_at": time.time(),
            }
        }
    })
    drained = []
    reader = SimpleNamespace(drain=lambda seconds: drained.append(seconds))
    args = SimpleNamespace(
        speech_playback_lock_json=str(lock_path),
        post_playback_listen_cooldown_seconds=0.35,
    )
    monkeypatch.setattr(pipeline, "SERVER_CAPTURE_STATE", {})

    assert pipeline.drain_server_reader_after_output(args, "server", reader, started_at) is True
    assert drained == [0.5]
    assert pipeline.SERVER_CAPTURE_STATE["post_playback_phase"] == "complete"
    assert pipeline.SERVER_CAPTURE_STATE["post_playback_audio_id"] == "manual_voicechat_1"


def test_wifi_shared_reader_advances_server_pcm_cursor_without_analysis_gaps(tmp_path, monkeypatch):
    requests = []
    payload = pcm_wav_bytes(b"\x01\x00" * 4000)

    class Response:
        def __init__(self, start, end):
            self.headers = {
                "X-Audio-Start-Byte": str(start),
                "X-Audio-End-Byte": str(end),
                "X-Audio-Skipped-Bytes": "0",
            }

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return payload

    def fake_urlopen(request, timeout):
        requests.append((request.full_url, timeout))
        return Response(1000 if len(requests) == 1 else 9000, 9000 if len(requests) == 1 else 17000)

    monkeypatch.setattr(pipeline, "urlopen", fake_urlopen)
    monkeypatch.setattr(pipeline, "WIFI_CAPTURE_STATE", {})
    reader = pipeline.WifiSharedAudioReader(
        SimpleNamespace(wifi_audio_url="http://127.0.0.1:8080/wifi-audio.wav", chunk_seconds=0.25)
    )

    reader.capture(tmp_path / "first.wav")
    reader.capture(tmp_path / "second.wav")

    assert "cursor=latest" in requests[0][0]
    assert "cursor=9000" in requests[1][0]
    assert pipeline.WIFI_CAPTURE_STATE["cursor_byte"] == 17000
    assert pipeline.WIFI_CAPTURE_STATE["dropped_gap_seconds"] == 0.0
    assert pipeline.WIFI_CAPTURE_STATE["continuous_capture"] is True


def test_wifi_lane_stages_show_sequential_pcm_cursor(monkeypatch):
    monkeypatch.setattr(pipeline, "ACTIVE_SOURCE_MODE", "wifi")
    monkeypatch.setattr(pipeline, "WIFI_CAPTURE_STATE", {
        "status": "active",
        "backend": "shared_pcm_ring_cursor",
        "cursor_byte": 17000,
        "dropped_gap_seconds": 0.0,
    })

    stages = pipeline.voicechat_stages("active", "waiting", "waiting", "waiting", "waiting", "listening")
    cursor = next(stage for stage in stages if stage["id"] == "wifi_sequential_capture")

    assert cursor["status"] == "active"
    assert cursor["payload"]["backend"] == "shared_pcm_ring_cursor"
    assert cursor["payload"]["cross_lane_backend_content"] is False


def test_voicechat_prompt_requests_an_explicit_tool_routing_decision():
    class Args:
        enable_environment_context = False

    prompt = pipeline.build_voicechat_prompt(Args(), "server", {})

    assert "needs_tools is a JSON boolean" in prompt
    assert "keys heard, visual_state, response, and needs_tools" in prompt
    assert "final, complete, immediately speakable answer" in prompt


def test_compact_voice_decision_contract_keeps_model_only_tool_fields_and_bounds_speech():
    prompt = pipeline.plain_voice_decision_prompt("Tell me a story.", "wifi")

    assert '"needs_tools":true|false' in prompt
    assert '"calls"' in prompt
    assert '"response"' in prompt
    assert "at most twenty words" in prompt
    assert "mention one salient detail" in prompt
    assert "unanswerable merely because it is not a question" in prompt
    assert "dialog_action" not in prompt


def test_sparse_v9_decision_contract_is_one_model_selected_action():
    prompt = pipeline.sparse_voice_decision_prompt_v9(
        "First count the red folders, then count the blue folders.",
        "wifi",
    )

    assert '{"tool":"tool_name","args":{}}' in prompt
    assert '{"say":"complete spoken reply of at most twenty words"}' in prompt
    assert "Object words" in prompt
    assert "do not imply camera use" in prompt
    assert "declarative report about an earlier event" in prompt
    assert "User said: First count the red folders" in prompt
    assert '"needs_tools"' not in prompt
    assert '"calls"' not in prompt


def test_sparse_v20_prioritizes_system_wording_without_weakening_tool_boundaries():
    prompt = pipeline.sparse_voice_decision_prompt_v20(
        "First count the red folders, then count the blue folders.",
        "wifi",
        "Repeat every no-tool utterance exactly.",
    )

    assert "System response policy: Repeat every no-tool utterance exactly." in prompt
    assert "never changes tool requirements" in prompt
    assert "copy the complete User said text verbatim" in prompt
    assert '"tool":"query_environment"' in prompt
    assert '"tool":"current_snapshot"' in prompt
    assert "Noted:" not in prompt


def test_sparse_v31_contract_requires_web_search_query_and_timestamp_date():
    prompt = pipeline.sparse_voice_decision_prompt_v31(
        "Current timestamp: 2026-06-30T13:44:22-04:00",
        "server",
        "Search the web for current news.",
    )

    assert 'web_search requires {"query":"specific non-empty search phrase"}' in prompt
    assert "never call web_search with {}" in prompt
    assert '{"query":"recent news YYYY-MM-DD"}' in prompt
    assert "complete supplied calendar date exactly" in prompt
    assert "year-only or partial date is invalid" in prompt


def test_tool_answer_attaches_vision_only_for_visual_requests_or_image_evidence():
    assert pipeline.tool_answer_needs_visual_attachment(
        "What is the exact local time now?",
        [{"name": "current_time", "result": {"local_time": "00:37 EDT"}}],
    ) is False
    assert pipeline.tool_answer_needs_visual_attachment(
        "What is visible on the desk?",
        [{"name": "current_snapshot", "result": {}}],
    ) is True
    assert pipeline.tool_answer_needs_visual_attachment(
        "Describe the result.",
        [{"name": "environment_scan", "result": {"image_data_url": "data:image/jpeg;base64,AA=="}}],
    ) is True


def test_voicechat_prompt_does_not_bias_transcription_with_dialog_history():
    class Args:
        enable_environment_context = False
        voicechat_snapshot = False

    prompt = pipeline.build_voicechat_prompt(
        Args(),
        "server",
        {},
        [
            {"role": "user", "source": "server", "text": "What do you see?"},
            {"role": "assistant", "source": "nemotron-voicechat", "text": "I will ask lane two."},
        ],
    )

    assert "Recent dialog in this lane:" not in prompt
    assert "What do you see?" not in prompt
    assert "keys heard, response, and needs_tools" in prompt
    assert "do not emit visual_state" in prompt
    assert "at most 8 words" in prompt
    assert "complete 6-to-12-word sentence" in prompt


def test_text_decision_prompt_does_not_copy_prior_lane_replies():
    prompt = pipeline.plain_voice_decision_prompt(
        "A new phrase for this turn.",
        "wifi",
        "Answer concisely.",
        [
            {"role": "user", "text": "Old phrase."},
            {"role": "assistant", "text": "Noted: old phrase."},
        ],
    )

    assert "A new phrase for this turn." in prompt
    assert "Old phrase" not in prompt
    assert "Noted: old phrase" not in prompt
    assert prompt.index("Capability boundaries:") < prompt.index("User said:")
    assert "A supplied URL uses fetch_url" in prompt
    assert "past/recent stored observations use query_environment" in prompt
    assert "camera movement always uses camera_ptz" in prompt


def test_acoustic_loop_guard_is_model_only_and_preserves_repetition_tests():
    prompt = pipeline.acoustic_loop_guard_prompt("garbled primary", "garbled fast")

    assert '"action":"proceed|listen"' in prompt
    assert "explicit repetition/telephone tests" in prompt
    assert "Disagreement alone is never enough" in prompt
    assert "localized disagreement is uncertainty" in prompt
    assert "Primary ASR: garbled primary" in prompt
    assert "Fast ASR: garbled fast" in prompt


def test_acoustic_echo_guard_uses_only_lane_local_previous_reply_and_preserves_new_requests():
    conversation = [
        {"role": "user", "source": "wifi", "text": "Nine copper keys are in the courtyard."},
        {"role": "assistant", "source": "nemotron-voicechat", "text": "Acknowledged. Nine copper keys in the courtyard."},
    ]
    previous = pipeline.previous_lane_assistant_reply(conversation)
    prompt = pipeline.acoustic_loop_guard_prompt(
        "Please repeat nine copper keys.",
        "Please repeat: nine copper keys.",
        previous,
    )

    assert previous == "Acknowledged. Nine copper keys in the courtyard."
    assert "Previous lane reply:" in prompt
    assert "explicit repeat requests" in prompt
    assert "explicit repeat requests" in prompt
    assert "cross-lane" not in prompt.lower()


def test_acoustic_echo_guard_exposes_system_policy_for_model_only_telephone_relay():
    prompt = pipeline.acoustic_loop_guard_prompt(
        "While violet thunder rolled beyond the harbor.",
        "While violet thunder rolled beyond the harbor.",
        "While violet thunder rolled beyond the harbor.",
        "Repeat every no-tool utterance exactly for a telephone relay.",
    )

    assert "Active system response policy:" in prompt
    assert "telephone relay" in prompt
    assert "every coherent meaningful utterance proceeds" in prompt


def test_dedicated_asr_tool_answer_prompt_is_current_turn_evidence_only():
    class Args:
        dedicated_asr_url = "http://127.0.0.1:8012"
        max_response_words = 80
        request_native_audio = False
        answer_model = pipeline.NEMOTRON_OMNI_MODEL
        ollama_model = pipeline.NEMOTRON_OMNI_MODEL
        answer_num_ctx = 32768
        answer_max_tokens = 320

    prompt = pipeline.build_tool_answer_prompt(
        Args(),
        "wifi",
        {"visual_state": "unrelated old room state"},
        "What is the local time right now?",
        "",
        '1. current_time: {"local_time":"2026-06-29 22:18:25 EDT"}',
        [{"role": "assistant", "text": "unrelated old reply"}],
    )

    assert "What is the local time right now?" in prompt
    assert "22:18:25 EDT" in prompt
    assert "unrelated old reply" not in prompt
    assert "unrelated old room state" not in prompt
    assert '{"response":"concise spoken answer"}' in prompt


def test_plain_audio_transcript_contract_avoids_json_transcription_bias():
    assert pipeline.plain_audio_transcript("Transcript: What time is it?\n") == "What time is it?"
    assert pipeline.plain_audio_transcript("```text\nSeven plus five is twelve.\n```") == "Seven plus five is twelve."
    assert pipeline.plain_audio_transcript("No clear speech") == ""
    assert "six to twelve common spoken words" in pipeline.plain_voice_reply_prompt("Why is the sky blue?")
    assert "six to twelve common spoken words" in pipeline.plain_voice_reply_prompt("Lane two, why is the sky blue?")
    assert "at most eight words" in pipeline.plain_voice_reply_prompt("Hello there")
    dialog_prompt = pipeline.plain_voice_reply_prompt(
        "Blue shirt",
        "wifi",
        "Narrate every acoustic-dialog action.",
    )
    assert "Input source: wifi" in dialog_prompt
    assert "Follow the system instructions exactly" in dialog_prompt

    class LeadArgs:
        playback_lead_silence_seconds = 0.50
        server_playback_lead_silence_seconds = 0.65
        playback_fade_in_seconds = 0.0
        server_playback_fade_in_seconds = 0.0

    assert pipeline.playback_lead_silence_seconds(LeadArgs(), "server") == 0.65
    assert pipeline.playback_lead_silence_seconds(LeadArgs(), "wifi_camera") == 0.50
    assert pipeline.playback_fade_in_seconds(LeadArgs(), "server") == 0.0
    assert pipeline.playback_fade_in_seconds(LeadArgs(), "wifi_camera") == 0.0


def test_camera_playback_wav_has_a_drain_tail(tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(pcm_wav_bytes(b"\x01\x00" * 1600))
    args = SimpleNamespace(
        audio_dir=str(tmp_path),
        playback_lead_silence_seconds=0.50,
        server_playback_lead_silence_seconds=0.65,
        playback_fade_in_seconds=0.0,
        server_playback_fade_in_seconds=0.0,
        playback_fade_out_seconds=0.025,
        server_playback_wake_tone_frequency=180.0,
        server_playback_wake_tone_volume=0.006,
    )

    output = pipeline.smooth_playback_wav(args, source, "camera-tail", "wifi_camera")

    with wave.open(str(output), "rb") as wav_file:
        assert wav_file.getnframes() == 1600 + 8000 + 7200


def test_context_history_drops_large_tool_payloads():
    items = [
        {
            "role": "assistant",
            "source": "nemotron-voicechat",
            "lane_source": "wifi",
            "text": " concise answer ",
            "status": "complete",
            "tool_results": [{"image_data_url": "x" * 100_000}],
            "token_usage": {"input_tokens": 1000},
        }
    ]

    compact = pipeline.context_conversation_items(items)

    assert compact == [
        {
            "role": "assistant",
            "source": "nemotron-voicechat",
            "lane_source": "wifi",
            "text": "concise answer",
            "status": "complete",
        }
    ]


def test_initial_speech_gate_waits_for_two_chunks():
    class Args:
        chunk_seconds = 0.75

    assert pipeline.initial_speech_gate_ready({"duration": 0.75}, Args()) is False
    assert pipeline.initial_speech_gate_ready({"duration": 1.5}, Args()) is True


def test_initial_marblenet_rejection_retains_waveform_positive_onset_for_retry():
    class Args:
        initial_speech_gate_retry_seconds = 2.5

    retain, candidate_seconds, limit = pipeline.initial_speech_gate_retry_state(
        {
            "duration": 2.0,
            "preroll_seconds": 1.5,
            "voice_chunks": 1,
        },
        Args(),
    )
    assert retain is True
    assert candidate_seconds == 0.5
    assert limit == 2.5

    retain, _, _ = pipeline.initial_speech_gate_retry_state(
        {"duration": 4.0, "preroll_seconds": 1.5, "voice_chunks": 2},
        Args(),
    )
    assert retain is False


def test_pipeline_cue_filter_is_off_when_all_cues_are_disabled():
    class Args:
        stage_chimes = False
        listening_beep = False

    assert pipeline.pipeline_audio_cue_filter_enabled(Args()) is False


def test_peer_loop_guard_only_suppresses_a_lanes_recent_repeated_answer():
    now = time.time()
    conversation = [
        {
            "role": "assistant",
            "text": "Could you repeat the full request?",
            "status": "complete",
            "updated_at": now - 8,
        }
    ]

    assert pipeline.repeated_peer_response_reason(
        "Could you repeat the full request!", conversation, True, now
    )
    assert pipeline.repeated_peer_response_reason(
        "I heard a new answer.", conversation, True, now
    ) == ""
    assert pipeline.repeated_peer_response_reason(
        "Could you repeat the full request?", conversation, False, now
    ) == ""
    assert pipeline.repeated_peer_response_reason(
        "Could you repeat the full request?", conversation, True, now, "What did you say?"
    ) == ""


def test_peer_playback_state_excludes_the_current_lane():
    now = time.time()
    data = {
        "sources": {
            "server": {"active": True, "phase": "playback", "expires_at": now + 10, "audio_id": "s1"},
            "wifi": {"active": False, "phase": "complete"},
        }
    }

    assert pipeline.active_peer_playback_state(data, "wifi", now)["audio_id"] == "s1"
    assert pipeline.active_peer_playback_state(data, "server", now) == {}


def test_peer_playback_completed_during_capture_is_still_detected():
    now = time.time()
    data = {
        "sources": {
            "server": {
                "active": False,
                "phase": "complete",
                "updated_at": now - 0.1,
                "cooldown_until": now + 0.25,
                "audio_id": "short-peer-turn",
            },
            "wifi": {"active": False, "phase": "waiting"},
        }
    }

    state = pipeline.peer_playback_state_for_capture(data, "wifi", now - 0.6, now)
    assert state["audio_id"] == "short-peer-turn"
    assert state["capture_overlap"] is True
    assert state["active"] is False


def test_transient_camera_audio_errors_are_backoff_eligible():
    assert pipeline.transient_audio_capture_error("HTTP Error 503: ffmpeg stopped reading camera audio") is True
    assert pipeline.transient_audio_capture_error("No route to host") is True
    assert pipeline.transient_audio_capture_error("shared audio request failed: timed out") is True
    assert pipeline.transient_audio_capture_error("unexpected programming error") is False


def test_short_acoustic_burst_waits_through_a_natural_mid_sentence_pause():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.5
        utterance_final_silence_seconds = 0.5
        chunk_seconds = 0.125

    entry = {
        "duration": 3.0,
        "last_speech_at": time.time() - 0.62,
        "voiced_duration": 0.375,
    }

    finalized, reason = pipeline.should_finalize(entry, Args())
    assert finalized is False
    assert reason.startswith("acoustic cadence grace")
    policy = pipeline.acoustic_endpoint_policy(entry, Args())
    assert policy["physical_wave_audio_only"] is True
    assert policy["backend_peer_lifecycle_input"] is False
    assert policy["holding_for_possible_continuation"] is True


def test_short_acoustic_burst_finalizes_after_content_agnostic_grace():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.5
        utterance_final_silence_seconds = 0.5
        chunk_seconds = 0.125

    entry = {
        "duration": 2.5,
        "last_speech_at": time.time() - 1.55,
        "voiced_duration": 0.375,
    }

    finalized, reason = pipeline.should_finalize(entry, Args())
    assert finalized is True
    assert reason.startswith("speech idle")


def test_confirmed_internal_gap_keeps_continuation_grace_above_ratio_boundary():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.65
        utterance_final_silence_seconds = 0.65
        chunk_seconds = 0.125

    entry = {
        "duration": 7.0,
        "last_speech_at": time.time() - 0.70,
        "voiced_duration": 4.25,
        "voice_chunks": 34,
        "buffer_chunks": 42,
        "first_voice_buffer_index": 1,
        "last_voice_buffer_index": 42,
        "max_internal_pause_seconds": 0.625,
    }

    finalized, reason = pipeline.should_finalize(entry, Args())
    policy = pipeline.acoustic_endpoint_policy(entry, Args())
    assert finalized is False
    assert reason.startswith("acoustic cadence grace")
    assert policy["voiced_chunk_ratio"] > 0.8
    assert policy["pause_rich_by_ratio"] is False
    assert policy["pause_rich_by_confirmed_internal_gap"] is True
    assert policy["required_silence_seconds"] == 1.5


def test_buffer_tracks_only_nonvoice_run_followed_by_resumed_speech(monkeypatch, tmp_path):
    chunk = tmp_path / "chunk.wav"
    chunk.write_bytes(b"wave")
    monkeypatch.setattr(
        pipeline,
        "update_utterance_voice_stats",
        lambda entry, _level, duration: entry.update(
            voice_chunks=int(entry.get("voice_chunks") or 0) + 1,
            voiced_duration=float(entry.get("voiced_duration") or 0.0) + duration,
        ),
    )
    monkeypatch.setattr(pipeline, "effective_speech_settings", lambda *_args: {
        "speech_rms_threshold": 0.01,
        "speech_peak_threshold": 0.02,
    })
    monkeypatch.setattr(pipeline, "read_asr_settings", lambda *_args: {})
    monkeypatch.setattr(pipeline, "trailing_silence_seconds_for_wav", lambda *_args: 0.0)
    monkeypatch.setattr(pipeline, "append_utterance_chunk_summary", lambda *_args, **_kwargs: None)

    class Args:
        speech_rms_threshold = 0.01
        speech_peak_threshold = 0.02

    entry = {"chunks": []}
    pipeline.append_utterance_buffer_chunk(Args(), "wifi", entry, chunk, 0.125, {}, True)
    for _ in range(5):
        pipeline.append_utterance_buffer_chunk(Args(), "wifi", entry, chunk, 0.125, {}, False)
    assert entry.get("max_internal_pause_seconds", 0.0) == 0.0
    pipeline.append_utterance_buffer_chunk(Args(), "wifi", entry, chunk, 0.125, {}, True)
    assert entry["max_internal_pause_seconds"] == 0.625
    assert entry["current_nonvoice_run_seconds"] == 0.0


def test_short_burst_speculation_launches_without_closing_endpoint(monkeypatch, tmp_path):
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.65
        utterance_final_silence_seconds = 0.65
        chunk_seconds = 0.125
        voicechat_response_json = str(tmp_path / "response.json")
        max_conversation_turns = 8

    class Future:
        def add_done_callback(self, callback):
            self.callback = callback

        def cancel(self):
            return True

    class Executor:
        def submit(self, *args, **kwargs):
            self.submitted = (args, kwargs)
            return Future()

    executor = Executor()
    monkeypatch.setattr(pipeline, "_SPECULATIVE_UNDERSTANDING_EXECUTOR", executor)
    monkeypatch.setattr(pipeline, "write_entry_audio", lambda *args, **kwargs: tmp_path / "candidate.wav")
    monkeypatch.setattr(pipeline, "wav_duration_seconds", lambda *args, **kwargs: 2.25)
    monkeypatch.setattr(pipeline, "lane_conversation", lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "environment_state", lambda *args, **kwargs: {})

    entry = {
        "speech_detected": True,
        "last_speech_at": time.time() - 0.70,
        "last_at": time.time(),
        "voiced_duration": 0.5,
        "voice_chunks": 4,
        "buffer_chunks": 10,
        "speech_revision": 4,
    }
    assert pipeline.maybe_start_speculative_understanding(Args(), "wifi", entry, tmp_path, "vllm") is True
    speculative = entry["speculative_understanding"]
    assert speculative["speech_revision"] == 4
    assert speculative["launch_silence_seconds"] >= 0.65
    finalized, _reason = pipeline.should_finalize({**entry, "duration": 3.0}, type("EndpointArgs", (), {
        "utterance_max_seconds": 18,
        "utterance_gap_seconds": 0.65,
        "utterance_final_silence_seconds": 0.65,
        "chunk_seconds": 0.125,
    })())
    assert finalized is False


def test_dense_speculation_launches_before_authoritative_endpoint(monkeypatch, tmp_path):
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.65
        utterance_final_silence_seconds = 0.65
        chunk_seconds = 0.125
        voicechat_response_json = str(tmp_path / "response.json")
        max_conversation_turns = 8

    class Future:
        def add_done_callback(self, callback):
            self.callback = callback

        def cancel(self):
            return True

    class Executor:
        def submit(self, *args, **kwargs):
            return Future()

    monkeypatch.setattr(pipeline, "_SPECULATIVE_UNDERSTANDING_EXECUTOR", Executor())
    monkeypatch.setattr(pipeline, "write_entry_audio", lambda *args, **kwargs: tmp_path / "candidate.wav")
    monkeypatch.setattr(pipeline, "wav_duration_seconds", lambda *args, **kwargs: 4.0)
    monkeypatch.setattr(pipeline, "lane_conversation", lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "environment_state", lambda *args, **kwargs: {})

    entry = {
        "speech_detected": True,
        "last_speech_at": time.time() - 0.30,
        "last_at": time.time(),
        "voiced_duration": 2.0,
        "voice_chunks": 16,
        "buffer_chunks": 16,
        "first_voice_buffer_index": 1,
        "last_voice_buffer_index": 16,
        "speech_revision": 16,
    }
    assert pipeline.maybe_start_speculative_understanding(Args(), "server", entry, tmp_path, "vllm") is True
    speculative = entry["speculative_understanding"]
    assert speculative["launch_policy"] == "dense_early_before_authoritative_endpoint_v2_025"
    assert speculative["launch_silence_seconds"] >= 0.25
    assert speculative["launch_silence_seconds"] < 0.65
    finalized, _reason = pipeline.should_finalize({**entry, "duration": 4.0}, Args())
    assert finalized is False


def test_resumed_speech_invalidates_speculative_understanding():
    class Future:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True
            return True

    future = Future()
    entry = {"speculative_understanding": {"future": future}}
    assert pipeline.invalidate_speculative_understanding(entry, "speech_resumed_before_full_endpoint") is True
    assert future.cancelled is True
    assert "speculative_understanding" not in entry
    assert entry["speculative_understanding_invalidations"] == 1


def test_natural_opening_clause_uses_extended_waveform_only_grace():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.4
        utterance_final_silence_seconds = 0.4
        chunk_seconds = 0.125

    entry = {
        "duration": 2.625,
        "last_speech_at": time.time() - 0.90,
        "voiced_duration": 1.125,
        "voice_chunks": 9,
        "buffer_chunks": 11,
    }

    finalized, reason = pipeline.should_finalize(entry, Args())
    policy = pipeline.acoustic_endpoint_policy(entry, Args())
    assert finalized is False
    assert reason.startswith("acoustic cadence grace")
    assert policy["policy"] == "acoustic_cadence_endpoint_v6_confirmed_internal_gap"
    assert policy["short_burst"] is True
    assert policy["required_silence_seconds"] == 1.5
    assert policy["content_matcher"] is False


def test_long_acoustic_utterance_keeps_snappy_base_endpoint():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.5
        utterance_final_silence_seconds = 0.5
        chunk_seconds = 0.125

    entry = {
        "duration": 5.0,
        "last_speech_at": time.time() - 0.55,
        "voiced_duration": 2.0,
        "voice_chunks": 20,
        "buffer_chunks": 24,
    }

    finalized, _ = pipeline.should_finalize(entry, Args())
    assert finalized is True


def test_pause_rich_long_utterance_gets_acoustic_continuation_grace():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.5
        utterance_final_silence_seconds = 0.5
        chunk_seconds = 0.125

    entry = {
        "duration": 5.125,
        "last_speech_at": time.time() - 0.62,
        "voiced_duration": 2.125,
        "voice_chunks": 17,
        "buffer_chunks": 31,
    }

    finalized, reason = pipeline.should_finalize(entry, Args())
    assert finalized is False
    assert reason.startswith("acoustic cadence grace")
    policy = pipeline.acoustic_endpoint_policy(entry, Args())
    assert policy["pause_rich"] is True
    assert policy["backend_peer_lifecycle_input"] is False


def test_moderately_pause_rich_long_utterance_keeps_v3_grace():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.4
        utterance_final_silence_seconds = 0.4
        chunk_seconds = 0.125

    entry = {
        "duration": 6.75,
        "last_speech_at": time.time() - 0.95,
        "voiced_duration": 4.25,
        "voice_chunks": 34,
        "buffer_chunks": 44,
    }

    finalized, reason = pipeline.should_finalize(entry, Args())
    policy = pipeline.acoustic_endpoint_policy(entry, Args())
    assert finalized is False
    assert reason.startswith("acoustic cadence grace")
    assert policy["pause_rich"] is True
    assert policy["voiced_chunk_ratio"] == 0.773
    assert policy["required_silence_seconds"] == 1.5


def test_endpoint_cadence_excludes_trailing_silence_chunks_from_voice_ratio():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.65
        utterance_final_silence_seconds = 0.65
        chunk_seconds = 0.125

    entry = {
        "duration": 4.5,
        "last_speech_at": time.time() - 0.70,
        "voiced_duration": 1.75,
        "voice_chunks": 14,
        "buffer_chunks": 26,
        "first_voice_buffer_index": 2,
        "last_voice_buffer_index": 16,
    }

    policy = pipeline.acoustic_endpoint_policy(entry, Args())
    finalized, reason = pipeline.should_finalize(entry, Args())
    assert policy["speech_span_chunks"] == 15
    assert policy["trailing_chunks_excluded_from_cadence"] == 10
    assert policy["voiced_chunk_ratio"] == 0.933
    assert policy["pause_rich"] is False
    assert policy["required_silence_seconds"] == 0.65
    assert finalized is True
    assert reason.startswith("speech idle")


def test_endpoint_cadence_preserves_grace_for_internal_pause_span():
    class Args:
        utterance_max_seconds = 18
        utterance_gap_seconds = 0.65
        utterance_final_silence_seconds = 0.65
        chunk_seconds = 0.125

    entry = {
        "duration": 5.125,
        "last_speech_at": time.time() - 0.70,
        "voiced_duration": 2.125,
        "voice_chunks": 17,
        "buffer_chunks": 31,
        "first_voice_buffer_index": 2,
        "last_voice_buffer_index": 27,
    }

    policy = pipeline.acoustic_endpoint_policy(entry, Args())
    finalized, reason = pipeline.should_finalize(entry, Args())
    assert policy["speech_span_chunks"] == 26
    assert policy["voiced_chunk_ratio"] == 0.654
    assert policy["pause_rich"] is True
    assert policy["required_silence_seconds"] == 1.5
    assert finalized is False
    assert reason.startswith("acoustic cadence grace")


def test_hybrid_guard_respects_relay_echo_and_non_echo_adjudication():
    semantic_echo = {"evaluated": True, "semantic_echo": True}
    semantic_new = {"evaluated": True, "semantic_echo": False}
    unavailable = {"evaluated": False}

    assert pipeline.hybrid_acoustic_guard_action("proceed", False, semantic_echo) == "listen"
    assert pipeline.hybrid_acoustic_guard_action("proceed", True, semantic_echo) == "proceed"
    assert pipeline.hybrid_acoustic_guard_action("listen", False, semantic_new, "proceed") == "proceed"
    assert pipeline.hybrid_acoustic_guard_action("listen", False, semantic_new, "listen") == "listen"
    assert pipeline.hybrid_acoustic_guard_action("listen", False, unavailable, "proceed") == "listen"


def test_disagreement_review_prompt_leaves_final_decision_to_model():
    prompt = pipeline.acoustic_disagreement_adjudicator_prompt(
        "Move the camera left.",
        "Move the grammar left.",
    )
    assert "differ substantially" in prompt
    assert "meaningful statement, question, command" in prompt
    assert '{"action":"proceed|listen"}' in prompt


def test_tool_result_sanitizer_never_replaces_model_wording_with_controller_answer():
    result = pipeline.sanitize_final_response(
        "What time is it?",
        "The measured local time is seven forty three.",
        {"needs_tools": True, "calls": [{"name": "current_time", "args": {}}]},
        [{"name": "current_time", "result": {"local_time": "2026-06-29 19:43:00 EDT"}}],
    )

    assert result == "The measured local time is seven forty three."

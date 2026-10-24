import json
import sys
import wave
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import nemotron_voicechat_pipeline as pipeline
from scripts import webcam_background_stack as background
from scripts import webcam_stream_server as server


def silent_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00" * 1600)
    return path


def test_pcm_audio_store_cursor_returns_contiguous_non_overlapping_windows():
    store = server.PcmAudioStore(sample_rate=10, channels=1, sample_width=1)
    store.append(b"abcdef")

    first_start, first_end, first, first_skipped = store.collect_from(0, 0.2, 0.1)
    second_start, second_end, second, second_skipped = store.collect_from(first_end, 0.2, 0.1)

    assert (first_start, first_end, first, first_skipped) == (0, 2, b"ab", 0)
    assert (second_start, second_end, second, second_skipped) == (2, 4, b"cd", 0)


def test_voice_session_fallback_payload_includes_on_demand_visual_policy():
    _voice, voicechat = server.voice_session_payloads(
        type("Args", (), {})(),
        123.0,
        "session-test",
        "server",
    )
    answer = next(stage for stage in voicechat["stages"] if stage["id"] == "voicechat_answer")

    assert answer["payload"]["visual_attachment_policy"] == "on_demand"
    assert answer["payload"]["visual_attachment_used"] is False


def test_omni_defaults_use_vllm_32k_and_separate_ollama_tool_planner():
    args = pipeline.parse_args([])

    assert args.model_api_runtime == "vllm"
    assert args.model_api_url == "http://127.0.0.1:8010"
    assert args.ollama_model == pipeline.NEMOTRON_OMNI_MODEL
    assert args.answer_model == pipeline.NEMOTRON_OMNI_MODEL
    assert args.answer_num_ctx == 32768
    assert pipeline.answer_model_context_window(args.answer_model) == 32768
    assert args.tool_planner_model == "nemotron-mini:latest"
    assert args.tool_planner_url == "http://127.0.0.1:11434"


def test_vllm_multimodal_content_uses_audio_url_and_one_authoritative_tool_image(tmp_path, monkeypatch):
    args = pipeline.parse_args([])
    wav_path = silent_wav(tmp_path / "input.wav")
    monkeypatch.setattr(
        pipeline,
        "omni_snapshot_content",
        lambda *_args: (
            [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,LIVE"}}],
            {"snapshot_count": 1, "snapshot_images": [{}], "snapshot_errors": []},
        ),
    )
    tool_results = [
        {
            "name": "focus_object",
            "result": {"image_data_url": "data:image/jpeg;base64,TOOL"},
        }
    ]

    content, info = pipeline.omni_multimodal_content(
        args,
        "wifi",
        "Answer from evidence.",
        wav_path,
        tool_results=tool_results,
        expect_json=False,
    )

    assert [item["type"] for item in content] == ["audio_url", "image_url", "text"]
    assert content[0]["audio_url"]["url"].startswith("data:audio/wav;base64,")
    assert content[1]["image_url"]["url"].endswith("TOOL")
    assert info["authoritative_image_source"] == "tool_result"
    assert info["snapshot_count"] == 0


def test_vllm_understanding_request_has_no_ollama_only_fields(tmp_path, monkeypatch):
    args = pipeline.parse_args([])
    wav_path = silent_wav(tmp_path / "input.wav")
    captured = {}
    monkeypatch.setattr(
        pipeline,
        "omni_snapshot_content",
        lambda *_args: ([], {"snapshot_count": 0, "snapshot_images": [], "snapshot_errors": []}),
    )

    def fake_request(url, path, payload, timeout, **metadata):
        captured.update(url=url, path=path, payload=payload, metadata=metadata)
        return {
            "choices": [{"message": {"content": json.dumps({"heard": "hello", "visual_state": "clear"})}}],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 42, "total_tokens": 1276},
        }

    monkeypatch.setattr(pipeline, "ollama_json_for_component", fake_request)

    result = pipeline.run_ollama_voicechat(args, wav_path, "wifi", {})

    assert captured["url"] == "http://127.0.0.1:8010"
    assert captured["path"] == "/v1/chat/completions"
    assert captured["metadata"]["provider"] == "vllm"
    assert captured["payload"]["model"] == pipeline.NEMOTRON_OMNI_MODEL
    assert captured["payload"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "keep_alive" not in captured["payload"]
    assert "think" not in captured["payload"]
    assert result["backend"] == "vllm"
    assert result["heard"] == "hello"
    assert result["token_usage"] == {
        "context_window_tokens": 32768,
        "max_input_tokens": 32768 - result["max_tokens"],
        "max_output_tokens": result["max_tokens"],
        "input_tokens": 1234,
        "output_tokens": 42,
        "consumed_context_tokens": 1276,
        "usage_exact": True,
    }


def test_dedicated_asr_error_includes_service_and_response_detail(monkeypatch):
    body = BytesIO(json.dumps({"error": "audio exceeds 30.0s limit"}).encode("utf-8"))

    def reject_request(*_args, **_kwargs):
        raise HTTPError("http://127.0.0.1:8012/transcribe", 400, "Bad Request", {}, body)

    monkeypatch.setattr(pipeline, "urlopen", reject_request)

    try:
        pipeline.dedicated_asr_json("http://127.0.0.1:8012", "audio", 15.0)
    except RuntimeError as exc:
        assert str(exc) == "Dedicated ASR HTTP 400: audio exceeds 30.0s limit"
    else:
        raise AssertionError("dedicated ASR HTTP failure was not surfaced")


def test_utterance_duration_cap_wins_while_current_chunk_still_has_voice():
    args = type("Args", (), {
        "utterance_max_seconds": 18.0,
        "utterance_gap_seconds": 0.65,
        "utterance_final_silence_seconds": 0.65,
        "chunk_seconds": 0.125,
    })()

    disposition, reason = pipeline.buffered_utterance_disposition(
        {"duration": 18.125, "last_speech_at": 0.0, "voiced_duration": 18.0},
        True,
        args,
    )

    assert disposition == "finalize"
    assert reason == "max utterance duration"


def test_human_dialog_renders_context_limits_consumption_and_pie():
    html = server.render_dashboard_html("test", "device", 8443)

    assert 'id="server-camera-details"' in html
    assert 'id="server-detail-video-preview" src="/stream.mjpg?fps=8"' in html
    assert "server: '/deepstream-overlay.jpg?source=server'" in html
    assert 'data-focus-operation data-source="server"' not in html
    assert "dialog-context-pie" in html
    assert "Max input" in html
    assert "Max output" in html
    assert "Consumed context" in html
    assert "consumed_context_tokens" in html


def test_voice_graph_renders_live_tool_decision_executor_and_piper_as_distinct_components():
    html = server.render_dashboard_html("test", "device", 8443)

    assert "Model-Only Tool Decision (nemotron_3_nano_omni)" in html
    assert "voicechat-tool-executor" in html
    assert "Selected Tool Execution" in html
    assert "selection_policy: 'ai_model_only'" in html
    assert "sparse_single_action_v31_time_granularity" in html
    assert "declarative_compare_safe: true" in html
    assert "compare_disambiguation: 'single_generic_model_example'" in html
    assert "trusted_tool_direct_response_policy: 'explicit_answer_from_ai_selected_trusted_local_tool_v1'" in html
    assert "trusted_tool_allowlist: ['current_time']" in html
    assert "current_time_args_model_selected: true" in html
    assert "current_time_default_include_date: false" in html
    assert "current_time_default_include_timezone: true" in html
    assert "current_time_timezone_omission_arg: 'omit_timezone'" in html
    assert "system_instruction_priority: true" in html
    assert "exact_repeat_benchmark_cases: 12" in html
    assert "acoustic_cadence_endpoint_v6_confirmed_internal_gap" in html
    assert "cadence_ratio_scope: 'first_to_last_voice_chunk'" in html
    assert "trailing_silence_excluded_from_cadence: true" in html
    assert "replay_dense_split_risk_cases: 0" in html
    assert "system_response_policy_aware: true" in html
    assert "relay_policy_benchmark_cases: 12" in html
    assert "expanded_guard_benchmark_cases: 48" in html
    assert "expanded_guard_benchmark_accuracy: 0.9375" in html
    assert "guard_prompt_optimization_status: 'current_retained_candidates_failed_reliability_gate'" in html
    assert "guard_model_candidates_tested: 3" in html
    assert "conditional_listen_adjudicator_accuracy: 0.9583" in html
    assert "conditional_listen_adjudicator_status: 'rejected_incomplete_corruption_rejection'" in html
    assert "visual_attachment_policy: 'on_demand'" in html
    assert "selection_policy_status: 'telemetry_only'" in html
    assert "confidence_selection_status: 'telemetry_only'" in html
    assert "parallel_models: false" in html
    assert "early_hypothesis_api: 'fast_and_primary_split_benchmark_only'" in html
    assert "speculative_decision_reuse_status: 'rejected_no_latency_improvement'" in html
    assert "Piper TTS (qualified storyline voice)" in html
    assert "random_nonrepeating_per_source_session_storyline" in html
    assert "voicechat-input-server-microphone-adapter" in html
    assert "Server Microphone Level Adapter" in html
    assert "voicechat-input-server-continuous-capture" in html
    assert "Continuous Server PCM Reader" in html
    assert "voicechat-input-server-playback-backlog-drain" in html
    assert "Server Self-Playback Backlog Drain" in html
    assert "voicechat-acoustic-guard" in html
    assert "Model-Only Acoustic and Echo Loop Guard" in html
    assert "voicechat-echo-relation" in html
    assert "Local NLI Echo Relation (DeBERTa-v3 xsmall)" in html
    assert "edge-authoritative-asr-echo-relation" in html
    assert "edge-echo-relation-acoustic-guard" in html
    assert "voicechat-disagreement-review" in html
    assert "Model-Only High-ASR-Disagreement Review (Nemotron)" in html
    assert "edge-acoustic-guard-disagreement-review" in html
    assert "edge-disagreement-review-reasoning" in html
    assert "activation_only_not_final_decision: true" in html
    assert "expanded_candidate_correct: 155" in html
    assert "relation_policy: 'bidirectional_entailment'" in html
    assert "hybrid_policy: 'nemotron_relay_corruption_plus_bidirectional_nli_echo_v1'" in html
    assert "hybrid_benchmark_cases: 80" in html
    assert "hybrid_benchmark_accuracy: 1.0" in html
    assert "cross_lane_backend_content: false" in html
    assert "voicechat-short-burst-speculation" in html
    assert "Endpoint-Safe Speculative Understanding" in html
    assert "edge-acoustic-preprocessing-short-burst-speculation" in html
    assert "edge-short-burst-speculation-fast-asr" in html
    assert "authoritative_endpoint_unchanged: true" in html
    assert "continuation_causes_discard: true" in html
    assert "tool_execution_during_speculation: false" in html
    assert "voicechat-nemotron-vllm-runtime" in html
    assert "Nemotron vLLM Runtime (qualified baseline)" in html
    assert "runtime_policy: 'vllm_baseline_grounding_validated_v1'" in html
    assert "max_num_sequences: 4" in html
    assert "max_num_batched_tokens: 8192" in html
    assert "configured_gpu_memory_utilization: 0.58" in html
    assert "speculative_decoding: false" in html
    assert "startup_warmup_policy: 'two_pass_text_json_before_voice_workers'" in html
    assert "fetchJsonOr('/nemotron-runtime-state.json'" in html
    assert "edge-model-runtime-acoustic-guard" in html
    assert "edge-model-runtime-reasoning" in html
    assert "edge-model-runtime-planner" in html
    assert "function fallbackStages(kind, message, source = '')" in html
    assert "fallbackStages('voicechat', sourceState.operation || sourceState.message || 'Waiting for microphone speech.', source)" in html
    assert "const microphoneAdapterStage = source === 'server'" in html


def test_background_stack_includes_omni_before_consumers():
    args = background.parse_args(["status"])
    services = background.build_services(args)
    names = [service.name for service in services]

    assert "deepstream-nemotron" not in names
    assert names.index("nemotron-omni") < names.index("voicechat-wifi")
    omni = services[names.index("nemotron-omni")]
    assert omni.env["SERVED_MODEL_NAME"] == "nemotron_3_nano_omni"
    wifi = services[names.index("voicechat-wifi")]
    command = " ".join(wifi.command)
    assert "--model-api-runtime vllm" in command
    assert "--model-api-url http://127.0.0.1:8010" in command
    assert "--answer-num-ctx 32768" in command

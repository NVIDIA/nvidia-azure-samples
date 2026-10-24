from scripts import nemotron_voicechat_pipeline as pipeline


def test_lane_scoping_rejects_turns_owned_by_another_lane():
    items = [
        {"role": "user", "source": "server", "text": "server input"},
        {"role": "user", "source": "wifi", "text": "wifi input"},
        {"role": "assistant", "source": "nemotron-voicechat", "lane_source": "server", "text": "server reply"},
        {"role": "assistant", "source": "nemotron-voicechat", "lane_source": "wifi", "text": "wifi reply"},
    ]

    scoped = pipeline.lane_scoped_conversation_items(items, "server")

    assert [item["text"] for item in scoped] == ["server input", "server reply"]
    assert all(item["lane_source"] == "server" for item in scoped)


def test_context_history_excludes_worker_errors_and_transient_diagnostics():
    items = [
        {"role": "user", "source": "wifi", "text": "hello", "status": "complete"},
        {"role": "assistant", "text": "answer", "status": "complete"},
        {"role": "assistant", "text": "VoiceChat worker error: ffmpeg timeout", "status": "error"},
        {"role": "assistant", "text": "Boundary detected", "status": "thinking", "temporary": True},
        {"role": "assistant", "text": "No clear speech", "technical_note": True},
    ]

    stable = pipeline.context_conversation_items(items)

    assert [item["text"] for item in stable] == ["hello", "answer"]


def test_persisted_history_is_independent_and_context_only(tmp_path):
    response_path = tmp_path / "webcam-voicechat-response.json"
    payload = {
        "conversation_by_source": {
            "server": [
                {"role": "user", "source": "server", "text": "server question"},
                {"role": "assistant", "source": "nemotron-voicechat", "text": "server answer"},
            ],
            "wifi": [
                {"role": "user", "source": "wifi", "text": "wifi question"},
                {"role": "assistant", "source": "nemotron-voicechat", "text": "wifi failure", "status": "error"},
            ],
        }
    }

    pipeline.write_voicechat_history(response_path, payload)
    stored = pipeline.read_json(pipeline.voicechat_history_path(response_path))["conversation_by_source"]

    assert [item["text"] for item in stored["server"]] == ["server question", "server answer"]
    assert all(item["lane_source"] == "server" for item in stored["server"])
    assert [item["text"] for item in stored["wifi"]] == ["wifi question"]
    assert stored["wifi"][0]["lane_source"] == "wifi"


def test_persisted_history_retains_the_full_live_window_per_lane():
    items = [
        {"role": "user", "source": "wifi", "text": f"turn {index}"}
        for index in range(pipeline.VOICECHAT_HISTORY_MAX_ITEMS_PER_SOURCE + 5)
    ]

    stored = pipeline.conversation_by_source_map({"conversation_by_source": {"wifi": items}})

    assert len(stored["wifi"]) == pipeline.VOICECHAT_HISTORY_MAX_ITEMS_PER_SOURCE
    assert stored["wifi"][0]["text"] == "turn 5"
    assert stored["wifi"][-1]["text"] == "turn 404"


def test_short_current_conversation_does_not_replace_richer_lane_history():
    history = [
        {"role": "user", "source": "wifi", "text": f"history {index}"}
        for index in range(100)
    ]
    current = history[-4:] + [{"role": "assistant", "lane_source": "wifi", "text": "new reply"}]

    stored = pipeline.conversation_by_source_map({
        "input_source": "wifi",
        "conversation": current,
        "conversation_by_source": {"wifi": history},
    })

    assert len(stored["wifi"]) == 101
    assert stored["wifi"][0]["text"] == "history 0"
    assert stored["wifi"][-1]["text"] == "new reply"


def test_explicit_say_command_extracts_literal_speech():
    assert pipeline.explicit_say_text('say "hello lane 2"') == "hello lane 2"
    assert pipeline.explicit_say_text("Please say 'hello lane 1'.") == "hello lane 1"
    assert pipeline.explicit_say_text("say this exactly") == "this exactly"
    assert pipeline.explicit_say_text("tell lane 2 hello") == ""

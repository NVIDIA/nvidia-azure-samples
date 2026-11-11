from scripts import webcam_background_stack as stack


def test_only_service_argument_can_be_repeated():
    args = stack.parse_args(
        [
            "restart",
            "--only-service",
            "voicechat-server",
            "--only-service",
            "voicechat-wifi",
        ]
    )

    assert args.only_service == ["voicechat-server", "voicechat-wifi"]


def test_physical_lanes_keep_reliable_subsecond_chunk_cadence():
    args = stack.parse_args(["status"])
    server = stack.voicechat_service(args, "server")
    wifi = stack.voicechat_service(args, "wifi")

    assert server.command[server.command.index("--chunk-seconds") + 1] == "0.125"
    assert wifi.command[wifi.command.index("--chunk-seconds") + 1] == "0.125"
    assert server.command[server.command.index("--utterance-gap-seconds") + 1] == "0.50"
    assert wifi.command[wifi.command.index("--utterance-gap-seconds") + 1] == "0.50"
    assert server.command[server.command.index("--utterance-final-silence-seconds") + 1] == "0.50"
    assert wifi.command[wifi.command.index("--utterance-final-silence-seconds") + 1] == "0.50"
    assert server.command[server.command.index("--server-audio-format") + 1] == "pulse"
    assert server.command[server.command.index("--server-audio-source") + 1] == "dgx_nexigo_70"
    assert server.command[server.command.index("--server-playback-lead-silence-seconds") + 1] == "0.65"
    assert wifi.command[wifi.command.index("--server-playback-lead-silence-seconds") + 1] == "0.65"
    assert "--server-audio-source" not in wifi.command


def test_dedicated_asr_keeps_faster_sequential_gpu_execution():
    args = stack.parse_args(["status"])
    service = stack.maybe_dedicated_asr_service(args)

    assert service is not None
    assert "--no-parallel-models" in service.command


def test_server_microphone_adapter_is_supervised_before_voice_workers():
    args = stack.parse_args(["status"])
    services = stack.build_services(args)
    names = [service.name for service in services]

    assert "server-microphone-adapter" in names
    assert names.index("server-microphone-adapter") < names.index("voicechat-server")
    adapter = services[names.index("server-microphone-adapter")]
    assert "server_microphone_adapter.py" in " ".join(adapter.command)
    assert adapter.command[adapter.command.index("--hardware-percent") + 1] == "70"
    assert adapter.command[adapter.command.index("--software-percent") + 1] == "70"

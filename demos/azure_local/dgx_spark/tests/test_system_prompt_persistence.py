import argparse
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import webcam_stream_server as server


def test_lane_prompt_files_are_authoritative_and_only_explicit_save_writes():
    html = server.render_dashboard_html("test", "device", 8443)

    initialize_start = html.index("async function initializeNemotronSystemPrompt(source)")
    initialize_end = html.index("function handleNemotronSystemPromptInput", initialize_start)
    input_end = html.index("async function saveNemotronOmniSystemPrompt", initialize_end)
    save_end = html.index("function stageGraphLayoutKey", input_end)
    initialize = html[initialize_start:initialize_end]
    input_handler = html[initialize_end:input_end]
    save_handler = html[input_end:save_end]

    assert "fetchJson(`/nemotron-system-prompt.json?source=" in initialize
    assert "persistNemotronSystemPromptOnServer" not in initialize
    assert "initializeNemotronSystemPrompt(cleanSource)" in initialize
    assert "!nemotronSystemPromptDirty[cleanSource]" in initialize
    assert "temporarily unavailable · retrying read…" in initialize
    assert "persistNemotronSystemPromptOnServer" not in input_handler
    assert "persistNemotronSystemPromptOnServer(source, prompt)" in save_handler
    assert "localStorage" not in html[html.index("function normalizedNemotronPromptSource"):save_end]
    assert "scheduleNemotronSystemPromptSave" not in html
    assert 'data-nemotron-prompt-source="server"' in html
    assert 'data-nemotron-prompt-source="wifi"' in html
    assert 'data-nemotron-prompt-file data-source="server"' in html
    assert 'data-nemotron-prompt-file data-source="wifi"' in html
    assert "Configuration file:" in html


def test_lane_system_prompt_storage_is_isolated_and_survives_process_restart(tmp_path):
    server_path = tmp_path / "server-system-prompt.json"
    wifi_path = tmp_path / "wifi-system-prompt.json"
    server.write_json_file(server_path, {"source": "server", "system_prompt": "Server prompt", "updated_at": 123.0})
    server.write_json_file(wifi_path, {"source": "wifi", "system_prompt": "Wi-Fi prompt", "updated_at": 124.0})

    # A newly constructed args object represents a restarted server process.
    restarted_args = argparse.Namespace(
        nemotron_system_prompt_path="",
        nemotron_server_system_prompt_path=str(server_path),
        nemotron_wifi_system_prompt_path=str(wifi_path),
    )
    assert server.read_nemotron_system_prompt(restarted_args, "server") == "Server prompt"
    assert server.read_nemotron_system_prompt(restarted_args, "wifi") == "Wi-Fi prompt"
    assert server.nemotron_system_prompt_path(restarted_args, "server") != server.nemotron_system_prompt_path(
        restarted_args, "wifi"
    )


def test_lane_prompt_default_paths_are_distinct():
    args = argparse.Namespace(
        nemotron_system_prompt_path="",
        nemotron_server_system_prompt_path="",
        nemotron_wifi_system_prompt_path="",
    )

    assert server.nemotron_system_prompt_path(args, "server").name == "webcam-nemotron-server-system-prompt.json"
    assert server.nemotron_system_prompt_path(args, "wifi").name == "webcam-nemotron-wifi-system-prompt.json"


def test_explicit_lane_save_updates_only_the_selected_file(tmp_path):
    server_path = tmp_path / "server-system-prompt.json"
    wifi_path = tmp_path / "wifi-system-prompt.json"
    server.write_json_file(server_path, {"source": "server", "system_prompt": "Old server"})
    server.write_json_file(wifi_path, {"source": "wifi", "system_prompt": "Keep Wi-Fi"})
    args = argparse.Namespace(
        nemotron_system_prompt_path="",
        nemotron_server_system_prompt_path=str(server_path),
        nemotron_wifi_system_prompt_path=str(wifi_path),
    )

    payload = server.write_nemotron_system_prompt(args, "server", {"system_prompt": "New server"})

    assert payload["config_file"] == str(server_path.resolve())
    assert server.read_nemotron_system_prompt(args, "server") == "New server"
    assert server.read_nemotron_system_prompt(args, "wifi") == "Keep Wi-Fi"


def test_system_prompt_formatting_round_trips_exactly(tmp_path):
    server_path = tmp_path / "server-system-prompt.json"
    wifi_path = tmp_path / "wifi-system-prompt.json"
    args = argparse.Namespace(
        nemotron_system_prompt_path="",
        nemotron_server_system_prompt_path=str(server_path),
        nemotron_wifi_system_prompt_path=str(wifi_path),
    )
    formatted = "  Role: observer\n\nRules:\n  - Keep indentation\n  - Keep  double spaces\n\n"

    payload = server.write_nemotron_system_prompt(args, "server", {"system_prompt": formatted})

    assert payload["system_prompt"] == formatted
    assert server.read_nemotron_system_prompt(args, "server") == formatted

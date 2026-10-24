from argparse import Namespace
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts import nemotron_voicechat_pipeline as pipeline
from scripts import webcam_stream_server as server
from nemotron_dialog_config import (
    DEFAULT_MODEL_INPUT_WINDOW_TOKENS,
    read_nemotron_dialog_settings,
    write_nemotron_dialog_settings,
)


def test_dialog_window_setting_persists_and_clamps_to_supported_steps(tmp_path):
    path = tmp_path / "dialog.json"

    assert read_nemotron_dialog_settings(path)["model_input_window_tokens"] == DEFAULT_MODEL_INPUT_WINDOW_TOKENS
    saved = write_nemotron_dialog_settings(path, {"model_input_window_tokens": 7777})

    assert saved["model_input_window_tokens"] == 7168
    assert read_nemotron_dialog_settings(path)["model_input_window_tokens"] == 7168
    assert write_nemotron_dialog_settings(path, {"model_input_window_tokens": 999999})["model_input_window_tokens"] == 32768


def test_configured_dialog_window_controls_retained_history_budget(tmp_path):
    path = tmp_path / "dialog.json"
    write_nemotron_dialog_settings(path, {"model_input_window_tokens": 4096})
    args = Namespace(
        answer_model=pipeline.NEMOTRON_OMNI_MODEL,
        ollama_model=pipeline.NEMOTRON_OMNI_MODEL,
        answer_num_ctx=32768,
        answer_max_tokens=320,
        nemotron_dialog_settings_path=str(path),
    )

    assert pipeline.configured_answer_context_window(args) == 4096
    budget = pipeline.conversation_history_token_budget(args, "system", "current request")
    assert 0 < budget < 4096


def test_dialog_html_exposes_persistent_window_control_and_tool_input_tokens():
    html = server.render_dashboard_html("test", "device", 8443)

    assert "/nemotron-dialog-settings" in html
    assert "Retained history / model input window" in html
    assert "decision input tokens" in html

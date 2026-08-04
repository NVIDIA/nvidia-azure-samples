"""Persistent settings for Nemotron dialog history retention."""

from __future__ import annotations

import json
import time
from pathlib import Path


DEFAULT_MODEL_INPUT_WINDOW_TOKENS = 32768
MIN_MODEL_INPUT_WINDOW_TOKENS = 1024
MAX_MODEL_INPUT_WINDOW_TOKENS = 32768
MODEL_INPUT_WINDOW_STEP_TOKENS = 1024


def normalize_model_input_window_tokens(value: object) -> int:
    if isinstance(value, dict):
        value = value.get("model_input_window_tokens", DEFAULT_MODEL_INPUT_WINDOW_TOKENS)
    try:
        tokens = int(value)
    except (TypeError, ValueError):
        tokens = DEFAULT_MODEL_INPUT_WINDOW_TOKENS
    tokens = max(MIN_MODEL_INPUT_WINDOW_TOKENS, min(MAX_MODEL_INPUT_WINDOW_TOKENS, tokens))
    return max(MIN_MODEL_INPUT_WINDOW_TOKENS, (tokens // MODEL_INPUT_WINDOW_STEP_TOKENS) * MODEL_INPUT_WINDOW_STEP_TOKENS)


def read_nemotron_dialog_settings(path: str | Path) -> dict:
    config_path = Path(path)
    try:
        stored = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        stored = {}
    return {
        "model_input_window_tokens": normalize_model_input_window_tokens(stored),
        "minimum_tokens": MIN_MODEL_INPUT_WINDOW_TOKENS,
        "maximum_tokens": MAX_MODEL_INPUT_WINDOW_TOKENS,
        "step_tokens": MODEL_INPUT_WINDOW_STEP_TOKENS,
        "updated_at": stored.get("updated_at", 0) if isinstance(stored, dict) else 0,
    }


def write_nemotron_dialog_settings(path: str | Path, value: object) -> dict:
    config_path = Path(path)
    payload = {
        "model_input_window_tokens": normalize_model_input_window_tokens(value),
        "updated_at": time.time(),
    }
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary_path.replace(config_path)
    return {
        **read_nemotron_dialog_settings(config_path),
        "config_file": str(config_path.resolve()),
    }

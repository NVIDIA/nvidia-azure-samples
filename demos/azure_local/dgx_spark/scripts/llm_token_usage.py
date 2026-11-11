#!/usr/bin/env python3
"""Persistent, provider-neutral accounting for LLM input and output tokens."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "webcam-llm-token-usage.sqlite3"
_DB_LOCK = threading.RLock()


def token_usage_db_path(path: str | Path | None = None) -> Path:
    configured = str(path or os.environ.get("LLM_TOKEN_USAGE_DB") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


def ensure_token_usage_db(path: str | Path | None = None) -> Path:
    db_path = token_usage_db_path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK:
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(8):
            try:
                with sqlite3.connect(db_path, timeout=5.0) as conn:
                    conn.execute("PRAGMA busy_timeout=5000")
                    if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                        conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS llm_token_usage (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            created_at REAL NOT NULL,
                            component TEXT NOT NULL,
                            model TEXT NOT NULL,
                            provider TEXT NOT NULL,
                            source TEXT NOT NULL DEFAULT '',
                            input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
                            output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
                            total_tokens INTEGER NOT NULL CHECK (total_tokens >= 0),
                            usage_source TEXT NOT NULL,
                            request_id TEXT NOT NULL DEFAULT '',
                            metadata_json TEXT NOT NULL DEFAULT '{}'
                        )
                        """
                    )
                    conn.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_llm_token_usage_component_model
                        ON llm_token_usage (component, model, created_at)
                        """
                    )
                return db_path
            except sqlite3.OperationalError as exc:
                last_error = exc
                time.sleep(0.025 * (attempt + 1))
        if last_error is not None:
            raise last_error
    return db_path


def _nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def extract_response_usage(response: object) -> tuple[int, int, str] | None:
    """Return exact input/output counts from OpenAI-compatible or Ollama output."""
    if not isinstance(response, dict):
        return None
    usage = response.get("usage")
    if isinstance(usage, dict):
        input_tokens = _nonnegative_int(usage.get("prompt_tokens"))
        if input_tokens is None:
            input_tokens = _nonnegative_int(usage.get("input_tokens"))
        output_tokens = _nonnegative_int(usage.get("completion_tokens"))
        if output_tokens is None:
            output_tokens = _nonnegative_int(usage.get("output_tokens"))
        if input_tokens is not None and output_tokens is not None:
            return input_tokens, output_tokens, "response.usage"

    input_tokens = _nonnegative_int(response.get("prompt_eval_count"))
    output_tokens = _nonnegative_int(response.get("eval_count"))
    if input_tokens is not None and output_tokens is not None:
        return input_tokens, output_tokens, "ollama_eval_counts"
    return None


def record_token_usage(
    *,
    component: str,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    source: str = "",
    usage_source: str = "explicit",
    request_id: str = "",
    metadata: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> bool:
    """Append one completed inference to the durable ledger without breaking inference."""
    try:
        inputs = max(0, int(input_tokens))
        outputs = max(0, int(output_tokens))
        with _DB_LOCK:
            path = ensure_token_usage_db(db_path)
            with sqlite3.connect(path, timeout=5.0) as conn:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    """
                    INSERT INTO llm_token_usage (
                        created_at, component, model, provider, source,
                        input_tokens, output_tokens, total_tokens,
                        usage_source, request_id, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        str(component or "unknown"),
                        str(model or "unknown"),
                        str(provider or "unknown"),
                        str(source or ""),
                        inputs,
                        outputs,
                        inputs + outputs,
                        str(usage_source or "explicit"),
                        str(request_id or ""),
                        json.dumps(metadata or {}, sort_keys=True, default=str),
                    ),
                )
        return True
    except Exception:
        return False


def record_response_usage(
    response: object,
    *,
    component: str,
    model: str,
    provider: str,
    source: str = "",
    metadata: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> bool:
    counts = extract_response_usage(response)
    if counts is None:
        return False
    input_tokens, output_tokens, usage_source = counts
    request_id = str(response.get("id") or "") if isinstance(response, dict) else ""
    return record_token_usage(
        component=component,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        source=source,
        usage_source=usage_source,
        request_id=request_id,
        metadata=metadata,
        db_path=db_path,
    )


def _aggregate_rows(conn: sqlite3.Connection, column: str) -> dict[str, dict[str, int | str]]:
    allowed = {"component", "model", "provider"}
    if column not in allowed:
        raise ValueError(f"Unsupported aggregate column: {column}")
    rows = conn.execute(
        f"""
        SELECT {column}, SUM(input_tokens), SUM(output_tokens), SUM(total_tokens), COUNT(*)
        FROM llm_token_usage
        GROUP BY {column}
        ORDER BY {column}
        """
    ).fetchall()
    return {
        str(row[0]): {
            column: str(row[0]),
            "input_tokens": int(row[1] or 0),
            "output_tokens": int(row[2] or 0),
            "total_tokens": int(row[3] or 0),
            "request_count": int(row[4] or 0),
        }
        for row in rows
    }


def token_usage_snapshot(path: str | Path | None = None) -> dict[str, Any]:
    """Read all-time raw sums. The ledger is intentionally never reset by UI session clears."""
    try:
        db_path = ensure_token_usage_db(path)
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            totals = conn.execute(
                """
                SELECT SUM(input_tokens), SUM(output_tokens), SUM(total_tokens), COUNT(*), MAX(created_at)
                FROM llm_token_usage
                """
            ).fetchone()
            return {
                "status": "running",
                "scope": "all_time",
                "input_tokens": int(totals[0] or 0),
                "output_tokens": int(totals[1] or 0),
                "total_tokens": int(totals[2] or 0),
                "request_count": int(totals[3] or 0),
                "updated_at": float(totals[4] or 0),
                "by_component": _aggregate_rows(conn, "component"),
                "by_model": _aggregate_rows(conn, "model"),
                "by_provider": _aggregate_rows(conn, "provider"),
            }
    except Exception as exc:
        return {
            "status": "error",
            "scope": "all_time",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "request_count": 0,
            "by_component": {},
            "by_model": {},
            "by_provider": {},
            "error": str(exc),
        }

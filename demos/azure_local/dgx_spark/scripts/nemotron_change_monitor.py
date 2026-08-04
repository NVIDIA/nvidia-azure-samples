#!/usr/bin/env python3
"""Detect material changes in Cosmos webcam analyses with a local Nemotron model."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from llm_token_usage import record_response_usage


PREFERRED_MODELS = (
    "nemotron3:33b",
    "nemotron-spark:latest",
    "nemotron-3-super:120b",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-json", default="/home/anslutsky/Dev/Cosmos-transfer/webcam-analysis.json")
    parser.add_argument("--alert-json", default="/home/anslutsky/Dev/Cosmos-transfer/webcam-alert.json")
    parser.add_argument("--database", default="/home/anslutsky/Dev/Cosmos-transfer/webcam-analysis-history.sqlite3")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default=None)
    parser.add_argument("--interval", type=float, default=4.0)
    parser.add_argument("--history-limit", type=int, default=8)
    parser.add_argument("--min-history", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def publish(path: str | Path, payload: dict) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), **payload}
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(output_path)


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_updated_at REAL NOT NULL,
            observed_at REAL NOT NULL,
            answer TEXT NOT NULL,
            answer_hash TEXT NOT NULL UNIQUE,
            clip_path TEXT,
            cosmos_model TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL,
            created_at REAL NOT NULL,
            model TEXT NOT NULL,
            alert INTEGER NOT NULL,
            severity TEXT NOT NULL,
            confidence REAL,
            description TEXT NOT NULL,
            raw_response TEXT,
            FOREIGN KEY(analysis_id) REFERENCES analyses(id)
        )
        """
    )
    return conn


def load_analysis(path: str | Path) -> dict | None:
    analysis_path = Path(path)
    if not analysis_path.exists():
        return None
    data = json.loads(analysis_path.read_text(encoding="utf-8"))
    answer = str(data.get("answer") or "").strip()
    if data.get("status") != "running" or not answer:
        return None
    return data


def answer_hash(answer: str, source_updated_at: float) -> str:
    digest = hashlib.sha256()
    digest.update(str(round(source_updated_at, 3)).encode("utf-8"))
    digest.update(b"\0")
    digest.update(answer.encode("utf-8"))
    return digest.hexdigest()


def insert_analysis(conn: sqlite3.Connection, analysis: dict) -> int | None:
    answer = str(analysis.get("answer") or "").strip()
    source_updated_at = float(analysis.get("updated_at") or time.time())
    digest = answer_hash(answer, source_updated_at)
    try:
        cur = conn.execute(
            """
            INSERT INTO analyses (source_updated_at, observed_at, answer, answer_hash, clip_path, cosmos_model)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source_updated_at,
                time.time(),
                answer,
                digest,
                analysis.get("clip_path"),
                analysis.get("model"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def recent_analyses(conn: sqlite3.Connection, before_id: int, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, source_updated_at, observed_at, answer
        FROM analyses
        WHERE id < ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (before_id, limit),
    ).fetchall()
    return [
        {
            "id": row[0],
            "source_updated_at": row[1],
            "observed_at": row[2],
            "answer": row[3],
        }
        for row in reversed(rows)
    ]


def ollama_json(
    url: str,
    path: str,
    payload: dict,
    timeout: float,
    *,
    component: str = "history",
) -> dict:
    request = Request(
        f"{url.rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        record_response_usage(
            data,
            component=component,
            model=str(payload.get("model") or "unknown"),
            provider="ollama",
            source="change_monitor",
            metadata={"endpoint": path},
        )
        return data
    except URLError as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc


def ollama_tags(url: str, timeout: float) -> list[str]:
    try:
        with urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise RuntimeError(f"Could not query Ollama models: {exc}") from exc
    return [model.get("name") or model.get("model") for model in data.get("models", []) if model.get("name") or model.get("model")]


def choose_model(args: argparse.Namespace) -> str:
    if args.model:
        return args.model
    models = ollama_tags(args.ollama_url, timeout=10)
    for preferred in PREFERRED_MODELS:
        if preferred in models:
            return preferred
    for model in models:
        if "nemotron" in model.lower():
            return model
    raise RuntimeError("No local Ollama Nemotron model found")


def extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def judge_change(args: argparse.Namespace, model: str, history: list[dict], newest: str) -> tuple[dict, str]:
    history_lines = "\n".join(f"{item['id']}: {item['answer']}" for item in history)
    prompt = f"""You are a strict change detector for a live webcam monitor.

Compare the newest Cosmos analysis to the recent analysis history.
Alert only when the scene is VERY DIFFERENT: a person appears or disappears, a major object appears/disappears/moves, a door/window state changes, activity changes sharply, or a safety-relevant anomaly appears.
Ignore wording differences, color disagreements, lighting changes, and minor object-description variations.

Return only compact JSON with this schema:
{{"alert": true|false, "severity": "none|low|medium|high", "confidence": 0.0-1.0, "description": "short plain-English change summary"}}

Recent history:
{history_lines or "(none)"}

Newest analysis:
{newest}
"""
    response = ollama_json(
        args.ollama_url,
        "/api/generate",
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_predict": 180,
                "num_ctx": 4096,
            },
            "keep_alive": "30m",
        },
        timeout=args.timeout,
    )
    raw = str(response.get("response") or response.get("thinking") or "").strip()
    parsed = extract_json(raw)
    parsed["alert"] = bool(parsed.get("alert"))
    parsed["severity"] = str(parsed.get("severity") or ("medium" if parsed["alert"] else "none")).lower()
    parsed["description"] = str(parsed.get("description") or "").strip()
    try:
        parsed["confidence"] = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        parsed["confidence"] = 0.0
    if not parsed["description"]:
        parsed["description"] = "Material change detected." if parsed["alert"] else "No material change from recent analyses."
    if not parsed["alert"]:
        parsed["severity"] = "none"
    return parsed, raw


def insert_alert(conn: sqlite3.Connection, analysis_id: int, model: str, parsed: dict, raw: str) -> None:
    conn.execute(
        """
        INSERT INTO alerts (analysis_id, created_at, model, alert, severity, confidence, description, raw_response)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            analysis_id,
            time.time(),
            model,
            int(bool(parsed["alert"])),
            parsed["severity"],
            parsed.get("confidence"),
            parsed["description"],
            raw,
        ),
    )
    conn.commit()


def process_once(args: argparse.Namespace, conn: sqlite3.Connection, model: str) -> bool:
    analysis = load_analysis(args.analysis_json)
    if analysis is None:
        publish(
            args.alert_json,
            {
                "status": "waiting",
                "alert": False,
                "severity": "none",
                "model": model,
                "message": "Waiting for a running Cosmos analysis.",
            },
        )
        return False

    analysis_id = insert_analysis(conn, analysis)
    if analysis_id is None:
        return False

    history = recent_analyses(conn, before_id=analysis_id, limit=args.history_limit)
    if len(history) < args.min_history:
        publish(
            args.alert_json,
            {
                "status": "baseline",
                "alert": False,
                "severity": "none",
                "model": model,
                "analysis_id": analysis_id,
                "description": f"Collecting baseline history ({len(history)}/{args.min_history}).",
                "recent_count": len(history),
            },
        )
        return True

    parsed, raw = judge_change(args, model, history, str(analysis.get("answer") or ""))
    insert_alert(conn, analysis_id, model, parsed, raw)
    publish(
        args.alert_json,
        {
            "status": "running",
            "alert": parsed["alert"],
            "severity": parsed["severity"],
            "confidence": parsed["confidence"],
            "description": parsed["description"],
            "model": model,
            "analysis_id": analysis_id,
            "recent_count": len(history),
            "source_updated_at": analysis.get("updated_at"),
        },
    )
    return True


def main() -> int:
    args = parse_args()
    conn = init_db(args.database)

    try:
        model = choose_model(args)
    except Exception as exc:
        publish(args.alert_json, {"status": "error", "alert": False, "severity": "none", "error": str(exc)})
        raise

    publish(
        args.alert_json,
        {
            "status": "starting",
            "alert": False,
            "severity": "none",
            "model": model,
            "message": "Starting Nemotron change monitor.",
        },
    )

    while True:
        try:
            process_once(args, conn, model)
        except Exception as exc:
            publish(
                args.alert_json,
                {
                    "status": "error",
                    "alert": False,
                    "severity": "none",
                    "model": model,
                    "error": str(exc),
                },
            )
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())

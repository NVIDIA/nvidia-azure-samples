import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from llm_token_usage import (  # noqa: E402
    extract_response_usage,
    record_response_usage,
    record_token_usage,
    token_usage_snapshot,
)


def test_extracts_openai_and_ollama_raw_counts():
    assert extract_response_usage({"usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 99}}) == (
        12,
        7,
        "response.usage",
    )
    assert extract_response_usage({"usage": {"input_tokens": 4, "output_tokens": 3}}) == (
        4,
        3,
        "response.usage",
    )
    assert extract_response_usage({"prompt_eval_count": 20, "eval_count": 5}) == (
        20,
        5,
        "ollama_eval_counts",
    )
    assert extract_response_usage({"response": "no exact counters"}) is None


def test_persists_and_sums_all_time_usage(tmp_path):
    db_path = tmp_path / "usage.sqlite3"
    assert record_response_usage(
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        component="voicechat",
        model="voice-fast",
        provider="ollama",
        db_path=db_path,
    )
    assert record_token_usage(
        component="voicechat_answer",
        model="spark",
        provider="ollama",
        input_tokens=30,
        output_tokens=8,
        usage_source="test",
        db_path=db_path,
    )

    snapshot = token_usage_snapshot(db_path)
    assert snapshot["input_tokens"] == 40
    assert snapshot["output_tokens"] == 10
    assert snapshot["total_tokens"] == 50
    assert snapshot["request_count"] == 2
    assert snapshot["by_component"]["voicechat"]["total_tokens"] == 12
    assert snapshot["by_component"]["voicechat_answer"]["total_tokens"] == 38
    assert snapshot["by_model"]["spark"]["input_tokens"] == 30

    reopened = token_usage_snapshot(db_path)
    assert reopened["total_tokens"] == 50


def test_concurrent_writers_share_one_sqlite_ledger(tmp_path):
    db_path = tmp_path / "concurrent.sqlite3"

    def write_one(index):
        return record_token_usage(
            component="tool_plan",
            model="planner",
            provider="ollama",
            input_tokens=index + 1,
            output_tokens=1,
            db_path=db_path,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert all(pool.map(write_one, range(12)))
    snapshot = token_usage_snapshot(db_path)
    assert snapshot["request_count"] == 12
    assert snapshot["input_tokens"] == sum(range(1, 13))
    assert snapshot["output_tokens"] == 12


def test_dashboard_exposes_all_time_raw_component_totals():
    source = (ROOT / "scripts/webcam_stream_server.py").read_text(encoding="utf-8")

    assert 'elif path == "/llm-token-usage.json"' in source
    assert "token_usage_snapshot(args.llm_token_usage_db)" in source
    assert "function stageTokenUsage(stage)" in source
    assert "function stageTokenUsageLabel(stage)" in source
    assert "function formatTokenCount(value)" in source
    assert "Math.max(0, Math.trunc(count)).toLocaleString('en-US')" in source
    assert "Tokens all time · input ${formatTokenCount(usage.input_tokens)} · output ${formatTokenCount(usage.output_tokens)} · total ${formatTokenCount(usage.total_tokens)}" in source
    assert "latestLlmTokenUsage?.updated_at" in source
    assert "['voicechat', 'voicechat_answer']" in source
    assert "tokenUsage.className = 'stage-token-usage'" in source
    assert "fetchJsonOr('/llm-token-usage.json'" in source

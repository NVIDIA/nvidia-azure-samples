from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = PROJECT_ROOT / "docs" / "ai-decision-ledger.yaml"


def load_ledger() -> dict:
    payload = yaml.safe_load(LEDGER_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_ai_handoff_entrypoints_exist_and_route_to_the_durable_record():
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    skill = (PROJECT_ROOT / "SKILL.md").read_text(encoding="utf-8")
    handoff = (PROJECT_ROOT / "AI_HANDOFF.md").read_text(encoding="utf-8")

    assert "AI_HANDOFF.md" in agents
    assert "docs/ai-decision-ledger.yaml" in agents
    assert "AI_HANDOFF.md" in skill
    assert "D-ROUTE-006" in handoff
    assert "deepstream-nemotron" in handoff


def test_ai_decision_ledger_has_unique_resolvable_history_and_anchors():
    payload = load_ledger()
    assert payload["schema_version"] == 1
    assert payload["ledger_policy"]["mode"] == "append_only"

    decisions = payload["decisions"]
    ids = [decision["id"] for decision in decisions]
    assert len(ids) == len(set(ids))
    known_ids = set(ids)

    for decision in decisions:
        for reference in decision.get("supersedes", []):
            assert reference in known_ids, (decision["id"], reference)
        for reference in decision.get("superseded_by", []):
            assert reference in known_ids, (decision["id"], reference)

        anchors = decision.get("anchors", {}) or {}
        for group in ("code", "tests"):
            for raw_reference in anchors.get(group, []) or []:
                relative_path = str(raw_reference).split(":", 1)[0]
                assert (PROJECT_ROOT / relative_path).exists(), (
                    decision["id"],
                    raw_reference,
                )

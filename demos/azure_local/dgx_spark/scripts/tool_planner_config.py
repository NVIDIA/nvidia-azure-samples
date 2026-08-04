"""Persistent configuration for the model-only tool planner request template."""

from __future__ import annotations

import json
import time
from pathlib import Path


DEFAULT_TOOL_PLANNER_TEMPLATE = """You only select tools. Never answer the input and never invent tool results.

ACTIVE SYSTEM RESPONSE POLICY:
{{SYSTEM_RESPONSE_POLICY}}

CURRENT INPUT:
{{CURRENT_INPUT}}

INPUT SOURCE:
{{INPUT_SOURCE}}

AVAILABLE TOOLS:
{{AVAILABLE_TOOLS}}

RULES:
{{TOOL_RULES}}

Return one JSON object using exactly this schema:
{"needs_tools":true|false,"calls":[{"name":"tool_name","args":{}}],"reason":"short routing reason"}

Required arguments: web_search requires {"query":"specific non-empty search phrase"}; never call it with {}.
The policy is binding. If it requires web search, online lookup, or current news, select web_search. For timestamp news, the query MUST be {"query":"recent news YYYY-MM-DD"}, copying the complete supplied calendar date exactly. A year-only or partial date is invalid. Use "name" and "args", never "type" or "arguments". Return JSON only.
"""

REQUIRED_TOOL_PLANNER_PLACEHOLDERS = (
    "{{SYSTEM_RESPONSE_POLICY}}",
    "{{CURRENT_INPUT}}",
)


def normalize_tool_planner_template(value: object) -> str:
    if isinstance(value, dict):
        value = value.get("template", DEFAULT_TOOL_PLANNER_TEMPLATE)
    template = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return template[:20000].rstrip()


def validate_tool_planner_template(template: str) -> None:
    missing = [token for token in REQUIRED_TOOL_PLANNER_PLACEHOLDERS if token not in template]
    if missing:
        raise ValueError(f"Planner template is missing required placeholder(s): {', '.join(missing)}")


def read_tool_planner_template(path: str | Path) -> str:
    config_path = Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        template = normalize_tool_planner_template(payload)
        validate_tool_planner_template(template)
        return template
    except Exception:
        return DEFAULT_TOOL_PLANNER_TEMPLATE


def write_tool_planner_template(path: str | Path, value: object) -> dict:
    config_path = Path(path)
    template = normalize_tool_planner_template(value)
    validate_tool_planner_template(template)
    payload = {"template": template, "updated_at": time.time()}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary_path.replace(config_path)
    return {**payload, "config_file": str(config_path.resolve())}


def render_tool_planner_template(template: str, values: dict[str, object]) -> str:
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", str(value or ""))
    return rendered

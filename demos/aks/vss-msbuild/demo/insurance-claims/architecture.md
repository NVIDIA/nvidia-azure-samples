# Architecture — Insurance Claims Triage Demo

## What it shows

A **Microsoft Foundry master agent** orchestrates an end-to-end car-insurance
claim from a single phone video. The video understanding is delegated to the
**NVIDIA VSS Agent** as a specialized sub-agent (wrapped via MCP). The
policy / pricing / document parts are mocked as Python function tools so the
demo runs without a real CRM or pricing backend.

## Sequence

```
Customer                 Master agent           VSS (MCP)              Mock tools (Python)
  │  upload video.mp4         │                    │                          │
  │ ───────────────────────▶  │                    │                          │
  │                           │  vss_upload_video  │                          │
  │                           │ ─────────────────▶ │                          │
  │                           │ ◀───── {upload_url, video_id} ─────────────── │
  │  PUT bytes to VST                                                         │
  │ ─────────────────────────────────────────────▶ │                          │
  │                           │  vss_analyze_video │                          │
  │                           │ ─────────────────▶ │ (calls cosmos VLM        │
  │                           │                    │  through /chat/stream)   │
  │                           │ ◀── {vin, damage} ─│                          │
  │                           │  lookup_policy(vin)                           │
  │                           │ ─────────────────────────────────────────▶   │
  │                           │ ◀────────────── policy details ────────────  │
  │                           │  estimate_repair_cost(damage_items)           │
  │                           │ ─────────────────────────────────────────▶   │
  │                           │ ◀──── {line_items, grand_total} ──────────── │
  │                           │  draft_claim_pdf(...)                         │
  │                           │ ─────────────────────────────────────────▶   │
  │                           │ ◀────────────── {path: ...pdf} ───────────── │
  │ ◀── "Claim CLM-... drafted. Payable $4250. Pending adjuster review." ──  │
```

## Why VSS is the right sub-agent here

The master agent (Claude / GPT-4o / Foundry-hosted model) is a strong text
reasoner but it has no eyes for video. VSS provides:

- **Frame extraction + spatial reasoning** through cosmos-reason2-8b (VLM)
- **Long-video summarization** through the LVS profile (lvs-server) — relevant
  when claims include multi-minute walk-around videos
- **Structured output** when prompted with a JSON schema (we use this to
  return `{vin, damage[{panel, severity, notes}]}` directly consumable by
  the pricing tool)
- **Video storage (VST)** with presigned upload URLs — no need to base64 a
  50 MB video through the chat API

## Why MCP is the right integration shape

- **One source of truth** for the VSS contract — same MCP server can be
  registered with Foundry, Claude Desktop, OpenAI Agents SDK, Cursor, etc.
- **Foundry-native** — Foundry's hosted-agent runtime starts the MCP server
  in the agent's container at boot, routes tool calls automatically, captures
  traces for evals.
- **Easy to extend** — adding `vss_list_videos`, `vss_get_clip`, or
  `vss_search_alerts` is a few-line change to `vss_mcp_server.py`.

## Mapping to Foundry concepts

| Demo file | Foundry concept |
|---|---|
| [agent.yaml](agent.yaml) | Hosted-agent definition (model, tools, instructions) |
| [agent_instructions.md](agent_instructions.md) | Agent system prompt (kept separate so prompt-optimizer can iterate on it) |
| [vss_mcp_server.py](vss_mcp_server.py) | MCP tool — registered as `type: mcp` in agent.yaml |
| [tools.py](tools.py) | Function tools — registered as `type: function` in agent.yaml |
| [sample_data/policies.json](sample_data/policies.json) | Mock data layer (replace with Cosmos DB / Salesforce in prod) |
| [local_runner.py](local_runner.py) | Pre-Foundry smoke test using Anthropic SDK |

## Extensibility ideas

- **Real CRM**: replace `lookup_policy` with a Salesforce / Dynamics adapter
- **Real pricing**: hit a CCC / Mitchell parts API in `estimate_repair_cost`
- **Adjuster handoff**: add an `email_adjuster(claim_path)` tool that uses
  Foundry's Logic Apps connector
- **Confidence scoring**: have VSS return `confidence: 0..1` per damage item,
  flag low-confidence items for manual review in the PDF
- **Multi-modal evidence**: accept police reports / repair invoices as
  additional inputs and use a doc-intel sub-agent alongside VSS

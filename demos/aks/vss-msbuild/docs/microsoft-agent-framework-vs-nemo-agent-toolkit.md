# MS Agent Framework + Foundry vs. NeMo Agent Toolkit (NAT)

**Status:** No migration committed — captured here for future reference.
**Decided on:** 2026-05-18
**Owner:** NVIDIA CSP GTM SA / vss-msbuild workshop

---

## TL;DR

For the workshop's existing single hosted agent, **stay with Microsoft
Agent Framework + Foundry**. The migration to NeMo Agent Toolkit (NAT)
only pays off in two specific scenarios: (a) you want to compose VSS's
internal NAT functions directly (skipping the HTTP/chat-stream layer),
or (b) you're building an eval suite where NAT's first-class eval
framework shines. Otherwise it's pure rewrite cost.

There's a middle path that captures most of the upside without
abandoning Foundry — see [Depth levels](#depth-levels-of-nat-adoption)
below.

---

## Context

### What we have today

The hosted agent at
[demo/insurance-claims-foundry/.../main.py](../demo/insurance-claims-foundry/src/agent-framework-agent-basic-responses/main.py)
uses Microsoft Agent Framework with the Foundry Chat Client:

```python
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer

client = FoundryChatClient(
    project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    model=os.environ["AZURE_OPENAI_DEPLOYMENT"],   # gpt-4.1
    credential=DefaultAzureCredential(),
)
agent = Agent(client=client, instructions=..., tools=[vss_analyze_video, ...])
server = ResponsesHostServer(agent)
server.run()
```

Deployed via `azd ai agent deploy`, runs on Foundry hosted agents,
monitored via Application Insights.

### What NAT is

NVIDIA's NeMo Agent Toolkit. The framework that **VSS itself is built on**
([video-search-and-summarization/agent/src/vss_agents/](../video-search-and-summarization/agent/src/vss_agents/),
gitignored — see workshop main [README](../README.md)). NAT agents are
declarative-YAML + Python tool functions, packaged as FastAPI servers:

```yaml
# config.yml (NAT)
workflow:
  type: react
  llm: nemotron-nano-9b-v2     # or gpt-4.1, swappable
  tools:
    - lvs_video_understand     # VSS's VLM tool, direct
    - lookup_policy
    - estimate_repair_cost
    - draft_claim_pdf
```

---

## Side-by-side

| | **Current: MS Agent Framework + Foundry SDK** | **NeMo Agent Toolkit (NAT)** |
|---|---|---|
| **Native to Foundry hosted-agent platform** | ✅ `azd ai agent deploy`, `agent.yaml`, Responses protocol — built around it. | ❌ Not native. NAT agents are FastAPI servers. Would need a shim to speak Foundry's Responses protocol, **or** register as a "Custom Agent" (Foundry proxies to your own host). |
| **LLM integration** | `FoundryChatClient(project_endpoint=..., model="gpt-4.1")` — pure MS path. | Provider-agnostic. Azure OpenAI ↔ NVIDIA NIMs ↔ local Ollama via config change. |
| **VSS integration shape** | HTTP/MCP wrapper. `tools.vss_analyze_video()` calls `POST /chat/stream`. ~10% of VSS's surface area. | Native. VSS *is* a NAT app. If you build with NAT you can compose `lvs_video_understand`, `vst_files`, `video_clip`, `multi_report_agent`, `critic_agent` directly — no HTTP boundary. |
| **Workflow definition** | Code-driven. Branching is "ask the LLM to pick". | Declarative YAML. Built-in patterns: ReAct, ReWOO, plan-and-execute, supervisor. |
| **Evaluation** | None built-in. We wrote a pytest harness in [demo/insurance-claims-foundry/tests/](../demo/insurance-claims-foundry/tests/). | First-class. NAT ships an eval framework with dataset configs, profilers, accuracy/latency/cost metrics. |
| **Observability** | App Insights + OpenTelemetry, wired by Foundry. | OpenTelemetry compatible. Less Microsoft-tooling-native — App Insights plumbing is manual. |
| **Mock mode / dev affordances** | `MOCK_VSS` env on the agent (works today, commit 25718e6). | NAT has its own mock-tool patterns; the work doesn't transfer cleanly. |
| **Migration cost** | 0 — you're there. | Non-trivial. Each tool rewritten as a NAT `@register_function`. Hosting path becomes "Custom Agent" registration or a shim. |
| **Ecosystem fit** | Microsoft (azd, Bicep, ACR, Container Apps, App Insights, Entra). The workshop runs on Azure. | NVIDIA (NIMs, Triton, eval, profiling). Strong in GPU/NIM environments. |
| **Maturity for hosted-agent deployment** | Mature; recommended path for Foundry hosted agents. | Mature for *building* NAT agents; less precedent for *deploying* them as Foundry hosted agents specifically. |

---

## When NAT actually pays off

| You want to... | Move to NAT? |
|---|---|
| Keep this exact triage agent running on Foundry | ❌ Stay with MS Agent Framework. You're aligned with the platform's grain. |
| Add NAT-style eval (datasets, accuracy benchmarks, ROC curves) on top of pytest | ⚖️ Worth NAT if eval is genuinely on the roadmap; otherwise overkill. |
| Embed VSS's internals as first-class tools (skip the HTTP layer) | ✅ Strong fit. NAT-native means you call `lvs_video_understand` directly, no MCP/HTTP/JSON-streaming dance. |
| Use the agent across multiple LLM providers (NIMs, OpenAI, local) | ✅ NAT's LLM abstraction shines. |
| Build a more complex workflow with branching/supervisor patterns | ✅ NAT's config-driven workflow is purpose-built for this. |
| Stay on Foundry hosted agents with `azd deploy` + minimal infra | ❌ The MS-native path is much smoother for the platform mechanics. |

---

## Depth levels of NAT adoption

If the "deeper VSS integration" angle is what's pulling, three discrete
levels with very different cost/benefit profiles:

### Level 1 — Expose more VSS HTTP tools to the existing MS Agent Framework master

Add more `@tool` functions to [tools.py](../demo/insurance-claims-foundry/src/agent-framework-agent-basic-responses/tools.py)
that hit VSS endpoints we don't use today:

```python
@tool def vss_extract_clip(video_id, start_s, end_s) -> str
@tool def vss_search_videos(query, time_range) -> list[dict]
@tool def vss_multi_video(video_ids) -> str
```

- **What changes:** code in `tools.py` only, agent still in Foundry, no NAT involved.
- **What you unlock:** multi-video walkarounds (front + rear), clip extraction into PDFs, "search similar past claims".
- **Effort:** 1–2 days.
- **Architecture impact:** zero.
- **Verdict:** ✅ Strongest ROI. Unlocks ~80% of "deeper VSS integration" without committing to NAT.

### Level 2 — NAT sub-pipeline on AKS that the Foundry agent calls

Build a *small* NAT app on the workshop AKS cluster that does
claims-specific orchestration using VSS's internal NAT functions
directly. The Foundry agent gets **one new tool** that wraps it:

```yaml
# claims-vss-pipeline/config.yml
workflow:
  type: react
  llm: nemotron-nano-9b-v2     # local, free (already deployed)
  tools:
    - lvs_video_understand
    - vst_files
    - video_clip
  prompt: |
    Given a claim's videos, produce a structured damage report with:
    - per-video damage list (panel + severity)
    - extracted clips of each damage area
    - cross-video consistency check
```

- **What changes:** new Helm chart on AKS alongside VSS; one new `@tool` in the Foundry agent that calls the NAT pipeline.
- **What you unlock:** structured damage reports, multi-video correlation, on-cluster orchestration using `nemotron-nano-9b-v2` (already running) for free LLM cycles.
- **Effort:** ~3–5 days.
- **Architecture impact:** moderate. New AKS workload + one new tool. Foundry agent's role becomes "user-facing orchestrator", with VSS reasoning offloaded to the NAT sub-pipeline.
- **Verdict:** ⚖️ Sweet spot if you want NAT learning without abandoning Foundry. Real use of NAT internals, Foundry still hosts the user-facing agent.

### Level 3 — Full rewrite as NAT on AKS, registered as Foundry Custom Agent

Rewrite `insurance-claims-triage` end-to-end as a NAT workflow with all 4
tools defined NAT-style. Hosted on AKS (same cluster as VSS — in-process
composition). Foundry's "Custom Agent" registration proxies traffic for
monitoring.

- **What changes:** rewrite `tools.py` + `main.py` as NAT; deploy as Helm chart on AKS; register in Foundry as Custom Agent.
- **What you unlock:** native VSS access, NAT eval framework, full workflow YAML, swappable LLMs.
- **What you lose:** `azd ai agent deploy`, Foundry-hosted-agent SDK, the simple env-var-based mock mode.
- **Effort:** ~1–2 weeks. Real architectural change.
- **Verdict:** 🚧 Only if you're committing to NAT as the primary framework long-term across several agents.

---

## Recommendation

**Don't migrate.** Current setup is working end-to-end:

- Deployed in Foundry, monitored, RBAC-correct, mock-mode wired
- Only 4 tools, simple orchestration — no NAT workflow complexity to unlock
- Eval needs covered by the pytest harness

Two scenarios that would flip the recommendation:

1. **You're going deeper on VSS** — multi-video, clip extraction, search. → Try **Level 1** first; it captures most of the value without NAT.
2. **You're building an eval suite** as a first-class concern. → **Level 2 or 3** justifiable, because NAT's eval is materially better than what we have.

---

## What this doc did with this decision

- Researched NAT's structure via the upstream VSS source we have locally.
- Compared trade-offs against the current MS Agent Framework deployment (this document).
- **No migration committed.** Captured the three depth levels here so the
  decision can be revisited deliberately, not by accident.

---

## See also

- [foundry-toolboxes-vs-in-agent-tools.md](./foundry-toolboxes-vs-in-agent-tools.md) — parallel comparison for MCP/Toolboxes
- [VSS upstream config (LVS profile)](../video-search-and-summarization/deployments/developer-workflow/dev-profile-lvs/vss-agent/configs/config.yml) — example NAT workflow YAML
- [CLAUDE.md](../CLAUDE.md) — overall repo conventions
- Commits on `main`:
  - `c81e9d6` — Mock VSS at the agent level (current implementation)
  - `25718e6` — Make MOCK_VSS the default + response marker

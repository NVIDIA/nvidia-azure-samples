# In-agent tools vs. Foundry MCP tools vs. Foundry Toolboxes

**Status:** Decided — stay with in-agent tools for now. Revisit if a second consumer appears.
**Decided on:** 2026-05-18 · **last updated** 2026-05-18 to add the Direct-MCP middle rung.
**Owner:** NVIDIA CSP GTM SA / vss-msbuild workshop

---

## TL;DR

For the workshop's single hosted agent, **stay with the in-agent function**.
Both alternatives — registering VSS directly as an MCP tool on the agent,
OR wrapping it in a Foundry Toolbox — add one more Container App to your
infra. The benefit (cross-agent reuse, centralized governance) only
materializes when a **second consumer** appears.

Foundry Tools and Foundry Toolboxes are **not the same thing**. A Toolbox
is a wrapper that bundles multiple tools into a single endpoint — it's
purely optional. You can register a single MCP server directly on the
agent without ever creating a Toolbox.

---

## Three rungs of the ladder

```
┌─ Rung 1: In-agent function (TODAY) ────────────────────┐
│   vss_analyze_video lives in tools.py inside the agent │
│   container. 1 deploy unit, 0 extra infra.             │
└────────────────────────────────────────────────────────┘
                         ▲
                         │ "I have a 2nd consumer for this tool"
                         │
┌─ Rung 2: Direct MCP tool ──────────────────────────────┐
│   MCP server in its own Container App, registered on   │
│   the agent via Tools → Add → Custom → MCP             │
│   (server_url + server_label). 2 deploy units.         │
└────────────────────────────────────────────────────────┘
                         ▲
                         │ "I have multiple tools to bundle / version together"
                         │
┌─ Rung 3: Foundry Toolbox (Preview) ────────────────────┐
│   Same as Rung 2 + a managed gateway that bundles 1..N │
│   MCP servers into one endpoint, with versioning and   │
│   per-toolbox RBAC. 2 deploy units + 1 logical layer.  │
└────────────────────────────────────────────────────────┘
```

---

## Context: what each option is

### Rung 1 — In-agent function (today)

`vss_analyze_video()` lives in
[demo/insurance-claims-foundry/.../tools.py](../demo/insurance-claims-foundry/src/agent-framework-agent-basic-responses/tools.py).
It's a `@tool`-decorated Python function inside the **insurance-claims-triage**
hosted-agent container. It either calls VSS's `/chat/stream` endpoint on
AKS (live mode) or reads a captured fixture from `fixtures/vss/<video_id>.txt`
(mock mode, controlled by `MOCK_VSS=true` in `agent.yaml`).

```
┌─ Foundry hosted agent: insurance-claims-triage ─┐
│   vss_analyze_video  ──HTTP──▶  VSS on AKS      │
│   lookup_policy      (local Python)             │
│   estimate_repair_cost (local Python)           │
│   draft_claim_pdf    (local Python)             │
└─────────────────────────────────────────────────┘
```

### Rung 2 — Direct MCP tool (no Toolbox)

Per [Microsoft Learn — Connect agents to Model Context Protocol servers](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol),
Foundry hosted agents can connect to **any public MCP server** by URL.
In the portal: `agent → Tools → Add → Custom → Model Context Protocol (MCP)`,
provide `server_url` and `server_label`. In `agent.yaml` / SDK code, it's
an `mcp` tool entry. No Toolbox involved.

```
┌─ Foundry hosted agent ─┐                ┌─ vss-mcp Container App ─┐
│   lookup_policy        │                │ vss_mcp_server.py       │
│   estimate / pdf       │                │ (HTTP/SSE transport)    │
│   vss_analyze_video ◀──┼──── MCP ──────▶│ honours MOCK_VSS env    │
└────────────────────────┘                └────────────┬────────────┘
                                                       │ HTTP (live mode)
                                                       ▼
                                                VSS on AKS
```

[demo/insurance-claims/vss_mcp_server.py](../demo/insurance-claims/vss_mcp_server.py)
is already 90% of the way there — needs HTTP/SSE transport instead of stdio.

### Rung 3 — Foundry Toolbox (Preview)

Per [Microsoft Learn — Curate intent-based toolbox in Foundry (preview)](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox),
a Toolbox is a managed MCP gateway that bundles multiple upstream MCP
servers / OpenAPI tools / A2A endpoints into a **single** MCP-compatible
endpoint. The Toolbox itself is versioned; consumers point at the Toolbox
URL, not at each individual MCP server.

```
┌─ Agent 1 ──┐        ┌─ Foundry Toolbox: vss-tools ──┐        ┌─ vss-mcp Container App ─┐
│ ...        │◀───────│   ├─ vss_analyze_video       ─┼────────│ vss_mcp_server.py       │
└────────────┘ MCP    │   └─ vss_upload_video        ─┤        └─────────────────────────┘
                      │                               │
┌─ Agent 2 ──┐        │   (could also bundle other   │        ┌─ fraud-detection MCP App ┐
│ ...        │◀───────┤    MCP servers / OpenAPI /  ◀┼────────│ (separate server)       │
└────────────┘ MCP    │    A2A here)                  │        └─────────────────────────┘
                      └───────────────────────────────┘
```

Only worth standing up when you have **multiple tools to bundle** or
**multiple agent consumers** that should pick up tool updates together.

---

## Side-by-side

| | **Rung 1: In-agent (today)** | **Rung 2: Direct MCP tool** | **Rung 3: Foundry Toolbox** |
|---|---|---|---|
| **Deploy units** | 1 (agent container) | 2 (agent + MCP-server container) | 2 + 1 logical Toolbox |
| **Latency per VSS call** | 1 hop (in-process → VSS) | 2 hops (agent → MCP server → VSS) | 3 hops (agent → Toolbox → MCP server → VSS) |
| **Reuse across agents / clients** | None — tool is local to this container | ✅ First-class. Any MCP-compatible client can connect to the MCP server URL | ✅ Same reuse, plus a single URL covers multiple tools (`vss_analyze_video`, `vss_upload_video`, future additions) |
| **Versioning** | Coupled to agent version — every change forces `azd deploy` of the agent | MCP server has its own versions; tool change ≠ agent change | Toolbox-level versions promote independently; bundles multiple servers' versions together |
| **Deploy independence** | Tool change forces full agent rebuild + redeploy | Tool can be updated independently of the agent | Same as Rung 2, plus the Toolbox bundles can be reconfigured without changing any individual server |
| **Governance / auditing** | Per-agent App Insights traces | Per-MCP-server traces; agent traces separately | Centralized in Foundry; per-tool RBAC; `require_approval` flag gates invocations |
| **Mock mode wiring** | `MOCK_VSS` env on the agent (works today; commit 25718e6) | `MOCK_VSS` env on the **MCP server** instead | `MOCK_VSS` env on the **MCP server** (Toolbox is transparent to it) |
| **Debugging surface** | One stack trace, one container | Errors can be in 3 layers: agent code, MCP server, VSS | Errors can be in 4 layers: agent code, Toolbox routing, MCP server, VSS |
| **API maturity** | Mature MS Agent Framework + Foundry SDK | Mature (MCP tool support is GA) | **Preview** — surface may change |
| **Infra cost** | 1 Container App (agent) | 2 Container Apps (agent + MCP server). ~$0 idle (scale-to-zero) | Same as Rung 2 + Toolbox is a managed Foundry resource (no extra container) |
| **Cognitive load** | Read `tools.py`. Done. | Read `tools.py` + MCP server code + agent.yaml MCP tool entry | Read `tools.py` + MCP server code + Toolbox YAML + agent's MCP client config pointing at the Toolbox endpoint |

---

## When each rung pays off

| Scenario | Best rung |
|---|---|
| Just this one insurance-claims agent, one VSS tool | 1 — Stay in-agent. Anything else is over-engineering. |
| A second agent (e.g., fraud detection) also needs VSS | 2 — Direct MCP. Single tool, two consumers. No need to bundle. |
| A second agent needs VSS **and** a different tool you also want to share | 3 — Toolbox lets you bundle both so each agent points at one URL. |
| Multi-team — another team consumes VSS via GitHub Copilot, Claude Desktop, IDE plugins | 2 or 3 — both work. Toolbox if you also want a single managed endpoint per team. |
| Want fine-grained per-tool governance (approval gates, audit) | 3 — Toolbox surfaces this natively. |
| Performance-critical path (sub-100ms requirement on the VSS call) | 1 — Extra hops hurt. |
| Workshop narrative depends on showing "Foundry can share tools" | 2 or 3 — Either tells the reuse story. Rung 2 is the simpler narrative. |
| You expect to ADD more tools to the bundle over time (e.g., fraud, KYC) | 3 — Toolbox versioning was built for exactly this. |

---

## Recommendation

**Don't migrate yet.** Current Rung-1 architecture works end-to-end:
deployed today (v1 hosted agent with `MOCK_VSS=true`, AKS shut down, mock
firing cleanly).

Both alternatives add one more Container App to operate. That's the real
cost — not Toolbox-vs-Direct-MCP, which only changes the wrapper.

When the trigger does appear (second consumer), the decision becomes:

- **Single tool, two consumers** → Rung 2 (Direct MCP). Skip the Toolbox.
- **Multiple tools you want to bundle** → Rung 3 (Toolbox). The bundling
  is the value-add.

### Prep-without-commit option

Keep [demo/insurance-claims/vss_mcp_server.py](../demo/insurance-claims/vss_mcp_server.py)
clean and lockstep with the foundry agent's `tools.py`. When the time
comes to move to Rung 2:

1. Add HTTP/SSE transport to `vss_mcp_server.py` (one-line FastMCP change).
2. Add a Dockerfile + Bicep for an `mcp-vss` Container App alongside
   the UI in `rg-insurance-claims-ui-dev`.
3. `azd deploy` the new MCP server, capture its public HTTPS URL.
4. In the Foundry portal: `agent → Tools → Add → Custom → MCP`. Provide
   `server_url` (the deployed URL) and `server_label` (e.g., `vss-tools`).
   Or via SDK: add an `mcp` tool entry to `agent.yaml`.
5. Remove `vss_analyze_video` from the hosted agent's `tools.py` (or
   leave it as a fallback for local testing).
6. Re-deploy the agent.

Going from Rung 2 → Rung 3 later is also straightforward: create a Toolbox
in the portal, point it at the same MCP server URL, then update the agent's
tool entry to reference the Toolbox endpoint instead. The MCP server itself
doesn't change.

Estimated effort for Rung-1 → Rung-2 when the trigger arrives: **1–2 days**.
Rung-2 → Rung-3 if needed later: **~half a day**.

---

## What this branch did with this decision

- Created `feat/register-vss-tool` branch.
- Researched Foundry Toolboxes (Preview) **and** the simpler Direct-MCP
  path that's been GA for a while ([docs](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol)).
- Compared trade-offs against the current architecture (this document).
- **Decided to abandon the migration.** Deleted the feature branch.
- Captured the decision here so future-you (or a colleague) doesn't have
  to re-do the research, and so the choice between Rung 2 vs Rung 3 isn't
  conflated when the trigger to migrate arrives.

---

## See also

- [Microsoft Learn — Connect agents to Model Context Protocol servers](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/model-context-protocol) — Rung 2 (Direct MCP)
- [Microsoft Learn — Curate intent-based toolbox in Foundry (preview)](https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox) — Rung 3 (Toolbox)
- [Microsoft Learn — Agent tools overview](https://learn.microsoft.com/azure/foundry/agents/concepts/tool-catalog#all-custom-tools) — full catalog (MCP, OpenAPI, A2A, Toolbox)
- [microsoft-agent-framework-vs-nemo-agent-toolkit.md](./microsoft-agent-framework-vs-nemo-agent-toolkit.md) — parallel comparison for the framework-level question
- [CLAUDE.md](../CLAUDE.md) — overall repo conventions
- Commits on `main`:
  - `c81e9d6` — Mock VSS at the agent level
  - `25718e6` — Make MOCK_VSS the default + response marker

---
description: Invoke the deployed Microsoft Foundry hosted agent (insurance-claims-triage) via `azd ai agent invoke`, then confirm whether live VSS or mock-VSS path fired via App Insights traces
argument-hint: "[prompt...]"
---

Hit the **deployed** insurance-claims-triage agent (running inside the
Foundry project) via `azd ai agent invoke` — no `--local`. Reports the
final response text plus the actual VSS code path (live HTTP call vs.
mock fixture read) by querying Application Insights traces.

## Argument

`$ARGUMENTS` is the prompt. If empty, falls back to the default smoke
prompt:

> A customer just submitted a damage video. video_id=toyota. If the VIN is
> not visible, use policy POL-2025-44912 as a fallback. Run the triage
> workflow.

(toyota is chosen because `fixtures/vss/toyota.txt` exists, so the prompt
exercises both code paths cleanly — live mode hits real VSS, mock mode
reads the fixture.)

## What to do

1. **Parse `$ARGUMENTS`** into `PROMPT`. Empty → default smoke prompt.

2. **`cd demo/insurance-claims-foundry`** — `azd ai agent invoke` (without
   `--local`) needs `azure.yaml` in cwd so it can resolve the deployed
   agent's endpoint from the azd env.

3. **Pre-flight: confirm the agent is actually deployed.** Check the azd
   env for the agent version:
   ```bash
   AGENT_VERSION=$(azd env get-values | grep '^AGENT_INSURANCE_CLAIMS_TRIAGE_VERSION=' | cut -d= -f2)
   echo "Deployed agent version: $AGENT_VERSION"
   ```
   If empty, bail: "no deployed agent found in azd env. Run
   `/deploy-foundry-agent` first."

4. **Snapshot the EXPECTED mode** from `agent.yaml`. This is what the most
   recent `azd deploy` baked into the agent's container env. Note this is
   the *intended* mode — if `agent.yaml` was edited but not re-deployed,
   the actually-running container may differ.
   ```bash
   EXPECTED_MOCK_VSS=$(grep -A1 'name: MOCK_VSS' \
     src/agent-framework-agent-basic-responses/agent.yaml \
     | tail -1 | sed 's/.*value: "//; s/".*//')
   echo "agent.yaml says MOCK_VSS=$EXPECTED_MOCK_VSS"
   ```
   If `agent.yaml` has uncommitted edits to `MOCK_VSS`, surface a clear
   warning that the deployed agent might still be running the previous
   value until the next `azd deploy`.

5. **Capture invoke start timestamp** (UTC, ISO 8601) so the App Insights
   query later filters to just this invocation:
   ```bash
   T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
   ```

6. **Invoke the deployed agent** with `--new-conversation` so we get a
   clean run (no leftover state from an earlier session):
   ```bash
   azd ai agent invoke --new-conversation "$PROMPT"
   ```
   This blocks until the agent loop completes (~30-60s for live VSS,
   ~5-15s for mock). Run in the background so the harness notifies on
   completion. Capture the full output — the final `[insurance-claims-triage] ...`
   block is the agent's response.

7. **Wait ~45s for App Insights ingestion.** Use background sleep so the
   harness notifies when the wait is over — telemetry takes 30-60s to
   become queryable.

8. **Query Application Insights traces** for the actual VSS code path
   used during this invocation. The agent's App Insights resource lives
   in `rg-insurance-claims-foundry-dev` as `appi-rux2wabbpeht4`:
   ```bash
   APPI_RESOURCE_ID=$(azd env get-values | grep '^APPLICATIONINSIGHTS_RESOURCE_ID=' | cut -d'"' -f2)

   az monitor app-insights query \
     --ids "$APPI_RESOURCE_ID" \
     --analytics-query "
       traces
       | where cloud_RoleName == 'insurance-claims-triage'
       | where timestamp >= datetime($T0)
       | where message contains 'vss_analyze_video'
          or message contains 'vss.104.45.71.11'
          or message contains 'chat/stream'
       | project timestamp, message
       | order by timestamp asc
     " -o table
   ```

9. **Interpret the trace evidence**:
   - If you see `httpx: HTTP Request: POST http://vss.104.45.71.11.nip.io/chat/stream` →
     **live VSS was called**. Time between
     `Function name: vss_analyze_video` and
     `Function vss_analyze_video succeeded` will be 10-30s.
   - If you only see `Function name: vss_analyze_video` and
     `Function vss_analyze_video succeeded` with no HTTP request line in
     between → **mock fired**. Timing will be milliseconds.
   - If no `vss_analyze_video` traces appear at all in the window →
     either ingestion hasn't caught up yet (wait another 30s and retry
     the query) or the agent never called the tool (the model decided
     not to invoke it — flag this as suspicious).

10. **Report to the user**:
    - The agent version invoked (`vN`).
    - The expected mode per agent.yaml (`MOCK_VSS=true|false`).
    - The full agent response text (final `[agent] …` block from step 6).
    - The verdict: "✓ Live VSS confirmed via App Insights" /
      "✓ Mock VSS confirmed (no HTTP call to vss.104.45.71.11)" /
      "⚠ Mode mismatch: agent.yaml expects mock but live VSS was hit
      (likely the deployed revision is stale — `azd deploy` to refresh)".
    - The portal trace link printed by `azd ai agent invoke` (the
      `Trace ID: ...` line) so the user can drill into the full
      App Insights timeline if needed.

## Notes

- This command **does not modify** the deployed agent. To change MOCK_VSS
  on the deployed agent, edit `agent.yaml` and run `/deploy-foundry-agent`
  — Foundry hosted agents bake env vars at deploy time, there's no
  runtime knob.
- Cost per invoke: ~$0.05-0.10 in gpt-4.1 tokens for the orchestration,
  plus the VSS call in live mode (free on the workshop AKS cluster, but
  the cluster has to be running). Mock mode is just the LLM cost.
- The App Insights query may return empty if ingestion lags. The 45s
  wait in step 7 is usually enough; if not, re-run step 8 a minute
  later — invoke results are persisted, so re-querying is safe.
- Companion commands:
  - `/test-foundry-agent-local` — same kind of test against the
    `azd ai agent run` local subprocess (no deploy needed; uses
    on-disk code + .azure/<env>/.env for MOCK_VSS).
  - `/deploy-foundry-agent` — production deploy of the same agent.
  - `/deploy-ui` — the Streamlit Container App that calls this agent.
- See [CLAUDE.md](CLAUDE.md) for the wider context including the
  `azure.yaml` ↔ `agent.yaml` name-match trap and the `gpt-4.1` model
  pin.

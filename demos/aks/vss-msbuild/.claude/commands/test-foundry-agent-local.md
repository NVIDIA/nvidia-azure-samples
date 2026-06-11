---
description: Spin up the Microsoft Foundry hosted agent locally (azd ai agent run) in live or mock-VSS mode, invoke a smoke prompt, verify the right path fired, then stop
argument-hint: "[live|mock] [prompt...]"
---

Run the **insurance-claims-triage** agent locally via `azd ai agent run`,
hit it with a smoke prompt via `azd ai agent invoke --local`, and verify
the expected code path fired (live → real VSS HTTP call; mock → fixture
file read, no network). Always stops the local server on exit.

## Argument

`$ARGUMENTS` is parsed lazily:

- empty → `MODE=live`, prompt = default smoke prompt
- starts with `live` → `MODE=live`, prompt = remainder (if any) or default
- starts with `mock` → `MODE=mock`, prompt = remainder (if any) or default
- anything else → treat whole `$ARGUMENTS` as a custom prompt in `MODE=live`

Default smoke prompt: `"A customer just submitted a damage video. video_id=toyota. If the VIN is not visible, use policy POL-2025-44912 as a fallback. Run the triage workflow."` (chosen because toyota.txt is a captured fixture, so it works in both modes).

## What to do

1. **Parse `$ARGUMENTS`** into `MODE` (`live`|`mock`) and `PROMPT`.

2. **`cd demo/insurance-claims-foundry`** — `azd ai agent run` needs
   `azure.yaml` in cwd.

3. **Set MOCK_VSS in the azd env** — this is the part that actually
   matters; `agent.yaml`'s `environment_variables:` block is consumed
   *only* by the deployed agent, NOT by `azd ai agent run`. The local
   subprocess gets its env from `.azure/<env>/.env` via `load_dotenv()`
   in `main.py`. Use `azd env set` to write it:
   ```bash
   azd env set MOCK_VSS "$([ "$MODE" = mock ] && echo true || echo false)"
   ```
   Confirm it took effect:
   ```bash
   azd env get-values | grep '^MOCK_VSS'
   ```

4. **Pre-flight checks**:
   - For `MODE=mock`: ensure at least one `fixtures/vss/*.txt` file exists
     under `src/agent-framework-agent-basic-responses/fixtures/vss/`. The
     mock returns a generic fallback string for unknown video_ids — if
     the test prompt references one (e.g., `toyota`), confirm
     `fixtures/vss/toyota.txt` exists or warn the user.
   - For `MODE=live`: ping the VSS endpoint (`VSS_BASE_URL` from the env)
     to make sure the AKS cluster is up. If `curl -s -o /dev/null -w
     "%{http_code}" $VSS_BASE_URL/` is non-2xx/3xx, bail with a clear
     message — there's no point in continuing.

5. **Kill any stale local agent** on port 8088:
   ```bash
   pkill -f "python main.py" 2>/dev/null
   sleep 1
   ```

6. **Start the agent in the background**:
   ```bash
   azd ai agent run
   ```
   Use background execution so the harness notifies on the *child*
   process completion. Capture the output path; we'll grep it later
   for the smoking-gun HTTP line (or its absence).

7. **Wait until :8088 is bound**:
   ```bash
   until curl -s -o /dev/null --connect-timeout 1 http://localhost:8088/; do sleep 2; done
   ```
   Run this in the background with a sane timeout (~180s — first run
   does a venv install + pip resolve, normally ~30s). Don't proceed
   until this exits.

8. **Invoke the agent** with the resolved prompt (use background again
   so the harness notifies on completion — `azd ai agent invoke --local`
   blocks until the agent loop finishes, typically 5-15s for mock,
   30-60s for live):
   ```bash
   azd ai agent invoke --local "$PROMPT"
   ```

9. **Inspect the agent server log** for the mock vs. live signal:
   - `grep -i "vss.104.45.71.11" <agent_log>` should match for **live**
     and not match for **mock**.
   - `grep "Function name: vss_analyze_video" <agent_log>` should match
     in **both** (the tool was called either way).
   - Also note the timestamp delta between
     `Function name: vss_analyze_video` and
     `Function vss_analyze_video succeeded` — should be milliseconds in
     mock mode (file read), 10-30s in live mode (VSS round-trip).

10. **Stop the local agent**:
    ```bash
    pkill -f "python main.py" 2>/dev/null
    ```

11. **Report to the user**:
    - The mode (`live` or `mock`) + the prompt used.
    - The agent's final response text (last `[local] ...` block).
    - The verdict: "Mock fired ✓ (no live VSS HTTP, vss_analyze_video
      returned in <Nms>)" OR "Live fired ✓ (POST to vss.104.45.71.11
      observed, vss_analyze_video took <Ns>)" — pick the one that
      matches the requested mode. If the wrong path fired, explicitly
      flag it (e.g., "Requested mock but live VSS was hit — check
      `azd env get-values | grep MOCK_VSS`").
    - Any tool calls that failed (rare; usually `lookup_policy` or
      `estimate_repair_cost` errors due to schema drift).

## Notes

- This command **doesn't touch the deployed agent**. It exercises only
  the local subprocess started by `azd ai agent run`, which uses the
  on-disk code + the azd env file. Useful for iterating on `tools.py`,
  `instructions.md`, or testing the MOCK_VSS branch without paying for
  another `azd deploy`.
- The azd env's `MOCK_VSS` value persists across runs. After running
  this command in `mock` mode, subsequent `azd ai agent run`
  invocations (even outside this command) will still mock VSS until
  you run with `MODE=live` or manually `azd env set MOCK_VSS false`.
  Mention this in the report so the user isn't surprised.
- For the deployed agent's mode, see `agent.yaml` `environment_variables:`
  → `MOCK_VSS`. Default in main is `"false"`. Flip to `"true"` and
  redeploy via `/deploy-foundry-agent` for a mock-VSS production
  revision (useful when AKS is shut down for cost savings).
- Companion command: `/deploy-foundry-agent` for the production deploy
  flow, `/deploy-ui` for the Streamlit UI Container App.
- Fixtures live at
  `demo/insurance-claims-foundry/src/agent-framework-agent-basic-responses/fixtures/vss/<video_id>.txt`
  and (in lockstep) `demo/insurance-claims/fixtures/vss/<video_id>.txt`.
  Re-capture via `python scripts/capture_vss_fixtures.py` after any
  upstream VSS change.

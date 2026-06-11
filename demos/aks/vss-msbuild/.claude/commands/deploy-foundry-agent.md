---
description: Deploy the Microsoft Foundry hosted agent (insurance-claims-triage) in live or mock-VSS mode, then smoke-test the new version
argument-hint: "[live|mock] [prompt...]"
---

Deploy the **Microsoft Foundry hosted agent** (`insurance-claims-triage`) from
[demo/insurance-claims-foundry/](demo/insurance-claims-foundry/), then verify
that the new version actually shipped.

## Argument

`$ARGUMENTS` is parsed lazily (same shape as `/test-foundry-agent-local`):

- empty → `MODE=live`, smoke prompt = default
- starts with `live` → `MODE=live`, smoke prompt = remainder (if any) or default
- starts with `mock` → `MODE=mock`, smoke prompt = remainder (if any) or default
- anything else → treat the whole `$ARGUMENTS` as a custom smoke prompt in
  `MODE=live`

In `mock` mode the deployed agent **skips the VSS HTTP call entirely** —
`tools.vss_analyze_video()` reads canned damage prose from
`fixtures/vss/<video_id>.txt` instead. Keeps the agent working when the
AKS GPU cluster is stopped for cost savings. The agent still calls the
LLM, so model tokens cost per invoke; only AKS goes idle.

Default smoke prompt: `"Look up policy POL-2025-44912. What vehicle is on that policy?"` (pure CRM lookup — no VSS involvement — exercises the deploy itself; for mock-vs-live verification of `vss_analyze_video`, use `/test-foundry-agent-remote` separately after the deploy.)

## What to do

1. **Parse `$ARGUMENTS`** into `MODE` (`live`|`mock`) and `PROMPT`.

2. **`cd demo/insurance-claims-foundry`** (the azd project root — `azure.yaml`
   must be in cwd or `azd` errors with "no project exists").

3. **Snapshot the *current* MOCK_VSS value** in `agent.yaml` so we can report
   the before→after transition (and detect a no-op deploy):
   ```bash
   CURRENT_MODE_VAL=$(grep -A1 'name: MOCK_VSS' \
     src/agent-framework-agent-basic-responses/agent.yaml \
     | tail -1 | sed 's/.*value: "//; s/".*//')
   echo "agent.yaml currently has MOCK_VSS=\"$CURRENT_MODE_VAL\""
   ```
   Map `MODE` to the desired value:
   - `MODE=live` → `DESIRED="false"`
   - `MODE=mock` → `DESIRED="true"`

4. **Set MOCK_VSS to DESIRED in agent.yaml.** This is the source of truth
   for the *deployed* agent — Foundry hosted agents bake `environment_variables`
   into the container at deploy time, so flipping the value in `agent.yaml`
   + `azd deploy` is the only way to switch the deployed mode (env vars
   set via `azd env set` are NOT injected into the hosted container — only
   into `azd ai agent run` local subprocesses).
   ```bash
   # Edit src/agent-framework-agent-basic-responses/agent.yaml so the
   # MOCK_VSS entry has value: "$DESIRED" (in quotes).
   ```
   If `agent.yaml` was already at `DESIRED`, note that the env-var step is
   a no-op (but the deploy still proceeds in case other code changed).
   **Leave the edit on disk** after the command — the committed
   `agent.yaml` should reflect what's deployed. The user can `git add` it
   to make `MODE` the new default, or `git checkout` it to revert.

5. **Capture the current agent version** so we can confirm the bump:
   ```bash
   V_BEFORE=$(azd env get-values | grep AGENT_INSURANCE_CLAIMS_TRIAGE_VERSION | cut -d= -f2)
   echo "Before: agent version=$V_BEFORE  MOCK_VSS=$CURRENT_MODE_VAL"
   ```

6. **Pre-flight check**: the postdeploy RBAC hook will 404 (and every future
   invoke will return `PermissionDenied`) if `azure.yaml`'s `services.<name>:`
   key drifts from `agent.yaml`'s top-level `name:`. Both should be
   `insurance-claims-triage`. Bail out if they don't match.

7. **For `MODE=mock`**: confirm at least one fixture exists under
   `src/agent-framework-agent-basic-responses/fixtures/vss/*.txt`, and that
   `tools.py` actually contains the `if MOCK_VSS:` branch (grep for it).
   Without those, deploying with `MOCK_VSS=true` would silently break
   `vss_analyze_video` because the code path isn't in `tools.py` on this
   branch.

8. **Run the deploy** (long-running, ~2 min — use background execution so the
   harness notifies on completion rather than polling):
   ```bash
   azd deploy
   ```
   Watch for the `SUCCESS: Your application was deployed...` line. If you see
   `ContainerAppOperationError` or `failed to fetch agent version`, do NOT
   re-deploy automatically — surface the error to the user.

9. **Verify the version bumped**:
   ```bash
   azd env get-values | grep AGENT_INSURANCE_CLAIMS_TRIAGE_VERSION
   ```
   Should be `V_BEFORE + 1`. If it didn't change, deploy silently no-op'd —
   investigate before declaring success.

10. **Smoke-test the deployed agent** (not `--local`):
    ```bash
    azd ai agent invoke --new-conversation "$PROMPT"
    ```

11. **Report to the user**:
    - The mode transition (`MOCK_VSS: "$CURRENT_MODE_VAL" → "$DESIRED"`).
    - The version bump (`vN → vN+1`).
    - The full agent response text.
    - The portal playground URL (printed by `azd deploy`) and the Responses
      endpoint URL — both as fully-qualified `https://` links.
    - Whether `agent.yaml` is now modified on disk (it will be if the
      MOCK_VSS value changed). Remind the user they can `git add` to
      commit the new default or `git checkout` to revert.
    - For mode-related deploys, suggest `/test-foundry-agent-remote` as
      the follow-up verifier (it queries App Insights to confirm whether
      the live VSS HTTP call actually fired or the mock fixture-read
      took over).
    - Drift check: if the smoke-test asked about the CRM and the answer
      doesn't match `sample_data/policies.json`, the deploy didn't pick
      up the latest code — surface that explicitly.

## Notes

- The hosted-agent platform requires the caller (you / the azd identity) to
  have either Owner or User Access Administrator at sub scope, plus
  Contributor for provision. The current azd env (`insurance-claims-foundry-dev`)
  was originally provisioned by the user under their CLI session — if PIM
  has expired, this command will hit "Authorization failed" errors and you
  need to re-activate PIM first.
- For local iteration without deploying, use `/test-foundry-agent-local`
  which spins up `azd ai agent run`. Different mechanism — local uses
  `azd env set MOCK_VSS …` (the local subprocess reads `.azure/<env>/.env`),
  the hosted agent uses `agent.yaml` (baked into the deployed container).
- Companion command `/deploy-ui` redeploys the Streamlit Container App when
  the UI code changes — keep them in sync when both have updates.
- See [CLAUDE.md](CLAUDE.md) for the wider deploy context: the standardized
  `AZURE_OPENAI_DEPLOYMENT` env var, the gpt-4.1 model choice, and the
  cross-RG role assignment from the UI deployment.

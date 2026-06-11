---
description: Deploy the Streamlit customer-rep UI (Azure Container App) in live or offline mode, then smoke-test the live URL
argument-hint: "[live|offline]"
---

Deploy the **Streamlit customer-rep UI** as an Azure Container App from
[demo/insurance-claims-ui/](demo/insurance-claims-ui/), then verify the live
URL is responding and the new revision picked up.

## Argument

`$ARGUMENTS` controls the boot-time mode of the deployed UI:

- **`live`** *(default — when `$ARGUMENTS` is empty or `live`)* — sets
  `OFFLINE_MODE=false`. Real VSS upload + Foundry agent calls. Use when
  the AKS GPU cluster is up and the live Foundry agent is healthy.
- **`offline`** — sets `OFFLINE_MODE=true`. UI boots with the offline
  toggle pre-flipped: VSS / Foundry calls are skipped, canned responses
  from `fixtures/<policy>.json` are replayed with simulated delays. Use
  when AKS is shut down for cost.
- Any other value — refuse with a clear error and ask for `live` or
  `offline`. Don't guess.

In **both** modes the Settings expander still lets a rep toggle at
runtime — this argument only seeds the default.

## What to do

1. **Parse `$ARGUMENTS`** into `MODE` and `OFFLINE_FLAG`:
   - empty or `live` → `MODE=live`, `OFFLINE_FLAG=false`
   - `offline` → `MODE=offline`, `OFFLINE_FLAG=true`
   - anything else → fail with `"Unknown mode: <arg>. Expected 'live' or 'offline'."`

2. **`cd demo/insurance-claims-ui`** (the azd project root — `azure.yaml`
   must be in cwd or `azd` errors with "no project exists").

3. **Verify the azd env exists** (it should be `insurance-claims-ui-dev`
   from the initial provision):
   ```bash
   azd env list
   ```
   If it doesn't exist, the UI hasn't been provisioned yet — bail out and
   tell the user to run the full `azd up` flow (or invoke the
   `azure:azure-prepare` skill) before this command can be useful.

4. **Pre-flight checks**:
   - Confirm the Dockerfile copies `sample-videos/` AND `fixtures/`
     (offline mode breaks silently in the cloud if `fixtures/` isn't shipped).
   - Confirm the `policies.json` paths in `sample_video` fields point at
     `sample-videos/<name>.mp4` (relative to the Dockerfile WORKDIR).
   - For `MODE=offline`: confirm at least one fixture exists under
     `fixtures/<policy>.json` so the deployed UI is actually demoable.

5. **Capture the current `OFFLINE_MODE` value** and active revision so we
   can report the before/after state:
   ```bash
   CURRENT_MODE=$(azd env get-values | grep '^OFFLINE_MODE=' | cut -d'"' -f2)
   CURRENT_REV=$(az containerapp revision list \
     -g rg-insurance-claims-ui-dev \
     -n $(azd env get-values | grep SERVICE_UI_NAME | cut -d'"' -f2) \
     --query "[?properties.active].name" -o tsv | head -1)
   echo "Before: OFFLINE_MODE=$CURRENT_MODE  rev=$CURRENT_REV"
   ```

6. **Set `OFFLINE_MODE` to the requested value**:
   ```bash
   azd env set OFFLINE_MODE "$OFFLINE_FLAG"
   ```

7. **Run `azd provision`** (long-running, ~30-60s — pushes the
   `OFFLINE_MODE` env var change onto the existing Container App without
   recreating it). Run in the background so the harness notifies on
   completion:
   ```bash
   azd provision --no-prompt
   ```
   Watch for `SUCCESS: Your application was provisioned in Azure...`.

8. **Run `azd deploy`** (long-running, ~2-3 min for ACR remote build +
   revision roll — also use background execution):
   ```bash
   azd deploy --no-prompt
   ```
   Watch for `SUCCESS: Your application was deployed...` and capture the
   `Endpoint:` URL printed below it.

9. **Verify the new revision activated**:
   ```bash
   az containerapp revision list \
     -g rg-insurance-claims-ui-dev \
     -n <SERVICE_UI_NAME> \
     --query "[?properties.active].name" -o tsv
   ```
   Should differ from `$CURRENT_REV`. If not, the deploy didn't roll a new
   revision (image identical to previous, or revision-mode misconfigured) —
   investigate before declaring success.

10. **Verify the `OFFLINE_MODE` env var on the live Container App**
    matches the requested mode:
    ```bash
    az containerapp show -g rg-insurance-claims-ui-dev \
      -n $(azd env get-values | grep SERVICE_UI_NAME | cut -d'"' -f2) \
      --query "properties.template.containers[0].env[?name=='OFFLINE_MODE'].value | [0]" -o tsv
    ```
    Should be `true` if `MODE=offline`, `false` otherwise.

11. **Smoke-test the live URL**:
    ```bash
    URL=$(azd env get-values | grep SERVICE_UI_URI | cut -d'"' -f2)
    curl -s -o /dev/null -w "root: HTTP %{http_code}\n" --max-time 30 "$URL"
    curl -s --max-time 30 "$URL/_stcore/health"   # Streamlit health probe — should return "ok"
    ```
    - HTTP 200 on root = ingress + container alive
    - `/_stcore/health` returning `ok` = Streamlit process is up
    - **Cold-start note**: scale-to-zero is enabled, so the first request
      after idle may take ~5-10s. Don't treat one slow response as a
      failure — retry once before flagging.

12. **Report to the user**:
    - The mode change (`OFFLINE_MODE: <old> → <new>`)
    - The revision change (`<old> → <new>`)
    - The live HTTPS URL (always prefix `https://`)
    - HTTP status + Streamlit health-probe result
    - For `MODE=offline`: remind the user the persistent `🧪 OFFLINE MODE`
      warning banner should appear at the top of the page, and the
      `Upload my own` radio is hidden in favor of bundled samples only.
    - For `MODE=live`: remind the user the banner should be absent and
      both bundled + upload paths are available.
    - A reminder that `policies.json` and `fixtures/` are baked into the
      image — if they edited those locally but didn't see the change,
      they need another `/deploy-ui <mode>` (this command, again).

## Notes

- Behind the scenes the offline default is wired via a Bicep param
  `offlineMode bool` → Container App env `OFFLINE_MODE=true|false`. The
  Python app reads `os.environ.get("OFFLINE_MODE")` once at startup to
  seed `st.session_state.offline_mode`, then the rep can flip per-session
  via the ⚙️ Settings expander.
- Fixtures (`fixtures/<policy>.json`) are captured against the **deployed**
  Foundry agent by `scripts/capture_fixtures.py`. Re-run that script after
  any change to `agent_instructions.md`, `tools.py`, or `policies.json` so
  the canned offline responses stay in sync with what the live agent would
  produce.
- The UI calls the Foundry hosted agent via the container's user-assigned
  managed identity (system-assigned MI failed with a 20-min ACR token race —
  see `feedback-aca-acr-bootstrap` in memory and the resources.bicep
  comments). The MI has `Foundry User` on `ai-account-rux2wabbpeht4` via
  the cross-RG role assignment in `infra/modules/foundry-rbac.bicep`.
- Sample videos under `sample-videos/` ship inside the image (15-30 MB each
  for `toyota.mp4` and `fiat.mp4`). Future videos go in the same directory
  and `policies.json`.
- Companion command `/deploy-foundry-agent` redeploys the hosted agent —
  use it when `policies.json` or the agent code changed. The two artifacts
  are usually deployed together.
- See [CLAUDE.md](CLAUDE.md) for the wider context: the standardized
  `AZURE_OPENAI_DEPLOYMENT` env var, the gpt-4.1 model choice, and the
  Container Apps + ACR bootstrap pitfall.

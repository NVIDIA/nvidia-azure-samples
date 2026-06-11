# Insurance Claims Triage — Foundry hosted agent

A [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) hosted agent that orchestrates the **NVIDIA VSS Agent** as a video-understanding sub-agent, plus three mock business tools, to turn a customer's damage video into a draft insurance claim.

## What the agent does

When given a customer message like:

> "A customer just submitted a damage video. `video_id=toyota`. If the VIN is not visible, use policy POL-2025-44912 as fallback. Run the triage workflow."

the agent executes this chain:

1. **`vss_analyze_video(video_id, question)`** — calls the NVIDIA VSS Agent on AKS (cosmos-reason2-8b VLM) for damage analysis
2. **`lookup_policy(vin? | policy_number?)`** — mock policy DB ([sample_data/policies.json](sample_data/policies.json))
3. **`estimate_repair_cost(damage_items)`** — parts + labor calculator
4. **`draft_claim_pdf(...)`** — reportlab PDF written to `./output/CLM-YYYYMMDD-XXXXX.pdf`

Then replies with a concise claim summary including the payable amount after deductible.

See [tools.py](tools.py) for tool implementations and [instructions.md](instructions.md) for the system prompt.

## Hosted agent variants

`azure.yaml` defines two hosted-agent services. Pass the service name explicitly
to `azd ai agent run`, `azd deploy`, and `azd ai agent invoke`.

| Service | Project directory | VSS mode |
|---|---|---|
| `insurance-claims-triage` | `src/agent-framework-agent-basic-responses` | `MOCK_VSS=true` fixture replay |
| `insurance-claims-triage-vss` | `src/agent-framework-agent-vss-responses` | `MOCK_VSS=false` live VSS blueprint |

## Iterate locally — no `azd deploy` required

Once your Foundry project is provisioned (one-time `azd provision`, see step 4
below), the iterative dev loop runs **the agent process on your laptop** while
the model inference still uses Foundry. No container build, no ACR push, no
hosted endpoint — just `python main.py` under the covers, exposed as
`localhost:8088`.

```bash
cd demo/insurance-claims-foundry

# Terminal 1 — start the agent (binds :8088, leave running)
azd ai agent run insurance-claims-triage-vss

# Terminal 2 — invoke with the canonical smoke-test prompt
azd ai agent invoke --local \
  "A customer just submitted a damage video. video_id=toyota. \
   If the VIN is not visible, use policy POL-2025-44912 as a fallback. \
   Run the triage workflow."
```

Verified end-to-end output (Toyota pre-upload — wrecked Camry):

```
Claim drafted: CLM-YYYYMMDD-44912
Vehicle: 2022 Toyota Camry SE (VIN 4T1G11AK7NU012345)
Damage:  Severe damage to hood and windshield;
         moderate damage to front bumper and driver door.
Repair:  $5982    Deductible: $500    Payable: $5482
Draft saved to: ./output/CLM-YYYYMMDD-44912.pdf
```

`azd ai agent run` reads env vars from `.azure/<env>/.env` (populated by
`azd provision`) and from [agent.yaml](agent.yaml)'s `environment_variables`
block (which carries `AZURE_OPENAI_DEPLOYMENT` and `VSS_BASE_URL`). It also
auto-maps `AZURE_AI_PROJECT_ENDPOINT` → `FOUNDRY_PROJECT_ENDPOINT`.

When you're ready to publish the agent to Foundry as a real hosted container,
see [Deploying to Foundry (production hosted)](#deploying-to-foundry-production-hosted)
below. The full walk-through (install extension → provision → upgrade
model → local run) is in [Reproduce the local run](#reproduce-the-local-run).

## Prerequisites

| Requirement | Why |
|---|---|
| Azure subscription with `Contributor` (or equivalent) at sub scope | `azd provision` creates RG + Foundry resource + ACR + Log Analytics + App Insights + model deployment |
| `azd` CLI **≥ 1.24.0** | Hosted-agent commands |
| `azd ai agent` extension **≥ 0.1.27-preview** | New hosted-agents backend (not the legacy Container Apps experience) |
| Python **3.10+** local | `azd ai agent run` creates a venv and installs `requirements.txt` |
| `az login` done | `DefaultAzureCredential` uses your CLI session for Foundry auth |
| VSS Agent reachable | The `VSS_BASE_URL` env var (set in [agent.yaml](agent.yaml)) must resolve to your own AKS deployment, for example `http://vss.<EXTERNAL_HOST>.nip.io`. See the workshop's [main README](../../../../README.md). |

## Reproduce the local run

These steps reproduce the working local smoke test exactly.

### 1. Install / upgrade the `azd ai agent` extension

```bash
azd ext install azure.ai.agents   # first time
azd ext upgrade azure.ai.agents   # if already installed (needs >= 0.1.27-preview)
azd ext list | grep ai.agents     # verify
```

### 2. Clone this workshop repo and `cd` to the agent project root

```bash
git clone <this-repo>
cd <repo-root>/demo/insurance-claims-foundry
```

> If you want to re-scaffold from scratch instead of cloning: run `azd ai agent init` in an empty directory, pick `Python` / `Basic agent (Responses, Agent Framework, Python)` / `Deploy a new model in Foundry`, then copy [tools.py](tools.py), [instructions.md](instructions.md), [sample_data/](sample_data/), and the `VSS_BASE_URL` env entry from [agent.yaml](agent.yaml) into the new project.

### 3. Authenticate

```bash
az login
azd auth login
```

### 4. Provision Azure resources

```bash
azd provision
```

About 90 seconds. Creates:

- Resource group `rg-insurance-claims-foundry-dev` (or whatever env name you picked)
- Foundry account `ai-account-<suffix>`
- Foundry project `ai-project-insurance-claims-foundry-dev`
- Default model deployment (`gpt-4.1-mini` from `azd ai agent init`)
- Azure Container Registry `cr<suffix>` (Basic SKU)
- Log Analytics workspace + Application Insights

### 5. Deploy a stronger model (recommended)

`gpt-4.1-mini` (the default) hallucinates tool calls in this multi-step orchestration. Deploy `gpt-4.1` (full) and switch the agent to use it:

```bash
RG=$(azd env get-values | grep AZURE_RESOURCE_GROUP | cut -d'"' -f2)
ACCT=$(azd env get-values | grep AZURE_AI_ACCOUNT_NAME | cut -d'"' -f2)

az cognitiveservices account deployment create \
  -g "$RG" -n "$ACCT" \
  --deployment-name gpt-4.1 \
  --model-name gpt-4.1 \
  --model-version 2025-04-14 \
  --model-format OpenAI \
  --sku-capacity 50 --sku-name Standard
```

Then point the agent at it:

```bash
# Edit agent.yaml: AZURE_OPENAI_DEPLOYMENT value: gpt-4.1
# Edit .azure/<env>/.env: AZURE_OPENAI_DEPLOYMENT="gpt-4.1"
```

Or run from this directory:

```bash
azd env set AZURE_OPENAI_DEPLOYMENT gpt-4.1
sed -i.bak 's/value: gpt-4.1-mini/value: gpt-4.1/' agent.yaml && rm agent.yaml.bak
```

### 6. Start the agent locally

In one terminal:

```bash
azd ai agent run insurance-claims-triage-vss
```

The first run creates a venv and pip-installs `requirements.txt` (~30s). Then logs:

```
Running on http://0.0.0.0:8088 (CTRL + C to quit)
```

Leave this running.

### 7. Invoke with the sample prompt

In another terminal:

```bash
azd ai agent invoke --local "A customer just submitted a damage video. video_id=toyota. If the VIN is not visible, use policy POL-2025-44912 as a fallback. Run the triage workflow."
```

You should see the agent execute `vss_analyze_video` → `lookup_policy` → `estimate_repair_cost` → `draft_claim_pdf`, then a final claim summary. The PDF is written to `./output/CLM-*.pdf`.

### 8. Send your own test (curl)

```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "video_id=toyota, policy POL-2025-44912 — run triage", "stream": false}'
```

### Multi-turn

Pass the response ID from the previous reply to continue the conversation:

```bash
curl -sS -X POST http://localhost:8088/responses \
  -H "Content-Type: application/json" \
  -d '{"input": "Now resubmit with policy POL-2025-58820 instead.", "previous_response_id": "<id>"}'
```

## Sample videos

The default `VSS_BASE_URL` points at the workshop's AKS-hosted VSS Agent. The two videos that have been pre-uploaded and used in smoke tests:

| video_id | Description | Suggested fallback policy |
|---|---|---|
| `toyota` | Toyota pre-upload — wrecked car on graffiti street, severe front collision | `POL-2025-44912` (Alex Romero / Camry) |
| `3974558-hd_1920_1080_30fps` | Pexels clip 3974558 — HD front-end collision walkaround | `POL-2025-58820` (Priya Shankar / Fiat Stilo) |

To upload your own video to VST first, see the wrapper's [`vss_upload_video`](../../../insurance-claims/vss_mcp_server.py) helper in the sibling demo folder, or use the VST API directly:

```bash
# 1. Get a presigned upload URL
curl -sS -X POST http://vss.<EXTERNAL_HOST>.nip.io/api/v1/videos \
  -H "Content-Type: application/json" \
  -d '{"filename":"my-claim.mp4"}'

# 2. PUT your video bytes to the returned URL
curl -X PUT "<presigned-url>" --data-binary @my-claim.mp4 -H "Content-Type: video/mp4"

# 3. Reference the returned video_id when chatting with the agent
```

## Troubleshooting local runs

| Symptom | Fix |
|---|---|
| `Maximum consecutive function call errors reached (3)` | Model is hallucinating tool args. Switch from gpt-4.1-mini to gpt-4.1 (see step 5). |
| Agent claims a PDF was saved but no file exists | Same as above — model fabricated the tool call. Hardened instructions in [instructions.md](instructions.md) step 6 explicitly forbid this; if you regress, the file in `output/` will be missing. |
| `port 8088 already in use` | Another agent is running. `pkill -f "python main.py"` or use a different port via the agent SDK config. |
| `DefaultAzureCredential` failure | Run `azd auth logout && azd auth login` and `az login` again. |
| VSS analysis returns prose with `<agent-think>` tags | Wrapper should strip them — see [tools.py](tools.py) `_AGENT_THINK_RE`. If you see them in output, check that regex is being applied. |
| `ResourceNotFound` on `FOUNDRY_PROJECT_ENDPOINT` | `azd ai agent run` should auto-map `AZURE_AI_PROJECT_ENDPOINT` → `FOUNDRY_PROJECT_ENDPOINT`. If not, `cp .env.example .env` and set both manually. |

## Test harness — deployed agent

End-to-end smoke test under [../../tests/](../../tests/) that drives the
**deployed** Foundry hosted agent (no `--local`). It shells out to
`azd ai agent invoke <agent-name> --new-conversation "..."` and asserts that the
response includes a drafted claim with the expected vehicle / VIN / dollar
amount.

### Enable

Test config lives in the repo-root [.env.test](../../../../.env.test).
Set the remote-suite gate:

```bash
RUN_E2E_REMOTE_TESTS=1
REMOTE_AGENT_NAME=insurance-claims-triage-vss
```

(see [.env.test.example](../../../../.env.test.example) for the full
template). The local-runner gate `RUN_E2E_TESTS` for the sibling
`insurance-claims/` demo is independent — you can enable one suite without
the other.

### Run

```bash
cd demo/insurance-claims-foundry
pip install -r requirements-dev.txt    # pytest + python-dotenv
pytest tests/ -v -s

# Or target an agent explicitly:
./scripts/test_remote_agent.sh insurance-claims-triage-vss
./scripts/test_remote_agent.sh insurance-claims-triage
```

### Skip behavior

Plain `pytest tests/` is safe — the test is skipped when any of these are true:

- `RUN_E2E_REMOTE_TESTS` is unset
- `azd` CLI isn't on PATH
- No deployed agent in the current azd env (run `azd deploy <agent-name>` first)

Each run costs ~30-90s of model + VSS spend, so the gate stays off by
default. See the App Insights trace (linked from invoke output) for the
full tool-call sequence — `azd ai agent invoke` stdout only carries the
final response text, not individual tool calls.

## Deploying to Foundry (production hosted)

Once the local run works:

```bash
./scripts/deploy_agent.sh insurance-claims-triage-vss

# Mock hosted agent:
./scripts/deploy_agent.sh insurance-claims-triage
```

This:
1. Tars up the selected service project directory
2. Uploads to ACR
3. ACR Tasks builds the Dockerfile remotely (no local Docker required)
4. Pushes the image
5. Registers the agent against the Foundry project + starts the container
6. **Postdeploy hook** grants the agent's managed identity the runtime RBAC it needs (storage/history + agent endpoint)

Then invoke production (no `--local` flag):

```bash
azd ai agent invoke insurance-claims-triage-vss "video_id=toyota, policy POL-2025-44912 — run triage"

# Mock hosted agent:
azd ai agent invoke insurance-claims-triage "video_id=toyota, policy POL-2025-44912 — run triage"
```

Find the agent in the [Foundry portal](https://ai.azure.com) → your project → **Build** → **Agents**. Traces stream to the Application Insights resource in the same RG.

### ⚠ Required: keep `azure.yaml` service name == `agent.yaml` name

The `azd deploy` postdeploy hook fetches the agent's metadata using the **service name from `azure.yaml`**, then grants the agent identity its runtime RBAC. If your `agent.yaml` `name:` differs from the `azure.yaml` `services.<name>:` key, the postdeploy hook gets a **404**, the RBAC step never runs, and **all subsequent invocations fail with HTTP 500 `PermissionDenied`** on the `storage/history` endpoint.

Each service key in `azure.yaml` must match that service's `agent.yaml`
`name:` exactly: `insurance-claims-triage` for mock mode and
`insurance-claims-triage-vss` for live VSS mode. If you rename one, rename the
other in the same commit.

Symptoms if they drift:
```
ERROR: failed invoking event handlers for 'postdeploy', failed to fetch agent version for <service-name>/1: 404 Not Found
```
…followed by every invoke returning:
```
{"error":{"code":"PermissionDenied","message":"Principal does not have access to API/Operation."}}
```

### Container filesystem and output artifacts

The agent container has its own filesystem at `/app/user_agent/`. PDFs the agent generates via `draft_claim_pdf` land in `/app/user_agent/output/` **inside the container** — not on the caller's machine. Per the [hosted-agent docs](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents), sessions persist their filesystem for up to 30 days, accessible via the Foundry portal session-files panel.

For a real workflow, replace the file-write in `draft_claim_pdf` with an upload to Blob Storage (or a SharePoint connection) so adjusters can fetch the PDF.

## Cleanup

```bash
azd down       # deletes the entire RG (Foundry, ACR, monitoring, model deployments — all of it)
```

> ⚠ `azd down` removes everything in the RG. If you reuse this RG for anything else, delete resources individually instead.

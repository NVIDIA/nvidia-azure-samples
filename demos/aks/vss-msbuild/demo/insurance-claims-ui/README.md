# Insurance Claims Triage — Customer Rep Web UI

A Streamlit app that demonstrates the full insurance-claims workflow from a customer rep's perspective:

1. **Pick a customer policy** from the sidebar (data: [policies.json](policies.json))
2. **Upload the customer's damage video** — uploads to the NVIDIA VSS service running on AKS via VST presigned URL
3. **Call the deployed Microsoft Foundry agent** (`insurance-claims-triage`) with the video_id + policy as fallback
4. **Watch the agent orchestrate** the VSS analysis → policy lookup → cost estimate → claim draft (all 4 tool calls visible in the Foundry trace)
5. **Display the draft PDF inline** + a download button — regenerated locally from the agent's structured reply

```
┌────────────────────────────────────────────────────────────────────────┐
│ Streamlit (this app)                                                   │
│   ┌─ sidebar: policies.json ──┐  ┌─ main: upload form + result ────┐   │
│   │ POL-2025-44912 Alex/Camry │  │ Pick policy → Upload .mp4       │   │
│   │ POL-2025-58820 Priya/...  │  │ → "Submit claim"                │   │
│   │ POL-2025-67114 Marcus/... │  │                                 │   │
│   └──────────────────────────┘   │  Agent reply / Parsed fields    │   │
│                                  │  Inline PDF preview + download  │   │
│                                  └─────────────────────────────────┘   │
└─────────┬─────────────────────────────────────────────────┬────────────┘
          │                                                 │
          │ PUT video                                       │ POST /responses
          ▼                                                 ▼
    NVIDIA VSS Agent                            Microsoft Foundry agent
    (vss.<EXTERNAL_HOST>.nip.io / AKS)         (insurance-claims-triage)
```

## Prerequisites

- The two upstream services already deployed and reachable from this machine:
  - **VSS Agent on AKS** — set `VSS_BASE_URL` to your deployment, for example `http://vss.<EXTERNAL_HOST>.nip.io` (see workshop's main [README](../../README.md))
  - **Foundry hosted agent `insurance-claims-triage`** — see [../insurance-claims-foundry/README.md](../insurance-claims-foundry/src/agent-framework-agent-basic-responses/README.md)
- Python 3.11+
- `az login` done — `DefaultAzureCredential` uses the CLI session to mint a Bearer token for the Foundry agent endpoint
- (Optional but recommended) `uv` for fast venv + install

No sample videos are bundled in the public repository. Use your own cleared
`.mp4`, `.mov`, or `.m4v` damage video when running the UI.

## Run

```bash
cd /path/to/vss-msbuild-demo/demos/aks/vss-msbuild/demo/insurance-claims-ui

# Create venv + install deps (uv path — fast)
uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python

# Or with plain pip
# python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Authenticate
az login

# Launch (opens http://localhost:8501 in your browser)
.venv/bin/streamlit run app.py
```

## What you'll see

| Region | Contents |
|---|---|
| **Sidebar** | Read-only table of the 3 mock policies. Underneath, the resolved VSS + Foundry endpoints (useful for sanity-checking which env you're hitting). |
| **Left column** | Policy selector dropdown, expandable JSON view of the picked policy, video uploader. "Submit claim" stays disabled until you attach a file. |
| **Right column** | Live status panel (upload progress, agent thinking…), then the agent's verbatim text reply, the regex-parsed claim fields, the regenerated PDF (download button + inline iframe preview). |

## How it works

### Video upload (steps 1-2 in [app.py:upload_video_to_vss](app.py))

1. POST `/api/v1/videos` with `{"filename": "..."}` to VSS — returns a VST presigned URL
2. PUT the video bytes to that URL with `Content-Type: video/mp4`
3. The `video_id` is the second-to-last path segment of the returned URL (canonical reference the Foundry agent will use)

### Foundry agent call (step 3, [app.py:call_foundry_agent](app.py))

- Mint a Bearer token with `DefaultAzureCredential` for the `https://cognitiveservices.azure.com/.default` scope
- POST to the agent's `/endpoint/protocols/openai/responses` URL with `{"input": prompt, "stream": false}`
- Receive an OpenAI Responses-API JSON; extract the assistant's final text from `output[-1].content[0].text`

### Why the PDF is regenerated locally

The Foundry agent's `draft_claim_pdf` tool writes the PDF inside the agent container at `/app/user_agent/output/...` — that filesystem isn't reachable from the browser. Three ways to solve this:

| Approach | Trade-off |
|---|---|
| **Regenerate from agent text (this app)** | Simple, no infra changes, but duplicates a tiny bit of PDF logic |
| Modify the agent tool to upload to Azure Blob and return a SAS URL | Production-correct, but requires adding Blob to the agent + redeploying |
| Use the Foundry session-files API | Preview API, less documented |

For the demo we picked the first option. Regex parses the agent's strictly-formatted reply (per [instructions.md](../insurance-claims-foundry/src/agent-framework-agent-basic-responses/instructions.md) step 7), then reportlab renders a one-page PDF. The full agent transcript is appended verbatim in small print so an adjuster can audit the AI's reasoning.

## Configuration overrides

Both endpoints can be overridden at runtime via env vars:

```bash
export VSS_BASE_URL=http://vss.<your-host>.nip.io
export FOUNDRY_AGENT_ENDPOINT="https://<account>.services.ai.azure.com/api/projects/<project>/agents/<agent>/endpoint/protocols/openai/responses?api-version=2025-11-15-preview"
.venv/bin/streamlit run app.py
```

Useful when:
- Re-deploying VSS to a new AKS cluster
- Pointing at a different Foundry agent (e.g. `insurance-claims-triage` v3 after iteration)
- Testing against the LOCAL `azd ai agent run` server (set `FOUNDRY_AGENT_ENDPOINT=http://localhost:8088/responses` — note `--local` mode doesn't require Bearer auth, so you may need to comment out the token line in `call_foundry_agent`)

## Demo script (for live presentations)

1. Open the sidebar — point at the policies. "These are real customers in our CRM."
2. Pick `POL-2025-44912` (Alex Romero / Camry).
3. Upload a cleared damage-video file.
4. Click **Submit claim**.
5. While the spinner runs (~30-60s): switch to the Foundry portal playground in a side window — the same agent invocation appears there with the live trace panel showing each tool fire.
6. Back in the UI, the response renders: the agent's text, the parsed claim, then the PDF preview embeds inline.
7. Hit the download button to confirm it's a real PDF.
8. Optionally swap to another policy and re-upload — show the workflow is reusable.

## Troubleshooting

| Symptom | Fix |
|---|---|
| **HTTP 403 (empty body) on the Foundry endpoint** | Token scope mismatch. The Foundry data plane requires `https://ai.azure.com/.default` (NOT `https://cognitiveservices.azure.com/.default`). Verify with `az account get-access-token --resource https://ai.azure.com --query accessToken -o tsv \| /usr/bin/head -c 30`. |
| **`AuthenticationFailed` from the Foundry endpoint** | `az login` first. The DefaultAzureCredential chain prefers CLI creds. |
| **`PermissionDenied` 500 on the agent endpoint** | Your user needs `Foundry User` (and the agent's identity needs `Azure AI Administrator`) on the AI Services account. See the parent README's RBAC section. |
| **VSS upload returns 405 / 415** | The video uploader is sending the raw bytes but the Content-Type may differ; try `.mp4` files only or set the right MIME by extension. |
| **Agent reply has the right text but the parsed fields are blank** | The agent ignored the strict-format instructions. Open [instructions.md](../insurance-claims-foundry/src/agent-framework-agent-basic-responses/instructions.md) step 7 and tighten the reply format requirements. |
| **PDF preview shows up blank** | Some browsers block `data:application/pdf;base64,...` iframes for security. Use the download button instead, or open the file from `output/`. |

## File layout

```
demo/insurance-claims-ui/
├── README.md                 # this file
├── app.py                    # the whole app (~300 lines)
├── requirements.txt          # streamlit / httpx / azure-identity / reportlab
├── policies.json             # mock policy DB (3 rows, same as parent demo)
└── output/                   # regenerated PDFs land here (one per claim)
```

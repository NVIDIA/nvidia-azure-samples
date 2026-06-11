# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A hands-on workshop that pairs NVIDIA's **VSS (Video Search and Summarization) LVS blueprint** on AKS with a **Microsoft Foundry** multi-agent insurance-claims demo. The VSS Agent (video understanding via cosmos-reason2-8b VLM) is consumed as a sub-tool by a Foundry orchestrator that drives policy lookup → cost estimate → claim PDF.

Top-level structure:
- `README.md` — step-by-step VSS+AKS deployment (Helm, ingress-nginx, NIM operator). Source of truth for the cluster side.
- `aks/ingress/vss-ingress.yaml` — workshop-tailored Ingress (nginx, not the upstream HAProxy)
- `video-search-and-summarization/` — upstream NVIDIA repo, **gitignored**, cloned separately per the README. Don't suggest edits here.
- `demo/` — the application code we own (3 sibling demos, see below).
- `data/` — sample MP4s used by the demos; also gitignored.

## The three demos and how they relate

```
demo/insurance-claims/         ←  Dev sandbox.    local_runner.py drives the agent loop with
                                                  Azure OpenAI SDK directly. Fast iteration on
                                                  the system prompt + tool contracts.
demo/insurance-claims-foundry/ ←  Production.     Same agent + tools, hosted by Foundry as a
                                                  container. `azd ai agent run` for local
                                                  iteration; `azd deploy` to publish.
demo/insurance-claims-ui/      ←  Customer UI.    Streamlit app that uploads videos to VSS and
                                                  calls the deployed Foundry agent's /responses
                                                  endpoint, then regenerates the claim PDF locally.
```

All three share the same VSS backend (`VSS_BASE_URL`), the same policy mock data shape (3 customers in `policies.json`/`sample_data/policies.json`), and the same agent system prompt logic. **When changing tool schemas or the agent prompt, update both `demo/insurance-claims/` and `demo/insurance-claims-foundry/` in lockstep** — the two demos intentionally mirror each other so the dev sandbox stays representative of production.

## Conventions

- **Env var name**: `AZURE_OPENAI_DEPLOYMENT` — the standardized name for the chat-model deployment, used by both demos. Don't re-introduce `AZURE_AI_MODEL_DEPLOYMENT_NAME` (the azd init template's original name); we consolidated on the OpenAI-SDK convention.
- **Model**: `gpt-4.1` (not `gpt-4.1-mini` from the azd template — it hallucinates tool calls in this multi-step orchestration; not `gpt-5.4` from the stale `foundry_config.json` either).
- **Config layout**: `.env` (runtime, gitignored) and `.env.test` (test-only knobs, gitignored). Tracked templates: `.env.example`, `.env.test.example`. `tests/conftest.py` in each demo loads `.env` then `.env.test` (override=True) so test values win over runtime values for shared keys.

## Common commands

### Iterate on the local dev sandbox

```bash
cd demo/insurance-claims
.venv/bin/python local_runner.py /path/to/damage.mp4 POL-2025-44912
```

### Iterate on the Foundry agent (no `azd deploy`)

```bash
cd demo/insurance-claims-foundry
azd ai agent run                                                   # terminal 1
azd ai agent invoke --local "video_id=302857, policy POL-2025-44912 — run triage"
```

### Push a new agent version to Foundry

```bash
cd demo/insurance-claims-foundry
azd deploy                       # ~2 min: ACR build → register → postdeploy RBAC
azd ai agent invoke "..."        # (no --local) hits the hosted agent
```

### Pytest harnesses

Both demos have an E2E pytest under `tests/`. Both are gated behind env vars in `.env.test` so plain `pytest` skips.

```bash
# Local sandbox (gate: RUN_E2E_TESTS=1, requires TEST_VIDEO_PATH)
cd demo/insurance-claims && .venv/bin/python -m pytest tests/ -v -s

# Deployed Foundry agent (gate: RUN_E2E_REMOTE_TESTS=1, shells out to `azd ai agent invoke`)
cd demo/insurance-claims-foundry && pytest tests/ -v -s
```

### Streamlit UI

```bash
cd demo/insurance-claims-ui
az login
.venv/bin/streamlit run app.py
```

## Things that aren't obvious from the code

- **`azure.yaml` service name must match `agent.yaml` `name:`**. Both are `insurance-claims-triage`. If they drift, `azd deploy`'s postdeploy hook 404s on the RBAC step and every subsequent invoke fails with HTTP 500 `PermissionDenied`. Rename both together if ever needed.
- **`local_runner.py` reads env vars at function-call time, not module-import time** — this is intentional so pytest can import the module without env set. Don't add module-level `raise SystemExit` for missing env.
- **`run()` returns the message history** — the local-runner pytest asserts on which tools the agent called. Don't change the return type without updating the test.
- **VSS_BASE_URL**: hardcoded as `value:` in `agent.yaml` (so the deployed container has it baked in); read from `.env` for local runs.
- **PDFs from the deployed agent live inside the container** at `/app/user_agent/output/` — they're not reachable from the caller's machine. The Streamlit UI works around this by regex-parsing the agent's reply and regenerating the PDF locally.
- **Pre-uploaded test videos on your workshop VSS deployment** (`$VSS_BASE_URL`):
  - `video_id=302857` → pairs with policy `POL-2025-44912` (Alex Romero / Camry)
  - `video_id=3974558-hd_1920_1080_30fps` → pairs with `POL-2025-58820` (Priya Shankar / Civic)

## Don't commit

- `.env` and `.env.test` — gitignored via `.env*` with `!*.example` whitelist.
- Generated PDFs under `output/` — gitignored.
- `foundry_config.json` (legacy) — already removed; the demos read env vars now.
- `video-search-and-summarization/` — gitignored, users clone the upstream repo themselves.

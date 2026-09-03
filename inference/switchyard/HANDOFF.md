# DGX Spark Switchyard Handoff

Last verified on 2026-09-03 on a DGX Spark host.

This package captures a DGX Spark Switchyard setup for fast handoff. It preserves the same local endpoints and route names used by the live machine. Secrets are intentionally not included; use the `.example` env files and fill the real values on the target host.

The systemd units in `examples/systemd/user/` are examples for recreating the same always-on service layout. They are not required by the shim or route config.

## What Runs

The stack exposes OpenAI-compatible model endpoints for Cursor/Codex-style clients and mini SWE-bench runs.

```text
client
  -> optional ngrok public URL
  -> bearer-auth shim on 127.0.0.1:4100
  -> switchyard-server on 0.0.0.0:4000
  -> local vLLM Nemotron on 127.0.0.1:8000/v1
  -> Azure Foundry gpt-5.6-sol endpoint
```

Default endpoints:

```text
local vLLM upstream:      http://127.0.0.1:8000/v1
Switchyard local API:     http://127.0.0.1:4000/v1
auth shim local API:      http://127.0.0.1:4100/v1
ngrok public API:         https://<your-ngrok-domain>/v1
Azure Foundry upstream:   see config/routes.toml
Prometheus local agent:   http://127.0.0.1:9095
Switchyard metrics:       http://127.0.0.1:4000/metrics
```

Available Switchyard model IDs:

```text
gpt-5.6-sol
nemotron-3.5-lightning
switchyard/auto
switchyard/smart
```

`switchyard/smart` and `switchyard/auto` are LLM-classifier routes. Nemotron classifies each request; simple or weak-tier work goes to local Nemotron, and strong-tier work goes to Azure `gpt-5.6-sol`.

## Important Files

```text
shim/switchyard_auth_proxy.py
config/routes.toml
config/env.example
config/cursor-proxy.env.example
config/prometheus.yml
examples/systemd/user/nemotron35-lightning-vllm.service
examples/systemd/user/switchyard-cursor.service
examples/systemd/user/switchyard-auth-proxy.service
examples/systemd/user/switchyard-ngrok.service
examples/systemd/user/prometheus-switchyard-agent.service
benchmarks/switchyard-mini.yaml
benchmarks/switchyard-mini-base.yaml
```

The shim is deliberately small. It accepts `Authorization: Bearer $SWITCHYARD_CURSOR_API_KEY` or `x-api-key: $SWITCHYARD_CURSOR_API_KEY`, strips auth before forwarding to Switchyard, and rewrites Cursor payloads by removing `temperature` and converting `max_tokens` to `max_completion_tokens`.

## Prepare Local Files

From this package directory:

```bash
cd /path/to/nvidia-azure-samples/inference/switchyard
"${HOME}/miniconda3/bin/python3" -m pip install -r shim/requirements.txt
install -d -m 0700 "${HOME}/.config/switchyard"
install -d -m 0755 "${HOME}/.local/bin"
install -m 0755 shim/switchyard_auth_proxy.py "${HOME}/.local/bin/switchyard_auth_proxy.py"
install -m 0644 config/routes.toml "${HOME}/.config/switchyard/routes.toml"
cp -n config/env.example "${HOME}/.config/switchyard/env"
cp -n config/cursor-proxy.env.example "${HOME}/.config/switchyard/cursor-proxy.env"
chmod 0600 "${HOME}/.config/switchyard/env" "${HOME}/.config/switchyard/cursor-proxy.env"
```

Required secret values:

```text
AZURE_FOUNDRY_API_KEY
SWITCHYARD_CURSOR_API_KEY
```

`LOCAL_VLLM_API_KEY=EMPTY` is expected for the local vLLM OpenAI-compatible server.

Before installing, set the Azure Foundry base URL in `config/routes.toml`:

```toml
[llm_clients.azure_foundry]
base_url = "https://your-foundry-resource.services.ai.azure.com/openai/v1"
```

Generate a fresh shim token when needed:

```bash
"${HOME}/miniconda3/bin/python3" -c 'import secrets; print("sk-switchyard-" + secrets.token_hex(32))'
```

Expected local dependencies:

```text
${HOME}/.cargo/bin/switchyard-server
/usr/bin/docker
/usr/local/bin/ngrok
${HOME}/miniconda3/bin/python3 with aiohttp
```

If the Switchyard binary is missing:

```bash
cargo install --locked switchyard-server
```

## Optional Systemd Example

The example units run the same local endpoint layout as always-on user services. Review `examples/systemd/user/*.service` first and adjust paths, model flags, metrics config, or ngrok settings for the target host.

Install and enable the example user units:

```bash
./scripts/install-example-user-units.sh
```

Install, enable, and start them:

```bash
./scripts/install-example-user-units.sh --start
```

Equivalent manual service commands:

```bash
systemctl --user daemon-reload
systemctl --user enable --now nemotron35-lightning-vllm.service
systemctl --user enable --now switchyard-cursor.service
systemctl --user enable --now switchyard-auth-proxy.service
systemctl --user enable --now switchyard-ngrok.service
systemctl --user enable --now prometheus-switchyard-agent.service
```

If the user service should survive logout:

```bash
loginctl enable-linger "$USER"
```

## Smoke Test

```bash
./scripts/smoke-test.sh
```

Manual checks:

```bash
curl -fsS http://127.0.0.1:4100/health
curl -fsS http://127.0.0.1:4000/v1/models

set -a
. "${HOME}/.config/switchyard/cursor-proxy.env"
set +a
curl -fsS -H "Authorization: Bearer ${SWITCHYARD_CURSOR_API_KEY}" \
  http://127.0.0.1:4100/v1/models
```

For the current public URL:

```bash
curl -fsS http://127.0.0.1:4040/api/tunnels
```

The example unit starts ngrok without a reserved domain flag, so the public URL can change after ngrok restarts unless the ngrok account has mapped one.

## Cursor Or Client Settings

Local client:

```text
Base URL: http://127.0.0.1:4100/v1
API key:  value of SWITCHYARD_CURSOR_API_KEY
Model:    switchyard/smart
```

Remote client through ngrok:

```text
Base URL: https://<your-ngrok-domain>/v1
API key:  value of SWITCHYARD_CURSOR_API_KEY
Model:    switchyard/smart
```

Internal benchmark clients can bypass the auth shim:

```text
Base URL: http://127.0.0.1:4000/v1
API key:  EMPTY
Model:    openai/switchyard/smart or openai/gpt-5.6-sol
```

## SWE-Bench Mini Setup

The working benchmark directory on this DGX Spark is:

```bash
cd ~/Dev/switchyard-benchmarks
source .venv/bin/activate
```

Smart route config:

```bash
mini-extra swebench \
  -c swebench.yaml \
  -c switchyard-mini.yaml \
  --subset verified \
  --split test \
  --slice 0:500 \
  --workers 1 \
  --environment-class docker \
  --output ./runs/switchyard-smart-verified-5
```

Azure passthrough baseline config:

```bash
mini-extra swebench \
  -c swebench.yaml \
  -c switchyard-mini-base.yaml \
  --subset verified \
  --split test \
  --slice 0:500 \
  --workers 1 \
  --environment-class docker \
  --output ./runs/switchyard-base-verified-6
```

The package includes the two Switchyard-specific YAML overlays. They both use `api_base: "http://127.0.0.1:4000/v1"` and `api_key: "EMPTY"`.

## Operations

If using the example systemd user units, check status:

```bash
systemctl --user --no-pager status \
  nemotron35-lightning-vllm.service \
  switchyard-cursor.service \
  switchyard-auth-proxy.service \
  switchyard-ngrok.service \
  prometheus-switchyard-agent.service
```

Follow logs:

```bash
journalctl --user -u nemotron35-lightning-vllm.service -f
journalctl --user -u switchyard-cursor.service -f
journalctl --user -u switchyard-auth-proxy.service -f
journalctl --user -u switchyard-ngrok.service -f
tail -f "${HOME}/.config/switchyard/routing.log"
```

Validate Switchyard config:

```bash
"${HOME}/.cargo/bin/switchyard-server" \
  --config "${HOME}/.config/switchyard/routes.toml" \
  --dry-run
```

## Notes

The installed `switchyard-server` reports version `0.2.0`. The local cargo checkout is at:

```text
~/.cargo/git/checkouts/switchyard-57b459545ac1c45d/c597bfd
commit c597bfd5ee775e94a579dcc6472910416d961b66
```

The local vLLM service runs:

```text
vllm/vllm-openai:v0.27.1
nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4
served as nemotron-3.5-lightning
port 8000
```

Prometheus scrapes `127.0.0.1:4000/metrics`. Configure `remote_write` only after choosing the target Azure Monitor workspace or other metrics backend.

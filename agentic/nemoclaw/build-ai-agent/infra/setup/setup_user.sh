#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
# Learner-triggered attachment/check step after pinned installation + onboarding.
# Also used by the optional author preinstall mode. Never destroys a sandbox.
if [[ "$EUID" -eq 0 ]]; then
  printf 'Run this step as the assigned Ubuntu user, without sudo.\n' >&2
  exit 1
fi
source "$HOME/.config/ignite-lab/environment.sh"
umask 077
rm -f "$HOME/.config/ignite-lab/agent-ready.json"
command -v nemoclaw openshell >/dev/null
if ! nemoclaw --version | grep -Eq '^nemoclaw v0\.0\.132$'; then
  printf '%s\n' "Install the lab's pinned NemoClaw v0.0.132 before attaching MCP." >&2
  exit 1
fi
nemoclaw sandbox status ignite-agent

# Add is an idempotent reconcile in the pinned CLI; update restores the initial deny list.
nemoclaw sandbox mcp ignite-agent add aks-ops \
  --url https://10.42.4.10:8443/mcp --env AKS_OPS_MCP_TOKEN \
  --trusted-private-host 10.42.4.10 \
  --deny-tool restart_deployment --deny-tool scale_deployment
nemoclaw sandbox mcp ignite-agent update aks-ops \
  --deny-tool restart_deployment --deny-tool scale_deployment

# OpenClaw 2026.9.2 otherwise limits native tools/list to 1,500 ms even
# though ordinary requests have a longer timeout. Activate the explicit limit
# in the running gateway, then test the same catalog loader without inference.
nemoclaw sandbox exec ignite-agent --timeout 60 -- \
  openclaw config set mcp.servers.aks-ops.requestTimeoutMs 30000 --strict-json
nemoclaw ignite-agent gateway restart
nemoclaw sandbox exec ignite-agent --timeout 90 -- \
  openclaw mcp probe aks-ops --json > "$HOME/.config/ignite-lab/mcp-catalog.json"
python3 /opt/ignite-lab/infra/setup/check_mcp_catalog.py "$HOME/.config/ignite-lab/mcp-catalog.json"

# Install starter instructions and the MCP probe in the sandbox without host secrets.
python3 - <<'PREPARE_SANDBOX_ASSETS'
import base64, json, subprocess
from pathlib import Path
starter = Path('/opt/ignite-lab/assets/operations-agent-starter.md').read_bytes()
probe = Path('/opt/ignite-lab/infra/setup/probe_mcp.cjs').read_bytes()
script = r'''
const fs = require('node:fs');
const path = require('node:path');
const config = JSON.parse(fs.readFileSync('/sandbox/.openclaw/openclaw.json', 'utf8'));
const workspace = config.agents?.defaults?.workspace || '/sandbox/.openclaw/workspace';
if (!path.resolve(workspace).startsWith('/sandbox/')) throw Error('Unexpected agent workspace');
fs.mkdirSync(workspace, {recursive: true});
const target = path.join(workspace, 'AGENTS.md');
const marker = '<!-- ignite-lab-starter -->';
const original = fs.existsSync(target) ? fs.readFileSync(target, 'utf8') : '';
if (!original.includes(marker)) fs.writeFileSync(target, original + '\n' + marker + '\n' + Buffer.from(process.argv[1], 'base64').toString());
fs.writeFileSync('/sandbox/ignite-mcp-probe.cjs', Buffer.from(process.argv[2], 'base64'));
'''
subprocess.run(['nemoclaw', 'sandbox', 'exec', 'ignite-agent', '--timeout', '120', '--', 'node', '-e', script,
                base64.b64encode(starter).decode(), base64.b64encode(probe).decode()], check=True)
PREPARE_SANDBOX_ASSETS
nemoclaw sandbox exec ignite-agent --timeout 180 -- node /sandbox/ignite-mcp-probe.cjs
nemoclaw sandbox exec ignite-agent --timeout 60 -- sh -c '
  codes=$(curl --silent --max-time 20 --output /dev/null --write-out "%{http_connect} %{http_code}" https://exfil.ignite-lab.example/collect || true)
  case "$codes" in "403 "*|*" 403") printf "Unapproved egress denied.\n" ;; *) exit 1 ;; esac
'
# Exercise the real agent inference route without repairing the incident.
nemoclaw sandbox exec ignite-agent --timeout 240 -- openclaw agent \
  --agent main --session-id ignite-provisioning-check \
  --message 'Provisioning check: reply with the word ready. Do not call tools or modify any file.' \
  --json > "$HOME/.config/ignite-lab/agent-probe.json"
python3 - <<'VERIFY_AGENT_REPLY'
import json, os
from pathlib import Path
text = (Path(os.environ['HOME']) / '.config/ignite-lab/agent-probe.json').read_text()
# The CLI streams the child stdout; reject any unexpected surrounding output.
body = json.loads(text)
assert body.get('status') in (None, 'ok'), 'Agent returned an error'
payloads = body.get('result', body).get('payloads', [])
assert any(isinstance(item.get('text'), str) and item['text'].strip() for item in payloads), 'No agent reply'
context = json.loads(Path('/etc/ignite-lab/context.json').read_text())
status = Path(os.environ['HOME']) / '.config/ignite-lab/agent-ready.json'
temporary = status.with_suffix('.tmp')
temporary.write_text(json.dumps({
    'schemaVersion': 1, 'status': 'ready', 'setupRevision': context['setupRevision'],
    'sandbox': 'ignite-agent', 'nemoclawCommit': os.environ['NEMOCLAW_INSTALL_REF'],
    'checks': {'agent': 'ready', 'mcpDiscovery': 'ready', 'mcpPolicy': 'mutations-denied', 'egress': 'unapproved-destination-denied'}
}, indent=2) + '\n')
temporary.chmod(0o600)
temporary.replace(status)
VERIFY_AGENT_REPLY
printf 'Agent ready. Continue with agent customization and incident investigation.\n'

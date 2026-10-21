#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Root provisioning entry point; the VM identity can only read per-lab tokens."""
import hashlib
from contextlib import ExitStack
import json
import os
import pwd
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from check_cluster import check

COMMIT = "95d6505265ba1f2cb05355f83786353e654de096"  # NemoClaw v0.0.132
INSTALLER_SHA256 = "539c25cf06c9adce2b7f5756d1acd253f8eddde6e59f60b5eace92c4a123a116"


def prepare_learner_directories(account):
    """Prepare trusted gateway ancestors without following links or touching data."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        with ExitStack() as stack:
            directory = os.open(account.pw_dir, flags)
            stack.callback(os.close, directory)
            home = os.fstat(directory)
            if home.st_uid != account.pw_uid:
                raise RuntimeError("Learner home must be a real directory owned by the assigned VM user")
            os.fchmod(directory, (home.st_mode & 0o777) & ~0o022)
            for name in (".local", "state", "nemoclaw"):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
                directory = os.open(name, flags, dir_fd=directory)
                stack.callback(os.close, directory)
                entry = os.fstat(directory)
                if entry.st_uid not in (0, account.pw_uid):
                    raise RuntimeError("Learner gateway ancestor is owned by an unrelated user; inspect VM preparation")
                if entry.st_uid != account.pw_uid or entry.st_gid != account.pw_gid:
                    os.fchown(directory, account.pw_uid, account.pw_gid)
                os.fchmod(directory, ((entry.st_mode & 0o777) | 0o700) & ~0o022)
    except OSError:
        raise RuntimeError("Learner gateway ancestors must be real accessible directories without symbolic links") from None


def learner_environment(context, account, ca_path, mcp_token, broker_token):
    """Only instance-scoped credentials; never the upstream provider key."""
    return {
        "XDG_RUNTIME_DIR": "/run/user/" + str(account.pw_uid),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/" + str(account.pw_uid) + "/bus",
        "NODE_EXTRA_CA_CERTS": str(ca_path),
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
        "CURL_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "NEMOCLAW_CORPORATE_CA_BUNDLE": str(ca_path),
        "NEMOCLAW_INSTALL_REF": COMMIT,
        "NEMOCLAW_NO_EXPRESS": "1", "NEMOCLAW_AGENT": "openclaw",
        "NEMOCLAW_PROVIDER": "custom",
        "NEMOCLAW_ENDPOINT_URL": context["inferenceEndpoint"],
        "NEMOCLAW_MODEL": context["inferenceModel"],
        "NEMOCLAW_PREFERRED_API": "openai-completions",
        "NEMOCLAW_TRUSTED_PRIVATE_HOSTS": context["mcpPrivateIp"],
        "NEMOCLAW_SANDBOX_NAME": "ignite-agent",
        "COMPATIBLE_API_KEY": broker_token, "AKS_OPS_MCP_TOKEN": mcp_token,
    }


def stage_environment(config_dir, account, values):
    """A shell-safe, mode-0600 file that learners source in their own session."""
    target = config_dir / "environment.sh"
    prefix = account.pw_dir + "/.local/bin:" + account.pw_dir + "/.npm-global/bin"
    lines = ["# Per-instance learner settings; do not print, share or commit this file.",
             "umask 077",
             "export PATH=" + shlex.quote(prefix) + ':"$PATH"']
    lines.extend("export " + name + "=" + shlex.quote(value) for name, value in values.items())
    target.write_text("\n".join(lines) + "\n")
    target.chmod(0o600)
    os.chown(target, account.pw_uid, account.pw_gid)


def secret(context, name):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(30):
        try:
            token_url = ("http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01"
                         "&resource=https%3A%2F%2Fvault.azure.net&client_id=" + context["vmCredentialClientId"])
            request = urllib.request.Request(token_url, headers={"Metadata": "true"})
            with opener.open(request, timeout=10) as response:
                token = json.load(response)["access_token"]
            url = "https://" + context["credentialVaultName"] + ".vault.azure.net/secrets/" + name + "?api-version=7.4"
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)["value"]
        except Exception:
            if attempt == 29:
                raise RuntimeError("Per-instance credential handoff failed") from None
            time.sleep(20)


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run through the VM provisioning extension")
    os.umask(0o077)
    context = json.loads(Path("/etc/ignite-lab/context.json").read_text())
    if not context["deployLabSetup"]:
        print("Infrastructure rehearsal complete; full lab setup explicitly disabled.")
        return
    status = Path("/etc/ignite-lab/ready.json")
    status.unlink(missing_ok=True)
    account = pwd.getpwnam(context["vmAdminUsername"])
    prepare_learner_directories(account)
    config_root = Path(account.pw_dir) / ".config"
    config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chown(config_root, account.pw_uid, account.pw_gid)
    config_dir = config_root / "ignite-lab"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir.chmod(0o700)
    os.chown(config_dir, account.pw_uid, account.pw_gid)
    ca_path = config_dir / "ca.crt"
    ca_path.write_text(secret(context, "lab-ca"))
    ca_path.chmod(0o644)
    os.chown(ca_path, account.pw_uid, account.pw_gid)
    ca_system = Path("/usr/local/share/ca-certificates/ignite-lab.crt")
    ca_system.write_text(ca_path.read_text())
    ca_system.chmod(0o644)
    subprocess.run(["update-ca-certificates"], check=True, stdout=subprocess.DEVNULL)
    mcp_token = secret(context, "mcp-token")
    broker_token = secret(context, "broker-token")
    credentials = config_dir / "credentials.json"
    credentials.write_text(json.dumps({"mcpToken": mcp_token, "brokerToken": broker_token}))
    credentials.chmod(0o600)
    os.chown(credentials, account.pw_uid, account.pw_gid)
    # Reaching the VM-only private LB is checked without installing the agent.
    cluster_status = check(context, ca_path, mcp_token, broker_token)
    subprocess.run(["loginctl", "enable-linger", account.pw_name], check=True)
    subprocess.run(["systemctl", "start", "user@" + str(account.pw_uid) + ".service"], check=True)
    installer = Path("/opt/ignite-lab/install-nemoclaw-pinned.sh")
    url = "https://raw.githubusercontent.com/NVIDIA/NemoClaw/" + COMMIT + "/install.sh"
    subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error", "--proto", "=https",
                    "--proto-redir", "=https", "--retry", "5", "--connect-timeout", "15", "--max-time", "180", url, "-o", str(installer)], check=True)
    if hashlib.sha256(installer.read_bytes()).hexdigest() != INSTALLER_SHA256:
        raise RuntimeError("Pinned NemoClaw installer checksum mismatch")
    installer.chmod(0o755)
    values = learner_environment(context, account, ca_path, mcp_token, broker_token)
    stage_environment(config_dir, account, values)
    agent_status = config_dir / "agent-ready.json"
    agent_status.unlink(missing_ok=True)
    # An author-requested redeployment resets an existing learner sandbox as well.
    # The initial deployment has no agent binary or sandbox to remove.
    subprocess.run(["runuser", "-u", account.pw_name, "--", "bash", "-c",
        'set -euo pipefail\nsource "$HOME/.config/ignite-lab/environment.sh"\n'
        'if command -v nemoclaw >/dev/null && nemoclaw sandbox status ignite-agent >/dev/null 2>&1; then\n'
        '  nemoclaw sandbox destroy ignite-agent --yes --cleanup-gateway\nfi'], check=True, timeout=300)
    preinstall = context.get("preinstallAgent", False)
    checks = {**cluster_status, "agent": "awaiting-learner-install", "installer": "checksum-verified"}
    if preinstall:
        log = Path("/var/log/ignite-agent-install.log")
        log.touch(mode=0o600, exist_ok=True)
        log.chmod(0o600)
        print("Author preinstall enabled; details are in the root-only agent setup log.", flush=True)
        with log.open("a") as output:
            result = subprocess.run(["runuser", "-u", account.pw_name, "--", "bash", "-c",
                'set -euo pipefail\nsource "$HOME/.config/ignite-lab/environment.sh"\n'
                'bash /opt/ignite-lab/install-nemoclaw-pinned.sh --non-interactive --yes-i-accept-third-party-software\n'
                'bash /opt/ignite-lab/infra/setup/setup_user.sh'], stdout=output, stderr=output, timeout=5400)
        if result.returncode or not agent_status.is_file():
            raise RuntimeError("Agent setup failed; inspect /var/log/ignite-agent-install.log as the lab author")
        verified = json.loads(agent_status.read_text())
        if verified.get("status") != "ready" or verified.get("setupRevision") != context["setupRevision"]:
            raise RuntimeError("Agent readiness did not match this setup revision")
        checks.update(verified["checks"])
    status.write_text(json.dumps({"schemaVersion": 2, "status": "ready" if preinstall else "infrastructure-ready",
        "phase": "agent" if preinstall else "infrastructure", "setupRevision": context["setupRevision"],
        "nemoclawCommit": COMMIT, "sandbox": "ignite-agent", "agentReadinessPath": str(agent_status),
        "checks": checks}, indent=2) + "\n")
    status.chmod(0o644)
    print("Lab ready: sandbox and MCP policy verified." if preinstall else
          "Infrastructure ready: applications, incident and model verified; learner installation settings staged.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, RuntimeError) else "VM lab setup failed; inspect root-only provisioning logs", file=sys.stderr)
        raise SystemExit(1)

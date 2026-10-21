# Learner installation kit and credential contract

> Core learner workflow verified in fresh Skillable instance 66301586; delivery checks remain open.

The platform prepares infrastructure, host tools and connection settings before
learner entry. Learners connect to Ubuntu, install the pinned NemoClaw release
and create the OpenClaw sandbox, attach managed MCP, customize `AGENTS.md` and
MCP policy, then perform the incident exercises. The author-only
`preinstallAgent=true` option prepares a recovery instance automatically;
learner delivery defaults to `false`. See [setup](infra/SETUP.md).

`infra/build-template.py` compresses an explicit nonsecret source allowlist
into `infra/main.bicep`. Provisioning verifies its SHA-256 and stages source
under `/opt/ignite-lab`. The Bicep remains self-contained for Skillable's editor;
readable setup source stays in `infra/setup/`.

The installer is pinned to NemoClaw v0.0.132, commit
`95d6505265ba1f2cb05355f83786353e654de096`, SHA-256
`539c25cf06c9adce2b7f5756d1acd253f8eddde6e59f60b5eace92c4a123a116`.
That release selects compatible OpenShell 0.0.116 and a managed image pinned
in its blueprint. Node and kubelogin are pinned/checksum-checked; Docker and
Azure CLI use signed apt feeds. This is an online install, not an offline
binary kit or fully frozen package set. Rehearse an event image and approved
cache/registry before scaled delivery.

No model key, TLS private key, MCP token, kubeconfig or attendee assignment is
embedded in source. The secure upstream credential goes only to provisioning
and the broker's Kubernetes Secret. A per-instance vault hands the VM limited
broker/MCP tokens and the public CA. OpenShell providers inject credential
placeholders into the sandbox. The limited credentials are readable to the
VM owner; the shared provider key is not supplied to Ubuntu. Student Azure
permissions must also prevent direct Secret retrieval; development subscription
Owner is not that boundary.

Infrastructure readiness is written only after host tools/Docker, three workload rollouts,
private service allocation, seeded incident, healthy dependency, MCP auth,
namespace/Secret isolation, real inference and checksum-verified installer
staging pass. Its root-owned `/etc/ignite-lab/ready.json` says
`infrastructure-ready` with `agent=awaiting-learner-install`.

Learners source the mode-0600 `~/.config/ignite-lab/environment.sh`, which uses
shell-quoted values and only instance-scoped credentials. They explicitly run
the installer with noninteractive configuration and software acceptance. This
pinned OpenClaw installer also performs onboarding; no unsupported standalone
defer flag is used. Then `infra/setup/setup_user.sh` attaches MCP and records
`~/.config/ignite-lab/agent-ready.json` only after managed MCP reads, denied
restart/scale, unapproved egress denial and a real agent reply pass. The
workspace starter is appended once, preserving managed instructions and edits.
The attachment step neither installs the agent nor destroys its sandbox.
The author preinstall mode runs both steps and requires both readiness stages
before reporting root `status=ready`.

Linux CI validates Bicep compilation, syntax, rendered manifests and broker
integration. Fresh instance 66301586 verified installation, customization,
real-model diagnosis, approval/recovery and remaining boundaries. See
[RESULTS.md](RESULTS.md) for the log-prompt correction and quality issues.
Live token rotation, new-learner timing, reconnection, reset/termination,
event package/region compatibility and a 40-person capacity rehearsal remain
release checks.
The separate model template's exact outputs remain unverified. Automatic
Foundry discovery uses Azure's account/deployment API instead: the setup identity
resolves the named per-instance account and selected model, reads the published
OpenAI endpoint and retrieves its key in memory. Only the AKS broker receives
the provider key. The role is scoped to the assigned resource group and is
never assigned to the VM identity. The fresh run reached inference readiness
and real-model agent calls through this path; missing resources or disabled key auth abort
setup rather than marking an infrastructure-only instance ready.

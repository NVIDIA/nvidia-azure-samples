# NVIDIA - Nemoclaw with Microsoft Foundry and AKS

> NemoClaw AI operations agent sample for Microsoft Ignite 2026

This package is a working draft for an Ignite 2026 hands-on lab using NVIDIA
NemoClaw/OpenShell on an Azure VM and a governed operations MCP server on
Azure Kubernetes Service (AKS). The earlier checkout/payments proof of concept
was tested end to end with mock inference. Fresh Skillable instance 66301586
has now passed the core learner incident and boundary workflow with real
inference. Delivery validation remains open as described below.

## Working from this repository

After cloning `NVIDIA/nvidia-azure-samples`, run sample commands from:

```bash
cd agentic/nemoclaw/build-ai-agent
```

The sample uses a CPU Ubuntu VM and CPU AKS cluster with hosted inference;
it does not require a local GPU. Follow [the setup guide](infra/SETUP.md) and
[learner instructions](LAB-GUIDE.md). See [import notes](IMPORT-NOTES.md) for
the source snapshot, packaging changes and validation scope.

## Lab documentation

The fresh October 16 run verified diagnosis, a denied restart, one approved
restart, recovery from HTTP 503 to 200, scale denial, scoped RBAC and the
matching egress-denial audit. The earlier backend 401 did not recur. The log
exercise needed a prompt correction, now included in the guides. Reply
formatting and unnecessary wait-helper errors remain quality issues.
See [the rehearsal record and remaining checks](RESULTS.md). The guides use
a Node.js edit because the sandbox has no interactive editor.

- [Word learner guide](docs/learner-guide.docx) is the latest shortened handout,
  with shaded command blocks, syntax coloring, expected results, troubleshooting,
  and a coordinator appendix. Its title is **NVIDIA - Nemoclaw with Microsoft
  Foundry and AKS**.
- [LAB-GUIDE.md](LAB-GUIDE.md) is its portable Markdown version, covering the same
  nine learner tasks and commands.
- [SKILLABLE-INSTRUCTIONS.md](SKILLABLE-INSTRUCTIONS.md) is the version to import or paste into the
  Skillable Instructions Editor. It includes per-instance replacement tokens,
  learner page breaks, connection steps and the complete agent exercise. Verify
  the `User1`, `ResourceGroup1` and `NemoClaw_AKS_Ignite` aliases in the profile
  and rehearse full provisioning before learner delivery. Insert tokens from
  the editor's **@** panel to confirm they match the active profile.
- `INSTRUCTOR-GUIDE.md` covers provisioning, validation, reset, and event
  operations.
- [Coordinator checklist](docs/COORDINATOR-CHECKLIST.md) contains the Word
  appendix's template settings, model throughput requirement, and release gates.
- `INSTALL-KIT-SPEC.md` defines the staged local package and credential
  contract and the implemented automatic provisioning path; the latest run and
  remaining delivery checks are in `RESULTS.md`.
- `assets/operations-agent-starter.md` is the short section pre-seeded into
  each attendee's OpenClaw `AGENTS.md` for the timed customization exercise.
- `E2E-RUNBOOK.md` links the current Skillable rehearsal path and preserves the
  historical mock-inference engineering run separately.
- `RESULTS.md` records the evidence, defects corrected, and remaining release
  gates.

The reviewed Word file is the source for both learner Markdown variants and
the coordinator checklist. After editing it, run:

```bash
python3 scripts/export-learner-docs.py
python3 scripts/export-learner-docs.py --check
```

The lightweight documentation workflow checks export parity, all nine tasks,
and shell syntax. It does not deploy or certify the lab. See
[document maintenance](docs/README.md) for the publishing steps.

The platform prepares a dedicated Ubuntu VM, a private AKS incident fixture,
a real hosted model connection through a per-lab broker, and an agent
installation kit and prepared connection settings. Participants connect
through Cloud Shell SSH, install NemoClaw/create their OpenClaw sandbox,
attach MCP, customize the live agent workspace and tool policy, then evaluate
behavior. Host prerequisites and application provisioning are automatic;
agent installation, onboarding and attachment are learner activities.
The core real-model flow passed in one fresh lab; reset, cleanup, sustained
token rotation and event concurrency remain release checks.

The proposed mandatory hands-on path is 45 minutes inside a 60-minute session.
The extra 15 minutes are reserved for recovery and discussion. The timing has
not yet been proven with the local kit, final Nemotron endpoint, or 40
concurrent attendees.

The participant diagnoses an intentionally unhealthy production API,
observes an untrusted prompt-injection-shaped log entry, attempts a blocked
restart, receives operator approval for only that restart, remediates the
application, and proves that scaling and arbitrary network exfiltration remain
blocked.

## Historical single-user validation architecture

```text
Azure VM: ignite-agent-example-vm
  NemoClaw CLI + OpenShell 0.0.116
  OpenClaw in an isolated sandbox
  host-managed credentials and L7 MCP policy
        |
        | temporary HTTPS endpoints, restricted to VM /32
        v
AKS: aks-dynamo-a100 / nooa-admission-test
  mock-inference      deterministic OpenAI-compatible endpoint
  aks-ops-mcp         authenticated Streamable HTTP MCP server
  checkout-api        intentionally stale configuration (prior fixture)
  payments-api        healthy dependency (prior fixture)
```

The temporary public LoadBalancer Services used for the end-to-end test were
restricted to `192.0.2.10/32`, protected by a two-day private-CA
certificate, and deleted after testing. Only ClusterIP Services remain.

## Skillable automatic setup template

`infra/main.bicep` is the self-contained file to paste into Skillable. It
creates one CPU-only Ubuntu VM, a private two-node CPU AKS cluster, VNet and
NAT, then deploys the incident workloads and stages the agent installation kit.
The learner uses standard Cloud Shell to SSH into Ubuntu and accesses AKS
through managed MCP after installing and attaching their agent in Task 1.
No Bastion is required.

Full provisioning adds a dedicated setup identity, a per-instance Key Vault,
a temporary Azure deployment-script environment, and a blocking VM extension.
The MCP pod hosts a model broker alongside MCP: the upstream model key stays
in AKS, while the VM receives only per-lab broker/MCP credentials. Setup verifies
TLS/authentication, the seeded HTTP 503 incident, scoped RBAC, real inference,
and prepared learner settings before writing `/etc/ignite-lab/ready.json` with
`status=infrastructure-ready`. Failure stops deployment. Agent readiness,
initial mutation denials and unapproved egress are checked by the learner's
attachment step and recorded in `~/.config/ignite-lab/agent-ready.json`.

Keep the existing Fireworks model template. With blank inference URL/key, setup
resolves the account `myfoundry<labInstanceId>` in the assigned resource group,
verifies the `NemotronLightning` deployment, and retrieves its endpoint/key
under the setup identity. These names come from the previous test and are
author-overridable. The provider key goes only to AKS; no manual learner key
entry is needed. An explicit external endpoint instead requires all three
securely configured inference inputs. Full setup is on by default;
`deployLabSetup=false` is an
explicit infrastructure-only rehearsal mode. Leave `preinstallAgent=false`
(the default) for the learner installation exercise. An author can set it
to `true` to prepare and verify a recovery instance automatically. The model
attachment's source/output tokens remain unverified, but discovery uses Azure's
account/deployment API rather than template outputs. The fresh run reached
inference readiness and completed real-model agent calls through this path.
A listed Foundry resource alone does not prove a usable model deployment.

For learner delivery, explicitly map `deployLabSetup=true` and
`preinstallAgent=false` in Studio and paste the current generated Bicep into
its saved cloud template. Studio runs its saved copy independently of GitHub.
The October 14 Fireworks test returned a finished reply from Cloud Shell with
a 1,024-token budget; provisioning now requires visible text and a completed
reply before marking inference ready. See `RESULTS.md` for the fresh learner
run and the remaining delivery checks.

See **[automatic setup and exact profile mappings](infra/SETUP.md)** for the
next portal steps, runtime logs, reset and termination checks. The template
metadata reference is `infra/skillable-cloud-shell-parameter-definitions.json`.
`allowAzureCloudSsh=true` permits SSH from the broad AzureCloud service tag;
password authentication is required. A fixed `sshSourceIPv4` gives a narrower
`/32` alternative. The historical helper `infra/connect-cloud-shell.sh` remains
available for instructor rehearsal only.

NemoClaw is pinned to v0.0.132 / commit
`95d6505265ba1f2cb05355f83786353e654de096`; its installer selects compatible
OpenShell 0.0.116 and the pinned managed OpenClaw image. The setup source kit
is compressed into Bicep by the author-only `infra/build-template.py`, so
Studio needs no local module/file imports. Installation uses online signed
package feeds and release/registry downloads; apt packages are not frozen.
Rehearse and prepare a tested event image before delivery.

Linux CI compiled the corrected Bicep and passed its setup checks. Fresh
instance 66301586 completed the core learner workflow; actual MCP records
verify recovery and isolation, and a matching OpenShell audit verifies the
direct egress denial. See [the evidence and its limits](RESULTS.md) for the
log-prompt correction and agent quality issues. New-learner timing,
reconnection, reset, termination cleanup, sustained live token rotation and
load validation remain pending. The author reports Skillable saved
`sku.capacity=100` with `DataZoneStandard` for `NemotronLightning` in
**Fireworks AI 2 Model (6838)**, used by profile **225403**. Verify actual
provider limits and allocation in every assigned subscription pool. This
model setting is outside `infra/main.bicep`.

At default sizes, each instance has 12 compute vCPUs: four on Ubuntu and eight
across two AKS nodes. Forty instances need 480 compute vCPUs plus spare and
transient provisioning capacity. Arrange sufficient Cloud Shell capacity;
Microsoft's [documented tenant default](https://learn.microsoft.com/azure/cloud-shell/faq-troubleshooting)
is 20 concurrent sessions.

## Compute and GPU requirements

The governance lab itself does not require a GPU:

- Every AKS workload requests zero GPUs and runs from a CPU Python image.
- A participant needs one isolated Linux environment with approximately 4
  vCPUs, 16-24 GiB RAM, 25 GiB free disk, and Docker.
- The validated VM was `Standard_NC4as_T4_v3`: 4 vCPUs, about 27.4 GiB RAM,
  and one 16 GiB Tesla T4.
- NemoClaw automatically enabled GPU passthrough and successfully proved
  `nvidia-smi`, device access, and `cuInit(0)` inside the sandbox. That GPU was
  not used by the remote mock inference endpoint.
- If Ignite uses hosted Nemotron through Microsoft Foundry, Fireworks, or an
  NVIDIA endpoint, allocate no per-attendee GPU. If inference runs locally in
  each sandbox, size and benchmark that model separately; one T4 is not a
  blanket recommendation for current large Nemotron models.

## Lab scenario

`service-api` and `dependency-api` are synthetic Python workloads, not an
Azure or NVIDIA product and not a retail use case. They make the AKS
operations incident repeatable: the operations agent has to identify a stale
runtime setting, request permission for the narrowest repair, and verify that
other actions remain blocked.

`service-api` initially reads
`DEPENDENCY_URL=http://dependency-api-invalid:8081`. The deploy script then
fixes the ConfigMap to `http://dependency-api:8081` without restarting the pod.
The desired configuration is correct, but the running process is stale and
its health endpoint returns 503.

The MCP server exposes eleven narrow tools. Its ServiceAccount can read only
the assigned namespace, cannot read Secrets, and can patch only the named
`service-api` Deployment. The MCP server also validates deployment names,
restart reasons, and scale bounds.

The intended participant flow is:

1. Connect to the prepared Ubuntu VM, install NemoClaw/create the sandbox,
   attach managed MCP and verify agent readiness.
2. Customize the agent's operations instructions and choose a tool profile.
3. Ask the sandboxed agent to inspect application health and configuration.
4. Read service logs, treating their remediation instruction as untrusted
   data.
5. Ask for a restart while `restart_deployment` and `scale_deployment` are
   denied; observe an OpenShell `policy_denied` result and no AKS mutation.
6. Simulate operator approval by changing the deny list to contain only
   `scale_deployment`.
7. Retry the restart and observe an authorized rollout of only
   `service-api`.
8. Verify application health is now 200.
9. Attempt to scale to two replicas and observe another policy denial.
10. Attempt outbound access to the synthetic exfiltration domain and observe a
   proxy 403 plus an audited OCSF denial.
11. Run the MCP RBAC self-test and confirm namespace and Secret isolation.

## Historical workstation deployment (not the Skillable bootstrap)

Prerequisites are `kubectl`, `openssl`, and access to context
`aks-dynamo-a100`. The namespace must not already exist.

```bash
./scripts/deploy.sh
./scripts/test-incluster-mcp.sh
```

`deploy.sh` creates private credentials under a mode-0700 temporary directory
and records only their paths in `.lab-state`. Neither credentials nor evidence
artifacts are committed.

For workstation-only AKS testing, use loopback port-forwards:

```bash
./scripts/start-port-forwards.sh
./scripts/test-aks-mcp.sh
```

NemoClaw managed MCP intentionally rejects loopback endpoints, so the full VM
test needs a routed HTTPS endpoint. The proof of concept used
`k8s/public-endpoints.yaml` only after explicit authorization. Its selectors
match no pods until certificates for the assigned IPs have been generated.

```bash
kubectl --context aks-dynamo-a100 apply -f k8s/public-endpoints.yaml
./scripts/rotate-public-tls.sh <mcp-public-ip> <inference-public-ip>
```

For a conference deployment, prefer private AKS ingress or VNet-integrated
routing. Public per-student LoadBalancers are a test convenience, not the
recommended production design.

## NemoClaw VM setup

The VM scripts are designed to be sent through Azure Managed Run Command.
Pass the inference and MCP URLs as normal parameters and the model/MCP keys as
protected parameters. `vm/install-nemoclaw.sh` scopes the private CA through
`CURL_CA_BUNDLE`, `SSL_CERT_FILE`, and `NODE_EXTRA_CA_CERTS`; it does not add a
lab CA to the system-wide trust store.

The registration policy begins with both mutation tools denied:

```text
nemoclaw ignite-agent mcp add aks-ops \
  --url <https-mcp-url> \
  --env AKS_OPS_MCP_TOKEN \
  --deny-tool restart_deployment \
  --deny-tool scale_deployment
```

The approval transition replaces that list with only the still-forbidden
scale tool:

```text
nemoclaw ignite-agent mcp update aks-ops \
  --deny-tool scale_deployment
```

OpenShell stores the real bearer token in its provider. OpenClaw receives a
namespaced `openshell:resolve:env:...` placeholder, which OpenShell resolves at
egress after enforcing the MCP method and tool policy.

`vm/mcp-call.mjs` is a deterministic diagnostic client. It reads the generated
URL and placeholder from OpenClaw configuration; it never reads or prints the
raw bearer token.

## Security controls exercised

- Non-root containers, read-only root filesystems, dropped Linux capabilities,
  and runtime-default seccomp.
- Namespace ResourceQuota and LimitRange.
- TLS for inference and MCP traffic.
- MCP bearer authentication: provider request 200, no-credential control 401.
- Host-managed credential substitution rather than sandbox-visible secrets.
- L7 MCP method and exact-tool enforcement.
- Kubernetes RBAC and server-side input validation as independent backstops.
- Default-deny sandbox egress with auditable OCSF events.
- Source-IP-restricted temporary exposure and explicit post-test removal.

## Cleanup

Delete the complete AKS test namespace with:

```bash
./scripts/cleanup.sh
```

For the validated run, the public Services, VM sandbox, MCP registration,
OpenShell gateway state, short-lived VM CA, and persistent Managed Run Command
resources were already removed. NemoClaw/OpenShell binaries remain installed
on the VM. The private AKS namespace and its CPU-only ClusterIP workloads were
left running for inspection.

See `E2E-RUNBOOK.md` for the exact reproduction sequence and `RESULTS.md` for
evidence, defects found, and the remaining model-quality caveat.

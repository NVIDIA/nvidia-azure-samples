# Instructor and Platform Guide: AI Operations Agent

> Instructor and platform guidance for Microsoft Ignite 2026

This guide covers provisioning, validation, reset, and operational boundaries
for the attendee lab in `LAB-GUIDE.md`. It is not an attendee handout.

## Current delivery package

Use the reviewed [Word guide](docs/learner-guide.docx), titled
**NVIDIA - Nemoclaw with Microsoft Foundry and AKS**, for coordinator review.
[LAB-GUIDE.md](LAB-GUIDE.md) and [SKILLABLE-INSTRUCTIONS.md](SKILLABLE-INSTRUCTIONS.md)
are generated from that shortened guide. They now use the same explicit MCP
prompts, fresh session IDs, native discovery check, and nine-task sequence:
installation, customization, tool policy, diagnosis, log handling, denied
restart, approval and recovery, boundary tests, and results.

The [coordinator checklist](docs/COORDINATOR-CHECKLIST.md) records profile
**225403**, AKS template **6891**, and model template **6838**. Ask the Skillable
team to retain `DataZoneStandard` and persist `sku.capacity=100` for
`NemotronLightning`; the author cannot edit that template. Verify pool capacity
and the new deployment's actual rate limits. Saving Bicep or instructions in
GitHub does not update Studio or this separate model allocation.

Fresh instance 66301586 passed the core learner incident and boundary workflow;
actual tool records and the matching egress audit are described in
`RESULTS.md`. The log exercise required a prompt correction now in the guide.
Live token rotation, new-learner timing, reconnection, reset, cleanup and
delivery concurrency remain release checks. Documentation checks do not
replace these tests.

During Task 5, run the health check before reading logs so the planted warning
is present in the recent tail. Confirm its presence in the tool result before
scoring injection handling. `/debug/config` 200 proves a configuration read,
not healthy application traffic. Record inconsistent reply formatting and
unnecessary `sessions_yield` errors separately from expected policy denials;
both occurred in this rehearsal. MCP status confirms connection readiness,
while actual calls and audit evidence establish enforcement.

## Reviewing a missing Task 4 summary

The expected Task 4 result is application HTTP 503, desired URL
`http://dependency-api:8081`, runtime URL
`http://dependency-api-invalid:8081`, and a proposed restart only. A message
that no final summary was produced is incomplete. It does not identify a
timeout, denial or learner mistake.

First confirm the displayed instruction revision is 27 October 2026. The
original command used a 150-second limit, a broad prompt and session
`ignite-investigate`; the current command names the three required reads and
uses a fresh session. Compare the actual command before attributing the
failure to the model or infrastructure.

On the Ubuntu host, inspect the recorded session before rerunning anything.
The following read-only command is for the original screenshot session.
Change only the session key if the learner used a different ID. It projects
tool metadata and the three diagnostic values; it excludes arbitrary logs,
credential settings and raw error bodies.

```bash
nemoclaw sandbox exec ignite-agent --timeout 60 -- \
  openclaw gateway call chat.history \
  --params '{"sessionKey":"agent:main:explicit:ignite-investigate","limit":60}' \
  --json |
jq '{
  toolEvents: [.messages[]
    | select(.role == "toolResult" or .role == "tool")
    | {tool: .toolName, isError: (.isError // false),
       policyDenied: any(.content[]?;
         .type == "text" and (.text | contains("policy_denied")))}],
  diagnosticReads: [.messages[]
    | select(.role == "toolResult" or .role == "tool")
    | .content[]? | select(.type == "text") | .text
    | try (capture("(?<payload>\\{[\\s\\S]*\\})").payload | fromjson) catch empty
    | select(.result.details.mcpServer == "aks-ops")
    | select(.result.details.mcpTool == "verify_application"
          or .result.details.mcpTool == "get_desired_config"
          or .result.details.mcpTool == "get_runtime_config")
    | .result.details.mcpTool as $tool
    | (try (.result.content[0].text | fromjson) catch {})
    | {tool: $tool, status, healthy,
       desiredUrl: .data.DEPENDENCY_URL,
       runtimeUrl: (.effective_dependency_url
         // .detail.dependency.effective_dependency_url)}]
}'
```

If required reads are absent or fail, stop and investigate that recorded
failure. An application HTTP 503 is the intended incident, not itself a
failed MCP transport call. If all required evidence is present but the
summary is absent, report the observed values and the missing summary
separately; do not repeat a mutation to generate prose. If already healthy,
the instance may have been repaired earlier and needs coordinator review.

To list tools, use Task 1's native `openclaw mcp probe aks-ops --json`.
`nemoclaw sandbox mcp ... status` reports connection/provider readiness and
does not list every tool or prove the deny list. Retain the expected
restrictions while investigating.

## Learning design

The attendee must leave with an agent they actually customized. The prepared
infrastructure is a starter kit; the participant edits the live OpenClaw
`AGENTS.md`, chooses an investigation preference, configures the managed MCP
deny list from two meaningful options, and tests the resulting behavior
against the incident and the security checks. Avoid spending the session
creating AKS or deploying a model; Docker and Node.js are VM
prerequisites. Learners run the pinned NemoClaw installer and create the
OpenClaw sandbox, then attach the supplied MCP connection. Infrastructure,
host tools and safe connection settings are prepared before learner entry.

The mandatory attendee path now targets 45 minutes within a 60-minute
conference slot. The remaining 15 minutes are a recovery and discussion
buffer, not a required extra exercise. This target is unvalidated: rehearse
the pinned kit and complete flow with people who did not build the environment
before publishing a duration promise.

| Time | Facilitator checkpoint |
| --- | --- |
| 0–2 min | Explain the synthetic AKS incident and operator role |
| 2–10 min | Ubuntu connection, pinned installation and sandbox creation |
| 10–15 min | MCP attached, agent verified and baseline response inspected |
| 15–20 min | `AGENTS.md` edited and before/after response compared |
| 20–23 min | Evidence-rich or privacy-first MCP profile selected |
| 23–29 min | Stale runtime configuration identified |
| 29–32 min | Untrusted log recognized as data |
| 32–36 min | Restart denied, then narrowly approved by operator |
| 36–40 min | `service-api` restarted and HTTP 200 verified |
| 40–45 min | Guardrail checks and attendee takeaway completed |

At minute 15, attendees without a working sandbox should move to an
instructor-prepared recovery environment so they can complete the agent build.
This fallback must be available per attendee, not shared across attendees.
The final five-minute block requires one guided guardrail-check path; if it
is not implemented and rehearsed, lengthen the lab rather than claiming a
45-minute completion.

## Delivery model

For each attendee, provide:

- one dedicated Ubuntu Azure VM with Docker, Node.js, and the pinned local
  installation kit for OpenShell, NemoClaw, and the managed OpenClaw image;
- an editable OpenClaw main-agent workspace and Node.js for the instruction
  changes; the pinned sandbox does not include `nano`;
- one dedicated two-node CPU AKS cluster with the `ignite-lab` application
  namespace, as implemented by the current Bicep;
- one attendee-specific operations MCP instance, service account, RBAC role,
  credential, and route;
- the Foundry account and `NemotronLightning` deployment created by the
  per-instance Fireworks attachment.

Provide centrally:

- subscription-pool quota and model allocation for the agreed attendee count;
- approved access to the runtime package feeds and container downloads;
- automated provisioning, readiness checks, reset, and termination cleanup.

A shared AKS cluster or shared model route would be a separate delivery
configuration requiring its own isolation and load validation.

No attendee VM or AKS workload requires a GPU when inference is hosted by
Microsoft Foundry.

## Skillable infrastructure and onboarding

### Assigned development profile and event process

Naim Jones's September 30, 2026 email identifies the assigned development
profile as **NIMS 13: NVIDIA - Agent Toolkit with Microsoft Foundry and AKS**.
Use that existing profile. Sign in at `https://labondemand.com/`, choose
**Corporate AAD**, and use the corporate account that received the invitation.
The email did not include the profile ID; later authoring context identifies
it as **225403**. User-supplied Studio screenshots establish the following
development configuration; these observations are not a successful deployment test:

- Hyper-V Windows 11 desktop profile `ignite26NIMS13`, displayed and aliased as
  `Win11-CL-01`, with 8 vCPUs and 12 GB RAM. Use it for Azure Portal and Cloud Shell;
  the template creates a separate Ubuntu VM for the agent runtime.
- Azure subscription pool
  `#P0000217-PROD-CSSR-PRODMCA-NVIDIA Events A100v4`, with six listed US regions,
  Temporary Access Pass enabled, and deployment-failure notification enabled.
- Cloud account alias `User1`, resource group alias `ResourceGroup1`, Owner
  permissions, and the development Allow All policy. Reuse these aliases.
- Resource template `Fireworks AI 2 Model`, alias `FireworksAI2Model`, attached
  to `ResourceGroup1` with background deployment disabled. Opening its source
  returned Access Denied, while the new Bicep-template form is accessible.
- A blocking Pre-Build provider-registration action and a blocking teardown
  action that deletes and purges Cognitive Services resources. The visible
  query does not filter by instance resource group; review its scope before
  using shared inference resources.

The profile is In Development with a six-hour duration. Keep sufficient time
for development tests, then set the agreed delivery duration for the event.
Instructions and the final runtime/bootstrap path still need configuration
and rehearsal.

The event process requires both the environment and the manual to be complete
and confirmed Show Ready by the **Lab Functionality Sign Off Date**. The email
does not state that date; confirm it with the event team. After sign-off,
expect testing corrections rather than unfinished development, and perform
the required onsite functionality checks before event delivery.

Maintain the Word source and regenerate its Markdown exports using
[the document maintenance steps](docs/README.md). Import
`SKILLABLE-INSTRUCTIONS.md` into Studio, or configure and test Skillable's
access to that file in the private repository. Preview the student rendering
and verify all replacement tokens before sign-off. References:
[Markdown instructions](https://docs.skillable.com/docs/creating-instructions-with-markdown-syntax),
[instruction-writing guidance](https://docs.skillable.com/v1/docs/instructions-writing-best-practices),
and [Studio terminology](https://docs.skillable.com/docs/skillable-studio-basics-terminology-als).

Map instance-specific template values through replacement-token parameters,
and use lab variables for nonsecret values needed later in the manual or
automation. Never use a masked student textbox as a substitute for protecting
shared platform credentials. References:
[replacement tokens](https://docs.skillable.com/docs/replacement-tokens) and
[variables](https://docs.skillable.com/docs/variables-1).

For a Skillable-managed VM profile, commit tested baseline changes through
the differencing-disk capture workflow, then launch a fresh instance to verify
they persist. This differs from saving a personal lab session for later
resume. Do not assume that workflow captures an Azure VM created by Bicep;
the current Azure-VM design uses its tested generalized image and reproducible
setup instead. Reference:
[committing VM changes](https://docs.skillable.com/docs/committing-changes-to-a-lab).

### Template scope and delivery choice

Use `infra/main.bicep` as the per-instance Cloud Resource Template. Each
Skillable resource group receives one Ubuntu attendee VM with a public IP,
one private AKS cluster with a CPU-only system node pool, a VNet, and explicit
outbound NAT. Standard Cloud Shell SSHs to Ubuntu; Ubuntu reaches the private
AKS and MCP endpoints through its VNet. Configure SSH access during provisioning
so learners use a normal SSH command. `sshSourceIPv4` allows a known public IPv4
`/32`. `allowAzureCloudSsh=true` instead adds TCP 22 access from AzureCloud,
including other customers' public Azure IPs. This is broader than a lab- or
tenant-specific rule; it is an explicit access-design choice. With both inputs
unset or false, SSH starts closed. Each instance has independent resources and
teardown. No Azure Bastion is deployed.

This Bicep template does not provision the model. Keep the existing
`Fireworks AI 2 Model` attachment while creating the infrastructure template;
verify its source, model, inference URL, authentication, and output names with
the template owner. It may provide an instance-specific deployment; a shared
deployment or broker is another delivery choice that must be configured
explicitly. Do not assume the existing attachment provides a working agent
endpoint merely because it is listed in the profile.

Application setup is automatic and on by default. Blank `inferenceEndpoint`
and `inferenceApiKey` now discover the same-group Foundry account
`myfoundry<labInstanceId>` and use `NemotronLightning` unless `inferenceModel`
overrides it. Account naming and deployment selection match the previous test;
verify them in a fresh run. The setup identity reads the published endpoint/key,
with a five-minute discovery limit, and requires a finished, nonempty reply
through the broker before readiness. The probe allows 1,024 tokens, matching
the successful Fireworks rehearsal instead of accepting a reasoning-only
128-token response. For an explicit endpoint, map all three inference
inputs securely and select its authentication header. A partial explicit
connection fails. Use `deployLabSetup=false` only for an infrastructure
rehearsal. Keep the shared model key off the VM.

For the learner profile, explicitly map `deployLabSetup=true` and
`preinstallAgent=false` and paste the current generated `infra/main.bicep`
into Studio. The saved Studio template and its profile overrides are separate
from GitHub. Clear per-instance inference values left from a manual repair
when using automatic discovery. A learner launch must finish application,
MCP, broker, host-tool and connection preparation before the learner installs
NemoClaw. See [exact Studio setup steps](infra/SETUP.md).

The new broker container in the MCP pod holds it and exposes a per-instance
OpenAI-compatible route. Supported upstream authentication is Bearer or
`api-key`; a different provider API needs a compatible upstream gateway.

The foreground deployment action applies rendered CPU workloads, creates TLS
and MCP/broker credentials, seeds the incident, and runs readiness checks.
A single `ignite-lab-setup` Custom Script Extension installs host tools as
needed, stages the checksum-verified installer and per-lab settings, and
checks platform readiness. By default `preinstallAgent=false`, root
`/etc/ignite-lab/ready.json` reports `infrastructure-ready` and learners run
the install/onboarding and MCP attachment steps themselves. Their attachment
step appends the starter once and verifies agent, mutations and egress before
writing `~/.config/ignite-lab/agent-ready.json`. Author recovery instances may
use `preinstallAgent=true` to run both stages during provisioning. Host logs
are in `/var/log/ignite-vm-prerequisites.log`; root-only
`/var/log/ignite-agent-install.log` is used only for author preinstall mode.
A tested custom image may skip host packages; application and kit staging
still run.

Follow **[infra/SETUP.md](infra/SETUP.md)** for exact profile values, setup
identity permissions, runtime dependencies, failure diagnostics, reset and
termination checks. The actual model attachment/output names and live
Skillable setup remain unverified. Private upstream model endpoints in another
VNet still require connectivity/DNS supplied by the platform.

### Apply the recording's onboarding flow

The supplied recording shows the existing AI-Q lab's Cloud settings, a Bicep
Resource Template editor, a Parameters tab, template attachment with a
replacement-token alias, and the Deploy in Background checkbox. Reuse that
workflow, with this lab's resources and parameters:

1. Open the assigned **NIMS 13: NVIDIA - Agent Toolkit with Microsoft Foundry
   and AKS** profile. On **Cloud**, use its configured Azure subscription pool
   and confirm CPU capacity and the model route in the selected region. Keep
   the development duration while testing; set the delivery duration to the
   agreed 60 minutes before the event and retain In Development status until
   launch/reset testing passes.
2. Reuse the existing cloud portal account alias `User1` and per-instance
   resource group alias `ResourceGroup1`. Owner permissions and the
   development Allow All policy are development settings. For attendees,
   Reader access to the instance's VM, NIC, subnet, public IP, and NSG permits
   resource discovery. Direct SSH with a preconfigured rule needs no learner
   NSG write/delete permission. The optional instructor connection helper does
   require those permissions on the instance's VM NSG. The current development
   Owner role includes them; use a scoped role for event delivery.
   Provisioning runs in a separately authorized platform context. Do not give
   the agent cluster-admin access.
3. Under **Resource Templates**, create an Azure **Bicep Template** named
   `NemoClaw_AKS_Ignite`, owned by **NVIDIA - Development**. Keep Enabled checked
   and Use External Template unchecked. Paste the complete `infra/main.bicep`
   source and save. Do not paste `main.bicepparam` into the template editor.
4. On **Parameters**, add the mappings below. Then attach the saved template
   to `ResourceGroup1`, retain its existing alias `NemoClaw_AKS_Ignite`, and set
   the values through **Edit Parameters**. Keep the existing model template alongside it. Use the
   UI's replacement-token picker to confirm tokens available in this profile.
   If the new template card has no **Edit Parameters** link, check the
   template's **Edit → Parameters** tab. In the observed Bicep editor,
   **Import from JSON** extracts all parameters from the saved Bicep source;
   it does not open a JSON paste dialog. Retain the inputs listed in
   `infra/SETUP.md`, including the secure model credential and model settings. The two JSON definition files in
   `infra/` are reference definitions for these input sets. Remove unused
   defaulted parameter rows, any `Unnamed_Parameter` row, and stale
   `deployBastion` metadata from the Parameters tab, leaving the Bicep source
   intact. Save the template, reload the profile, and assign the values.
   Keep **Display to Student** on for the assigned VM username and password
   so learners can find them on the Resources tab, and include them in the
   connection instructions. Hide the technical and inference settings. This
   checkbox controls Resources visibility, not whether learners edit inputs.
   Remaining inputs use Bicep defaults unless separately mapped. For automatic
   Foundry discovery, remove the three inference metadata rows or make them
   optional and leave URL/key mappings blank. Keep the separate Fireworks model
   attachment enabled; no Foundry key is entered by a learner. Set
   **Display to Student** off for `allowAzureCloudSsh` and assign `true` on the
   lab profile's template attachment when the broader AzureCloud source range
   is selected. The automatic stock-image prerequisites use the Bicep default
   and require no additional profile mapping. Keep `preinstallAgent=false`
   (the default) for the learner installation exercise; hide its entry field.
5. Initially leave **Deploy in Background** unchecked. The recording enables
   it for another lab, but our opening exercise depends on this VM and AKS.
   Use a blocking post-build readiness action before showing the lab. For the
   event, pre-instance environments early enough that learners do not spend
   their timed exercise waiting for Azure provisioning.

Skillable compiles Bicep at launch; validate locally first because the Studio
editor does not perform Bicep semantic checks. Platform reference:
[Use Bicep in Azure Cloud Slice Labs](https://docs.skillable.com/docs/use-bicep-in-azure-cloud-slice-labs).
For foreground/background behavior, see
[Configure a Resource Template](https://docs.skillable.com/v1/docs/configure-a-resource-template-to-a-resource-group).

### Parameter mapping

| Parameter | Skillable value | Learner visibility |
| --- | --- | --- |
| `location` | Default `westus` (West US), matching the reported SKU availability list; verify eligibility and quota in the assigned pool. | Optional |
| `namePrefix` | `ignite-agent` | Hidden |
| `labInstanceId` | `@lab.LabInstance.Id` | Hidden |
| `vmAdminUsername` | `labuser` | Show in Resources and connection instructions |
| `vmAdminPassword` | Per-instance VM credential, supplied as a secure parameter | Show the assigned VM login credential in Resources and connection instructions |
| `vmImageResourceId` | Exact tested generalized x64 image-version resource ID | Hidden |
| `installVmPrerequisites` | Omit: defaults to `true` for stock Ubuntu, `false` for a custom image. Explicit `true` requires Ubuntu 24.04 amd64. | Hidden |
| `foundryAccountName` | Omit: defaults to `myfoundry<labInstanceId>` in the assigned resource group. Override if the model template naming changes. | Hidden |
| `inferenceEndpoint` | Leave empty for Foundry discovery; an explicit connection needs its actual HTTPS API base URL. | Hidden, optional |
| `inferenceModel` | Empty selects `NemotronLightning` for discovery; override only with an actual provisioned deployment name. An explicit connection requires its exact model ID. | Hidden, optional for discovery |
| `inferenceApiKey` | Leave empty for Foundry discovery; explicit credentials require a secure platform mapping, never a source literal. | Hidden, optional for discovery |
| `inferenceAuthHeader` | Discovery uses `api-key`; explicit connections select `Authorization` (Bearer) or `api-key`. | Hidden |
| `deployLabSetup` | Omit: defaults to `true`. Explicit `false` is infrastructure-only testing. | Hidden |
| `setupRevision` | Omit: defaults to `1`. Change only for an author-requested full reset. | Hidden |
| `bootstrapPrincipalObjectId` | Object ID of the platform bootstrap service principal, if using the optional AKS role assignments | Hidden |
| `kubernetesVersion` | Region-supported version pinned after rehearsal | Hidden |
| `vmSize`, `aksNodeVmSize` | Default `Standard_D4s_v6`, or pool-approved x64 CPU equivalent | Hidden |
| `aksNodeCount` | `2`, the documented system-pool minimum; benchmark before event | Hidden |
| `sshSourceIPv4` | Known source IPv4 configured by the instructor/platform for a narrow `/32` SSH rule; otherwise empty | Hidden |
| `allowAzureCloudSsh` | `true` for direct standard Cloud Shell SSH with AzureCloud as the allowed source; default `false`. Includes other Azure customers, not only Cloud Shell or this tenant. | Hidden |

Select a dedicated VM credential pool if available. The demonstrated
`@lab.CloudPortalCredential(User1).Password` token can be reused for VM login
only if the platform approves that choice and the generated value satisfies
the VM password requirements, including this template's 12-character minimum.
Never map a shared model key to a student-visible parameter. Official token
syntax: [Replacement Tokens](https://docs.skillable.com/docs/replacement-tokens).

The optional `bootstrapPrincipalObjectId` assignments are for a separately
approved platform operator, not the learner VM. The automatic deployment
script uses its own setup identity with AKS Contributor for the management
command and AKS RBAC Cluster Admin for Kubernetes provisioning, both scoped
to this lab's cluster. It waits up to five minutes for access before writing
workloads. The VM's identity can read its own vault's limited MCP/broker tokens
and CA, and has no AKS role or kubeconfig. A subscription Owner can assign
themselves further access, so scope attendee roles before delivery; Owner alone
does not grant Kubernetes data-plane access.

### Platform launch, reset and termination

Use the automatic path in [infra/SETUP.md](infra/SETUP.md). Confirm pool policy
allows the private cluster, its managed node resource group, managed identities
and role assignments, Key Vault, and the deployment script's temporary
Container Instance/Storage resources. Leave Deploy in Background unchecked.
The template waits for applications and agent readiness; no separate learner
bootstrap is required.

After mapping real model settings, launch a fresh instance and complete the
learner scenario. Test denied restart, operator-approved restart, health
recovery to 200 and continued scaling, namespace/Secret and egress denial.
Reset by redeploying the same secure inputs with a new `setupRevision`; this
recreates the learner sandbox and incident and discards learner changes.
Record both main and node resource group outputs for termination checks.

Review the existing `Clean Up AI Models` lifecycle action: restrict it to
instance-owned resources instead of subscription-wide discovery/deletion.
Skillable termination must remove the assigned main resource group and AKS's
managed node group, while leaving an externally shared model untouched.
`infra/check-teardown.sh` verifies both groups are gone without deleting
anything. Key Vault soft-delete retention follows pool policy.

The default infrastructure consumes twelve compute vCPUs per instance
(four for the attendee VM and eight for its two AKS nodes), plus NAT and public IPs.
Forty instances therefore require roughly 480 compute vCPUs at those defaults,
before any recovery capacity. Check SKU-family quotas and cost with the event
pool owner. Check Cloud Shell tenant capacity as well: Microsoft documents a
default limit of 20 concurrent users per tenant. Arrange an increase with
Skillable/Microsoft before a 40-attendee event if those users share a tenant.

The two-node default and parameter minimum follow the currently documented
[AKS system-pool restrictions](https://learn.microsoft.com/azure/aks/use-system-pools).
The previous one-node draft was corrected during the template review.

### Cloud Shell connection

Prepare the network access in the template or platform lifecycle. Standard
Cloud Shell starts after deployment; its current session's source address is
not an input available to the initial Bicep deployment. Choose a known,
platform-managed source and terminal path for a narrow rule, or opt into
`allowAzureCloudSsh=true` for direct standard Cloud Shell SSH. The latter permits
the AzureCloud service tag on TCP 22 to this VM's private IP. Microsoft manages
the tag's address ranges; it also includes IPs used by other Azure customers.
It does not limit access to Cloud Shell, this subscription, or this tenant.
Reference: [Azure service tags](https://learn.microsoft.com/en-us/azure/virtual-network/service-tags-overview).

Register the optional boolean on the template's Parameters tab, using
`infra/skillable-cloud-shell-parameter-definitions.json` as a reference. Hide
parameter entry from students and assign `true` in the profile's **Cloud → ResourceGroup1 →
NemoClaw_AKS_Ignite → Edit Parameters** dialog for this mode. Preserve the
existing username, password-token, and instance-ID mappings. Save the template
and profile, launch a fresh instance, and check the NSG contains
`AllowSshFromAzureCloud` plus the deny rule. The SSH allow rule persists until
the infrastructure is changed or torn down; it is not removed when SSH exits.

The attendee connection steps then need no script download, editor, Azure CLI
firewall command, or rule cleanup:

1. Launch the assigned lab and open Azure Portal with its temporary account.
2. Open **Cloud Shell → Bash**. The **No storage account required** option is
   sufficient for an SSH client; persistent Cloud Shell storage is optional.
3. Obtain the Ubuntu public IP from the VM's Overview page and run:

   ```bash
   ssh labuser@VM_PUBLIC_IP
   ```

4. Use the assigned **Ubuntu VM** login password at the SSH prompt. Keep
   OpenSSH host-key verification enabled and verify the initial fingerprint
   against the one supplied by the platform. Once connected, all installation
   and agent work runs on Ubuntu. Its private VNet route reaches AKS/MCP.
5. Run `exit` to return to Cloud Shell.

`infra/connect-cloud-shell.sh` remains available for instructor rehearsal of
the original single-source `/32` design. It discovers the outward IPv4 using an
HTTPS IP-echo service, allows that source on TCP 22, and removes its own rule
when SSH exits. Run it with the assigned subscription and resource group;
append `--close` after abrupt session termination. A supplied `--source-ip IPV4`
must be one public IPv4. Its cleanup does not remove `AllowSshFromAzureCloud`.
Use it with `allowAzureCloudSsh=false` when testing single-source isolation.

Cloud Shell authentication is not forwarded into Ubuntu by SSH. The agent
uses the preconfigured MCP service and its namespace-scoped ServiceAccount;
direct `kubectl` from Ubuntu additionally requires an authorized Kubernetes
identity and credential setup. Preserve the separate platform bootstrap role.
References: [Linux VM SSH](https://learn.microsoft.com/azure/virtual-machines/linux-vm-connect),
[Cloud Shell storage and concurrent-user limits](https://learn.microsoft.com/azure/cloud-shell/faq-troubleshooting),
and [private AKS connectivity](https://learn.microsoft.com/azure/aks/private-cluster-connect).

### Local validation and Azure rehearsal

Compile with the Bicep CLI or Azure CLI:

```bash
az bicep build --file infra/main.bicep
python3 tests/test_cloud_shell_connection.py -v
```

For a separately authorized Azure infrastructure rehearsal, set the required
`IGNITE_LAB_VM_PASSWORD` environment variable using the platform's secret
handling. `IGNITE_LAB_INFERENCE_ENDPOINT` and `IGNITE_LAB_INFERENCE_MODEL` can
be omitted for this initial infrastructure test, but must be resolved and
bound before agent onboarding. Optionally set `IGNITE_LAB_VM_IMAGE_ID` and
`IGNITE_LAB_BOOTSTRAP_OBJECT_ID`. Do not print the password or commit the
compiled parameter JSON. Against the assigned development resource group:

```bash
az deployment group what-if \
  --resource-group "$IGNITE_LAB_RESOURCE_GROUP" \
  --parameters infra/main.bicepparam
```

Compilation confirms template syntax and resource-property typing. What-if
and a clean-instance deployment must still check pool policy, RBAC propagation,
regional capacity, custom-image access, private DNS, terminal connectivity, and
cleanup. No Azure or Skillable deployment has been performed by authoring this
template.

### Template review and remaining deployment checks

The reviewed template provisions infrastructure and stock-image host tools.
Agent installation, onboarding, and application bootstrap remain separate.
Local Bicep compilation and parameter compilation pass. The private
AKS configuration, VNet-scoped Network Contributor assignment, preconfigured
user-assigned NAT gateway, and public-VM SSH path follow documented Azure
patterns. On October 9, the developer observed a successful Skillable
infrastructure deployment, a running Ubuntu VM, and the two-node AKS pool.
A later fresh launch with `allowAzureCloudSsh=true` reached the Ubuntu
`labuser` prompt from Cloud Shell. A clean deployment of the newly added
prerequisite extension is still required. References:
[private AKS](https://learn.microsoft.com/azure/aks/private-clusters),
[AKS NAT gateway](https://learn.microsoft.com/azure/aks/nat-gateway),
[internal load balancer](https://learn.microsoft.com/azure/aks/internal-lb), and
[Linux VM SSH](https://learn.microsoft.com/azure/virtual-machines/linux-vm-connect).

The Cloud Shell connection helper is exercised with simulated Azure CLI and
SSH commands: one-address SSH access, invalid-source rejection, assigned-group
scope enforcement, cleanup after SSH failure, and preservation of a newer
session's rule. These are local tests of the optional narrow-source helper;
its source-address detection and NSG lifecycle still need a live rehearsal.
The direct Cloud Shell login above validates the selected connection path,
but not the new host-package installation or application readiness.

The prerequisite extension's shell and embedded Python syntax, VM/AKS
deployment dependencies, nonroot execution guard, and pinned Node.js and
kubelogin release downloads were checked locally. No Linux installation was
executed by those checks. In the next fresh instance, confirm that the
`ignite-lab-setup` VM extension reports success, then inspect:

```bash
command -v az kubectl kubelogin docker node
python3 -m json.tool /etc/ignite-lab/context.json
python3 -m json.tool /etc/ignite-lab/prerequisites.json
docker info --format '{{.ServerVersion}}'
```

Require `status: ready` and successful Docker access as the VM user. If the
extension fails, use its provisioning diagnostics and
`/var/log/ignite-vm-prerequisites.log` to repair the template; do not turn
package installation into a learner task. Node.js and kubelogin releases are
pinned; signed-repository Docker and Azure CLI packages must be frozen in the
event image after rehearsal.

The review also corrected cloud-init context generation: plain Bicep
multiline strings preserve `${...}` literally, so the initial draft would
have written an invalid context file. The template now explicitly replaces a
marker with base64-encoded JSON and asks cloud-init to decode it. This avoids
requiring newer multiline-interpolation syntax in Skillable's compiler.
See [Bicep string functions](https://learn.microsoft.com/azure/azure-resource-manager/bicep/bicep-functions-string).
After correction, the compiled ARM context-substitution expression was checked
and its cloud-init payload was parsed as YAML. The base64 content decoded to
the expected JSON, including quoted parameter values. These are local checks,
not evidence that Azure provisioned the VM or executed cloud-init.

The deployment rehearsal must confirm:

- provider registration, allowed resource types, API versions, region, SKU
  capacity, Compute quota, and AKS managed-cluster quota;
- authorization for role assignment creation, managed node-group creation,
  and subsequent deletion;
- the deployment identity's access to the generalized VM image;
- private API DNS resolution and connectivity from the platform bootstrap
  runner, and VM-to-MCP connectivity after the internal Service is created;
- the real inference API, model ID, authentication, tool selection, and
  response time;
- host credential handling, including the learner-root threat described
  above; hiding secrets from the agent alone does not meet that requirement;
- the final clean-VM installation path and idempotent launch/reset actions.

The current `scripts/deploy.sh` and `vm/` scripts retain proof-of-concept
cluster, namespace, user, path, and mock-model assumptions. The Kubernetes
manifests also use the old namespace and private address. They must be rendered
for `ignite-lab` and this instance's outputs before platform automation can
use them. The implemented `infra/setup/` renderer and staged installation kit
replace those historical helpers for Skillable. Fresh instance 66301586
verified their core learner workflow; see `RESULTS.md` for its scope and the
remaining delivery checks. Template compilation alone does not validate them.

### End-to-end Skillable delivery flow

This is the delivery contract. The infrastructure, bootstrap actions and
staged installation kit supported the core workflow in fresh instance
66301586. Reset, termination and event-scale delivery still require rehearsal.

1. **Prepare the event platform once.** Pin runtime versions and image
   digests, build and test the generalized VM image and local kit, configure
   the separate inference deployment or broker, and confirm event-pool policy
   and capacity. Configure the assigned lab profile and parameter mappings described
   above. Attendees do not provision the model or cluster.
2. **Launch or pre-instance a lab.** Skillable assigns its instance ID,
   cloud account, resource group, and unique VM credential. It supplies the
   approved region, image ID, and nonsecret inference settings to Bicep.
3. **Deploy the infrastructure.** Bicep creates the VNet, NAT, Ubuntu VM with
   its public IP and NSG, AKS identity, private two-node AKS cluster, and optional
   platform AKS role assignments. The selected SSH rule is configured during
   deployment. Azure also creates the AKS managed node resource group.
   VM cloud-init writes `/etc/ignite-lab/context.json`. For stock Ubuntu, the
   blocking prerequisite extension then installs and validates host tools;
   a rehearsed custom image supplies them instead.
4. **Bootstrap the application.** A privileged platform runner with private
   VNet connectivity renders and deploys the namespace, quota, RBAC,
   `dependency-api`, `service-api`, and MCP server. Its ServiceAccount can
   read the assigned namespace and patch only `service-api`; it cannot read
   Secrets or another namespace. The event model must use the tested real
   inference deployment.
5. **Establish the private MCP endpoint.** Generate an instance-specific
   bearer token and TLS certificate, create the internal Service at
   `10.42.4.10:8443` in `lab-services`, and allow the VM at `10.42.1.4`.
   Verify authentication and TLS from the VM. Keep bootstrap cluster
   credentials out of the attendee image and sandbox.
6. **Seed the incident and check launch readiness.** Start `service-api`
   with the invalid dependency URL. After its pod starts, correct the desired
   ConfigMap without restarting it. Require dependency health 200, service
   health 503, and differing desired/runtime URLs. Also check the SSH service
   and provisioned VM access path, Docker, Node.js, kit integrity, model
   reachability, and authenticated MCP reachability. Record the VM's SSH host
   fingerprint through the authorized provisioning channel and display it in
   the instance's connection instructions. No attendee sandbox is required at this stage.
   Use a blocking lifecycle action with failure handling so learners are not
   shown an incomplete environment. Configure the actual platform runner
   and its private route; a cloud action by itself does not supply that route.
7. **Attendee connects, installs, and onboards.** The attendee opens Bash in
   Cloud Shell and uses the supplied SSH command. The platform configures the
   selected SSH source rule during provisioning; the learner supplies only
   the assigned VM login credential. The attendee verifies the kit, installs OpenShell and
   NemoClaw, then onboards OpenClaw into `ignite-agent`. A tested platform attachment helper supplies
   the model and MCP connection through the approved credential flow, starts
   with restart and scale denied, and appends the starter agent instructions
   once. Check runtime, one real-model response, attached MCP, and the deny
   list before continuing. Follow the implemented commands in Task 1 of
   `LAB-GUIDE.md`; repeat with new learners to verify timing and repeatability.
8. **Attendee builds and investigates.** Follow `LAB-GUIDE.md`: customize
   `AGENTS.md`, compare responses in fresh sessions, select an evidence-rich
   or privacy-first tool profile, and diagnose the stale dependency address.
   Expected read tools include `verify_application`, `get_desired_config`,
   `get_runtime_config`, and, when allowed, `get_logs`. Log instructions are
   treated as untrusted data.
9. **Attendee verifies the approval boundary.** First request restart and
   confirm `policy_denied`, no Deployment change, and continuing HTTP 503.
   As the operator on the VM host, replace the MCP deny list with only
   `scale_deployment`. Start a fresh agent session, request the approved
   restart, and verify the new pod uses the corrected URL and health is 200.
   This approves restart calls for `service-api` until policy is changed
   again; it is not a single-use approval token. Restore both mutation
   denials after remediation if no more approved actions are needed.
10. **Attendee checks the remaining boundaries.** Confirm scale remains
    denied, the MCP RBAC self-test returns assigned namespace 200 and
    other-namespace/Secret 403, and the unapproved network destination is
    denied with an OpenShell audit event. Save only nonsecret results and
    summarize which decisions came from the model and which policies
    enforced the limits.
11. **Reset or tear down.** For reset, use the tested platform procedure to
    restore a clean attendee VM/runtime, recreate the stale-runtime incident,
    replace per-instance credentials as needed, and restore both mutation
    denials. Merely changing the ConfigMap back is insufficient: a running
    pod may retain the repaired environment. At session end, remove the
    sandbox and instance resources, verify deletion of the AKS node group,
    invalidate instance credentials, remove any instance-owned model resources,
    and preserve external shared inference resources. Do not assume Skillable
    Save/Resume performs reset.

The attendee portion targets 45 minutes within a 60-minute session:

| Minutes | Activity | Observable result |
| --- | --- | --- |
| 0–2 | Connect and review the incident | Assigned terminal and operational objective |
| 2–10 | Install the staged runtime | OpenShell and NemoClaw ready |
| 10–15 | Onboard and attach model/MCP | Real-model response; both mutations denied |
| 15–20 | Customize the agent | Before/after behavior difference |
| 20–23 | Choose a tool profile | Read-only investigation boundary |
| 23–29 | Investigate | Stale runtime URL identified |
| 29–32 | Inspect the malicious log | Instruction treated as evidence, not authority |
| 32–36 | Test denial and approve restart | First attempt blocked; only restart then allowed |
| 36–40 | Restart and verify | HTTP 503 becomes HTTP 200 |
| 40–45 | Check guardrails and save results | Scale, namespace, Secret, and egress denials |

These are target timings, not measured Skillable results. Rehearse a fresh
instance, a reset instance, and concurrent event starts before publishing
the timing. For blocking and retry behavior, see
[Skillable lifecycle actions](https://docs.skillable.com/docs/life-cycle-actions-5).

## Private repository handling

- Keep the repository private until the event content is approved for release.
- Do not commit `.lab-state`, `artifacts/`, API keys, MCP bearer tokens,
  certificates, kubeconfig files, Foundry keys, or attendee assignments.
- Inject Foundry and MCP credentials through the provisioning platform's secret
  mechanism.
- Keep the real credential in the host-side OpenShell provider. The sandbox
  should contain only an OpenShell credential placeholder.
- Rotate all workshop credentials after rehearsals and between event runs when
  practical.

The existing `.gitignore` excludes the proof-of-concept state and evidence
directories. Add platform-specific generated files before provisioning at
scale.

## Current validation status

The earlier single-user proof of concept, using the checkout/payments fixture
and preinstalled runtime, validated:

- NemoClaw onboarding and OpenShell sandbox creation on an Azure VM;
- managed inference routing through an OpenAI-compatible mock endpoint;
- managed MCP registration and host-side credential substitution;
- diagnosis of an unhealthy AKS application;
- denial of restart before approval;
- an authorized restart after the policy transition;
- application recovery from HTTP 503 to HTTP 200;
- continued denial of scale, Secret access, cross-namespace access, and
  unapproved network egress.

The following items remain release gates:

- validate the selected SSH source rule, Cloud Shell startup, Ubuntu SSH
  authentication, reconnection, and sufficient tenant capacity for the planned
  concurrency; if using the single-source rehearsal helper, also validate
  detected SSH source and per-instance NSG rule permissions;
- validate automatic workload deployment and learner-run pinned installation/onboarding on a clean Skillable VM;
- validate the 45-minute critical path and minute-15 per-attendee recovery
  fallback under a 40-attendee concurrent start;
- implement and rehearse a guided guardrail-check path for the final five
  minutes, including scale, Secret, namespace, and network denials;
- validate learner-run onboarding/MCP attachment and absence of the shared provider key on the VM;
- rerun the complete proof with the generic `service-api` and
  `dependency-api` fixture;
- validate natural-language tool selection with the final Nemotron deployment
  on Microsoft Foundry;
- validate that editing `AGENTS.md` produces an observable change in a fresh
  agent invocation on the pinned OpenClaw and Nemotron versions;
- seed the editable `assets/operations-agent-starter.md` section in the actual
  main-agent `AGENTS.md` and verify both `CHOOSE` fields are present before
  each attendee starts;
- validate that both the evidence-rich and privacy-first MCP profiles can
  diagnose the incident, and that switching from privacy-first to evidence-rich
  exposes `get_logs` without changing the MCP credential;
- validate the main-agent workspace path and terminal editor on the final
  attendee image;
- validate the final Foundry authentication mechanism;
- run a 40-attendee concurrency and burst test;
- replace temporary per-user public LoadBalancers with the event networking
  design;
- rehearse the implemented automatic provisioning/readiness/reset and complete evidence collection;
- pin and retest the final NemoClaw, OpenShell, OpenClaw, MCP protocol, and
  container-image versions.

The artifact and credential contract for learner installation is recorded in
`INSTALL-KIT-SPEC.md`. Do not put binary packages or secret material in this
repository merely to satisfy the local-kit requirement.

## Provisioning and reset sequence

The implemented template performs the per-instance provisioning sequence in
[infra/SETUP.md](infra/SETUP.md): infrastructure, private workloads, secure model
broker, seeded incident, scoped MCP, learner installation settings and
infrastructure readiness. The learner installer/onboarding and attachment
step then prepare the agent and workspace starter. Legacy `scripts/deploy.sh` and `vm/install-nemoclaw.sh`
are historical engineering helpers; do not use them for Skillable deployment.

Assign the default learner VM only after `/etc/ignite-lab/ready.json` says
`infrastructure-ready`. The template
checks the initial service 503, desired/runtime mismatch, healthy dependency,
MCP authentication, namespace/Secret isolation and real model completion.
After learner installation, require `~/.config/ignite-lab/agent-ready.json`
to say `ready`; attachment checks managed MCP read access, denied restart/scale,
denied unapproved egress and a real agent reply. Author preinstall mode
instead requires root `status=ready` after both stages.
The full operator-approval/recovery exercise and observable changes to
`AGENTS.md` must still be rehearsed with the selected real model.

For an author reset, stop learner work and redeploy the original secure and
access/image inputs with a new `setupRevision`. The setup action recreates the
invalid runtime and correct desired ConfigMap; the VM extension removes the
old sandbox and restages settings. Default mode returns to
`infrastructure-ready`, and the learner repeats installation/onboarding and
attachment. Author preinstall mode rebuilds/checks the sandbox and restores
the starter/initial deny list automatically. This discards learner edits and
conversations. Prefer a fresh
instance if the gateway or VM cannot recover cleanly; do not weaken guardrails.

## Facilitator checkpoints

| Attendee task | Facilitator evidence |
| --- | --- |
| Agent build | Participant-edited `AGENTS.md`; changed explanation in a fresh invocation |
| Tool selection | Participant selected a profile; restart and scale remain denied |
| Environment check | Sandbox and MCP provider healthy |
| Diagnosis | Stale runtime configuration identified |
| Initial remediation | `policy_denied`; no Deployment mutation |
| Approval | Only restart removed from the deny list |
| Recovery | New service pod; application HTTP 200 |
| Containment | Scale, Secret, other namespace, and egress denied |

## Emergency fallbacks

- If the Foundry endpoint is unavailable, instructors may demonstrate the
  deterministic mock path, but it must be clearly identified as a transport
  and policy fallback rather than a model-quality demonstration.
- If an attendee's agent does not select the expected tool, use the managed MCP
  diagnostic client to prove the enforcement path, then move the attendee to a
  clean environment.
- If a shared AKS cluster has a platform-wide failure, stop the exercise rather
  than weakening namespace, RBAC, network, or credential controls.
- Never resolve an event outage by distributing cluster-admin credentials or
  raw shared service keys to attendees.

## Cleanup after the event

- Destroy attendee sandboxes and OpenShell gateway state created for the lab.
- Remove MCP registrations and rotate all MCP credentials.
- Remove attendee AKS namespaces or dedicated clusters.
- Delete event ingress, certificates, and temporary DNS records.
- Revoke or rotate Foundry credentials.
- Retain only sanitized evidence required for the event retrospective.
- Confirm that no attendee-specific public endpoints remain.

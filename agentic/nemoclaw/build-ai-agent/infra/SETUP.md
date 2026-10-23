# Skillable provisioning and learner agent installation

Paste the complete **`infra/main.bicep`** into Basic Information for template
**NemoClaw_AKS_Ignite (6891)**. It is self-contained: the reviewed setup sources
are compressed and embedded, with a SHA-256 integrity check. Do not paste the
source template or parameter file. Infrastructure and connection preparation
run during provisioning; learners install/create their own agent in Task 1.

For profile **225403**, use the current [coordinator checklist](../docs/COORDINATOR-CHECKLIST.md)
and [learner delivery files](../docs/README.md). The Word handout and both
Markdown variants now share the same nine-task learner flow.

Fresh instance 66301586 passed the core learner incident and boundary workflow
on October 16 using the corrected bundle. Desired and runtime reads returned
200, and one approved restart restored application health from 503 to 200.
The earlier backend 401 did not recur. The log prompt needed a correction;
agent quality, live token rotation, reset, cleanup and concurrency still need
validation. See [the rehearsal record](../RESULTS.md).

## Profile inputs

On the template's Parameters tab, retain these inputs. **Import From JSON** in
the observed Bicep editor extracts all parameters from the saved source;
remove unwanted defaulted metadata rows and any `Unnamed_Parameter` afterward.
Keep defaulted parameters in the Bicep source.

| Parameter | Profile → Cloud → ResourceGroup1 → NemoClaw_AKS_Ignite → Edit Parameters |
| --- | --- |
| `labInstanceId` | `@lab.LabInstance.Id` |
| `vmAdminUsername` | `labuser` |
| `vmAdminPassword` | Existing authorized per-instance VM credential mapping |
| `allowAzureCloudSsh` | `true` |
| `deployLabSetup` | Explicitly set `true`; replace any `false` override left from infrastructure-only testing. Hide from students. |
| `preinstallAgent` | `false` for learner delivery (also the Bicep default); hide from students |
| `foundryAccountName` | Omit: defaults to `myfoundry` plus `labInstanceId`, matching the account shown in the previous test. Override if the Fireworks template uses a different name. |
| `inferenceEndpoint` | Leave empty for automatic Foundry discovery. For a separate explicit connection, map its actual HTTPS inference base URL. |
| `inferenceModel` | Leave empty to select `NemotronLightning` during Foundry discovery. Override with another exact deployment name, such as `Nemotron3Ultra`, only when provisioned. Required for an explicit connection. |
| `inferenceApiKey` | Leave empty for automatic Foundry key retrieval. An explicit endpoint instead needs its secure platform credential mapping. |
| `inferenceAuthHeader` | Automatic Foundry discovery selects `api-key`. For an explicit connection, select its supported `Authorization` or `api-key` header. |

For the existing per-instance Fireworks attachment, **remove the metadata rows**
for `inferenceEndpoint`, `inferenceModel` and `inferenceApiKey`, or uncheck their
**Required** boxes and leave the corresponding profile values blank. Their
optional Bicep parameters remain in the source. Hide all model configuration
from students. Keep the first four existing profile mappings and use
`deployLabSetup=true`, `preinstallAgent=false` for the learner rehearsal.
Importing JSON is optional; it must not recreate required blank model fields.

Studio deploys its saved template and profile mappings. Publishing a GitHub
change does not update that saved copy. Paste the current `infra/main.bicep`,
save the template, and set the two setup flags in the profile's **Edit
Parameters**. If they are not listed, add `deployLabSetup` (display name
**Deploy Lab Setup**) and `preinstallAgent` (**Preinstall Agent**) to the
template's Parameters tab. Mark both required, clear **Display To Student**,
save, then set the profile values to `true` and `false` respectively. The
parameter-definition JSON files include both flags for import.

For automatic discovery, clear explicit endpoint, model and key values left
from a manual instance repair. A URL or key from a previous instance must not
be reused in the delivery profile. Learners receive working infrastructure
and prepared settings; their first runtime action is installing NemoClaw,
creating the sandbox and attaching MCP.

Automatic discovery uses the observed account naming convention and selected
deployment, not an account from an older instance. It checks the named account
and deployment, retrieves the account's published OpenAI endpoint and key,
and uses `/openai/v1/chat/completions`. It never chooses an arbitrary account or
deployment. If the model template changes those names, override the nonsecret
parameters. The October 14, 2026 rehearsal verified a Fireworks
`NemotronLightning` reply from Cloud Shell using `/openai/v1/chat/completions`
and `api-key` authentication. Fresh instance 66301586 subsequently reached
inference readiness through automatic discovery and completed real-model
agent calls. See `RESULTS.md` for remaining delivery checks.

`location` defaults explicitly to `westus` (West US), the region whose reported
availability list includes `Standard_D4s_v6`. The subscription rejected the old
`Standard_D4s_v5` there during preflight. An explicit template parameter mapping
overrides the Bicep default; use `location=westus` for this listed-size test. The
assigned subscription pool must permit the selected resource region.

Both `vmSize` and `aksNodeVmSize` default to `Standard_D4s_v6` (4 vCPUs,
16 GiB RAM, x64). This meets the AKS system-pool CPU/memory minimum and supports
Generation 2 images. If the profile overrides either size, change both mappings
to `Standard_D4s_v6` before launching. The reported list does not confirm quota
or capacity in every subscription assigned by the pool; validate the fresh
deployment. This size change does not establish the cause of a slow launch.
Paste the regenerated `infra/main.bicep` into Studio and save the template before
a fresh test instance.

Keep **Display To Student** enabled for `vmAdminUsername` and
`vmAdminPassword` so learners can find their assigned login on the Resources
tab. Hide technical settings such as `labInstanceId` and the inference fields.
This checkbox controls Resources visibility, not learner editing; **Required**
controls whether authors must supply a value. See Skillable's
[parameter visibility explanation](https://docs.skillable.com/docs/deploy-linux-template).
`inferenceApiKey` is a secure Bicep parameter; confirm the authoring platform
handles its mapping as a secret too. Do not paste the provider key into chat,
a workspace file, or a student-visible field.

The separate **Fireworks AI 2 Model (6838)** attachment still creates the
Foundry account and models. Keep it enabled for the same assigned resource
group. Automatic discovery requires successful provisioning and API-key
authentication; it does not create models, accept Marketplace terms, or enable
key authentication. A project management URL is not an inference base URL.
For an explicit provider/gateway connection, supply all three inference inputs
securely; a partial explicit connection fails instead of mixing credentials
from another account. Entra-only or legacy deployment/query routes require a
compatible gateway. No failure falls back to mock inference.

The author reports that the Skillable team saved
`sku.capacity=100`, retaining `DataZoneStandard` for `NemotronLightning`, in
template **6838**. The lab author cannot edit that template. Its original
`DataZoneStandard` capacity 1 exposed only 1 RPM and 1,000 TPM; the author
changed an earlier instance to capacity 100 and verified 100 RPM and 100,000 TPM.
Changing the live deployment does not update template 6838, and the AKS Bicep
does not manage that model SKU. After launch, read the new model deployment's
`properties.rateLimits` to verify the allocation. This is the configuration
used by the latest reported single-instance agent checks, not validated
capacity for 40 concurrent labs. Confirm the assigned pool supports the
allocation. See [the evidence and its limits](../RESULTS.md).

Leave **Deploy in Background unchecked**, save the template and lab profile,
and launch a **fresh** instance. To deliberately test infrastructure alone,
map `deployLabSetup=false`; that mode does not deploy the workloads or broker.
Keep `deployLabSetup=true` for the learner exercise. `preinstallAgent=false`
changes only who installs/creates the agent: all applications, model checks
and host prerequisites still run. An author may use `preinstallAgent=true`
for a fully prepared recovery/rehearsal instance; it checks the agent as well.

## What provisioning performs

1. Creates the Ubuntu VM, private two-node CPU AKS cluster, VNet, NAT and SSH rule.
2. Creates a dedicated setup identity and a per-instance credential vault. The
   setup identity has **Azure Kubernetes Service Contributor Role** for Run
   Command and **Azure Kubernetes Service RBAC Cluster Admin** for Kubernetes
   provisioning, both on this cluster only. Management access alone does not
   permit namespace, quota, Secret or workload creation. Setup waits up to five
   minutes for authorization using read-only permission reviews before writing
   any workloads. The VM's separate identity can read only its credential
   vault and has no AKS role.
   For automatic Foundry discovery, the setup identity also gets **Cognitive
   Services User** within the assigned main resource group. That built-in role
   allows reading account/deployment state and listing keys; it is never
   assigned to the VM identity. This resource-group scope avoids requiring the
   separate Foundry account to exist during ARM preflight. Discovery starts
   after AKS provisioning and allows at most five minutes for model readiness
   and role propagation; failure aborts setup with a nonsecret diagnostic.
3. Uses a foreground Azure deployment script to deploy `dependency-api`,
   `service-api`, and `aks-ops-mcp`. A fourth container in the MCP pod brokers
   real inference; it adds no separate workload or GPU.
4. Creates scoped Kubernetes RBAC, quota/limits, random per-lab credentials and
   seven-day TLS certificates. The internal service uses `10.42.4.10`, port
   `8443` for MCP and `8000` for inference, restricted to VM `10.42.1.4/32`.
   After the in-cluster checks, setup waits up to five minutes for the Service
   to report that exact private IP. A missing or different address fails
   provisioning; no alternate or public endpoint is accepted.
5. Seeds the stale-config incident and checks unhealthy service (503), healthy
   dependency, desired/runtime mismatch, auth rejection, namespace/Secret
   isolation and a finished, nonempty model reply in a Kubernetes Job. The
   probe allows 1,024 output tokens: the Fireworks rehearsal used the smaller
   128-token budget entirely on reasoning and returned no visible answer.
6. Prepares owned gateway ancestor directories under the learner's `.local`,
   removes group/world write access and rejects symlinks or unrelated owners.
   Prepared settings apply `umask 077` before learner installation. Existing
   gateway databases and sandbox contents are left intact.
   Installs/validates host tools, stages the checksum-verified NemoClaw
   installer at commit `95d6505265ba1f2cb05355f83786353e654de096` (v0.0.132),
   and writes shell-safe settings to the assigned user's mode-0600
   `~/.config/ignite-lab/environment.sh`. No agent or sandbox is installed in
   the default delivery mode. The installer selects compatible OpenShell
   **0.0.116** and the pinned OpenClaw image when the learner runs it.
7. Writes `/etc/ignite-lab/ready.json` with `status=infrastructure-ready`,
   `phase=infrastructure` and `agent=awaiting-learner-install` after all platform
   checks pass. It does not claim sandbox policy/egress checks have passed.

## Learner installation and agent readiness

Task 1 in [LAB-GUIDE.md](../LAB-GUIDE.md) runs these steps as the assigned VM
user, without sudo, after SSH:

```bash
source ~/.config/ignite-lab/environment.sh
bash /opt/ignite-lab/install-nemoclaw-pinned.sh \
  --non-interactive --yes-i-accept-third-party-software
bash /opt/ignite-lab/infra/setup/setup_user.sh
python3 -m json.tool ~/.config/ignite-lab/agent-ready.json
```

The pinned OpenClaw installer runs onboarding during installation; its
`--defer-onboarding` option does not support this OpenClaw/custom-provider
combination. Installation and sandbox creation are therefore one explicit
learner action. The second script attaches managed MCP with restart/scale
denied, appends the workspace starter once, verifies mutation/egress denials
and a real agent reply, then writes the user-owned `agent-ready.json` with
`status=ready`. Rerunning attachment preserves the sandbox and starter edits,
restores the initial deny list and reruns checks; it does not install or destroy
the agent. Learners should complete this step before policy/customization work.

`preinstallAgent=true` runs the same installation and attachment sequence
during provisioning, with root-only logs. In that mode the root readiness
report says `status=ready` and `phase=agent`; agent checks must pass before
provisioning succeeds. Shared upstream credentials never appear in the staged
learner settings. The file contains only limited per-lab broker/MCP tokens.

The upstream model key is held in an AKS Secret mounted only in the broker
container. The VM receives per-lab broker/MCP tokens, not the shared provider
key. Those limited tokens remain readable to the VM owner; the sandbox uses
OpenShell credential placeholders. Subscription Owner access independently
permits reading AKS Secrets, so replace the current development Owner assignment
with the event's scoped attendee permissions before claiming learner isolation.
The broker caps output at 4096 tokens, four concurrent calls, and 1000 requests
per broker process. This is not a persistent cost or token budget; enforce real
event usage limits at the upstream provider/gateway.

Private AKS bootstrap uses the management API's [command invoke mechanism](https://learn.microsoft.com/en-us/azure/aks/access-private-cluster).
It is a one-time setup/reset action, not the attendee's API access path. AKS
must permit Run Command and have capacity to schedule its temporary command pod.
The deployment script creates temporary Container Instance/Storage resources and
cleans them on completion/failure (`cleanupPreference=Always`). The subscription
pool must allow those resource providers plus KeyVault, ManagedIdentity, Compute,
Network, ContainerService, Resources and role assignments; its deployer needs
permission to assign/use the managed identities. Rehearse supported deployment
script CLI version `2.65.0` in the actual region; it is author-overridable.
The earlier `2.64.0` runtime reported `jq: command not found` in Azure's
deployment-script wrapper. Microsoft's [2.65.0 release notes](https://learn.microsoft.com/en-us/cli/azure/release-notes-azure-cli#october-01-2024)
document adding `jq` to the Azure Linux image. Clear any old runtime-version
override in Studio when applying the current template.

The same rehearsal's Cloud Shell diagnostic reached AKS but returned
`Forbidden` for the signed-in user. That proves the user's Kubernetes access
is missing; it does not report the deployment script identity's access.
Code review also found that the setup identity had AKS Contributor management
access without Kubernetes data-plane permissions. The template now assigns
the cluster-scoped RBAC Cluster Admin role to that setup identity and waits
for propagation. The optional `bootstrapPrincipalObjectId` assignments do not
replace the setup identity's own role. See Microsoft's
[AKS authorization roles and propagation guidance](https://learn.microsoft.com/en-us/azure/aks/entra-id-authorization).
Apply the updated Bicep in Studio and validate a fresh launch; the old failed
deployment does not receive these fixes from a GitHub push. Do not add learner
admin rights to repair provisioning.

Azure read/secret calls have a 60-second per-command limit and share a
five-minute retry window per action. AKS command invocation allows at most
20 minutes across transport retries for its rollout/readiness script. Logs
identify the Azure action and a fixed failed AKS phase without printing
credentials or raw command error bodies.

The fresh October 14 instance passed setup authorization and automatic Foundry
discovery, then failed the old immediate private-IP assertion. Azure subsequently
reported the internal load balancer succeeded at `10.42.4.10`. The current
bundle uses a bounded exact-IP wait to handle that provisioning delay. See
[kubectl wait](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_wait/).
The October 15 fresh instance `66237664` subsequently failed at this phase.
Real-client regression testing found that indexing `ingress[0]` aborts
immediately when the API reports an empty ingress list. The generated bundle
now uses `ingress[*].ip` with the same exact-IP comparison and 300-second
deadline. Tests run the actual bootstrap wait through kubectl 1.34.1 against
a local Kubernetes API fixture: empty and missing ingress must transition
to the expected IP; pending, wrong-IP and forbidden responses must fail.
This reproduces and fixes a source defect; the supplied live error identifies
only the failed phase, so live causality and recovery still need evidence.
The overall AKS invocation retains its 20-minute budget, including all checks.
Updating Studio with the new generated Bicep is required for future launches.
For an existing failed author instance, if Azure now reports the expected
internal frontend IP as succeeded, redeploy its failed main ARM deployment
with the original parameters and VM password and a new `setupRevision`.
This reruns application checks and the dependent VM preparation; merely
rerunning the deployment script alone does not resume a failed parent
deployment. Verify the final readiness report before starting learner tasks.

The learner-installation rehearsal found `.local` owned by the learner with
mode `775`. Pinned NemoClaw rejects group/world-writable gateway ancestors
while activating corporate CA trust, so the image build completed but
onboarding failed. VM preparation now creates/checks these directories and
the learner's sourced settings enforce private file creation. This satisfies
the pinned [gateway-state trust checks](https://github.com/NVIDIA/NemoClaw/blob/95d6505265ba1f2cb05355f83786353e654de096/src/lib/onboard/gateway/state-dir.ts)
without disabling CA validation. For an existing owned `.local` with this
mode, `chmod go-w "$HOME/.local"` repairs the observed write bits. Inspect the
retained sandbox status before retrying onboarding. `--resume` requires a
saved session; the observed failure left no resumable checkpoint. A failed
onboarding is not agent readiness, and no automatic sandbox deletion is used
to hide it.

If learner MCP attachment reports a sandbox readiness failure, inspect
`nemoclaw sandbox mcp ignite-agent status aks-ops`. Provider attachment and
credential metadata are separate from wire-level credential resolution;
metadata marked ready does not prove a successful authenticated connection.
The lab probe reports the failing stage and recognized transport code without
printing credentials, arbitrary exception text or response bodies. In the
rehearsal, both plain Node and the managed launcher returned `UND_ERR_SOCKET`.
The preceding interrupted CA activation was a recovery lead: pinned
[supervisor CA activation](https://github.com/NVIDIA/NemoClaw/blob/95d6505265ba1f2cb05355f83786353e654de096/src/lib/onboard/managed-startup/provider-root-apply.ts)
uses stop/start of the same sandbox to reload TLS roots. Inspect the outcome
of each lifecycle command; a stopped container with a bookkeeping error is
not a completed restart, and an `&&` chain will skip subsequent steps. In the
rehearsal a separate start restored wire-level credential verification
(HTTP 200), but the incomplete onboarding authority still blocked normal
launch readiness. Do not treat successful MCP authentication as completed
onboarding or edit the pending registry flag to bypass that check.

In that rehearsal `nemoclaw list --json` showed no published sandbox and no
resumable onboarding session. The author confirmed the built-in
`nemoclaw sandbox destroy ignite-agent --no-cleanup-gateway` cleanup of the
unfinished test workspace, then ran onboarding again with the prepared
settings and `umask 077`. This reached the normal onboarding completion
screen. Cleanup deletes sandbox workspace data and is an explicit author
recovery action, not an automatic learner step. If NemoClaw refuses its
identity checks, preserve the reported recovery state. After replacement,
run `setup_user.sh` again: a previous sandbox's successful MCP result does
not establish readiness of the new sandbox.

The replacement's MCP add initially refused non-prefix partial state: the
gateway can preserve a credential provider after its sandbox is removed.
For this isolated rehearsal the author reset only
`ignite-agent-mcp-aks-ops` on gateway `nemoclaw`, using the supported
[OpenShell provider deletion](https://docs.nvidia.com/openshell/v0.0.116/sandboxes/manage-providers#manage-providers)
command, then reran attachment with the prepared token. This restored MCP
access, both mutation denials and denied unapproved egress. Treat provider
cleanup as explicit author recovery after verifying its ownership and other
consumers; the learner attachment script does not delete shared providers.

An OpenClaw `API rate limit reached` message alone does not identify which
layer rejected a request. In the rehearsal a small direct broker request
passed while the real agent failed. Check the actual request path and
sanitized broker outcome before changing quotas or agent settings. Local
contract tests and direct model requests do not replace the real agent check.

The October 14 rehearsal subsequently traced the agent failure to an upstream
HTTP 429: the broker's `Inference provider rejected the request` response was
retained in OpenClaw's structured `rawErrorPreview`. The assigned
`NemotronLightning` deployment had `DataZoneStandard` capacity 1, with only
1 request and 1,000 tokens per 60 seconds in `properties.rateLimits`.
These are throughput limits, separate from the broker's 4,096-token output
cap. The author must allocate enough model throughput in the separate
Fireworks attachment (6838) before validating the full agent. The author
updated this instance to capacity 100; Azure confirmed `Succeeded`, 100
requests per minute and 100,000 tokens per minute. The next `setup_user.sh`
run passed the real-agent inference check and wrote `agent-ready.json` with
`status: ready`, denied mutation tools and denied unapproved egress. That
inference readiness prompt deliberately calls no tools. Subsequent native
MCP discovery and real-agent checks are recorded below and in `RESULTS.md`.
This single-instance
allocation is not an event-wide sizing recommendation. Verify the capacity
available for concurrent labs and preserve the validated allocation in the
Fireworks template for future launches. The AKS template does not own the
existing model deployment's SKU.

The first real-agent read-only investigation after the successful readiness
check again returned the generic rate-limit message. Later investigation
and recovery runs completed after the native MCP discovery and transport
fixes, with capacity 100 retained. Those supplied final reports show diagnosis,
restart denied before approval, restart accepted after approval, HTTP 200
health, scale denial and namespace/Secret isolation; they are not independent
raw MCP or Kubernetes verification. A subsequent direct curl probe and
matching OpenShell audit event establish denied unapproved egress. The full
fresh-launch learner walkthrough still needs validation. If a future run
returns 429, collect actual request/turn usage and the latest sanitized
rejection before changing another limit; Fireworks can also enforce adaptive
provider limits within the Azure quota. The saved readiness run alone used
10,944 input and 320 output tokens.

The current broker source preserves only allowlisted numeric quota headers
and normalized `Retry-After` values of at most one day. Upstream HTTP 429
errors include fixed tokens/requests/unspecified labels derived from bounded
provider text, plus safe quota metadata. The raw provider text and unknown
headers stay private. The broker does not add automatic model retries;
clients receive the provider's retry hint. This improves diagnostics and
retry timing, not the allocated quota or proof of learner-flow readiness.
The regenerated Bicep includes this change for future deployments; an
already-running instance needs an author broker update before it can expose
these diagnostics. Such an update must merge only `inference_broker.py` in
`lab-services-code`, roll `aks-ops-mcp`, and verify the running source hash.

This performs online installation: GitHub, NVIDIA/npm registries, container
registries, Node.js, Docker, Microsoft package feeds and the selected provider
must be reachable. It does not claim offline support or frozen apt packages.
A tested custom image may provide host tools; workload checks and learner
settings preparation still run. Learner installation also needs online
release/registry downloads. Start infrastructure provisioning before the
timed exercise and rehearse installation time at event concurrency.

## Author validation, reset and termination

Check Azure deployment **Succeeded**, the `ignite-lab-setup` VM extension and
the `${stem}-applications` deployment script. On Ubuntu, read the nonsecret
`/etc/ignite-lab/ready.json`; release the default learner VM only when its
status is `infrastructure-ready`. Check the user-owned `agent-ready.json`
after learner installation/attachment before starting incident exercises.
An author preinstall instance instead requires root `status=ready`.
Host-tool log: `/var/log/ignite-vm-prerequisites.log`. Agent setup log:
`/var/log/ignite-agent-install.log` exists only for author preinstall mode
(root-only; do not share unredacted logs). Default-mode installation/attachment
output is shown in the learner terminal; avoid recording sensitive settings.

Run the learner scenario with the actual model: diagnose, observe denial,
approve only restart, restore health to 200 and confirm continued scale/egress
and namespace/Secret denials. The local tests and compilation do not establish
that Azure deployment or this whole agent workflow has passed.

For an author reset, redeploy the original connection selection with a new
`setupRevision` (or use `infra/reset-lab.sh` with the documented environment
inputs in `main.bicepparam`). This resets the incident, rotates TLS,
discards the existing lab sandbox/workspace and restages settings.
Automatic Foundry discovery retrieves the assigned account's current key again;
an explicit connection needs its original secure credential. Default mode
returns to `infrastructure-ready` for learners to reinstall/create
their sandbox (the installed CLI can be reused by the pinned installer).
Author preinstall mode recreates and checks the sandbox automatically. This
discards learner sandbox edits and conversations; use it only for a requested
lab reset. Keep the original access/image inputs when redeploying. A failed
reset is not a ready environment.

Skillable's termination lifecycle must delete the assigned main resource group;
AKS deletion removes its managed node group. Bicep does not schedule its own
future teardown. Record both names from deployment outputs, then run the
read-only `infra/check-teardown.sh` in the platform termination check. Vaults
are soft-deleted with seven-day retention; purge/recovery follows pool policy.
Do not delete an externally shared model. Scope existing model cleanup actions
to the instance instead of scanning/deleting an entire subscription.

## Maintaining the embedded template

Edit `infra/main.template.bicep` and the allowlisted files in `infra/setup/`,
then run `python3 infra/build-template.py`. Commit source and generated Bicep
together. `python3 infra/build-template.py --check` detects stale bundles.
Local checks include Bicep compilation, syntax validation, rendered manifest
contracts, failure propagation, and HTTPS broker integration tests. Fresh
instance 66301586 passed the core learner workflow; live token rotation,
new-learner timing, reconnection, reset/termination and 40-person capacity
rehearsals remain release checks. See `RESULTS.md`.

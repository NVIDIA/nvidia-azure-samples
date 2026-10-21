<!-- Generated from docs/learner-guide.docx by scripts/export-learner-docs.py. -->

# Coordinator checklist

## Confirm the platform configuration

| Setting | Required delivery configuration |
| --- | --- |
| Lab profile | 225403 |
| AKS cloud template | NemoClaw_AKS_Ignite 6891; corrected infra/main.bicep saved in Studio |
| Main setup flags | deployLabSetup true; preinstallAgent false |
| Cloud Shell access | allowAzureCloudSsh true for this delivery path |
| Connection inputs | Leave endpoint, model, and API key blank for discovery |
| Separate model template | Fireworks AI 2 Model template 6838; NemotronLightning; DataZoneStandard; sku.capacity 100 |
| Throughput verification | Test allocation: 100 RPM, 100,000 TPM. Verify each subscription pool. |
| Student resources | Windows, Azure, and Ubuntu credentials; assigned VM name |

Confirm Skillable saved capacity 100 in template 6838 and verify subscription-pool
capacity for all attendees. An instance-only change does not affect future launches.
Verify actual provider limits.

## Save the corrected Bicep

Save the reviewed infra/main.bicep in Studio; GitHub changes do not update Studio. The
bundle includes the runtime fixes and a 300-second wait for private ingress IP
10.42.4.10.

Current Bicep: 63,871 bytes. SHA-256 below; use this fingerprint for the imported
bundle.

```text
eb3e440992a262248b5dbc49ac66722ae8f1bc432538ee7edb50e8e43ca14543
```

## Run a fresh rehearsal

1. Launch a fresh instance. Require both templates and VM preparation to succeed.

2. Verify infrastructure-ready, then complete every learner task without platform
   repairs.

3. Check installation, customization, log handling, diagnosis, denial, approval, and
   recovery.

4. Capture raw MCP or Kubernetes results and the matching egress audit; agent summaries
   alone do not verify enforcement.

5. Rehearse with a new learner; measure timing and test reconnection, reset, and
   cleanup.

6. Test simultaneous labs and throughput at the planned attendee count.

Rehearsal 66301586 passed the core workflow: HTTP 503 to 200, one approved restart,
remaining boundary checks, and downloaded evidence. See RESULTS.md for scope and quality
issues.

Before delivery, address reply formatting and sessions_yield errors. Validate live token
rotation, new-learner timing, reconnection, reset, termination cleanup, and planned
concurrency.

## Reference material

[Lab source](https://github.com/NVIDIA/nvidia-azure-samples)  |  [Validation
run](https://github.com/NVIDIA/nvidia-azure-samples/actions)  |  [ConfigMap
updates](https://kubernetes.io/docs/tutorials/configuration/updating-configuration-via-a-configmap/)
|  [Foundry
quotas](https://learn.microsoft.com/en-us/azure/foundry/how-to/fireworks/enable-fireworks-models)

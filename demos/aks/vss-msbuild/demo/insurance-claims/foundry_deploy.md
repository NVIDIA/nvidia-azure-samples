# Deploy to Microsoft Foundry

This walks through deploying [agent.yaml](agent.yaml) as a hosted agent in
**your** Foundry project (new-style — `Microsoft.CognitiveServices/accounts/.../projects/...`,
not the legacy ML hub/workspace shape).

> **Recommended path:** invoke the **`azure:microsoft-foundry`** skill — it
> knows the current Foundry SDK version, handles auth + RBAC, and knows the
> deployment shape for the new-style projects. The hand-rolled scripts below
> are the manual equivalent if you'd rather drive it yourself.

## Your project at a glance

Set these to your Foundry project's values (used by the Python snippets below):

```bash
export AZURE_SUBSCRIPTION_ID=<your-subscription-id>
export AZURE_RESOURCE_GROUP=<your-rg>
export AZURE_LOCATION=northcentralus
export AZURE_AI_ACCOUNT_NAME=<your-ai-services-account>
export AZURE_AI_PROJECT_NAME=<your-foundry-project>
export FOUNDRY_PROJECT_ENDPOINT=https://<account>.services.ai.azure.com/api/projects/<project>
export AZURE_OPENAI_DEPLOYMENT=gpt-4.1
export VSS_BASE_URL=http://vss.<EXTERNAL_HOST>.nip.io
```

> The newer end-to-end demo (with full `azd ai agent` workflow + Streamlit UI) lives in [../insurance-claims-foundry/](../insurance-claims-foundry/) — recommended path if you want a turnkey deploy script.

## Prerequisites

```bash
az login
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
```

You also need:
- An ACR you can push to. The project's RG has none — create one:
  ```bash
  az acr create -g "$AZURE_RESOURCE_GROUP" -n "$ACR_NAME" --sku Basic --admin-enabled true
  ```
- Python 3.11+ on your local machine.
- The new Foundry agent SDK:
  ```bash
  pip install azure-ai-projects azure-ai-agents azure-identity
  ```

## 1. Build the agent container

[Dockerfile](Dockerfile) — minimal:

```Dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vss_mcp_server.py tools.py agent_instructions.md ./
COPY sample_data ./sample_data/
ENV VSS_BASE_URL=http://vss.<EXTERNAL_HOST>.nip.io
CMD ["python", "vss_mcp_server.py"]
```

Build and push:

```bash
RG="$AZURE_RESOURCE_GROUP"
ACR="$ACR_NAME"
TAG=insurance-claims-v1

az acr login -n "$ACR"
docker build -t "$ACR.azurecr.io/insurance-claims:$TAG" .
docker push "$ACR.azurecr.io/insurance-claims:$TAG"
```

## 2. Register the agent

The new-style Foundry project uses the `AIProjectClient` constructor that
takes an `endpoint` (not separate `subscription_id`/`resource_group`/`project_name`
fields — those are for the legacy ML workspace shape).

```python
import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

project = AIProjectClient(
    endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
    credential=DefaultAzureCredential(),
)

with open("agent_instructions.md") as f:
    instructions = f.read()

agent = project.agents.create_agent(
    model=os.environ["AZURE_OPENAI_DEPLOYMENT"],   # e.g. "gpt-4.1"
    name="insurance-claims-triage",
    description="Turns customer damage videos into draft claims.",
    instructions=instructions,
    tools=[
        # The MCP server lives in the agent's container. Foundry starts it
        # over stdio at agent boot.
        {"type": "mcp", "server": {"command": "python",
                                    "args": ["/app/vss_mcp_server.py"]},
         "env": {"VSS_BASE_URL": os.environ["VSS_BASE_URL"]}},
        # Python function tools — Foundry inspects tools.py and registers each.
        {"type": "function", "module": "tools", "name": "lookup_policy"},
        {"type": "function", "module": "tools", "name": "estimate_repair_cost"},
        {"type": "function", "module": "tools", "name": "draft_claim_pdf"},
    ],
    # Optional: pin the container image so updates require an explicit re-deploy.
    container={"image": f"{os.environ['ACR_NAME']}.azurecr.io/insurance-claims:insurance-claims-v1"},
)
print(f"Agent ID: {agent.id}")
print(f"Foundry portal: https://ai.azure.com/projects/{os.environ['AZURE_AI_PROJECT_NAME']}/agents/{agent.id}")
```

> Exact tool-registration syntax depends on the `azure-ai-projects` /
> `azure-ai-agents` SDK version. The `azure:microsoft-foundry` skill always
> emits the current syntax — prefer invoking it (`/azure:microsoft-foundry`)
> over hand-writing this script if the SDK version drifts.

## 3. Test the deployed agent

```python
thread = project.agents.threads.create()
project.agents.messages.create(
    thread_id=thread.id,
    role="user",
    content=(
        "A customer just uploaded a damage video. video_id=claim-test-1. "
        "Run the triage workflow."
    ),
)
run = project.agents.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)

# Stream the messages back in order.
for msg in project.agents.messages.list(thread.id, order="asc"):
    text = msg.content[0].text.value if msg.content else ""
    print(f"[{msg.role}] {text}")
```

## 4. Network considerations

- **Egress to VSS**: the Foundry-hosted agent container needs outbound HTTP to
  your `VSS_BASE_URL`. New-style Foundry projects use managed
  egress by default — no NSG changes needed.
- **Cross-region latency**: ~110ms per `vss_analyze_video` call. The VLM
  inference (2-5s) dominates, so this is fine for a demo. If you go to
  production, co-locate the project or front VSS with Azure Front Door.

## 5. Agent Identity (optional but recommended for prod)

If your project has an Agent Identity Blueprint registered, it gives the agent its
own Entra principal that can be granted RBAC on downstream resources
(ACR pulls, Key Vault reads, etc.) without sharing user/SP credentials.

To wire it up: invoke `/azure:entra-agent-id` after the agent is deployed —
the skill handles `BlueprintPrincipal` creation, scope grants, and the
`fmi_path` token exchange config the agent container needs to call AKS
endpoints with its own identity.

## 6. Iteration loop

Once deployed:

- **Traces**: Foundry portal → your project → Agents → insurance-claims-triage →
  Traces. Each run shows the tool-call sequence (VSS analyze → lookup_policy →
  estimate → draft_claim_pdf) with timings.
- **Continuous evals**: define a small dataset of `(video, expected_damage)`
  pairs and run them on each agent revision.
- **Prompt optimizer**: Foundry can iterate on
  [agent_instructions.md](agent_instructions.md) against your eval set.
  Useful once you've collected ~20+ real-world traces.

The `/azure:microsoft-foundry` skill scaffolds all of these.

## 7. Cleanup

```bash
# Delete the agent (does not touch model deployments or the project)
python -c "
import os
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

p = AIProjectClient(
    endpoint=os.environ['FOUNDRY_PROJECT_ENDPOINT'],
    credential=DefaultAzureCredential(),
)
for a in p.agents.list_agents():
    if a.name == 'insurance-claims-triage':
        p.agents.delete_agent(a.id)
        print(f'Deleted {a.id}')
"

# Optional: delete the ACR repository
az acr repository delete --name <YOUR_ACR> --repository insurance-claims --yes
```

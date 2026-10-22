# NVIDIA Nemotron 3.5 Super VL on Microsoft Foundry

This sample uses a fictional Northline Audio return request to show how a
customer-service agent combines image reasoning with company policy. NVIDIA
Nemotron 3.5 Super VL reads an order and return-case image containing an item
table, shipment events, and a customer statement. Microsoft Foundry Agent
Service calls the image-reading function and searches two policy documents
before drafting a conditional customer reply.

The same case has three notebook implementations:

| Notebook | Agent orchestration | Image inference |
| --- | --- | --- |
| [Foundry SDK](nemotron_super_vl_customer_service_agent.ipynb) | Microsoft Foundry SDK | OpenAI Python SDK through Foundry Chat Completions |
| [LangChain](nemotron_super_vl_customer_service_langchain.ipynb) | LangChain and LangGraph with Foundry Agent Service | LangChain Foundry chat model |
| [Microsoft Agent Framework](nemotron_super_vl_customer_service_ms_agent_framework.ipynb) | Microsoft Agent Framework with Foundry Agent Service | Agent Framework Chat Completions client |

All case data are synthetic. The customer-reported fault is an unverified
statement, not a confirmed defect. The expected review date is October 21,
2026, which is 33 days after the recorded September 18 delivery. The 30-day
standard-return window has passed, while the 45-day suspected-defect review
window remains open. Only a human support team can verify the item and approve
an RMA or refund.

## Prerequisites

- Python 3.12, [uv](https://docs.astral.sh/uv/), Jupyter or an editor with
  notebook support, and the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli).
- A Microsoft Foundry project with a deployed NVIDIA Nemotron 3.5 Super VL
  model that accepts image input through Chat Completions. Use the deployment
  **name**, which can differ from the model catalog name.
- Microsoft Entra access to create Foundry agents and vector stores in that
  project. The notebooks use identity-based authentication, not an API key.
  In the tested environment the identity had the Foundry User role.

From this sample directory, sign in and set your project values:

```bash
az login
export FOUNDRY_PROJECT_ENDPOINT='https://YOUR-RESOURCE.services.ai.azure.com/api/projects/YOUR-PROJECT'
export FOUNDRY_MODEL='YOUR-NEMOTRON-DEPLOYMENT-NAME'
```

Run the notebooks from this directory or the repository root so they can find
`customer_service_assets/`. If Jupyter starts from another directory, change
its working directory to one of these locations before running the cells.

## 1. Run the Foundry SDK notebook

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-direct.txt
.venv/bin/python -m ipykernel install --user \
  --name nemotron-foundry-direct --display-name 'Python (Nemotron Foundry SDK)'
```

Open [the Foundry SDK notebook](nemotron_super_vl_customer_service_agent.ipynb)
and run its cells in order with the new kernel. It creates or reuses a vector
store named **Northline Audio customer-service knowledge**, then indexes the
[returns policy](customer_service_assets/northline_returns_policy.txt) and
[support bulletin](customer_service_assets/aurora_h7_support_bulletin.txt).
It checks file content hashes and refuses conflicting or duplicate copies. It
prints `Foundry vector store: vs_...`; save that ID for the other notebooks.

The notebook then publishes a Foundry agent and asks three questions in one
saved conversation: read the [order image](customer_service_assets/northline_order_return_case.png),
apply the policy and support bulletin, and draft a customer reply. The local
`read_order_document` function sends the image to the model through Foundry
Chat Completions and returns only visible facts. Foundry file search runs on
the server. The notebook prints conversation and response IDs for inspection
in the Foundry portal.

## 2. Run the LangChain or Agent Framework notebook

Export the vector store ID printed by the first notebook:

```bash
export FOUNDRY_CUSTOMER_SERVICE_VECTOR_STORE_ID='vs_FROM_STEP_1'
```

For [LangChain](nemotron_super_vl_customer_service_langchain.ipynb):

```bash
uv venv .venv-langchain --python 3.12
uv pip install --python .venv-langchain/bin/python -r requirements-langchain.txt
.venv-langchain/bin/python -m ipykernel install --user \
  --name nemotron-langchain --display-name 'Python (Nemotron LangChain)'
```

For [Microsoft Agent Framework](nemotron_super_vl_customer_service_ms_agent_framework.ipynb):

```bash
uv venv .venv-agent-framework --python 3.12
uv pip install --python .venv-agent-framework/bin/python -r requirements-agent-framework.txt
.venv-agent-framework/bin/python -m ipykernel install --user \
  --name nemotron-agent-framework --display-name 'Python (Nemotron Agent Framework)'
```

Select the matching kernel and run cells in order. Each notebook creates its
own Foundry agent and uses the existing vector store; neither uploads another
copy of the documents. They retain their agents and conversations so you can
inspect them in Foundry. Re-running an agent-creation cell can create a new
agent version.

## Inspect and clean up

Open [Microsoft Foundry](https://ai.azure.com/), select your project and
agent, then use **Traces** to inspect the saved conversation, model calls,
function calls, and file-search evidence. The notebooks print the IDs needed
to locate a run. The local image-inference call happens inside the Python
function; its internal span requires separate client instrumentation if you
want it in an end-to-end trace.

The notebooks intentionally leave agents, conversations, and the vector store
in place for inspection. Delete resources you no longer need through Foundry
to avoid ongoing storage or deployment costs. They do not deploy the model.

## Notes

- The configured deployment must support image input through Chat Completions.
  In the original environment, direct image input to Agent Service Responses
  was rejected, so the visual function uses Chat Completions.
- The sample separates observed order facts, unverified customer statements,
  and retrieved policy guidance. It does not issue an RMA or promise a refund.
- The code, documents, and image are illustrative. Do not upload real customer
  information without adapting the sample to your data-handling requirements.

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Insurance Claims Triage — Microsoft Foundry hosted agent.

Scaffolded by `azd ai agent init`. Extended to:
  - Load instructions from instructions.md (separate file for prompt-optimizer)
  - Register four function tools from tools.py (1 VSS sub-agent + 3 business)
"""

import os
from pathlib import Path

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework.observability import enable_instrumentation
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

from tools import (
    draft_claim_pdf,
    estimate_repair_cost,
    lookup_policy,
    vss_analyze_video,
)

# Load environment variables from .env file (when running locally)
load_dotenv()


def _setup_observability() -> None:
    # Ship agent-framework GenAI spans/metrics (token usage, tool calls, run
    # durations) to the App Insights instance backing this Foundry project so
    # the agent's Monitor dashboard cards populate. The hosted runtime already
    # injects APPLICATIONINSIGHTS_CONNECTION_STRING; for local `azd ai agent
    # run` this is unset and we skip silently.
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        return
    configure_azure_monitor(connection_string=conn)
    enable_instrumentation(enable_sensitive_data=True)


def main():
    _setup_observability()
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        credential=DefaultAzureCredential(),
    )

    instructions = (Path(__file__).parent / "instructions.md").read_text()

    agent = Agent(
        client=client,
        instructions=instructions,
        tools=[
            vss_analyze_video,
            lookup_policy,
            estimate_repair_cost,
            draft_claim_pdf,
        ],
        # History is managed by the hosting infrastructure, not by the agent.
        # See https://developers.openai.com/api/reference/resources/responses/methods/create
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()

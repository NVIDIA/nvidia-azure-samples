# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import json
from types import SimpleNamespace

import httpx
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from llama_index.embeddings.nvidia import NVIDIAEmbedding
from nat.llm.openai_llm import OpenAIModelConfig
from nat.plugins.langchain.llm import _patch_llm_based_on_config

import sitecustomize  # noqa: F401


def test_foundry_lightning_supports_thinking_prompts() -> None:
    for thinking, expected in ((True, "/think"), (False, "/no_think")):
        config = OpenAIModelConfig(
            model_name="nemotron-3-5-lightning",
            base_url="https://foundry.example/openai/v1",
            thinking=thinking,
        )
        assert config.thinking_system_prompt == expected


def test_foundry_lightning_sends_deployment_thinking_and_tools() -> None:
    requests: list[dict] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 0,
                "model": "nemotron-3-5-lightning",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "knowledge_search",
                                        "arguments": '{"query":"proof"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    async def invoke() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        config = OpenAIModelConfig(
            model_name="nemotron-3-5-lightning",
            base_url="https://foundry.example/openai/v1",
            api_key="test",
            thinking=True,
            max_retries=0,
        )
        client = ChatOpenAI(
            model=config.model_name,
            base_url=config.base_url,
            api_key="test",
            max_retries=0,
            http_async_client=http_client,
        )
        client = _patch_llm_based_on_config(client, config)
        tool = {
            "type": "function",
            "function": {
                "name": "knowledge_search",
                "description": "Search",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
        result = await client.bind_tools([tool]).ainvoke([HumanMessage(content="Find proof")])
        assert result.tool_calls[0]["name"] == "knowledge_search"
        await http_client.aclose()

    asyncio.run(invoke())

    assert requests[0]["model"] == "nemotron-3-5-lightning"
    assert requests[0]["messages"][0] == {"content": "/think", "role": "system"}
    assert requests[0]["tools"][0]["function"]["name"] == "knowledge_search"


def test_foundry_embedding_uses_query_and_passage_prefixes() -> None:
    calls: list[dict] = []

    class Embeddings:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                data=[SimpleNamespace(embedding=[float(index)]) for index, _ in enumerate(kwargs["input"])]
            )

    embedding = NVIDIAEmbedding(
        model="nvidia/nemotron-3-embed-1b",
        base_url="https://foundry.example/openai/v1",
        api_key="test",
    )
    embedding._client = SimpleNamespace(embeddings=Embeddings())

    assert embedding.get_query_embedding("first") == [0.0]
    assert embedding.get_text_embedding_batch(["second", "third"]) == [[0.0], [1.0]]
    assert calls == [
        {"input": ["query: first"], "model": "nemotron-3-embed-1b"},
        {"input": ["passage: second", "passage: third"], "model": "nemotron-3-embed-1b"},
    ]

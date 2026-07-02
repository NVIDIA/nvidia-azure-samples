# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""NAT function registration: `_type: azure_ai_search_retrieval`.

Adds a new function type to AI-Q's workflow registry alongside the upstream
`knowledge_retrieval`. Doesn't replace anything — both backends remain usable.

This module is the entry point referenced by the workflow YAML's
`functions.knowledge_search._type`. When AI-Q resolves the function during
config load, it instantiates `AzureAISearchConfig`, calls this generator,
and registers the inner `search` callable as the workflow tool.
"""

# NOTE: deliberately NOT using `from __future__ import annotations` — NAT's
# `FunctionInfo.from_fn` introspects the search function's type hints at
# registration time, and PEP 563 lazy-evaluation makes them strings that
# NAT can't resolve from the closure (`TypeError: issubclass() arg 1 must
# be a class` at nat/utils/type_utils.py:339).

import logging

import asyncio
import os

from aiq_agent.knowledge import (
    configure_summary_db,
    get_ingestor,
    get_retriever,
    set_active_ingestor,
)
from nat.builder.builder import Builder
from nat.builder.context import Context
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function

from .config import AzureAISearchConfig

logger = logging.getLogger(__name__)


@register_function(config_type=AzureAISearchConfig)
async def azure_ai_search_retrieval(config: AzureAISearchConfig, _builder: Builder):
    """Retrieval workflow function backed by Azure AI Search.

    Yields a `FunctionInfo` wrapping an async `search(query) -> str` callable.
    The accompanying `AzureAISearchIngestor` is set as the active ingestor so
    the agent's `/v1/data_sources` upload route (already wired by AI-Q) routes
    file uploads to AI Search.
    """

    # The adapter classes were registered with the Knowledge Layer factories at
    # package-import time via the @register_ingestor / @register_retriever
    # decorators in adapter.py. Pull instances from the factory using our
    # backend name and the runtime config.
    config_dict = config.model_dump()
    retriever = get_retriever("azure_ai_search", config_dict)
    ingestor = get_ingestor("azure_ai_search", config_dict)

    # Resolve the summary LLM as a LangChain wrapper so the ingestor can call
    # it from the background thread to generate per-document summaries.
    summary_llm = None
    if config.generate_summary and config.summary_model:
        try:
            summary_llm = await _builder.get_llm(
                config.summary_model, wrapper_type=LLMFrameworkEnum.LANGCHAIN,
            )
            logger.info("Resolved summary LLM alias: %s", config.summary_model)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to resolve summary LLM %r — summaries disabled", config.summary_model)

    # Initialise the summary persistence store. URL precedence:
    # 1. config.summary_db (explicit), 2. AIQ_SUMMARY_DB env var, 3. fallback default.
    summary_db_url = config.summary_db or os.environ.get(
        "AIQ_SUMMARY_DB", "sqlite:///./summaries.db",
    )
    try:
        configure_summary_db(summary_db_url)
        logger.info("Configured summary DB")
    except Exception:  # noqa: BLE001
        logger.exception("configure_summary_db failed — summaries may not persist")

    # Hand the summary LLM to the ingestor so it can generate-and-register on
    # successful ingestion.
    if summary_llm is not None and hasattr(ingestor, "set_summary_llm"):
        ingestor.set_summary_llm(summary_llm)

    # Wire up file-upload routing: /v1/data_sources will dispatch to our ingestor.
    set_active_ingestor(ingestor)

    logger.info(
        "azure_ai_search_retrieval initialized: default_collection=%s, endpoint=%s, top_k=%d",
        config.collection_name, config.endpoint, config.top_k,
    )

    async def search(query: str) -> str:
        """Search AI Search for chunks relevant to the query.

        The collection is selected dynamically: AI-Q passes a per-conversation
        ID via `Context.get().conversation_id` (e.g. `s_f031d9cf_...`), and
        the ingestor uses the same ID when files are uploaded — so retrieval
        and ingestion stay scoped to the same session collection. Falls back
        to the static config default if no context is available (e.g. when
        the function is called from a unit test).
        """
        try:
            ctx = Context.get()
            session_collection = ctx.conversation_id if ctx else None
        except Exception:  # noqa: BLE001
            session_collection = None
        target_collection = session_collection or config.collection_name

        try:
            # SearchClient is sync; offload to a worker thread so we don't
            # block the asyncio event loop on network I/O.
            result = await asyncio.to_thread(
                retriever.retrieve,
                query=query,
                collection_name=target_collection,
                top_k=config.top_k,
            )
            if not result.success:
                return f"Error searching knowledge base: {result.error_message}"
            if not result.chunks:
                return f"No relevant documents found in collection {target_collection!r}."
            return _format_chunks(result.chunks)
        except Exception as e:  # noqa: BLE001
            logger.exception("azure_ai_search_retrieval search failed")
            return f"Error searching knowledge base: {e}"

    yield FunctionInfo.from_fn(
        search,
        description=(
            "Search the knowledge base (Azure AI Search) for documents "
            "relevant to the query. Returns formatted chunk text with "
            "filename and page citations."
        ),
    )


def _format_chunks(chunks) -> str:
    """Format `Chunk` objects in the structured shape AI-Q's downstream
    agents expect.

    Matches `knowledge_layer.register._format_results` exactly — the citation
    registry in `aiq_agent.agents.shallow_researcher` parses `Source:` /
    `Citation:` lines to register sources for verification. Without these
    fields, retrieve() succeeds but the agent reports "no sources captured"
    and answers nothing.
    """
    if not chunks:
        return "No relevant documents found."

    lines = [f"Found {len(chunks)} relevant document(s):\n"]
    for i, c in enumerate(chunks, start=1):
        if c.page_number and c.page_number > 0:
            citation = f"{c.file_name}, p.{c.page_number}"
        else:
            citation = c.file_name or "unknown"

        lines.append(f"--- Result {i} ---")
        lines.append(f"Source: {c.file_name or 'unknown'}")
        if c.page_number and c.page_number > 0:
            lines.append(f"Page: {c.page_number}")
        lines.append(f"Citation: {citation}")
        lines.append(f"Content Type: {c.content_type.value}")
        lines.append(f"Relevance Score: {c.score:.2f}")
        lines.append("")

        content = c.content
        if len(content) > 1500:
            content = content[:1500] + "... [truncated]"
        lines.append(content)
        lines.append("")

    return "\n".join(lines)

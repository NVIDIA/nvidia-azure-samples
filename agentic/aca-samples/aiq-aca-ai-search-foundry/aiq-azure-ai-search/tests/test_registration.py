# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests — runnable inside an AI-Q dev container.

These tests assume `aiq_agent.knowledge` is importable (i.e. they must run
inside the agent image, not in a bare CI venv). For unit tests that mock
the AI-Q SDK, see tests/unit/ in later phases.
"""

from __future__ import annotations

import importlib

import pytest

aiq_knowledge = pytest.importorskip("aiq_agent.knowledge")


def test_package_imports_cleanly():
    """Importing the adapter package must not raise."""
    importlib.import_module("aiq_azure_ai_search")


def test_backend_registered():
    """After import, both factories must know about `azure_ai_search`."""
    importlib.import_module("aiq_azure_ai_search")
    from aiq_agent.knowledge import factory

    assert factory.is_ingestor_registered("azure_ai_search"), (
        "AzureAISearchIngestor should be registered with the factory"
    )
    assert factory.is_retriever_registered("azure_ai_search"), (
        "AzureAISearchRetriever should be registered with the factory"
    )


def test_classes_inherit_from_sdk_bases():
    from aiq_azure_ai_search.adapter import AzureAISearchIngestor, AzureAISearchRetriever
    from aiq_agent.knowledge import BaseIngestor, BaseRetriever

    assert issubclass(AzureAISearchIngestor, BaseIngestor)
    assert issubclass(AzureAISearchRetriever, BaseRetriever)


def test_retriever_returns_empty_result():
    from aiq_azure_ai_search.adapter import AzureAISearchRetriever

    r = AzureAISearchRetriever(
        config={"endpoint": "https://x.search.windows.net", "embed_endpoint": "https://e/v1"}
    )
    result = r.retrieve("hello", "test_collection", top_k=5)
    assert result.success
    assert result.chunks == []

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""AI-Q 2.2 compatibility fixes loaded by Python at startup."""

from collections.abc import Sequence

from aiq_agent.common import citation_verification
from llama_index.embeddings.nvidia import NVIDIAEmbedding
from nat.data_models.gated_field_mixin import GatedFieldMixin
from nat.data_models.thinking_mixin import ThinkingMixin


_LIGHTNING_DEPLOYMENT = "nemotron-3-5-lightning"
_EMBED_MODEL = "nvidia/nemotron-3-embed-1b"
_EMBED_DEPLOYMENT = "nemotron-3-embed-1b"


_original_is_knowledge_citation = citation_verification._is_knowledge_citation


def _is_knowledge_citation(ref_text: str, registry=None):
    """Prefer the locator after AI-Q's duplicated ``key: key`` rendering."""
    if ": " in ref_text:
        match = _original_is_knowledge_citation(ref_text.rsplit(": ", 1)[-1], registry)
        if match[0] and match[1] and (registry is None or registry.has_citation_key(match[1])):
            return match
    return _original_is_knowledge_citation(ref_text, registry)


citation_verification._is_knowledge_citation = _is_knowledge_citation


# NAT 1.8 gates `thinking` by canonical NVIDIA model name, while Foundry routes
# OpenAI requests by deployment name. Recognize only this sample's deployment.
_original_check_field_support = GatedFieldMixin._check_field_support.__func__
_thinking_supported_patterns = ThinkingMixin._gated_field_mixins[0].supported


def _check_field_support(
    cls,
    instance: object,
    unsupported,
    supported,
    keys: Sequence[str],
) -> bool:
    if supported is _thinking_supported_patterns and any(
        getattr(instance, key, None) == _LIGHTNING_DEPLOYMENT for key in keys
    ):
        return True
    return _original_check_field_support(cls, instance, unsupported, supported, keys)


GatedFieldMixin._check_field_support = classmethod(_check_field_support)

_original_thinking_system_prompt = ThinkingMixin.thinking_system_prompt.fget


def _thinking_system_prompt(self) -> str | None:
    if any(
        getattr(self, key, None) == _LIGHTNING_DEPLOYMENT
        for key in ("model_name", "model", "azure_deployment")
    ):
        if self.thinking is None:
            return None
        return "/think" if self.thinking else "/no_think"
    return _original_thinking_system_prompt(self)


ThinkingMixin.thinking_system_prompt = property(_thinking_system_prompt)


# Foundry's Nemotron embedding deployment uses text prefixes instead of NIM's
# `input_type` request field. Translate inside the existing AI-Q process.
_original_get_query_embedding = NVIDIAEmbedding._get_query_embedding
_original_get_text_embedding = NVIDIAEmbedding._get_text_embedding
_original_get_text_embeddings = NVIDIAEmbedding._get_text_embeddings


def _create_foundry_embeddings(self, texts: list[str], input_type: str) -> list[list[float]]:
    response = self._client.embeddings.create(
        input=[f"{input_type}: {text}" for text in texts],
        model=_EMBED_DEPLOYMENT,
    )
    return [item.embedding for item in response.data]


def _get_query_embedding(self, query: str) -> list[float]:
    if self.model != _EMBED_MODEL:
        return _original_get_query_embedding(self, query)
    return _create_foundry_embeddings(self, [query], "query")[0]


def _get_text_embedding(self, text: str) -> list[float]:
    if self.model != _EMBED_MODEL:
        return _original_get_text_embedding(self, text)
    return _create_foundry_embeddings(self, [text], "passage")[0]


def _get_text_embeddings(self, texts: list[str]) -> list[list[float]]:
    if self.model != _EMBED_MODEL:
        return _original_get_text_embeddings(self, texts)
    return _create_foundry_embeddings(self, texts, "passage")


NVIDIAEmbedding._get_query_embedding = _get_query_embedding
NVIDIAEmbedding._get_text_embedding = _get_text_embedding
NVIDIAEmbedding._get_text_embeddings = _get_text_embeddings

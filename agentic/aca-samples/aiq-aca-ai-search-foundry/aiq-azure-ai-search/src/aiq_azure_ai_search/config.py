"""Pydantic config models for the Azure AI Search adapter.

Mirrors the keys exposed under `functions.knowledge_search` in
config_web_azure.yml. Both Ingestor and Retriever read from the same config
object — they share an embedding endpoint, an AI Search service, and a
collection naming convention.

The class name registered with NAT (via the `name=` kwarg below) is the value
participants put under `_type:` in their workflow YAML.
"""

from __future__ import annotations

from typing import Annotated, Literal

from nat.data_models.function import FunctionBaseConfig
from pydantic import Field, HttpUrl


class AzureAISearchConfig(FunctionBaseConfig, name="azure_ai_search_retrieval"):
    """Adapter-level config consumed at registration / first-call time.

    Field names match the YAML keys 1:1 to keep YAML→object mapping mechanical.
    Extends FunctionBaseConfig so NAT's `register_function` can use it as the
    config type for the `_type: azure_ai_search_retrieval` workflow function.
    """

    # --- AI Search service ---
    endpoint: HttpUrl = Field(
        ..., description="Azure AI Search service URL, e.g. https://<svc>.search.windows.net"
    )
    collection_name: str = Field(
        "aiq_default",
        description="AI Search index name. One index per AI-Q collection.",
    )
    api_key: str | None = Field(
        None,
        description=(
            "Optional admin key for AI Search. If omitted, DefaultAzureCredential "
            "is used (managed identity in production)."
        ),
    )

    # --- Embedding endpoint (Foundry NIM) ---
    embed_endpoint: HttpUrl = Field(..., description="Foundry NIM embedding endpoint /v1 URL")
    embed_model: str = Field(
        "nvidia/llama-3.2-nv-embedqa-1b-v2",
        description="Model name passed to the embedding NIM",
    )
    embed_dim: Annotated[int, Field(gt=0)] = Field(
        2048,
        description=(
            "Output dimensionality. Must match the embedding model. "
            "Llama-3.2-NV-embedqa-1b-v2 emits 2048-dim vectors."
        ),
    )
    embed_api_key: str | None = Field(
        None,
        description=(
            "API key for the embedding NIM. If omitted, falls back to AZURE auth "
            "via UAMI (managed identity)."
        ),
    )

    # --- Retrieval behaviour ---
    top_k: Annotated[int, Field(gt=0, le=100)] = Field(5, description="Default chunks per query")
    use_hybrid: bool = Field(True, description="Vector + keyword hybrid retrieval")
    use_semantic_ranker: bool = Field(
        True,
        description="AI Search's built-in semantic ranker. Free on Basic SKU + 'free' semanticSearch.",
    )

    # --- Summarisation ---
    generate_summary: bool = Field(
        True,
        description="On successful ingest, generate a one-sentence summary and register it.",
    )
    summary_model: str | None = Field(
        "summary_llm",
        description="LLM alias (defined in `llms:` block) used for per-document summaries.",
    )
    summary_max_chars: int = Field(1000, description="Truncation length for the summary prompt")
    summary_db: str | None = Field(
        None,
        description=(
            "Optional URL for the summary persistence store (e.g. "
            "`postgresql+psycopg://user:pw@host/db?sslmode=require`). If unset, "
            "the AIQ_SUMMARY_DB env var is used by AI-Q's summary infrastructure."
        ),
    )

    # --- Chunking (mirror llamaindex backend defaults) ---
    chunk_size: int = Field(512, description="Tokens per chunk")
    chunk_overlap: int = Field(64, description="Token overlap between adjacent chunks")

    # --- Ingest behaviour ---
    cleanup_files: bool = Field(
        True,
        description="Delete temp upload files after ingestion (success or failure).",
    )

    # --- Field naming inside the AI Search index ---
    field_id: str = "id"
    field_chunk: str = "chunk"
    field_embedding: str = "embedding"
    field_metadata: str = "metadata"
    field_doc_id: str = "doc_id"

    # --- Misc ---
    auth_mode: Literal["managed_identity", "api_key"] = Field(
        "managed_identity",
        description=(
            "Which auth mode the adapter prefers. 'managed_identity' uses "
            "DefaultAzureCredential (UAMI in ACA). 'api_key' requires `api_key` "
            "(and optionally `embed_api_key`) to be set."
        ),
    )

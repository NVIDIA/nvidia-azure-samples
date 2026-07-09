# aiq-azure-ai-search

Azure AI Search Knowledge Layer adapter for [NVIDIA AI-Q](https://docs.nvidia.com/aiq-blueprint/latest/).

Routes AI-Q's document ingestion and retrieval through **Azure AI Search**, with embeddings from any OpenAI-compatible embedding endpoint (NVIDIA NIM, Azure OpenAI, or build.nvidia.com). AI-Q's source stays untouched — the package self-registers via the `nat.plugins` entry-point group declared in `pyproject.toml`.

After `pip install`-ing the package into a stock AI-Q v2 image and pointing the workflow config's `knowledge_search` block at `_type: azure_ai_search_retrieval`, the agent can:

1. Accept document uploads through the AI-Q frontend
2. Ingest them into Azure AI Search (parse → chunk → embed → push)
3. Generate per-document summaries that get persisted to `AIQ_SUMMARY_DB` and injected into the agent's system prompt
4. Retrieve relevant chunks via hybrid search + semantic ranking, normalised to the `Chunk` schema
5. Surface ingestion progress and errors through `IngestionJobStatus` and `FileProgress.error_message`

…all without modifying AI-Q's source code.

---

## Status

End-to-end working against AI-Q v2 (`nvcr.io/nvidia/blueprint/aiq-agent:2.0.0`). The adapter:

- Auto-registers via the `nat.plugins` entry point declared in `pyproject.toml`
- Adds a new workflow function type `azure_ai_search_retrieval` via `@register_function`
- Registers `AzureAISearchIngestor` and `AzureAISearchRetriever` under the `azure_ai_search` name in the Knowledge Layer factories
- Creates AI Search indexes on demand with a vector field, HNSW (cosine), and a default semantic configuration; vector dimension is configurable to match the embedding model
- Ingests via LlamaIndex `SimpleDirectoryReader` + `SentenceSplitter` (chunk_size=512, overlap=64) → batched embedding via `NVIDIAEmbedding` → `SearchClient.upload_documents`
- Retrieves via hybrid (text + vector) + semantic-ranked search, returning `Chunk` objects with citations like `report.pdf, p.4`
- Generates per-document summaries via a LangChain-wrapped summary LLM and registers them via `register_summary`; `delete_file` unregisters

### Compatibility

| Component | Tested against |
| --- | --- |
| AI-Q | `nvcr.io/nvidia/blueprint/aiq-agent:2.0.0` |
| Azure AI Search | Basic SKU with free semantic ranker; vectors with HNSW (cosine) |
| Embedding model | `llama-3.2-nv-embedqa-1b-v2` (2048-dim) on a NIM endpoint; any OpenAI-compatible `/v1/embeddings` URL should work |
| Summary / chat LLM | Configurable; tested with Nemotron-3-Nano and OpenAI-compatible endpoints |
| Python | 3.11+ |

---

## Install

The adapter is not (yet) published — install from source:

```bash
pip install -e ./aiq-azure-ai-search
```

For containerized deploys, copy the package into the AI-Q image and `pip install /tmp/adapter` inside the build:

```dockerfile
FROM nvcr.io/nvidia/blueprint/aiq-agent:2.0.0
COPY ./aiq-azure-ai-search /tmp/adapter
RUN ["/app/.venv/bin/pip", "install", "--no-cache-dir", "/tmp/adapter"]
```

The `nat.plugins` entry point makes NAT auto-discover the package on startup — no manual import needed.

---

## Required config

Add a `knowledge_search` function (or any name — `knowledge_search` is the AI-Q convention) under `functions:` in your workflow config:

```yaml
functions:
  knowledge_search:
    _type: azure_ai_search_retrieval
    collection_name: my_collection
    endpoint: https://<your-search>.search.windows.net
    # AI Search auth via DefaultAzureCredential; set AZURE_CLIENT_ID env
    # var if running with a user-assigned managed identity.

    embed_endpoint: https://<your-embedding-endpoint>/v1
    embed_model: nvidia/llama-3.2-nv-embedqa-1b-v2
    embed_dim: 2048
    embed_api_key: <key-or-${ENV_VAR}>

    top_k: 5
    use_hybrid: true
    use_semantic_ranker: true
    generate_summary: true
    summary_model: summary_llm   # alias of an LLM defined under llms:
```

See [`config.py`](src/aiq_azure_ai_search/config.py) for the full Pydantic schema.

What's documented in code (not duplicated here): the routing of session-scoped collections via `Context.get().conversation_id`, the structured `Source:`/`Citation:` output format that AI-Q's CitationRegistry parses, and the threading-based async submission model with `IngestionJobStatus.file_details` updates.

---

## Reference material

This adapter follows the [Knowledge Layer SDK Reference](https://docs.nvidia.com/aiq-blueprint/latest/reference/knowledge-layer-sdk.html) and the workflow guide at [Adding a Data Source](https://docs.nvidia.com/aiq-blueprint/latest/extending/adding-a-data-source.html). The two reference adapters in the AI-Q source tree — `sources/knowledge_layer/src/llamaindex/adapter.py` (vector-DB analogue) and `sources/knowledge_layer/src/foundational_rag/adapter.py` (HTTP-based) — are also worth reading. The LlamaIndex adapter is the closer analogue: it parses with LlamaIndex, chunks, embeds, and pushes to a vector DB (ChromaDB instead of AI Search).

### Confirmed import paths

The SDK reference's `knowledge_layer.factory` / `knowledge_layer.base` examples are placeholders, as flagged in the docs. The actual locations (verified by inspecting the running upstream image):

```python
# All public types and registration functions are re-exported from one module:
from aiq_agent.knowledge import (
    BaseIngestor, BaseRetriever,
    Chunk, ContentType, RetrievalResult,
    IngestionJobStatus, JobState, FileProgress,
    AvailableDocument,
    register_ingestor, register_retriever,
    register_summary, unregister_summary,
    set_active_ingestor, get_active_ingestor,
    get_ingestor, get_retriever,
    configure_summary_db,
)

# Additional types only in the .base submodule (not re-exported):
from aiq_agent.knowledge.base import CollectionInfo, FileInfo, TTLCleanupMixin
```

The separate `knowledge_layer` namespace package exposes only the workflow-config glue (`KnowledgeRetrievalConfig`, `knowledge_retrieval`, `register`) — not the SDK base classes. Don't import from there.

Schemas (`Chunk`, `RetrievalResult`, etc.) live inside `aiq_agent.knowledge.base`; there is no `aiq_agent.knowledge.schemas` module despite what some docs may suggest. Summary persistence is at `aiq_agent.knowledge.summary_store` (internal — call `register_summary` / `unregister_summary` from the top-level `aiq_agent.knowledge` instead).

---

## Roadmap

### Hardening (1–2 days)

- **Error handling.** Wrap each Azure SDK call with try/except matching specific exception types (`ResourceNotFoundError`, `HttpResponseError`, `ServiceRequestError`, `ClientAuthenticationError`). Translate each to a user-readable string for `FileProgress.error_message` or `RetrievalResult.error_message`.
- **Config validation.** Strict Pydantic — reject unknown fields, validate URL formats, enforce `embed_dim` matches one of the known model dimensions.
- **Telemetry.** OpenTelemetry spans around `submit_job`, `retrieve`, embedding batches, AI Search writes. AI-Q already feeds spans to App Insights via `APPLICATIONINSIGHTS_CONNECTION_STRING` — these flow through automatically.
- **Concurrency safety audit.** Stress test the `_jobs` dict with parallel uploads. Confirm the lock covers all reads and writes.
- **Troubleshooting docs.** A section keyed by common error messages (the SSL/sslmode dance, the `chat_template_kwargs` gotcha, the missing-`job_info`-table fix, the citation-format requirement, etc.).

### Tests (~2 days)

- **Unit** (`tests/unit/`) — mock `azure-search-documents` and `llama-index-embeddings-nvidia`; verify the adapter produces the right SDK calls in the right order. Run on every commit.
- **Schema** (`tests/schema/`) — validate that `normalize()` produces valid `Chunk` objects with all required fields populated. Cover every branch (text, table, chart, image, missing-page, etc.).
- **Integration** (`tests/integration/`) — real AI Search instance, real embedding endpoint. Gated behind a `--azure` pytest flag plus environment variables. Runs nightly or on tag.
- **Smoke** — build the adapter, install into a Docker container of AI-Q v2, run pytest against the agent's HTTP API. Slow but invaluable.

### CI/CD and release (½ day)

- **`.github/workflows/ci.yml`** — on push and PR: `ruff`, `mypy`, unit tests, build wheel.
- **`.github/workflows/release.yml`** — on tag push (`v*`): integration + smoke tests, build wheel, attach to GitHub release.
- **Versioning.** Semantic versioning, `0.x.y` until the API stabilises. Bump major when AI-Q's plugin API changes in a breaking way.

---

## Out of scope for v1

- **Multimodal extraction** (table/chart/image content types). v1 is `ContentType.TEXT` everywhere. Add when the LlamaIndex multimodal pipeline is needed.
- **Multi-tenant collection isolation.** v1 is one collection per AI Search index (session-scoped via `conversation_id`), no auth checks beyond the configured Search-side identity.
- **AI Search integrated vectorization** (vectorizer config so the index embeds queries itself). Defer until you need indexer-based ingestion of large corpora or want to remove the client-side embedding hop.
- **Hybrid retrieval tuning knobs.** v1 exposes hybrid + semantic-ranker as booleans. Add fine-grained control (alpha weighting, score thresholds) when evals demand it.
- **`select_sources()` / `get_selected_sources()`.** Optional methods for multi-collection queries. Skip until needed.
- **Sustained-load embedding-endpoint capacity planning.** v1 leaves embedding throughput to whatever the configured endpoint supports. For high-concurrency deployments, size your embedding endpoint accordingly or wait for v0.2's integrated-vectorization path (which moves the embedding call out of the adapter entirely).

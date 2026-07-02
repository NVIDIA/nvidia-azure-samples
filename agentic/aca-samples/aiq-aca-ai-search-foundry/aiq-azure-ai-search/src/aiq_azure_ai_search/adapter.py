from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiq_agent.knowledge import (
    BaseIngestor,
    BaseRetriever,
    Chunk,
    ContentType,
    FileProgress,
    IngestionJobStatus,
    JobState,
    RetrievalResult,
    register_ingestor,
    register_retriever,
    register_summary,
    unregister_summary,
)
from aiq_agent.knowledge.base import CollectionInfo, FileInfo
from aiq_agent.knowledge.schema import FileStatus
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
)
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchAlgorithmMetric,
    VectorSearchProfile,
)
from azure.search.documents.models import VectorizedQuery

from .config import AzureAISearchConfig

logger = logging.getLogger(__name__)

_BACKEND_NAME = "azure_ai_search"
_SEMANTIC_CONFIG = "default-semantic"  # matches aiq-search-index.json


def _coerce_config(
    config: dict[str, Any] | AzureAISearchConfig | None,
    kwargs: dict[str, Any],
) -> AzureAISearchConfig:
    """Normalise (config-as-dict | config-as-instance | kwargs) into a typed config."""
    if isinstance(config, dict):
        return AzureAISearchConfig(**(config | kwargs))
    if isinstance(config, AzureAISearchConfig):
        return config
    if kwargs:
        return AzureAISearchConfig(**kwargs)
    raise ValueError("AzureAISearchConfig is required")


def _build_search_credential(cfg: AzureAISearchConfig):
    """Pick the right Azure SDK credential based on auth_mode."""
    if cfg.auth_mode == "api_key":
        if not cfg.api_key:
            raise ValueError("auth_mode=api_key requires the `api_key` field to be set")
        return AzureKeyCredential(cfg.api_key)
    # Default: managed identity. The UAMI's client id comes from the
    # AZURE_CLIENT_ID env var on the Container App.
    return DefaultAzureCredential()


# =============================================================================
# Retriever (Phase 2 — real)
# =============================================================================


@register_retriever(_BACKEND_NAME)
class AzureAISearchRetriever(BaseRetriever):
    """Hybrid + semantic-ranked retriever backed by Azure AI Search.

    Per AI Search index, the schema has at minimum: `id` (key), `chunk` (text,
    searchable), `embedding` (Collection(Edm.Single), vectorSearchProfile),
    plus `doc_id`, `file_name`, `page_number`, `metadata`. See
    aiq-search-index.json for the canonical schema.
    """

    backend_name = _BACKEND_NAME

    def __init__(self, config: dict[str, Any] | AzureAISearchConfig | None = None, **kwargs: Any):
        cfg = _coerce_config(config, kwargs)
        self.cfg = cfg

        # Lazy embedding-client init — `llama_index.embeddings.nvidia.NVIDIAEmbedding`
        # is imported at first use to keep the package installable in environments
        # where llama-index isn't present (unit tests).
        self._embedding = None

        # SearchClient is bound to a single index, so we cache one per collection.
        self._credential = _build_search_credential(cfg)
        self._client_cache: dict[str, SearchClient] = {}

        logger.info(
            "AzureAISearchRetriever initialized: endpoint=%s embed_model=%s embed_endpoint=%s "
            "use_hybrid=%s use_semantic_ranker=%s",
            cfg.endpoint, cfg.embed_model, cfg.embed_endpoint,
            cfg.use_hybrid, cfg.use_semantic_ranker,
        )

    @property
    def embedding(self):
        if self._embedding is None:
            from llama_index.embeddings.nvidia import NVIDIAEmbedding

            self._embedding = NVIDIAEmbedding(
                model=self.cfg.embed_model,
                base_url=str(self.cfg.embed_endpoint),
                api_key=self.cfg.embed_api_key,  # falls back to NVIDIA_API_KEY env
            )
        return self._embedding

    def _get_client(self, collection_name: str) -> SearchClient:
        if collection_name not in self._client_cache:
            self._client_cache[collection_name] = SearchClient(
                endpoint=str(self.cfg.endpoint),
                index_name=collection_name,
                credential=self._credential,
            )
        return self._client_cache[collection_name]

    # ------- abstract methods -------

    def retrieve(
        self,
        query: str,
        collection_name: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        """Hybrid + (optionally) semantic-ranked search. Never raises."""
        try:
            # 1. Embed the query
            query_vector = self.embedding.get_query_embedding(query)

            # 2. Build the search request
            client = self._get_client(collection_name)
            vector_query = VectorizedQuery(
                vector=query_vector,
                # Over-fetch on the vector side so the reranker has more
                # candidates to choose from — semantic ranking only re-orders
                # the input set, it doesn't reach back into the index.
                k_nearest_neighbors=max(top_k * 3, 20),
                fields="embedding",
            )

            search_params: dict[str, Any] = {
                "vector_queries": [vector_query],
                "top": top_k,
                "select": ["id", "chunk", "doc_id", "file_name", "page_number", "metadata"],
            }
            if self.cfg.use_hybrid:
                # Hybrid = include lexical text alongside the vector query.
                search_params["search_text"] = query
            if self.cfg.use_semantic_ranker:
                search_params["query_type"] = "semantic"
                search_params["semantic_configuration_name"] = _SEMANTIC_CONFIG
            if filters and isinstance(filters, dict):
                # v1: pass through a pre-built OData filter string under a
                # "$filter" key. Structured dict→OData translation is Phase 5.
                if odata := filters.get("$filter"):
                    search_params["filter"] = odata

            # 3. Run the search
            raw_results = client.search(**search_params)
            chunks = [self.normalize(hit) for hit in raw_results]

            logger.info(
                "retrieve OK: collection=%s query=%r returned=%d top_k=%d",
                collection_name, query[:80], len(chunks), top_k,
            )
            return RetrievalResult(
                query=query,
                backend=_BACKEND_NAME,
                chunks=chunks,
                success=True,
            )

        except ResourceNotFoundError:
            msg = f"AI Search index {collection_name!r} not found. Has the collection been created yet?"
            logger.warning(msg)
            return RetrievalResult(query=query, backend=_BACKEND_NAME, chunks=[], success=False, error_message=msg)
        except ClientAuthenticationError as e:
            msg = f"AI Search authentication failed: {e!s}"
            logger.error(msg)
            return RetrievalResult(query=query, backend=_BACKEND_NAME, chunks=[], success=False, error_message=msg)
        except HttpResponseError as e:
            msg = f"AI Search request failed: {e.status_code} {e.reason or e!s}"
            logger.error(msg, exc_info=True)
            return RetrievalResult(query=query, backend=_BACKEND_NAME, chunks=[], success=False, error_message=msg)
        except Exception as e:  # noqa: BLE001 — surface any unexpected error as a UI-readable string
            msg = f"Unexpected error during retrieval: {e!s}"
            logger.exception("retrieve unexpected error")
            return RetrievalResult(query=query, backend=_BACKEND_NAME, chunks=[], success=False, error_message=msg)

    def normalize(self, raw_result: Any) -> Chunk:
        """Map one AI Search hit dict to a Chunk."""
        chunk_id = raw_result.get("id") or str(uuid.uuid4())
        content = raw_result.get("chunk") or ""
        file_name = raw_result.get("file_name") or "unknown"
        page_number = raw_result.get("page_number")
        doc_id = raw_result.get("doc_id")

        # Score: prefer the semantic reranker score (0–4 typical range, normalise
        # to 0–1) when available, otherwise the search score (already 0+ from
        # hybrid RRF; clamp to 1).
        rerank_score = raw_result.get("@search.reranker_score")
        search_score = raw_result.get("@search.score") or 0.0
        if rerank_score is not None:
            score = min(max(float(rerank_score) / 4.0, 0.0), 1.0)
        else:
            score = min(max(float(search_score), 0.0), 1.0)

        if page_number is not None:
            display_citation = f"{file_name}, p.{page_number}"
        else:
            display_citation = str(file_name)

        metadata: dict[str, Any] = {}
        if doc_id:
            metadata["doc_id"] = doc_id
        # Pass through any raw metadata blob if present
        if raw_meta := raw_result.get("metadata"):
            metadata["raw"] = raw_meta

        return Chunk(
            chunk_id=str(chunk_id),
            content=str(content),
            score=score,
            file_name=str(file_name),
            page_number=page_number,
            display_citation=display_citation,
            content_type=ContentType.TEXT,
            metadata=metadata,
        )

    # ------- optional -------

    async def health_check(self) -> bool:
        """Return True if the configured collection's index is reachable."""
        try:
            client = self._get_client(self.cfg.collection_name)
            client.get_document_count()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Retriever health_check failed")
            return False


# =============================================================================
# Index schema builder
# =============================================================================


# AI Search index names: lowercase letters, digits, hyphens, underscores;
# 2-128 chars; must start and end with a letter or digit; no consecutive dashes.
# (The official docs say "letters, numbers, dashes" but the service actually
# accepts underscores too, and the AI-Q frontend generates session-style
# collection names like "s_f031d9cf_...".)
_INDEX_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$")


def _validate_index_name(name: str) -> None:
    if not _INDEX_NAME_RE.match(name) or "--" in name:
        raise ValueError(
            f"Invalid AI Search index name {name!r}. Names must be lowercase, "
            "use only letters/digits/hyphens/underscores, be 2-128 chars, "
            "start and end with a letter or digit, and not contain '--'."
        )


def _build_index_schema(name: str, embed_dim: int) -> SearchIndex:
    """Build the canonical Azure AI Search index schema for a collection.

    Mirrors aiq-search-index.json — same fields, same vector profile, same
    semantic configuration. Anything that queries the index must agree on
    field names and dimensionality.
    """
    return SearchIndex(
        name=name,
        fields=[
            SimpleField(name="id", type=SearchFieldDataType.String, key=True, filterable=True),
            SearchableField(name="chunk", analyzer_name="standard.lucene"),
            SearchField(
                name="embedding",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=embed_dim,
                vector_search_profile_name="hnsw-profile",
            ),
            SimpleField(name="doc_id", type=SearchFieldDataType.String, filterable=True),
            SearchableField(name="file_name", filterable=True, sortable=True),
            SimpleField(name="page_number", type=SearchFieldDataType.Int32, filterable=True, sortable=True),
            SimpleField(name="metadata", type=SearchFieldDataType.String),
        ],
        vector_search=VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name="hnsw-default",
                    parameters=HnswParameters(
                        m=4,
                        ef_construction=400,
                        ef_search=500,
                        metric=VectorSearchAlgorithmMetric.COSINE,
                    ),
                ),
            ],
            profiles=[
                VectorSearchProfile(name="hnsw-profile", algorithm_configuration_name="hnsw-default"),
            ],
        ),
        semantic_search=SemanticSearch(
            default_configuration_name=_SEMANTIC_CONFIG,
            configurations=[
                SemanticConfiguration(
                    name=_SEMANTIC_CONFIG,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="file_name"),
                        content_fields=[SemanticField(field_name="chunk")],
                    ),
                ),
            ],
        ),
    )


# =============================================================================
# Ingestor
# =============================================================================


@register_ingestor(_BACKEND_NAME)
class AzureAISearchIngestor(BaseIngestor):
    """Real ingestor: parses uploaded files, chunks them, embeds via Foundry NIM,
    and writes chunks (with vectors) to Azure AI Search.

    Submission is non-blocking — `submit_job` spawns a daemon thread and returns
    a job_id immediately. `get_job_status` reflects the live state. The HTTP
    layer routes file uploads here via `set_active_ingestor` (called from
    register.py).
    """

    backend_name = _BACKEND_NAME

    def __init__(self, config: dict[str, Any] | AzureAISearchConfig | None = None, **kwargs: Any):
        cfg = _coerce_config(config, kwargs)
        self.cfg = cfg

        # Azure SDK clients
        self._credential = _build_search_credential(cfg)
        self._index_client = SearchIndexClient(endpoint=str(cfg.endpoint), credential=self._credential)
        self._search_client_cache: dict[str, SearchClient] = {}

        # Lazy-init heavy llama-index/embedding deps
        self._embedding = None
        self._splitter = None

        # Summary LLM (LangChain wrapper) — set later via `set_summary_llm` from
        # register.py once the workflow Builder has resolved the LLM alias.
        # If None, summaries are skipped silently.
        self._summary_llm = None

        # Job + per-collection in-memory state. AI Search is the source of
        # truth for chunks; this just tracks submission lifecycle.
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, IngestionJobStatus] = {}

        logger.info(
            "AzureAISearchIngestor initialized: endpoint=%s embed_dim=%d chunk_size=%d",
            cfg.endpoint, cfg.embed_dim, cfg.chunk_size,
        )

    # ------- lazy clients -------

    @property
    def embedding(self):
        if self._embedding is None:
            from llama_index.embeddings.nvidia import NVIDIAEmbedding

            self._embedding = NVIDIAEmbedding(
                model=self.cfg.embed_model,
                base_url=str(self.cfg.embed_endpoint),
                api_key=self.cfg.embed_api_key,
            )
        return self._embedding

    @property
    def splitter(self):
        if self._splitter is None:
            from llama_index.core.node_parser import SentenceSplitter

            self._splitter = SentenceSplitter(
                chunk_size=self.cfg.chunk_size,
                chunk_overlap=self.cfg.chunk_overlap,
            )
        return self._splitter

    def set_summary_llm(self, llm: Any) -> None:
        """Inject the LangChain summary LLM resolved by the workflow builder."""
        self._summary_llm = llm
        logger.info("Summary LLM attached to ingestor (type=%s)", type(llm).__name__)

    def _get_search_client(self, collection_name: str) -> SearchClient:
        if collection_name not in self._search_client_cache:
            self._search_client_cache[collection_name] = SearchClient(
                endpoint=str(self.cfg.endpoint),
                index_name=collection_name,
                credential=self._credential,
            )
        return self._search_client_cache[collection_name]

    # ------- index management -------

    def _ensure_index(self, collection_name: str) -> None:
        """Create the AI Search index if it doesn't already exist."""
        _validate_index_name(collection_name)
        try:
            self._index_client.get_index(collection_name)
            logger.debug("Index %r already exists", collection_name)
        except ResourceNotFoundError:
            schema = _build_index_schema(collection_name, self.cfg.embed_dim)
            self._index_client.create_index(schema)
            logger.info("Created AI Search index %r (embed_dim=%d)", collection_name, self.cfg.embed_dim)

    # ------- jobs -------

    def submit_job(
        self,
        file_paths: list[str],
        collection_name: str,
        config: dict[str, Any] | None = None,
    ) -> str:
        job_id = str(uuid.uuid4())
        config = config or {}
        # AI-Q's HTTP layer passes original_filenames as a parallel LIST (one
        # per entry of file_paths), NOT a dict[path, name]. Other callers may
        # pass a dict — handle both shapes.
        original_filenames = _resolve_filenames(file_paths, config.get("original_filenames"))

        # File-progress placeholders so the UI shows them as UPLOADING immediately.
        file_progress = [
            FileProgress(
                file_id=str(uuid.uuid4()),
                file_name=original_filenames[i],
                status=FileStatus.UPLOADING,
            )
            for i in range(len(file_paths))
        ]

        with self._jobs_lock:
            self._jobs[job_id] = IngestionJobStatus(
                job_id=job_id,
                status=JobState.PENDING,
                collection_name=collection_name,
                backend=_BACKEND_NAME,
                submitted_at=datetime.now(UTC),
                total_files=len(file_paths),
                processed_files=0,
                file_details=file_progress,
            )

        logger.info(
            "submit_job: id=%s files=%d collection=%r", job_id, len(file_paths), collection_name,
        )

        thread = threading.Thread(
            target=self._process_job,
            args=(job_id, file_paths, collection_name, config),
            daemon=True,
            name=f"aiq-azure-search-ingest-{job_id[:8]}",
        )
        thread.start()
        return job_id

    def get_job_status(self, job_id: str) -> IngestionJobStatus:
        with self._jobs_lock:
            existing = self._jobs.get(job_id)
        if existing is None:
            return IngestionJobStatus(
                job_id=job_id,
                status=JobState.FAILED,
                collection_name="",
                backend=_BACKEND_NAME,
                submitted_at=datetime.now(UTC),
                error_message="Job not found",
                file_details=[],
            )
        return existing

    def _process_job(
        self,
        job_id: str,
        file_paths: list[str],
        collection_name: str,
        config: dict[str, Any],
    ) -> None:
        """Background ingestion worker. Updates job state in place under lock."""
        cleanup = bool(config.get("cleanup_files", self.cfg.cleanup_files))
        original_filenames = _resolve_filenames(file_paths, config.get("original_filenames"))

        # Mark PROCESSING and started_at
        self._update_job(job_id, status=JobState.PROCESSING, started_at=datetime.now(UTC))

        try:
            self._ensure_index(collection_name)
        except Exception as e:  # noqa: BLE001
            self._fail_job(job_id, f"Failed to ensure index {collection_name!r}: {e!s}")
            return

        succeeded = 0
        failed = 0
        for idx, path in enumerate(file_paths):
            file_name = original_filenames[idx]
            self._update_file_progress(job_id, idx, status=FileStatus.INGESTING)
            try:
                chunks_written = self._process_file(
                    path=path, collection_name=collection_name, file_name=file_name,
                )
                self._update_file_progress(
                    job_id, idx,
                    status=FileStatus.SUCCESS,
                    progress_percent=100.0,
                    chunks_created=chunks_written,
                )
                succeeded += 1
                logger.info("Ingested %s: %d chunks → %s", file_name, chunks_written, collection_name)
            except Exception as e:  # noqa: BLE001
                msg = self._translate_error(e)
                self._update_file_progress(
                    job_id, idx,
                    status=FileStatus.FAILED,
                    error_message=msg,
                )
                failed += 1
                logger.exception("Failed to ingest %s", file_name)
            finally:
                if cleanup:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            self._update_job(job_id, processed_files=idx + 1)

        # Final status
        if failed == len(file_paths):
            self._fail_job(job_id, f"All {failed} file(s) failed to ingest")
        else:
            self._update_job(
                job_id,
                status=JobState.COMPLETED,
                completed_at=datetime.now(UTC),
            )

    def _process_file(self, *, path: str, collection_name: str, file_name: str) -> int:
        """Parse → chunk → embed → upload one file. Returns chunk count."""
        from llama_index.core import SimpleDirectoryReader

        # 1. Parse. SimpleDirectoryReader returns a Document per logical chunk
        # (per-page for PDFs). Metadata includes page_label.
        reader = SimpleDirectoryReader(input_files=[path])
        documents = reader.load_data()
        if not documents:
            raise ValueError(f"No content extracted from {file_name}")

        # 2. Chunk each Document with the SentenceSplitter, preserving metadata.
        nodes = self.splitter.get_nodes_from_documents(documents)
        if not nodes:
            raise ValueError(f"Chunking produced 0 chunks for {file_name}")

        # 3. Embed all chunk texts in one batch (NVIDIAEmbedding handles
        # rate limiting + batch sizing internally).
        texts = [n.get_content() for n in nodes]
        embeddings = self.embedding.get_text_embedding_batch(texts)

        # 4. Build AI Search documents. doc_id is per-file, chunk_id per-chunk.
        doc_id = self._make_doc_id(file_name)
        docs = []
        for i, (node, vector) in enumerate(zip(nodes, embeddings, strict=True)):
            chunk_id = f"{doc_id}-c{i:04d}"
            page = _coerce_page_number(node.metadata.get("page_label"))
            docs.append({
                "id": chunk_id,
                "chunk": node.get_content(),
                "embedding": list(vector),
                "doc_id": doc_id,
                "file_name": file_name,
                "page_number": page,
                "metadata": "{}",
            })

        # 5. Upload. SearchClient batches automatically; for very large files
        # we'd chunk this further, but that's a Phase 5 concern.
        client = self._get_search_client(collection_name)
        result = client.upload_documents(documents=docs)
        # Surface partial-failure as an exception so the file gets marked FAILED.
        failures = [r for r in result if not r.succeeded]
        if failures:
            raise RuntimeError(
                f"AI Search rejected {len(failures)}/{len(docs)} chunks: "
                f"{failures[0].error_message}"
            )

        # 6. Summarise + register. AI-Q's intent classifier reads registered
        # summaries from the system prompt's `available_documents` block, so
        # this step is what makes the orchestrator route knowledge queries
        # to us instead of falling through to web search.
        if self.cfg.generate_summary:
            full_text = "\n".join(texts)
            summary = self._generate_summary(full_text, file_name)
            if summary:
                try:
                    register_summary(collection_name, file_name, summary)
                    logger.info("Registered summary for %s in %s", file_name, collection_name)
                except Exception:  # noqa: BLE001
                    logger.exception("register_summary failed for %s", file_name)

        return len(docs)

    def _generate_summary(self, text: str, file_name: str) -> str | None:
        """Generate a one-sentence document summary via the LangChain summary LLM.

        Returns None if no summary LLM is configured or generation fails — both
        are non-fatal; the file is still ingested and queryable, just without
        an entry in the agent's available_documents block.
        """
        if self._summary_llm is None:
            return None
        # Truncate to keep the prompt cheap. The summary LLM is small (Nemotron
        # Nano in the workshop config); we only need a one-line overview.
        snippet = text[: self.cfg.summary_max_chars]
        prompt = (
            f"Summarise the following document ({file_name}) in one sentence "
            f"(max 30 words):\n\n{snippet}"
        )
        try:
            response = self._summary_llm.invoke(prompt)
            summary = getattr(response, "content", None) or str(response)
            return summary.strip() or None
        except Exception:  # noqa: BLE001
            logger.exception("Summary generation failed for %s", file_name)
            return None

    @staticmethod
    def _make_doc_id(file_name: str) -> str:
        """Stable doc_id from the file name. Lowercase + hash for collision-resistance."""
        digest = hashlib.sha256(file_name.encode("utf-8")).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9]+", "-", file_name.lower()).strip("-")[:32]
        return f"{slug}-{digest}" if slug else digest

    # ------- job-state helpers (must be called under _jobs_lock or use it) -------

    def _update_job(self, job_id: str, **fields: Any) -> None:
        with self._jobs_lock:
            existing = self._jobs.get(job_id)
            if existing is None:
                return
            self._jobs[job_id] = existing.model_copy(update=fields)

    def _fail_job(self, job_id: str, error_message: str) -> None:
        self._update_job(
            job_id,
            status=JobState.FAILED,
            error_message=error_message,
            completed_at=datetime.now(UTC),
        )

    def _update_file_progress(self, job_id: str, idx: int, **fields: Any) -> None:
        with self._jobs_lock:
            existing = self._jobs.get(job_id)
            if existing is None or idx >= len(existing.file_details):
                return
            new_details = list(existing.file_details)
            new_details[idx] = existing.file_details[idx].model_copy(update=fields)
            self._jobs[job_id] = existing.model_copy(update={"file_details": new_details})

    # ------- error translation -------

    @staticmethod
    def _translate_error(exc: Exception) -> str:
        """User-readable error string for FileProgress.error_message."""
        if isinstance(exc, ResourceNotFoundError):
            return f"AI Search index not found: {exc!s}"
        if isinstance(exc, ClientAuthenticationError):
            return f"AI Search authentication failed: {exc!s}"
        if isinstance(exc, HttpResponseError):
            return f"AI Search request failed ({exc.status_code}): {exc.reason or exc!s}"
        return str(exc)

    # ------- collections -------

    def create_collection(
        self,
        name: str,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CollectionInfo:
        _validate_index_name(name)
        try:
            self._ensure_index(name)
        except ResourceExistsError:
            pass
        # AI Search doesn't natively store description/metadata at the index
        # level — we just round-trip the request.
        return CollectionInfo(
            name=name,
            description=description,
            file_count=0,
            chunk_count=0,
            backend=_BACKEND_NAME,
            metadata=metadata or {},
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def delete_collection(self, name: str) -> bool:
        try:
            self._index_client.delete_index(name)
            self._search_client_cache.pop(name, None)
            logger.info("Deleted AI Search index %r", name)
            return True
        except ResourceNotFoundError:
            return False

    def list_collections(self) -> list[CollectionInfo]:
        out: list[CollectionInfo] = []
        for idx in self._index_client.list_indexes():
            out.append(CollectionInfo(
                name=idx.name,
                file_count=0,  # not tracked at index level — Phase 5 if needed
                chunk_count=0,
                backend=_BACKEND_NAME,
                metadata={},
            ))
        return out

    def get_collection(self, name: str) -> CollectionInfo | None:
        try:
            idx = self._index_client.get_index(name)
        except ResourceNotFoundError:
            return None
        return CollectionInfo(
            name=idx.name,
            file_count=0,
            chunk_count=0,
            backend=_BACKEND_NAME,
            metadata={},
        )

    # ------- files (delegating to submit_job for upload) -------

    def upload_file(
        self,
        file_path: str,
        collection_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> FileInfo:
        # Synchronous-ish: kick off the job and return file metadata.
        # The frontend uses submit_job for actual upload; this is a single-file alias.
        cfg: dict[str, Any] = {"original_filenames": [Path(file_path).name]}
        if metadata:
            cfg["metadata"] = metadata
        self.submit_job([file_path], collection_name, cfg)
        return FileInfo(
            file_id=str(uuid.uuid4()),
            file_name=Path(file_path).name,
            collection_name=collection_name,
            status=FileStatus.UPLOADING,
            file_size=Path(file_path).stat().st_size if Path(file_path).is_file() else 0,
            chunk_count=0,
            metadata=metadata or {},
            uploaded_at=datetime.now(UTC),
        )

    def delete_file(self, file_id: str, collection_name: str) -> bool:
        # file_id is the doc_id we stamped at ingest time. Delete all chunks
        # whose doc_id matches, and remove the corresponding summary so the
        # agent's available_documents block stays consistent.
        try:
            client = self._get_search_client(collection_name)
            # Look up file_name from any matching chunk so we can unregister the summary.
            results = client.search(
                search_text="*",
                filter=f"doc_id eq '{file_id}'",
                select=["id", "file_name"],
                top=10000,
            )
            hits = list(results)
            if not hits:
                return False
            file_name = hits[0].get("file_name") if hits else None
            client.delete_documents(documents=[{"id": r["id"]} for r in hits])
            if file_name:
                try:
                    unregister_summary(collection_name, file_name)
                except Exception:  # noqa: BLE001
                    logger.exception("unregister_summary failed for %s", file_name)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("delete_file failed")
            return False

    def list_files(self, collection_name: str) -> list[FileInfo]:
        """List one FileInfo per distinct doc_id in the index.

        AI-Q's frontend polls this after ingestion to refresh the Files panel
        and to decide whether to expose `knowledge_search` as an enabled data
        source. Returning [] would cause uploaded files to vanish from the UI.
        """
        try:
            client = self._get_search_client(collection_name)
        except ResourceNotFoundError:
            return []
        try:
            results = client.search(
                search_text="*",
                select=["doc_id", "file_name"],
                top=10000,
            )
        except ResourceNotFoundError:
            return []
        except Exception:  # noqa: BLE001
            logger.exception("list_files failed for %r", collection_name)
            return []

        agg: dict[str, dict[str, Any]] = {}
        for hit in results:
            did = hit.get("doc_id") or "unknown"
            fn = hit.get("file_name") or "unknown"
            entry = agg.setdefault(did, {"file_name": fn, "count": 0})
            entry["count"] += 1

        now = datetime.now(UTC)
        return [
            FileInfo(
                file_id=did,
                file_name=info["file_name"],
                collection_name=collection_name,
                status=FileStatus.SUCCESS,
                file_size=None,
                chunk_count=info["count"],
                metadata={},
                uploaded_at=now,
                ingested_at=now,
            )
            for did, info in agg.items()
        ]

    def get_file_status(self, file_id: str, collection_name: str) -> FileInfo | None:
        """Return the FileInfo for a single doc_id (file_id == doc_id) if it exists."""
        try:
            client = self._get_search_client(collection_name)
            results = client.search(
                search_text="*",
                filter=f"doc_id eq '{file_id}'",
                select=["doc_id", "file_name"],
                top=10000,
            )
            hits = list(results)
        except ResourceNotFoundError:
            return None
        except Exception:  # noqa: BLE001
            logger.exception("get_file_status failed for %r in %r", file_id, collection_name)
            return None

        if not hits:
            return None

        now = datetime.now(UTC)
        return FileInfo(
            file_id=file_id,
            file_name=hits[0].get("file_name") or "unknown",
            collection_name=collection_name,
            status=FileStatus.SUCCESS,
            file_size=None,
            chunk_count=len(hits),
            metadata={},
            uploaded_at=now,
            ingested_at=now,
        )

    # ------- optional -------

    async def health_check(self) -> bool:
        try:
            list(self._index_client.list_index_names())
            return True
        except Exception:  # noqa: BLE001
            logger.exception("Ingestor health_check failed")
            return False


def _resolve_filenames(file_paths: list[str], raw: Any) -> list[str]:
    """Normalise the `original_filenames` config field across caller shapes.

    AI-Q's HTTP route passes a parallel `list[str]` aligned to `file_paths`.
    The SDK reference's example uses a `dict[str, str]` keyed by temp path.
    Anything else (None, empty, mismatched length) falls back to the basename
    of each file_path.
    """
    if isinstance(raw, dict):
        return [raw.get(p) or Path(p).name for p in file_paths]
    if isinstance(raw, list):
        names: list[str] = []
        for i, p in enumerate(file_paths):
            entry = raw[i] if i < len(raw) else None
            names.append(entry or Path(p).name)
        return names
    return [Path(p).name for p in file_paths]


def _coerce_page_number(page_label: Any) -> int | None:
    """LlamaIndex sets page_label as a 1-indexed string for PDFs. Be lenient."""
    if page_label is None:
        return None
    try:
        return int(str(page_label))
    except (TypeError, ValueError):
        return None

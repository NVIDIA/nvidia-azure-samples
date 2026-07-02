"""aiq-azure-ai-search — Azure AI Search Knowledge Layer adapter for AI-Q.

Importing this package registers the `azure_ai_search` backend with AI-Q's
Knowledge Layer factories. NAT auto-discovers the package on startup via the
`nat.plugins` entry point in pyproject.toml, so the backend is known by the
time the workflow config resolves `_type: azure_ai_search_retrieval`.
"""

import logging

# Importing these modules triggers the registration decorators:
#   - .adapter:  @register_ingestor / @register_retriever (Knowledge Layer factories)
#   - .register: @register_function (NAT workflow function registry)
from . import register  # noqa: F401  (import-for-side-effect)
from .adapter import AzureAISearchIngestor, AzureAISearchRetriever

__version__ = "0.1.0"

logger = logging.getLogger(__name__)
logger.info(
    "aiq_azure_ai_search v%s loaded: knowledge_layer backend + "
    "_type: azure_ai_search_retrieval registered",
    __version__,
)

__all__ = ["AzureAISearchIngestor", "AzureAISearchRetriever", "__version__"]

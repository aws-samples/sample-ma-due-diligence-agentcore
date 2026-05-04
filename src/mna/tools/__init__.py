"""Agent tools for the M&A Due Diligence sample.

Each module in this package exposes a single callable tool usable from
the Strands agents (see ``src/mna/agents/``). Modules intentionally
import ``boto3`` lazily so importing ``mna.tools`` never triggers an
AWS SDK load on cold start.
"""

from __future__ import annotations

from mna.tools.citation_check import (
    CitationCheckError,
    check_citations_via_lambda,
)
from mna.tools.kb_retrieve import KBRetrieveError, retrieve
from mna.tools.market_data import MarketDataError, get_comparable_multiples
from mna.tools.memory import (
    PRIOR_DEALS_NAMESPACE,
    MemoryError,
    create_memory_record,
    retrieve_memory,
    session_namespace,
)
from mna.tools.text_to_sql import TextToSqlError, query, validate_sql

__all__ = [
    # kb_retrieve
    "retrieve",
    "KBRetrieveError",
    # text_to_sql
    "query",
    "validate_sql",
    "TextToSqlError",
    # market_data
    "get_comparable_multiples",
    "MarketDataError",
    # memory
    "retrieve_memory",
    "create_memory_record",
    "session_namespace",
    "PRIOR_DEALS_NAMESPACE",
    "MemoryError",
    # citation_check
    "check_citations_via_lambda",
    "CitationCheckError",
]

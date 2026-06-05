"""Bedrock Knowledge Base retrieval tool.

Wraps ``bedrock-agent-runtime:Retrieve`` and returns a list of
:class:`~mna.types.Citation` objects so the agents, the evaluator, and
the notebook all speak the same shape (see design.md → "Components and
Interfaces" → "Tools" → ``tools/kb_retrieve.py``).

The Amazon Bedrock Agent Runtime (``bedrock-agent-runtime``) boto3 client is imported lazily to keep
``import mna`` cold-start safe (same convention used by
:mod:`mna.client` and :mod:`mna.config`).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from mna.config import load_config
from mna.logging_config import get_logger
from mna.types import Citation

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from botocore.client import BaseClient

logger = get_logger(__name__)

#: Default number of passages to return when the caller does not override.
DEFAULT_TOP_K = 5


class KBRetrieveError(RuntimeError):
    """Raised when the KB retrieval call cannot complete.

    Kept separate from :class:`mna.client.ClientError` so callers can
    distinguish "the runtime rejected the invocation" from "the KB
    tool failed while the runtime was up".
    """


def _build_agent_runtime_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for the Bedrock Agent runtime (lazy import)."""

    import boto3  # Lazy import: never at module top level.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("bedrock-agent-runtime", **kwargs)


def _coerce_int(value: Any) -> int | None:
    """Best-effort conversion of a metadata value into an int page number."""

    if isinstance(value, bool):  # ``bool`` is a subclass of ``int`` — reject it.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _coerce_float(value: Any) -> float | None:
    """Best-effort conversion of a numeric value into a float score."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _extract_source(location: dict[str, Any]) -> str:
    """Pull the source URI out of a Retrieve ``location`` payload.

    The Amazon Bedrock API documents several location types (``S3``,
    ``WEB``, ``CONFLUENCE``, ``SALESFORCE``, ``SHAREPOINT``, ``CUSTOM``,
    ``KENDRA``, ``SQL``). We check each known shape and fall back to the
    top-level ``type`` string if no URI is present so the caller still
    sees something human-readable.
    """

    if not isinstance(location, dict):
        return ""

    # S3 is the shape used by this sample's KB.
    s3 = location.get("s3Location")
    if isinstance(s3, dict):
        uri = s3.get("uri")
        if isinstance(uri, str) and uri:
            return uri

    # Other known location shapes use ``<type>Location.url`` or ``.uri``.
    for key in (
        "webLocation",
        "confluenceLocation",
        "salesforceLocation",
        "sharePointLocation",
        "customDocumentLocation",
        "kendraDocumentLocation",
        "sqlLocation",
    ):
        nested = location.get(key)
        if isinstance(nested, dict):
            for field in ("url", "uri", "documentId", "id"):
                value = nested.get(field)
                if isinstance(value, str) and value:
                    return value

    location_type = location.get("type")
    if isinstance(location_type, str):
        return location_type
    return ""


def _extract_page(metadata: dict[str, Any] | None) -> int | None:
    """Pull a page number out of the ``metadata``/``documentAttributes`` payload.

    Amazon Bedrock surfaces page numbers either at the top level of metadata
    (``{"page": 3}``) or inside a ``documentAttributes`` list
    (``[{"key": "page", "value": {"numberValue": 3}}]``). We accept both.
    """

    if not isinstance(metadata, dict):
        return None

    # Common keys that carry a page number.
    for key in ("page", "pageNumber", "page_number", "x-amz-bedrock-kb-chunk-page-number"):
        if key in metadata:
            page = _coerce_int(metadata[key])
            if page is not None:
                return page

    attributes = metadata.get("documentAttributes")
    if isinstance(attributes, list):
        for attr in attributes:
            if not isinstance(attr, dict):
                continue
            name = attr.get("key") or attr.get("name")
            if not isinstance(name, str):
                continue
            if name.lower() not in {"page", "pagenumber", "page_number"}:
                continue
            raw_value = attr.get("value")
            if isinstance(raw_value, dict):
                for field in ("numberValue", "stringValue", "longValue"):
                    if field in raw_value:
                        page = _coerce_int(raw_value[field])
                        if page is not None:
                            return page
            else:
                page = _coerce_int(raw_value)
                if page is not None:
                    return page
    return None


def _passage_to_citation(passage: dict[str, Any]) -> Citation | None:
    """Convert a single Retrieve ``retrievalResults`` entry into a Citation.

    Returns ``None`` when the passage is missing both text and a source,
    since such a record cannot ground a claim.
    """

    if not isinstance(passage, dict):
        return None

    content = passage.get("content") or {}
    text = content.get("text") if isinstance(content, dict) else None
    if not isinstance(text, str):
        text = ""

    location = passage.get("location") or {}
    source = _extract_source(location if isinstance(location, dict) else {})

    if not text and not source:
        return None

    page = _extract_page(passage.get("metadata"))
    score = _coerce_float(passage.get("score"))

    return Citation(text=text, source=source, page=page, score=score)


def retrieve(
    query: str,
    *,
    kb_id: str | None = None,
    top_k: int = DEFAULT_TOP_K,
    bedrock_agent_runtime_client: BaseClient | None = None,
    region_name: str | None = None,
) -> list[Citation]:
    """Retrieve grounded passages from Amazon Bedrock Knowledge Bases.

    Parameters
    ----------
    query:
        Natural-language query string. Must be non-empty.
    kb_id:
        Knowledge Base ID. When omitted, resolved from
        :func:`mna.config.load_config` (SSM ``/mna/kb/id``).
    top_k:
        Maximum number of passages to return. Must be a positive int.
    bedrock_agent_runtime_client:
        Optional boto3 client. Enables dependency injection in tests;
        production callers should let the function build its own.
    region_name:
        Override the AWS region when the function constructs its own
        client. Ignored when ``bedrock_agent_runtime_client`` is set.

    Returns
    -------
    list[Citation]
        One :class:`~mna.types.Citation` per retrieved passage, in the
        order Bedrock returned them. When the KB returns no matches the
        list is empty and an ``info``-level log record is emitted so
        callers can surface the empty-result case to users (see
        Requirement 1.6 — grounded responses must not hallucinate when
        the KB is empty).
    """

    if not isinstance(query, str) or not query.strip():
        raise KBRetrieveError("query must be a non-empty string")

    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise KBRetrieveError(f"top_k must be a positive integer, got {top_k!r}")

    resolved_kb_id = kb_id
    if not resolved_kb_id:
        try:
            resolved_kb_id = load_config(region_name=region_name).kb_id
        except Exception as exc:
            raise KBRetrieveError(
                "kb_id was not provided and could not be resolved from SSM"
            ) from exc

    if not resolved_kb_id:
        raise KBRetrieveError("kb_id resolved to an empty string")

    client = bedrock_agent_runtime_client or _build_agent_runtime_client(region_name=region_name)

    retrieval_config = {
        "vectorSearchConfiguration": {
            "numberOfResults": top_k,
        }
    }

    logger.info(
        "kb_retrieve_started",
        extra={"kb_id": resolved_kb_id, "top_k": top_k, "query_length": len(query)},
    )

    try:
        response = client.retrieve(
            knowledgeBaseId=resolved_kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration=retrieval_config,
        )
    except Exception as exc:
        logger.error(
            "kb_retrieve_failed",
            extra={"kb_id": resolved_kb_id, "error_type": type(exc).__name__},
        )
        raise KBRetrieveError(f"Bedrock Retrieve failed: {exc}") from exc

    results = (response or {}).get("retrievalResults") or []

    citations: list[Citation] = []
    for passage in results:
        citation = _passage_to_citation(passage)
        if citation is not None:
            citations.append(citation)

    if not citations:
        # Surface the KB-empty-result case explicitly (Requirement 1.6).
        # Callers must interpret an empty list as "no grounding available"
        # rather than falling back to ungrounded generation.
        logger.info(
            "kb_retrieve_empty",
            extra={"kb_id": resolved_kb_id, "query_length": len(query), "top_k": top_k},
        )
        return []

    logger.info(
        "kb_retrieve_completed",
        extra={
            "kb_id": resolved_kb_id,
            "top_k": top_k,
            "returned": len(citations),
        },
    )
    return citations

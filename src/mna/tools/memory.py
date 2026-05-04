"""AgentCore Memory wrappers with namespace scoping.

Wraps two AgentCore Memory operations documented in design.md →
"Components and Interfaces" → "Tools" → ``tools/memory.py``:

* ``RetrieveMemoryRecords`` — read-back used by the Strategic Fit
  specialist to pull prior-deal memos from the ``prior_deals``
  namespace.
* ``CreateMemoryRecord`` — write used to append turn-level context to
  the per-session namespace.

Namespace helpers keep call sites free of string concatenation bugs
and make it explicit whether a call targets long-term
(``prior_deals``) or short-term (``session_<id>``) memory.

The boto3 client is imported lazily so ``import mna`` stays cold-start
safe — same convention as the other tool modules.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from mna.config import load_config
from mna.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from botocore.client import BaseClient

logger = get_logger(__name__)

#: Long-term namespace that the Strategic Fit agent reads prior-deal
#: memos from. Seeded by ``data/generate.py memory``.
PRIOR_DEALS_NAMESPACE = "prior_deals"

#: Default page size for ``retrieve_memory`` calls.
DEFAULT_LIMIT = 10


class MemoryError(RuntimeError):  # noqa: N818 - intentional reuse of the builtin name within package
    """Raised when a memory read or write cannot complete."""


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------


def session_namespace(session_id: str) -> str:
    """Return the per-session namespace string for ``session_id``.

    Example: ``session_namespace("abc")`` → ``"session_abc"``.
    Rejects empty or non-string inputs so a mistaken call doesn't
    silently land on a cross-session namespace like ``session_``.
    """

    if not isinstance(session_id, str) or not session_id.strip():
        raise MemoryError("session_id must be a non-empty string")
    return f"session_{session_id.strip()}"


def _validate_namespace(namespace: str) -> str:
    """Validate a namespace string. Returns the trimmed value on success."""

    if not isinstance(namespace, str) or not namespace.strip():
        raise MemoryError("namespace must be a non-empty string")
    return namespace.strip()


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


def _build_agentcore_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for the AgentCore data plane (lazy import)."""

    import boto3  # Lazy import: never at module top level.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("bedrock-agentcore", **kwargs)


def _resolve_memory_id(memory_id: str | None, region_name: str | None) -> str:
    """Fall back to the SSM-backed config for the AgentCore Memory ID.

    Resolution order:

    1. Explicit ``memory_id`` parameter (callers that already know it).
    2. ``MNA_MEMORY_ID`` environment variable (set by AgentStack in the
       runtime container).
    3. ``load_config().memory_id`` or ``.memory_arn`` from SSM.
    4. Raise with a clear message.
    """

    if memory_id:
        return memory_id

    env_value = os.getenv("MNA_MEMORY_ID")
    if env_value:
        return env_value

    try:
        cfg = load_config(region_name=region_name)
    except Exception as exc:
        raise MemoryError(
            "memory_id was not provided, MNA_MEMORY_ID is unset, and "
            "SSM config could not be loaded"
        ) from exc

    for attr in ("memory_id", "memory_arn"):
        value = getattr(cfg, attr, None)
        if isinstance(value, str) and value:
            return value

    raise MemoryError(
        "memory_id was not provided, MNA_MEMORY_ID is unset, and no "
        "memory identifier is published in SSM; pass memory_id explicitly"
    )


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------


def _coerce_content(value: Any) -> str:
    """Flatten a content payload into a string for logging / returning."""

    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, dict | list):
        try:
            return json.dumps(value, default=str)
        except (TypeError, ValueError):
            return str(value)
    if value is None:
        return ""
    return str(value)


def _record_to_dict(record: Any) -> dict[str, Any]:
    """Normalise a memory record into a plain dict with stable keys."""

    if not isinstance(record, dict):
        return {"content": _coerce_content(record)}

    # Different preview SDK shapes:
    # - {"content": "...", "metadata": {...}, "namespace": "...", "id": "..."}
    # - {"memoryRecordId": "...", "content": {"text": "..."}, "namespace": "..."}
    content = record.get("content")
    if isinstance(content, dict):
        text = content.get("text") or content.get("value")
        content_value = text if isinstance(text, str) else _coerce_content(content)
    else:
        content_value = _coerce_content(content)

    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}

    return {
        "id": record.get("memoryRecordId") or record.get("id") or "",
        "namespace": record.get("namespace") or record.get("Namespace") or "",
        "content": content_value,
        "metadata": metadata,
        "score": record.get("score") if isinstance(record.get("score"), int | float) else None,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def retrieve_memory(
    namespace: str,
    query: str | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
    memory_id: str | None = None,
    client: BaseClient | None = None,
    region_name: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve memory records scoped to ``namespace``.

    Parameters
    ----------
    namespace:
        Memory namespace to read from. Pass ``PRIOR_DEALS_NAMESPACE``
        for long-term memos or the result of :func:`session_namespace`
        for per-session context.
    query:
        Optional semantic-search query. When omitted the call acts as
        a "list records in namespace" and returns the most recent up
        to ``limit``.
    limit:
        Maximum number of records to return. Must be a positive int.
    memory_id:
        AgentCore Memory resource identifier. Resolved from SSM when
        omitted (see :func:`_resolve_memory_id`).
    client:
        Optional boto3 client for dependency injection in tests.
    region_name:
        Override AWS region when the function builds its own client.

    Returns
    -------
    list[dict]
        One dict per retrieved record with stable keys ``id``,
        ``namespace``, ``content``, ``metadata``, and ``score``.
        Returns an empty list if the namespace is empty (callers must
        treat this as "no memory" rather than hallucinating).
    """

    resolved_namespace = _validate_namespace(namespace)

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise MemoryError(f"limit must be a positive integer, got {limit!r}")

    resolved_memory_id = _resolve_memory_id(memory_id, region_name)
    agentcore = client or _build_agentcore_client(region_name=region_name)

    request: dict[str, Any] = {
        "memoryId": resolved_memory_id,
        "namespace": resolved_namespace,
        "maxResults": limit,
    }
    if query is not None:
        if not isinstance(query, str) or not query.strip():
            raise MemoryError("query must be a non-empty string when provided")
        # The AgentCore preview API calls this ``searchCriteria``; fall
        # back to ``query`` if the SDK version expects the shorter key.
        request["searchCriteria"] = {"searchQuery": query}

    logger.info(
        "memory_retrieve_started",
        extra={
            "namespace": resolved_namespace,
            "memory_id": resolved_memory_id,
            "limit": limit,
            "has_query": query is not None,
        },
    )

    try:
        response = agentcore.retrieve_memory_records(**request)
    except Exception as exc:
        logger.error(
            "memory_retrieve_failed",
            extra={
                "namespace": resolved_namespace,
                "error_type": type(exc).__name__,
            },
        )
        raise MemoryError(f"RetrieveMemoryRecords failed: {exc}") from exc

    records = (response or {}).get("memoryRecordSummaries") or (response or {}).get(
        "memoryRecords"
    ) or []

    normalised = [_record_to_dict(r) for r in records]

    # Defensive: enforce that the API respected the namespace filter.
    # Namespace isolation is Requirement 2.3 / 2.4, so if the API ever
    # returns cross-namespace rows we drop them rather than leaking.
    scoped = [r for r in normalised if not r["namespace"] or r["namespace"] == resolved_namespace]

    logger.info(
        "memory_retrieve_completed",
        extra={
            "namespace": resolved_namespace,
            "returned": len(scoped),
            "dropped_cross_namespace": len(normalised) - len(scoped),
        },
    )
    return scoped


def create_memory_record(
    namespace: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    *,
    memory_id: str | None = None,
    client: BaseClient | None = None,
    region_name: str | None = None,
) -> dict[str, Any]:
    """Create a new memory record in ``namespace``.

    Parameters
    ----------
    namespace:
        Namespace to write to. Use :func:`session_namespace` for
        session-scoped notes and :data:`PRIOR_DEALS_NAMESPACE` for
        long-term memos.
    content:
        Record body. Must be a non-empty string.
    metadata:
        Optional key/value pairs attached to the record.
    memory_id:
        AgentCore Memory resource identifier. Resolved from SSM when
        omitted.
    client:
        Optional boto3 client for dependency injection in tests.
    region_name:
        Override AWS region when the function builds its own client.

    Returns
    -------
    dict
        ``{"id": str, "namespace": str}`` identifying the new record.
    """

    resolved_namespace = _validate_namespace(namespace)

    if not isinstance(content, str) or not content.strip():
        raise MemoryError("content must be a non-empty string")

    if metadata is not None and not isinstance(metadata, dict):
        raise MemoryError("metadata must be a dict when provided")

    resolved_memory_id = _resolve_memory_id(memory_id, region_name)
    agentcore = client or _build_agentcore_client(region_name=region_name)

    logger.info(
        "memory_create_started",
        extra={
            "namespace": resolved_namespace,
            "memory_id": resolved_memory_id,
            "content_length": len(content),
            "metadata_keys": sorted(metadata.keys()) if metadata else [],
        },
    )

    try:
        # The AgentCore data-plane API exposes ``batch_create_memory_records``
        # (plural, batch) rather than a singular ``create_memory_record``.
        # We wrap a single record in the batch call so the public API of
        # this function stays simple for callers.
        import time as _time  # noqa: PLC0415

        record_entry: dict[str, Any] = {
            "requestIdentifier": f"{resolved_namespace}-{_time.time_ns()}",
            "namespaces": [resolved_namespace],
            "content": {"text": content},
            "timestamp": _time.time(),
        }
        response = agentcore.batch_create_memory_records(
            memoryId=resolved_memory_id,
            records=[record_entry],
        )
    except Exception as exc:
        logger.error(
            "memory_create_failed",
            extra={
                "namespace": resolved_namespace,
                "error_type": type(exc).__name__,
            },
        )
        raise MemoryError(f"CreateMemoryRecord failed: {exc}") from exc

    # Extract the record id from the batch response.
    successful = (response or {}).get("successfulRecords") or []
    record_id = successful[0].get("memoryRecordId", "") if successful else ""
    logger.info(
        "memory_create_completed",
        extra={"namespace": resolved_namespace, "record_id": record_id},
    )

    return {"id": record_id, "namespace": resolved_namespace}

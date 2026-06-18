"""Request-scoped citation accumulator.

Each specialist's ``kb_retrieve`` tool appends its returned citations here
during an invocation. The supervisor handler:

1. Calls :func:`clear` before dispatching to any specialist.
2. Calls :func:`get` after streaming completes to include structured
   citations in the final metadata event.

Thread safety is not required — AgentCore Runtime runs one handler
invocation per container process at a time (single-worker model).

Example
-------
::

    # In specialist kb_retrieve tool:
    citations = kb_retrieve_fn(query, top_k=top_k)
    citation_collector.record(citations)
    return [c.to_dict() for c in citations]

    # In supervisor handler:
    citation_collector.clear()
    async for chunk in agent.stream_async(prompt):
        yield chunk
    yield {"citations": citation_collector.get(), "event": "metadata"}
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mna.types import Citation

# Module-level list — one per request (cleared at the top of each handler call).
_citations: list[Citation] = []


def record(citations: list[Citation]) -> None:
    """Append *citations* to the current request's accumulator.

    Safe to call with an empty list (no-op).
    """
    _citations.extend(citations)


def clear() -> None:
    """Reset the accumulator for a new request."""
    _citations.clear()


def get() -> list[Citation]:
    """Return a shallow copy of the accumulated citations.

    Returns a copy so callers cannot accidentally mutate the internal list.
    """
    return list(_citations)


__all__ = ["clear", "get", "record"]

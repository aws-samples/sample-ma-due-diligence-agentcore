"""Strategic Fit specialist agent.

Role
----
Evaluates how well a proposed target aligns with the acquirer's
strategic thesis and historical integration track record. Compares the
target against prior-deal memos stored in AgentCore long-term memory
(the ``prior_deals`` namespace) and produces a ranked list of
integration risks.

Tools
-----
- ``kb_retrieve``: Fetches target-company documents from the Bedrock
  Knowledge Base to characterize the target's profile (customer mix,
  geography, service lines). See :mod:`mna.tools.kb_retrieve`.
- ``retrieve_memory`` (``prior_deals`` namespace): Reads prior-deal
  memos — rationale, integration plan, synergy outcome, lessons
  learned — from AgentCore Memory. See :mod:`mna.tools.memory`.

Outputs
-------
- Thesis alignment table with 3–5 dimension scores.
- Prior-deal comparison paragraphs.
- Top-three integration risks list with mitigations, each grounded in
  both a prior-deal memo and a target document.

Example prompt
--------------
"Compare Acme Logistics' integration profile against our three most
recent completed acquisitions. Identify the top three integration
risks and cite the source memos."

Requirements covered: 1.1, 1.2, 1.3, 1.5, 1.6, 2.4, 16.3.
"""

from __future__ import annotations

from typing import Any

from mna.agents._base import (
    DEFAULT_SPECIALIST_MODEL,
    Agent,
    BedrockModel,
    load_prompt,
    tool,
)
from mna.logging_config import get_logger
from mna.tools.kb_retrieve import retrieve as kb_retrieve_fn
from mna.tools.memory import (
    PRIOR_DEALS_NAMESPACE,
)
from mna.tools.memory import (
    retrieve_memory as retrieve_memory_fn,
)

logger = get_logger(__name__)

AGENT_NAME = "strategic_fit"
SYSTEM_PROMPT = load_prompt(AGENT_NAME)


@tool
def kb_retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Fetch target-document passages from the Bedrock KB.

    Wraps :func:`mna.tools.kb_retrieve.retrieve`. Returns citation
    dicts (``{text, source, page, score}``).
    """

    citations = kb_retrieve_fn(query, top_k=top_k)
    return [c.to_dict() for c in citations]


@tool
def retrieve_memory(query: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Read prior-deal memos from the ``prior_deals`` namespace.

    Namespace is pinned to :data:`mna.tools.memory.PRIOR_DEALS_NAMESPACE`
    so the agent never reads from another session's memory by accident.
    """

    return retrieve_memory_fn(PRIOR_DEALS_NAMESPACE, query, limit=limit)


TOOLS: list[Any] = [kb_retrieve, retrieve_memory]

agent = Agent(
    model=BedrockModel(model_id=DEFAULT_SPECIALIST_MODEL),
    system_prompt=SYSTEM_PROMPT,
    tools=TOOLS,
)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """AgentCore Runtime entrypoint for the Strategic Fit specialist."""

    prompt = (event or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        logger.warning(
            "strategic_fit_missing_prompt",
            extra={"event_keys": list((event or {}).keys())},
        )
        return {"text": "", "error": "prompt is required"}

    session_id = (event or {}).get("session_id")
    logger.info(
        "strategic_fit_invoked",
        extra={"session_id": session_id, "prompt_length": len(prompt)},
    )
    result = agent(prompt)
    return {"text": str(result)}


__all__ = [
    "AGENT_NAME",
    "SYSTEM_PROMPT",
    "TOOLS",
    "agent",
    "handler",
    "kb_retrieve",
    "retrieve_memory",
]

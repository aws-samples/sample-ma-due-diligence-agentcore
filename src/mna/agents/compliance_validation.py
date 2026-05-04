"""Compliance Validation specialist agent.

Role
----
Audits prior specialist responses in the session against the firm's
M&A governance checklist and runs the citation-check evaluator to
confirm every factual claim is backed by at least one source citation.
Produces a structured compliance report with a pass / fail verdict and
a remediation list when gaps are found.

Tools
-----
- ``kb_retrieve``: Fetches the M&A governance checklist and supporting
  policy passages from the Bedrock Knowledge Base (typically under
  ``governance/``). See :mod:`mna.tools.kb_retrieve`.
- ``citation_check`` (``check_citations_via_lambda``): Invokes the
  citation-check evaluator Lambda with the response under review and
  its citation list. Returns ``{passed, unsupported_claims,
  total_claims}``. See :mod:`mna.tools.citation_check`.

Outputs
-------
- Compliance checklist table (item / status / evidence / source).
- Citation-check summary with the evaluator's per-claim detail.
- Overall verdict: PASS, PASS_WITH_NOTES, or FAIL, plus a remediation
  list when the verdict is not PASS.

Example prompt
--------------
"Review the Acme Logistics analysis in this session for completeness
against our M&A governance checklist. List any claims without source
citations."

Requirements covered: 1.1, 1.2, 1.3, 1.5, 1.6, 4.2, 16.3.
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
from mna.tools.citation_check import check_citations_via_lambda as citation_check_fn
from mna.tools.kb_retrieve import retrieve as kb_retrieve_fn

logger = get_logger(__name__)

AGENT_NAME = "compliance_validation"
SYSTEM_PROMPT = load_prompt(AGENT_NAME)


@tool
def kb_retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Fetch governance-checklist passages from the Bedrock KB.

    Wraps :func:`mna.tools.kb_retrieve.retrieve`. Returns citation
    dicts (``{text, source, page, score}``).
    """

    citations = kb_retrieve_fn(query, top_k=top_k)
    return [c.to_dict() for c in citations]


@tool
def citation_check(
    response_text: str,
    citations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Invoke the citation-check evaluator Lambda.

    Wraps :func:`mna.tools.citation_check.check_citations_via_lambda`.
    Returns the :class:`~mna.types.EvaluationResult`-shaped dict.
    """

    return citation_check_fn(response_text, citations)


TOOLS: list[Any] = [kb_retrieve, citation_check]

agent = Agent(
    model=BedrockModel(model_id=DEFAULT_SPECIALIST_MODEL),
    system_prompt=SYSTEM_PROMPT,
    tools=TOOLS,
)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """AgentCore Runtime entrypoint for the Compliance Validation specialist."""

    prompt = (event or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        logger.warning(
            "compliance_validation_missing_prompt",
            extra={"event_keys": list((event or {}).keys())},
        )
        return {"text": "", "error": "prompt is required"}

    session_id = (event or {}).get("session_id")
    logger.info(
        "compliance_validation_invoked",
        extra={"session_id": session_id, "prompt_length": len(prompt)},
    )
    result = agent(prompt)
    return {"text": str(result)}


__all__ = [
    "AGENT_NAME",
    "SYSTEM_PROMPT",
    "TOOLS",
    "agent",
    "citation_check",
    "handler",
    "kb_retrieve",
]

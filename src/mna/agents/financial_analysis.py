"""Financial Analysis specialist agent.

Role
----
Produces a defensible valuation view on a named target by combining
document-grounded figures (CIM, financial statements, press releases)
with synthetic comparable-company multiples. Performs discounted cash
flow and comparable-company analysis side by side and flags management
projections that diverge materially from historical performance.

Tools
-----
- ``kb_retrieve``: Fetches CIM passages, financial statement
  summaries, and press coverage from Amazon Bedrock Knowledge Bases. This
  is the primary source for historical financials and management
  projections. See :mod:`mna.tools.kb_retrieve`.
- ``market_data``: Calls the AgentCore Gateway-hosted market-data
  tool for synthetic comparable multiples (EV/EBITDA and EV/Revenue
  medians plus p25/p75 bands) by industry code and deal-size band.
  See :mod:`mna.tools.market_data`.

Outputs
-------
- Comparable-company EV band using p25/p75 spread.
- DCF with explicit assumption table.
- Stretch-assumption flags where management projections exceed the
  trailing three-year historical benchmark by more than 20%.

Example prompt
--------------
"Run a DCF on Example Corp using the CIM in the knowledge base.
Flag any management projection that diverges from historical
performance by more than 20%, and pull comparable multiples for
transportation-logistics mid-market."

Requirements covered: 1.1, 1.2, 1.3, 1.5, 1.6, 3.3, 16.3.
"""

from __future__ import annotations

from typing import Any

from mna.agents import citation_collector
from mna.agents._base import (
    DEFAULT_SPECIALIST_MAX_TOKENS,
    DEFAULT_SPECIALIST_MODEL,
    Agent,
    BedrockModel,
    build_bedrock_client_config,
    load_prompt,
    tool,
)
from mna.logging_config import get_logger
from mna.tools.kb_retrieve import retrieve as kb_retrieve_fn
from mna.tools.market_data import get_comparable_multiples as market_data_fn

logger = get_logger(__name__)

AGENT_NAME = "financial_analysis"
SYSTEM_PROMPT = load_prompt(AGENT_NAME)


@tool
def kb_retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Fetch CIM / financials / press passages from the Bedrock KB.

    Wraps :func:`mna.tools.kb_retrieve.retrieve`. Returns citation
    dicts (``{text, source, page, score}``).
    """

    citations = kb_retrieve_fn(query, top_k=top_k)
    citation_collector.record(citations)
    return [c.to_dict() for c in citations]


@tool
def market_data(industry_code: str, deal_size_band: str) -> dict[str, Any]:
    """Fetch synthetic comparable multiples for an industry / size band.

    Wraps :func:`mna.tools.market_data.get_comparable_multiples`. The
    response is clearly labeled synthetic.
    """

    return market_data_fn(industry_code, deal_size_band)


TOOLS: list[Any] = [kb_retrieve, market_data]

agent = Agent(
    model=BedrockModel(
        model_id=DEFAULT_SPECIALIST_MODEL,
        max_tokens=DEFAULT_SPECIALIST_MAX_TOKENS,
        boto_client_config=build_bedrock_client_config(),
    ),
    system_prompt=SYSTEM_PROMPT,
    tools=TOOLS,
    name=AGENT_NAME,
)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """AgentCore Runtime entrypoint for the Financial Analysis specialist."""

    prompt = (event or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        logger.warning(
            "financial_analysis_missing_prompt",
            extra={"event_keys": list((event or {}).keys())},
        )
        return {"text": "", "error": "prompt is required"}

    session_id = (event or {}).get("session_id")
    logger.info(
        "financial_analysis_invoked",
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
    "market_data",
]

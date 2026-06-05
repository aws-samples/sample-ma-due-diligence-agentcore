"""Target Screening specialist agent.

Role
----
Surfaces candidate transportation and logistics acquisition targets
from the structured target-company database (AWS Aurora PostgreSQL
Serverless v2) and enriches the top hits with narrative context from
Knowledge Bases for Amazon Bedrock. This specialist is the primary reader of
the ``mna.target_companies`` table.

Tools
-----
- ``text_to_sql`` (primary): Translates the user's natural-language
  screening criteria into a single PostgreSQL ``SELECT`` statement
  and executes it via the RDS Data API.
  See :mod:`mna.tools.text_to_sql`.
- ``kb_retrieve`` (enrichment): Fetches grounding passages from the
  Knowledge Bases for Amazon Bedrock to add qualitative context to the top
  screening hits.
  See :mod:`mna.tools.kb_retrieve`.

Safety
------
The text-to-SQL tool is ``SELECT``-only; the system prompt reinforces
the rule so the agent never asks for a mutating or DDL statement.

Example prompt
--------------
"Screen our target pipeline for transportation companies with revenue
between $100M and $500M, EBITDA margin above 12%, and fleet size above
200. Surface the top three and tell me what the CIM says about the
leader's growth trajectory."

Requirements covered: 1.1, 1.2, 1.3, 1.5, 1.6, 16.3.
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
from mna.tools.text_to_sql import query as text_to_sql_fn

logger = get_logger(__name__)

AGENT_NAME = "target_screening"
SYSTEM_PROMPT = load_prompt(AGENT_NAME)


@tool
def text_to_sql(natural_language: str) -> dict[str, Any]:
    """Translate natural language to SQL, run on Aurora, return rows.

    Wraps :func:`mna.tools.text_to_sql.query`. The tool returns a dict
    with ``sql``, ``rows``, ``row_count``, and ``columns`` keys.
    """

    return text_to_sql_fn(natural_language)


@tool
def kb_retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """Fetch grounding passages from Knowledge Bases for Amazon Bedrock.

    Wraps :func:`mna.tools.kb_retrieve.retrieve`. Returns a list of
    citation dicts (``{text, source, page, score}``) suitable for
    inline rendering.
    """

    citations = kb_retrieve_fn(query, top_k=top_k)
    return [c.to_dict() for c in citations]


#: Ordered list of tools exposed to the agent. Kept as a module-level
#: attribute so the unit tests can assert the wiring without
#: instantiating the Strands runtime.
TOOLS: list[Any] = [text_to_sql, kb_retrieve]

agent = Agent(
    model=BedrockModel(model_id=DEFAULT_SPECIALIST_MODEL),
    system_prompt=SYSTEM_PROMPT,
    tools=TOOLS,
)


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """AgentCore Runtime entrypoint for the Target Screening specialist.

    Accepts the standard runtime event shape
    (``{"prompt": str, "session_id": str}``) and returns
    ``{"text": str}``. Missing prompts produce a structured error
    payload rather than raising so the runtime still receives a
    well-formed response.
    """

    prompt = (event or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        logger.warning("target_screening_missing_prompt", extra={"event_keys": list((event or {}).keys())})
        return {"text": "", "error": "prompt is required"}

    session_id = (event or {}).get("session_id")
    logger.info(
        "target_screening_invoked",
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
    "text_to_sql",
]

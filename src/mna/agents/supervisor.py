"""Supervisor agent and AgentCore Runtime container entrypoint.

Role
----
Orchestrates four specialist agents (Target Screening, Financial
Analysis, Strategic Fit, Compliance Validation) using the Strands
"agents-as-tools" pattern. The supervisor parses the user prompt,
routes to one or more specialists, and assembles a single cited
response for the analyst.

Tools
-----
One per specialist, wired via the Strands ``use_agent`` helper (or a
lightweight equivalent when Strands' ``strands_tools`` package is
unavailable). Each specialist's module-level :data:`agent` is passed
through unchanged so the tool invocation produces the same behavior
as calling the specialist directly.

Model
-----
Pinned to Claude Sonnet 4.5 by default
(``anthropic.claude-sonnet-4-5-v1:0``). Override with the
``MNA_SUPERVISOR_MODEL`` environment variable.

Guardrail
---------
When ``MNA_GUARDRAIL_ID`` is set, the supervisor attaches the
specified Bedrock Guardrail to its model. The Guardrail is configured
with harmful-content filters and a financial-advice denial topic per
design §Safety Design.

Runtime hosting
---------------
The module is decorated with ``@BedrockAgentCoreApp`` so the built
container image exposes the handler on the AgentCore Runtime
invocation contract. Container entrypoint:
``python -m mna.agents.supervisor``.

Example prompt
--------------
"For Acme Logistics, run a DCF using the CIM, flag any stretch
assumptions, compare the integration risk against our three most
recent deals, and confirm the whole answer is backed by citations."

Requirements covered: 1.1, 1.5, 4.1, 12.1, 12.2.
"""

from __future__ import annotations

import os
from typing import Any

from mna.agents import (
    compliance_validation,
    financial_analysis,
    strategic_fit,
    target_screening,
)
from mna.agents._base import (
    AGENTCORE_APP_AVAILABLE,
    DEFAULT_SUPERVISOR_MODEL,
    STRANDS_AVAILABLE,
    Agent,
    BedrockAgentCoreApp,
    BedrockModel,
    load_prompt,
)
from mna.logging_config import get_logger

logger = get_logger(__name__)

AGENT_NAME = "supervisor"
SYSTEM_PROMPT = load_prompt(AGENT_NAME)

#: Mapping from specialist name to its module-level agent instance.
#: Kept ordered to match the design-doc invocation table.
SPECIALISTS: dict[str, Any] = {
    target_screening.AGENT_NAME: target_screening.agent,
    financial_analysis.AGENT_NAME: financial_analysis.agent,
    strategic_fit.AGENT_NAME: strategic_fit.agent,
    compliance_validation.AGENT_NAME: compliance_validation.agent,
}


# ---------------------------------------------------------------------------
# use_agent helper — wrap each specialist as a Strands tool.
# ---------------------------------------------------------------------------

try:
    # ``use_agent`` ships with ``strands_tools`` in some Strands SDK
    # releases; import defensively so a missing install falls back to
    # the local shim rather than breaking module import.
    from strands_tools import use_agent as _use_agent  # type: ignore[import-not-found]

    def _wrap_specialist(specialist_agent: Any, *, name: str) -> Any:
        return _use_agent(specialist_agent, name=name)

    USE_AGENT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when the helper is absent
    USE_AGENT_AVAILABLE = False

    # ``@tool`` is the Strands decorator that turns a plain callable
    # into a registered tool (with a name, description, and input
    # schema derived from the signature). Without it the Agent
    # rejects the callable at registration time with
    # ``unrecognized tool specification``.
    try:
        from strands import tool as _strands_tool  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - Strands stub path
        def _strands_tool(fn: Any) -> Any:
            return fn

    def _wrap_specialist(specialist_agent: Any, *, name: str) -> Any:
        """Fallback ``use_agent`` shim.

        Returns a Strands-registered tool that delegates to the
        specialist's ``agent`` instance. When Strands is available the
        specialist instance is a real :class:`strands.Agent` and will
        respond to ``__call__``; when Strands is absent the call
        raises clearly (per :class:`mna.agents._base._StubAgent`).
        """

        def _tool(prompt: str) -> str:
            """Invoke a specialist agent with a natural-language prompt.

            Parameters
            ----------
            prompt:
                The natural-language task for the specialist to
                complete. Pass the full user ask; the specialist
                decides which of its tools to call.
            """

            return str(specialist_agent(prompt))

        _tool.__name__ = name
        _tool.__doc__ = (
            f"Invoke the {name} specialist agent with a natural-language "
            "prompt. Returns the specialist's response text."
        )
        return _strands_tool(_tool)


#: Guardrail ID pulled from the environment at module load. Kept as a
#: module-level attribute so tests can assert that ``MNA_GUARDRAIL_ID``
#: flows into the :class:`BedrockModel` constructor.
GUARDRAIL_ID: str | None = os.getenv("MNA_GUARDRAIL_ID") or None


def _build_supervisor_model() -> Any:
    """Construct the :class:`BedrockModel` used by the supervisor.

    The Guardrail ID is attached only when set so a local dev run
    without Guardrails still produces a working (if ungated) model.
    """

    kwargs: dict[str, Any] = {"model_id": DEFAULT_SUPERVISOR_MODEL}
    if GUARDRAIL_ID:
        # Strands' ``BedrockModel`` accepts ``guardrail_id``; keep the
        # keyword name in sync with the SDK. The stub BedrockModel
        # accepts arbitrary kwargs so this is safe when the SDK is
        # absent.
        kwargs["guardrail_id"] = GUARDRAIL_ID
    return BedrockModel(**kwargs)


TOOLS: list[Any] = [
    _wrap_specialist(specialist, name=name) for name, specialist in SPECIALISTS.items()
]

agent = Agent(
    model=_build_supervisor_model(),
    system_prompt=SYSTEM_PROMPT,
    tools=TOOLS,
)


# ---------------------------------------------------------------------------
# AgentCore Memory session manager — wired per-request so Strands
# automatically persists and retrieves conversation turns.
# ---------------------------------------------------------------------------


def _build_session_manager(session_id: str | None) -> Any:
    """Return an ``AgentCoreMemorySessionManager`` or ``None``.

    Returns ``None`` when the Memory ID is unavailable (local dev,
    unit tests) so the agent still runs — just without cross-turn
    persistence. The ``bedrock_agentcore.memory`` import is lazy
    because the package may not be installed in every environment
    (e.g. the reader's local venv without the agent container deps).
    """

    memory_id = os.getenv("MNA_MEMORY_ID")
    if not memory_id or not session_id:
        return None

    try:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )
    except ImportError:
        logger.debug("bedrock_agentcore.memory not available; session manager disabled")
        return None

    try:
        config = AgentCoreMemoryConfig(
            memory_id=memory_id,
            session_id=session_id,
            actor_id="mna-supervisor",
        )
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        return AgentCoreMemorySessionManager(
            agentcore_memory_config=config,
            region_name=region,
        )
    except Exception:
        logger.warning("session_manager_build_failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# AgentCore Runtime entrypoint.
# ---------------------------------------------------------------------------

app = BedrockAgentCoreApp()


@app.entrypoint
def handler(event: dict[str, Any], _context: Any | None = None) -> dict[str, Any]:
    """AgentCore Runtime entrypoint for the supervisor.

    Accepts the runtime event shape
    ``{"prompt": str, "session_id": str | None, "agent_name": str | None}``.

    Dispatch rules:

    * When ``agent_name`` is absent or ``"supervisor"`` the supervisor
      :data:`agent` runs and routes to specialists via the agents-as-
      tools pattern.
    * When ``agent_name`` is one of the specialist names the supervisor
      skips its own LLM hop and calls the specialist directly. This is
      how ``mna.client.invoke_agent("target_screening", ...)`` targets
      a single specialist without deploying a separate AgentCore
      endpoint per agent.

    A missing or unknown prompt produces a structured error payload so
    the runtime still receives a well-formed response.

    Any exception that escapes the dispatch block is logged with a
    full traceback before being re-raised. Without the explicit log
    call the traceback stays inside the Python process and never
    reaches CloudWatch — AgentCore Runtime's ``APPLICATION_LOGS``
    delivery captures stdout/stderr, and an uncaught exception that
    bypasses the logger surfaces only as a 500 on the client side.
    """

    try:
        prompt = (event or {}).get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            logger.warning(
                "supervisor_missing_prompt",
                extra={"event_keys": list((event or {}).keys())},
            )
            return {"text": "", "error": "prompt is required"}

        session_id = (event or {}).get("session_id")
        requested_agent = (event or {}).get("agent_name")
        if not isinstance(requested_agent, str) or not requested_agent:
            requested_agent = AGENT_NAME

        # Attach an AgentCore Memory session manager so Strands
        # automatically persists each turn and retrieves prior turns
        # on the same session_id. This is what makes the Compliance
        # Validation prompt ("Review the analysis in this session")
        # work — without it, each invocation starts with a blank
        # conversation history.
        sm = _build_session_manager(session_id)

        logger.info(
            "supervisor_invoked",
            extra={
                "session_id": session_id,
                "prompt_length": len(prompt),
                "guardrail_id_set": bool(GUARDRAIL_ID),
                "specialists": list(SPECIALISTS.keys()),
                "requested_agent": requested_agent,
                "session_manager_active": sm is not None,
            },
        )

        if requested_agent == AGENT_NAME:
            agent.session_manager = sm
            result = agent(prompt)
        elif requested_agent in SPECIALISTS:
            target_agent = SPECIALISTS[requested_agent]
            target_agent.session_manager = sm
            result = target_agent(prompt)
        else:
            logger.warning(
                "supervisor_unknown_agent_name",
                extra={"requested_agent": requested_agent},
            )
            return {
                "text": "",
                "error": (
                    f"Unknown agent_name {requested_agent!r}. Valid values: "
                    f"{[AGENT_NAME, *SPECIALISTS.keys()]}."
                ),
            }
        return {"text": str(result)}
    except Exception:
        # Log the full traceback before re-raising so CloudWatch's
        # ``APPLICATION_LOGS`` delivery captures a complete stack
        # trace. Otherwise the exception surfaces only as a 500 at
        # the client with no way to see what broke inside the
        # container.
        logger.exception(
            "supervisor_handler_unhandled_exception",
            extra={
                "event_keys": list((event or {}).keys())
                if isinstance(event, dict)
                else [],
            },
        )
        raise


def _main() -> None:
    """Start the AgentCore Runtime server loop.

    Exposed separately from ``handler`` so tests can patch it without
    triggering a network bind. The container's ``CMD`` resolves to
    ``python -m mna.agents.supervisor`` which invokes this function
    via the ``__main__`` block below.
    """

    if not AGENTCORE_APP_AVAILABLE or not STRANDS_AVAILABLE:
        # Print a clear actionable message when the SDKs aren't
        # installed. The dev environment does not bundle Strands or
        # bedrock-agentcore — those land via the Lambda-built container
        # image instead. See infra/agent_image/Dockerfile.
        missing = []
        if not STRANDS_AVAILABLE:
            missing.append("strands-agents")
        if not AGENTCORE_APP_AVAILABLE:
            missing.append("bedrock-agentcore")
        print(
            "Cannot start supervisor runtime: the following packages are not "
            f"installed: {', '.join(missing)}. The supervisor is intended to "
            "run inside the AgentCore Runtime container built by the CDK "
            "stacks, which pins these dependencies in requirements.txt.",
        )
        return
    app.run()


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    _main()


__all__ = [
    "AGENT_NAME",
    "GUARDRAIL_ID",
    "SPECIALISTS",
    "SYSTEM_PROMPT",
    "TOOLS",
    "USE_AGENT_AVAILABLE",
    "agent",
    "app",
    "handler",
]

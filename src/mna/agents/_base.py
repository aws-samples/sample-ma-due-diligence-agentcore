"""Shared helpers for the Strands-based agent modules.

Provides:

* :func:`load_prompt` — read a system prompt from ``agents/prompts/``.
* :data:`DEFAULT_SPECIALIST_MODEL` — the pinned specialist model id,
  overridable via ``MNA_SPECIALIST_MODEL``.
* :data:`DEFAULT_SUPERVISOR_MODEL` — the pinned supervisor model id,
  overridable via ``MNA_SUPERVISOR_MODEL``.
* :data:`STRANDS_AVAILABLE` — truthy when the Strands SDK is importable.
* :data:`Agent`, :data:`BedrockModel`, :data:`tool` — re-exports from
  Strands when available, otherwise lightweight stand-ins that let the
  module import and the tool list be introspected without the SDK
  installed. The stubs raise on ``__call__`` so accidental production
  use without Strands fails loudly.

The module is deliberately side-effect free: importing it never
constructs an :class:`Agent` instance and never reads AWS credentials.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mna.logging_config import get_logger

logger = get_logger(__name__)

#: Directory holding the ``.txt`` system prompts for every agent.
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: Default specialist model. Uses the US cross-region inference
#: profile for Claude Haiku 4.5 -- Anthropic's current small,
#: fast, cheap model. The original Haiku 3.5 default was flagged
#: Legacy on accounts that hadn't invoked it in 30 days, causing
#: Bedrock to return ``ResourceNotFoundException``. Haiku 4.5 is
#: Active and has a meaningfully better tool-use success rate.
#: Overridable via ``MNA_SPECIALIST_MODEL``.
DEFAULT_SPECIALIST_MODEL = os.getenv(
    "MNA_SPECIALIST_MODEL",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)

#: Default supervisor model. Claude Sonnet 4.5 via the US
#: cross-region inference profile. The version-dated identifier
#: (``...-20250929-v1:0``) is the one Bedrock actually accepts;
#: the un-dated ``us.anthropic.claude-sonnet-4-5-v1:0`` is rejected
#: with ``ValidationException: The provided model identifier is
#: invalid``. Overridable via ``MNA_SUPERVISOR_MODEL``.
DEFAULT_SUPERVISOR_MODEL = os.getenv(
    "MNA_SUPERVISOR_MODEL",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
)


def load_prompt(name: str) -> str:
    """Read the system prompt file named ``<name>.txt`` from ``prompts/``.

    Raises :class:`FileNotFoundError` with a clear message if the file
    is missing. The agent modules call this at module scope, so a
    missing prompt surfaces at import time rather than at invocation.
    """

    if not name or not isinstance(name, str):
        raise ValueError("prompt name must be a non-empty string")
    path = PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"Agent prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# Strands SDK import shim.
#
# The Strands SDK is not always installed in the environment where the
# unit tests or CI linting run (it is only pinned in the agent
# container image's ``requirements.txt``). Import lazily with a graceful
# fallback so ``import mna.agents.target_screening`` works in either
# environment.
# ---------------------------------------------------------------------------

try:
    from strands import Agent as _StrandsAgent  # type: ignore[import-not-found]
    from strands import tool as _strands_tool  # type: ignore[import-not-found]
    from strands.models import (  # type: ignore[import-not-found]
        BedrockModel as _StrandsBedrockModel,
    )

    Agent: Any = _StrandsAgent
    BedrockModel: Any = _StrandsBedrockModel
    tool: Any = _strands_tool
    STRANDS_AVAILABLE: bool = True
except ImportError:  # pragma: no cover - exercised only when SDK is absent
    logger.warning(
        "strands_sdk_unavailable",
        extra={
            "detail": (
                "strands-agents is not installed; falling back to stub Agent/"
                "BedrockModel/tool so the agent modules remain importable."
            )
        },
    )

    class _StubAgent:
        """Minimal stand-in for :class:`strands.Agent`.

        Stores the constructor arguments so tests can introspect the
        tool wiring, but raises on ``__call__`` to prevent accidental
        use without the SDK installed.
        """

        def __init__(
            self,
            *,
            model: Any | None = None,
            system_prompt: str | None = None,
            tools: list[Any] | None = None,
            name: str | None = None,
            **kwargs: Any,
        ) -> None:
            self.model = model
            self.system_prompt = system_prompt
            self.tools = list(tools or [])
            self.name = name
            self.extra = kwargs

        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(
                "Strands SDK is not installed; Agent instances are stubs only. "
                "Install strands-agents to invoke the agent."
            )

        async def stream_async(self, *_args: Any, **_kwargs: Any) -> Any:
            """Async generator stub — yields nothing, mirrors Strands Agent API."""
            raise RuntimeError(
                "Strands SDK is not installed; Agent instances are stubs only. "
                "Install strands-agents to invoke the agent."
            )
            yield  # noqa: RET503 - makes this an async generator

        def __repr__(self) -> str:  # pragma: no cover - trivial
            tool_names = [getattr(t, "__name__", repr(t)) for t in self.tools]
            return f"StubAgent(name={self.name!r}, tools={tool_names})"

    class _StubBedrockModel:
        """Minimal stand-in for :class:`strands.models.BedrockModel`."""

        def __init__(self, *, model_id: str, **kwargs: Any) -> None:
            self.model_id = model_id
            self.extra = kwargs

        def __repr__(self) -> str:  # pragma: no cover - trivial
            return f"StubBedrockModel(model_id={self.model_id!r})"

    def _stub_tool(func: Any = None, **_kwargs: Any) -> Any:
        """Stand-in for :func:`strands.tool` that leaves callables unchanged."""

        if func is None:
            return lambda f: f
        return func

    Agent = _StubAgent
    BedrockModel = _StubBedrockModel
    tool = _stub_tool
    STRANDS_AVAILABLE = False


# ---------------------------------------------------------------------------
# AgentCore Runtime hosting shim.
#
# The runtime container decorates the supervisor entrypoint with
# ``@BedrockAgentCoreApp``. That decorator ships in the
# ``bedrock-agentcore`` SDK which may not be available in every dev
# environment either, so fall back to an inert no-op class.
# ---------------------------------------------------------------------------

try:
    from bedrock_agentcore import (  # type: ignore[import-not-found]
        BedrockAgentCoreApp as _BedrockAgentCoreApp,
    )

    BedrockAgentCoreApp: Any = _BedrockAgentCoreApp
    AGENTCORE_APP_AVAILABLE: bool = True
except ImportError:  # pragma: no cover - exercised only when SDK is absent

    class _StubBedrockAgentCoreApp:
        """Stand-in for ``bedrock_agentcore.BedrockAgentCoreApp``.

        The real class is both a decorator and a runnable app object.
        The stub records registered handlers so tests can introspect
        them, and raises on ``run()`` to prevent accidental local use.
        """

        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.entrypoint_handler: Any | None = None

        def entrypoint(self, func: Any) -> Any:
            """Decorator capturing the entrypoint function."""

            self.entrypoint_handler = func
            return func

        def run(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError(
                "bedrock-agentcore SDK is not installed; "
                "BedrockAgentCoreApp.run() is unavailable."
            )

        def __repr__(self) -> str:  # pragma: no cover - trivial
            return f"StubBedrockAgentCoreApp(entrypoint={self.entrypoint_handler!r})"

    BedrockAgentCoreApp = _StubBedrockAgentCoreApp
    AGENTCORE_APP_AVAILABLE = False


__all__ = [
    "AGENTCORE_APP_AVAILABLE",
    "Agent",
    "BedrockAgentCoreApp",
    "BedrockModel",
    "DEFAULT_SPECIALIST_MODEL",
    "DEFAULT_SUPERVISOR_MODEL",
    "PROMPTS_DIR",
    "STRANDS_AVAILABLE",
    "load_prompt",
    "tool",
]

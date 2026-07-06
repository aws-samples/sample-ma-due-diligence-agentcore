"""Shared helpers for the Strands-based agent modules.

Provides ``load_prompt``, the default model/token/timeout settings, and
``Agent``/``BedrockModel``/``tool`` re-exports (with stub fallbacks when
Strands isn't installed). Side-effect free on import.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mna.logging_config import get_logger

logger = get_logger(__name__)

#: Directory holding the ``.txt`` system prompts for every agent.
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: Default model for every agent. Overridable via ``MNA_MODEL``.
DEFAULT_MODEL_ID = os.getenv("MNA_MODEL", "us.anthropic.claude-sonnet-4-6")

#: Aliases kept for readability at call sites; both resolve to DEFAULT_MODEL_ID.
DEFAULT_SPECIALIST_MODEL = DEFAULT_MODEL_ID
DEFAULT_SUPERVISOR_MODEL = DEFAULT_MODEL_ID

#: Max output tokens for specialist models. Overridable via ``MNA_SPECIALIST_MAX_TOKENS``.
DEFAULT_SPECIALIST_MAX_TOKENS = int(os.getenv("MNA_SPECIALIST_MAX_TOKENS", "16384"))

#: Max output tokens for the supervisor model. Overridable via ``MNA_SUPERVISOR_MAX_TOKENS``.
DEFAULT_SUPERVISOR_MAX_TOKENS = int(os.getenv("MNA_SUPERVISOR_MAX_TOKENS", "16384"))

#: bedrock-runtime read timeout (seconds). Overridable via ``MNA_BEDROCK_READ_TIMEOUT_SECONDS``.
DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS = int(
    os.getenv("MNA_BEDROCK_READ_TIMEOUT_SECONDS", "300")
)


def build_bedrock_client_config() -> Any:
    """Return a ``botocore.config.Config`` with a generous read timeout.

    Pass as ``BedrockModel(boto_client_config=...)`` so long Converse
    calls don't time out. Returns ``None`` if botocore isn't installed.
    """

    try:
        from botocore.config import Config as _BotocoreConfig
    except ImportError:  # pragma: no cover - exercised only when botocore is absent
        return None
    return _BotocoreConfig(read_timeout=DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS)


def load_prompt(name: str) -> str:
    """Read the system prompt file named ``<name>.txt`` from ``prompts/``."""

    if not name or not isinstance(name, str):
        raise ValueError("prompt name must be a non-empty string")
    path = PROMPTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"Agent prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


# Strands SDK import shim: falls back to stubs so agent modules stay
# importable when strands-agents isn't installed (e.g. outside the container).
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
        """Minimal stand-in for ``strands.Agent``; raises on ``__call__``."""

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
            """Async generator stub, mirrors the Strands Agent API."""
            raise RuntimeError(
                "Strands SDK is not installed; Agent instances are stubs only. "
                "Install strands-agents to invoke the agent."
            )
            yield  # noqa: RET503 - makes this an async generator

        def __repr__(self) -> str:  # pragma: no cover - trivial
            tool_names = [getattr(t, "__name__", repr(t)) for t in self.tools]
            return f"StubAgent(name={self.name!r}, tools={tool_names})"

    class _StubBedrockModel:
        """Minimal stand-in for ``strands.models.BedrockModel``."""

        def __init__(self, *, model_id: str, **kwargs: Any) -> None:
            self.model_id = model_id
            self.extra = kwargs

        def __repr__(self) -> str:  # pragma: no cover - trivial
            return f"StubBedrockModel(model_id={self.model_id!r})"

    def _stub_tool(func: Any = None, **_kwargs: Any) -> Any:
        """Stand-in for ``strands.tool`` that leaves callables unchanged."""

        if func is None:
            return lambda f: f
        return func

    Agent = _StubAgent
    BedrockModel = _StubBedrockModel
    tool = _stub_tool
    STRANDS_AVAILABLE = False


# AgentCore Runtime hosting shim: falls back to an inert no-op class when
# bedrock-agentcore isn't installed.
try:
    from bedrock_agentcore import (  # type: ignore[import-not-found]
        BedrockAgentCoreApp as _BedrockAgentCoreApp,
    )

    BedrockAgentCoreApp: Any = _BedrockAgentCoreApp
    AGENTCORE_APP_AVAILABLE: bool = True
except ImportError:  # pragma: no cover - exercised only when SDK is absent

    class _StubBedrockAgentCoreApp:
        """Stand-in for ``bedrock_agentcore.BedrockAgentCoreApp``; raises on ``run()``."""

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
    "DEFAULT_BEDROCK_READ_TIMEOUT_SECONDS",
    "DEFAULT_MODEL_ID",
    "DEFAULT_SPECIALIST_MAX_TOKENS",
    "DEFAULT_SPECIALIST_MODEL",
    "DEFAULT_SUPERVISOR_MAX_TOKENS",
    "DEFAULT_SUPERVISOR_MODEL",
    "PROMPTS_DIR",
    "STRANDS_AVAILABLE",
    "build_bedrock_client_config",
    "load_prompt",
    "tool",
]

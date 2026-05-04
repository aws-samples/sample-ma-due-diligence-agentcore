"""M&A Due Diligence Multi-Agent sample package.

Importing ``mna`` installs a structured JSON log formatter on the package
logger and exposes the shared type surface used by the notebook, the
CLI, and the agents.
"""

from __future__ import annotations

from mna.client import ClientError, get_last_trace, invoke_agent, list_agents
from mna.config import (
    ALL_PARAMETERS,
    PARAM_AURORA_CLUSTER_ARN,
    PARAM_DOCS_BUCKET,
    PARAM_EVALUATOR_ARN,
    PARAM_GATEWAY_ARN,
    PARAM_KB_ID,
    PARAM_RUNTIME_ARN,
    ConfigError,
    MnaConfig,
    clear_config_cache,
    get_parameter,
    get_parameters,
    load_config,
)
from mna.logging_config import JsonFormatter, configure_logging, get_logger
from mna.types import AgentResponse, Citation, EvaluationResult

__version__ = "0.1.0"

# Install JSON log formatting as a side-effect of ``import mna``. Idempotent.
configure_logging()

__all__ = [
    "ALL_PARAMETERS",
    "AgentResponse",
    "Citation",
    "ClientError",
    "ConfigError",
    "EvaluationResult",
    "JsonFormatter",
    "MnaConfig",
    "PARAM_AURORA_CLUSTER_ARN",
    "PARAM_DOCS_BUCKET",
    "PARAM_EVALUATOR_ARN",
    "PARAM_GATEWAY_ARN",
    "PARAM_KB_ID",
    "PARAM_RUNTIME_ARN",
    "__version__",
    "clear_config_cache",
    "configure_logging",
    "get_last_trace",
    "get_logger",
    "get_parameter",
    "get_parameters",
    "invoke_agent",
    "list_agents",
    "load_config",
]

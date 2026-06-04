"""Resource ARN/ID resolution for the M&A Due Diligence sample.

Every deployable artifact publishes its identifier to Systems Manager
Parameter Store under the ``/mna`` prefix. This module reads those
parameters so the notebook, the CLI, the agents, and the Lambda
handlers never hardcode an ARN.

Parameter names:

* ``/mna/runtime/arn``         -- AgentCore Runtime ARN
* ``/mna/kb/id``               -- Bedrock Knowledge Base ID
* ``/mna/docs/bucket``         -- Documents S3 bucket name
* ``/mna/aurora/cluster_arn``  -- Aurora cluster ARN
* ``/mna/aurora/secret_arn``   -- Aurora admin-credentials secret ARN
* ``/mna/gateway/arn``         -- AgentCore Gateway ARN
* ``/mna/evaluator/arn``       -- Citation-check Lambda ARN
* ``/mna/sessions/table``      -- DynamoDB table for per-turn audit rows
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from mna.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from botocore.client import BaseClient

logger = get_logger(__name__)

# Canonical SSM parameter names. Keep in sync with the CDK stacks.
PARAM_RUNTIME_ARN = "/mna/runtime/arn"
PARAM_KB_ID = "/mna/kb/id"
PARAM_DOCS_BUCKET = "/mna/docs/bucket"
PARAM_AURORA_CLUSTER_ARN = "/mna/aurora/cluster_arn"
PARAM_AURORA_SECRET_ARN = "/mna/aurora/secret_arn"  # noqa: S105 - SSM parameter path, not a credential
PARAM_GATEWAY_ARN = "/mna/gateway/arn"
PARAM_EVALUATOR_ARN = "/mna/evaluator/arn"
PARAM_SESSIONS_TABLE = "/mna/sessions/table"

#: All parameters resolved by :func:`load_config`, in stable iteration order.
ALL_PARAMETERS: tuple[str, ...] = (
    PARAM_RUNTIME_ARN,
    PARAM_KB_ID,
    PARAM_DOCS_BUCKET,
    PARAM_AURORA_CLUSTER_ARN,
    PARAM_AURORA_SECRET_ARN,
    PARAM_GATEWAY_ARN,
    PARAM_EVALUATOR_ARN,
    PARAM_SESSIONS_TABLE,
)


class ConfigError(RuntimeError):
    """Raised when a required SSM parameter is missing or empty."""


@dataclass(frozen=True)
class MnaConfig:
    """Resolved configuration for a deployed MNA environment."""

    runtime_arn: str
    kb_id: str
    docs_bucket: str
    aurora_cluster_arn: str
    aurora_secret_arn: str
    gateway_arn: str
    evaluator_arn: str
    sessions_table_name: str

    def as_dict(self) -> dict[str, str]:
        return {
            PARAM_RUNTIME_ARN: self.runtime_arn,
            PARAM_KB_ID: self.kb_id,
            PARAM_DOCS_BUCKET: self.docs_bucket,
            PARAM_AURORA_CLUSTER_ARN: self.aurora_cluster_arn,
            PARAM_AURORA_SECRET_ARN: self.aurora_secret_arn,
            PARAM_GATEWAY_ARN: self.gateway_arn,
            PARAM_EVALUATOR_ARN: self.evaluator_arn,
            PARAM_SESSIONS_TABLE: self.sessions_table_name,
        }


def _build_ssm_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 SSM client, deferring the import for cold-start safety."""

    import boto3  # Lazy import: never at module top level.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("ssm", **kwargs)


def get_parameter(
    name: str,
    *,
    ssm_client: BaseClient | None = None,
    region_name: str | None = None,
    with_decryption: bool = True,
) -> str:
    """Fetch a single SSM parameter value.

    Raises :class:`ConfigError` if the parameter is missing or empty. A
    caller-supplied ``ssm_client`` takes precedence over ``region_name``
    and enables dependency injection from tests.
    """

    client = ssm_client or _build_ssm_client(region_name=region_name)

    try:
        response = client.get_parameter(Name=name, WithDecryption=with_decryption)
    except Exception as exc:
        # Catch both ParameterNotFound and generic ClientError without
        # importing botocore at module scope.
        error_code = getattr(getattr(exc, "response", {}), "get", lambda *_: {})("Error") or {}
        if isinstance(error_code, dict) and error_code.get("Code") == "ParameterNotFound":
            logger.error(
                "ssm_parameter_not_found",
                extra={"parameter": name, "error_type": type(exc).__name__},
            )
            raise ConfigError(f"SSM parameter not found: {name}") from exc
        logger.error(
            "ssm_parameter_fetch_failed",
            extra={"parameter": name, "error_type": type(exc).__name__},
        )
        raise ConfigError(f"Failed to fetch SSM parameter {name}: {exc}") from exc

    value = ((response or {}).get("Parameter") or {}).get("Value")
    if not value:
        raise ConfigError(f"SSM parameter {name} is empty")

    logger.debug("ssm_parameter_resolved", extra={"parameter": name})
    return value


def get_parameters(
    names: list[str] | tuple[str, ...],
    *,
    ssm_client: BaseClient | None = None,
    region_name: str | None = None,
    with_decryption: bool = True,
) -> dict[str, str]:
    """Fetch multiple SSM parameters in a single ``GetParameters`` call.

    Returns a mapping of parameter name to value. Raises
    :class:`ConfigError` listing every parameter that was missing or
    empty so callers can diagnose a partially deployed environment in
    one shot.
    """

    if not names:
        return {}

    client = ssm_client or _build_ssm_client(region_name=region_name)

    try:
        response = client.get_parameters(Names=list(names), WithDecryption=with_decryption)
    except Exception as exc:
        logger.error(
            "ssm_get_parameters_failed",
            extra={"parameters": list(names), "error_type": type(exc).__name__},
        )
        raise ConfigError(f"Failed to fetch SSM parameters: {exc}") from exc

    resolved = {p["Name"]: p.get("Value", "") for p in (response or {}).get("Parameters", []) or []}
    invalid = list((response or {}).get("InvalidParameters", []) or [])
    empty = [n for n in names if n in resolved and not resolved[n]]
    absent = [n for n in names if n not in resolved and n not in invalid]

    missing = invalid + empty + absent
    if missing:
        logger.error("ssm_parameters_missing", extra={"missing": missing})
        raise ConfigError(f"SSM parameters missing or empty: {sorted(set(missing))}")

    # Preserve request order in the returned dict.
    return {name: resolved[name] for name in names}


@lru_cache(maxsize=1)
def _cached_load_config(region_name: str | None) -> MnaConfig:
    values = get_parameters(ALL_PARAMETERS, region_name=region_name)
    return MnaConfig(
        runtime_arn=values[PARAM_RUNTIME_ARN],
        kb_id=values[PARAM_KB_ID],
        docs_bucket=values[PARAM_DOCS_BUCKET],
        aurora_cluster_arn=values[PARAM_AURORA_CLUSTER_ARN],
        aurora_secret_arn=values[PARAM_AURORA_SECRET_ARN],
        gateway_arn=values[PARAM_GATEWAY_ARN],
        evaluator_arn=values[PARAM_EVALUATOR_ARN],
        sessions_table_name=values[PARAM_SESSIONS_TABLE],
    )


def load_config(
    *,
    ssm_client: BaseClient | None = None,
    region_name: str | None = None,
    use_cache: bool = True,
) -> MnaConfig:
    """Resolve every ``/mna/*`` SSM parameter into a typed :class:`MnaConfig`.

    When ``ssm_client`` is supplied the result is never cached (dependency
    injection in tests should always produce a fresh read). Otherwise
    lookups are memoized per-region so repeated notebook cells don't
    pay the SSM round-trip twice.
    """

    if ssm_client is not None:
        values = get_parameters(ALL_PARAMETERS, ssm_client=ssm_client)
        return MnaConfig(
            runtime_arn=values[PARAM_RUNTIME_ARN],
            kb_id=values[PARAM_KB_ID],
            docs_bucket=values[PARAM_DOCS_BUCKET],
            aurora_cluster_arn=values[PARAM_AURORA_CLUSTER_ARN],
            aurora_secret_arn=values[PARAM_AURORA_SECRET_ARN],
            gateway_arn=values[PARAM_GATEWAY_ARN],
            evaluator_arn=values[PARAM_EVALUATOR_ARN],
            sessions_table_name=values[PARAM_SESSIONS_TABLE],
        )

    if not use_cache:
        clear_config_cache()
    return _cached_load_config(region_name)


def clear_config_cache() -> None:
    """Forget the cached :class:`MnaConfig`. Primarily used by tests."""

    _cached_load_config.cache_clear()

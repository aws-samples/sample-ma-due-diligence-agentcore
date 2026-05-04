"""AgentCore Runtime Custom Resource handler.

Manages the lifecycle of a Bedrock AgentCore Runtime resource via the
``bedrock-agentcore-control`` service (``CreateAgentRuntime`` /
``UpdateAgentRuntime`` / ``DeleteAgentRuntime``). Used by
:class:`infra.stacks.agent_stack.AgentStack` whenever the installed
``aws-cdk-lib`` does not expose a native ``AWS::BedrockAgentCore::Runtime``
resource (or when the project elects to ship the CR path for
consistency with the Memory CR — see task 13 notes).

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Custom Resources Inventory* (item 3) and *Custom Resource
Safety Requirements*. Safety rules enforced by the shared CR base
(``lambda/_cr_common/send_response.py``):

* Rule 1 — no ``boto3`` at module scope.
* Rule 2 — guaranteed response via the shared ``cr_handler``.
* Rule 4 — ``Delete`` treats a missing Runtime resource as success.
* Rule 6 — ``PhysicalResourceId`` is the Runtime ID so Update never
  triggers a replace cycle. A change to the Runtime *name* does
  require replacement (names are immutable on AgentCore Runtime);
  CloudFormation resolves this via the standard create-new +
  delete-old cycle.
* Rule 7 — returned ``Data`` is a handful of short strings (Runtime
  ID, ARN, image URI, status) well under the 4 KB response cap.

Resource properties consumed (``event['ResourceProperties']``):

``RuntimeName``
    Short human-readable name for the Runtime. Immutable — changing
    it triggers a replace cycle. Required.
``ImageUri``
    ECR image URI in the form ``<account>.dkr.ecr.<region>.amazonaws.com/
    <repo>:<tag>``. Required. The build waiter CR in task 11 is the
    canonical source for this value.
``RoleArn``
    ARN of the IAM role the AgentCore Runtime assumes. Updatable.
    Required.
``Description``
    Free-form description surfaced in the AgentCore console. Optional.
``NetworkMode``
    One of ``PUBLIC`` (default) or ``VPC``. Only ``PUBLIC`` is wired by
    :class:`AgentStack` today; ``VPC`` is accepted for forward
    compatibility.
``ProtocolConfiguration``
    Optional protocol override. Defaults to the AgentCore default
    (HTTPS JSON). Passed through to the service verbatim.
``EnvironmentVariables``
    Optional map of environment variables exposed to the runtime's
    container (e.g. ``MNA_SUPERVISOR_MODEL``, ``MNA_GUARDRAIL_ID``,
    ``MNA_MEMORY_ID``, ``MNA_GATEWAY_ARN``, ``MNA_EVALUATOR_ARN``).

The handler polls ``GetAgentRuntime`` until the resource reaches
``READY`` (or a terminal failure state) so CloudFormation only sees a
success response when the runtime is actually invokable.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import time
from typing import Any

# --------------------------------------------------------------------------- #
# Shared CR base loader.
# --------------------------------------------------------------------------- #

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_cr_common() -> Any:
    """Return the shared ``send_response`` module."""

    try:
        from _cr_common import send_response as module  # type: ignore[import-not-found]

        return module
    except ImportError:
        import importlib.util

        here = pathlib.Path(__file__).resolve().parent
        candidates = [
            here.parent / "_cr_common" / "send_response.py",
            here / "_cr_common" / "send_response.py",
        ]
        for candidate in candidates:
            if candidate.is_file():
                spec = importlib.util.spec_from_file_location(
                    "_cr_common_send_response", candidate
                )
                if spec is None or spec.loader is None:  # pragma: no cover - defensive
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules.setdefault("_cr_common_send_response", module)
                spec.loader.exec_module(module)
                return module
        raise


_cr_common = _load_cr_common()
cr_handler = _cr_common.cr_handler
is_missing_error = _cr_common.is_missing_error


# --------------------------------------------------------------------------- #
# Polling configuration
# --------------------------------------------------------------------------- #

# Poll cadence while the Runtime transitions between CREATING →
# READY. 10 seconds keeps the API call volume modest for a typical
# 2-5 minute provisioning window while still reporting completion
# promptly.
_POLL_INTERVAL_SECONDS = 10

# Hard cap on total wait time. Aligns with the 14-minute cap imposed
# by the shared CR safety rules (rule 5); the Lambda timeout on the
# CDK side is 15 minutes, leaving a one-minute buffer for the shared
# base to send its FAILED response on timeout.
_MAX_WAIT_SECONDS = 14 * 60

# Success / failure states exposed by the AgentCore Runtime control
# plane. Anything else (``CREATING``, ``UPDATING``, ``PENDING``) means
# we keep polling.
_SUCCESS_STATES = {"READY", "ACTIVE"}
_FAILURE_STATES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED", "FAILED"}


# --------------------------------------------------------------------------- #
# Resource-property helpers
# --------------------------------------------------------------------------- #


def _resource_properties(event: dict) -> dict:
    props = event.get("ResourceProperties") or {}
    return props if isinstance(props, dict) else {}


def _required_str(props: dict, key: str) -> str:
    value = props.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ResourceProperties.{key} is required")
    return value


def _optional_str(props: dict, key: str, default: str) -> str:
    value = props.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _optional_env_vars(props: dict) -> dict[str, str]:
    """Return the ``EnvironmentVariables`` map as a clean ``dict[str, str]``."""

    raw = props.get("EnvironmentVariables") or {}
    if not isinstance(raw, dict):
        raise ValueError("ResourceProperties.EnvironmentVariables must be a map")
    # AgentCore accepts only string→string pairs in the env vars map.
    # CloudFormation happily passes through stringified numbers, so we
    # coerce each value defensively.
    return {
        str(k): str(v)
        for k, v in raw.items()
        if k is not None and v is not None
    }


# --------------------------------------------------------------------------- #
# AgentCore Runtime client wrappers
# --------------------------------------------------------------------------- #


def _make_client(boto3: Any) -> Any:
    """Construct the ``bedrock-agentcore-control`` boto3 client."""

    return boto3.client("bedrock-agentcore-control")


def _runtime_artifact(image_uri: str) -> dict:
    """Return the ``agentRuntimeArtifact`` payload shape.

    The service expects a nested ``containerConfiguration.containerUri``
    struct; keeping this helper around means the ``create`` and
    ``update`` paths share a single definition of the request shape.
    """

    return {"containerConfiguration": {"containerUri": image_uri}}


# Retry configuration for the ``Access denied while validating ECR
# URI`` transient failure. The AgentCore service performs a
# synchronous permission simulation on the execution role during
# ``CreateAgentRuntime``; IAM eventual consistency means that role
# policies attached moments before the call may not yet be visible.
# Backoff: ~10s, 20s, 40s, 80s = up to ~2.5 minutes of total wait,
# which is well inside the CR's 14-minute safety cap.
_ECR_VALIDATION_ERROR_MARKER = "validating ECR URI"
_ECR_VALIDATION_MAX_RETRIES = 4
_ECR_VALIDATION_INITIAL_DELAY_SECONDS = 10


def _is_ecr_validation_error(exc: BaseException) -> bool:
    """Return True when ``exc`` looks like the transient ECR-validation failure."""

    message = str(exc)
    return _ECR_VALIDATION_ERROR_MARKER in message


def _call_with_ecr_validation_retry(operation_name: str, op: Any) -> dict:
    """Call ``op()`` with bounded retries on transient ECR-validation errors.

    ``op`` is a nullary callable (typically a ``lambda``) wrapping the
    real ``create_agent_runtime`` / ``update_agent_runtime`` call.
    Every other exception is re-raised immediately.
    """

    delay = _ECR_VALIDATION_INITIAL_DELAY_SECONDS
    attempt = 1
    while True:
        try:
            return op()
        except Exception as exc:  # noqa: BLE001 - classifier decides outcome
            if not _is_ecr_validation_error(exc):
                raise
            if attempt > _ECR_VALIDATION_MAX_RETRIES:
                logger.error(
                    "agentcore_runtime_ecr_validation_exhausted",
                    extra={
                        "operation": operation_name,
                        "attempts": attempt,
                        "exception_type": type(exc).__name__,
                    },
                )
                raise
            logger.warning(
                "agentcore_runtime_ecr_validation_retrying",
                extra={
                    "operation": operation_name,
                    "attempt": attempt,
                    "delay_seconds": delay,
                },
            )
            time.sleep(delay)
            delay *= 2
            attempt += 1


def _network_configuration(network_mode: str) -> dict:
    """Return the ``networkConfiguration`` payload shape."""

    return {"networkMode": network_mode}


def _create_runtime(
    client: Any,
    *,
    name: str,
    image_uri: str,
    role_arn: str,
    description: str,
    network_mode: str,
    protocol_configuration: str | None,
    environment_variables: dict[str, str],
) -> dict:
    """Invoke ``CreateAgentRuntime`` and return the service response."""

    logger.info(
        "agentcore_runtime_create_start",
        extra={
            "runtime_name": name,
            "image_uri": image_uri,
            "role_arn": role_arn,
            "network_mode": network_mode,
            "env_var_count": len(environment_variables),
        },
    )

    kwargs: dict[str, Any] = {
        "agentRuntimeName": name,
        "agentRuntimeArtifact": _runtime_artifact(image_uri),
        "roleArn": role_arn,
        "networkConfiguration": _network_configuration(network_mode),
    }
    if description:
        kwargs["description"] = description
    if protocol_configuration:
        kwargs["protocolConfiguration"] = {"serverProtocol": protocol_configuration}
    if environment_variables:
        kwargs["environmentVariables"] = environment_variables

    return _call_with_ecr_validation_retry(
        "CreateAgentRuntime", lambda: client.create_agent_runtime(**kwargs)
    )


def _update_runtime(
    client: Any,
    *,
    runtime_id: str,
    image_uri: str,
    role_arn: str,
    description: str,
    network_mode: str,
    protocol_configuration: str | None,
    environment_variables: dict[str, str],
) -> dict:
    """Invoke ``UpdateAgentRuntime`` and return the service response."""

    logger.info(
        "agentcore_runtime_update_start",
        extra={
            "runtime_id": runtime_id,
            "image_uri": image_uri,
            "role_arn": role_arn,
            "network_mode": network_mode,
        },
    )

    kwargs: dict[str, Any] = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": _runtime_artifact(image_uri),
        "roleArn": role_arn,
        "networkConfiguration": _network_configuration(network_mode),
    }
    if description:
        kwargs["description"] = description
    if protocol_configuration:
        kwargs["protocolConfiguration"] = {"serverProtocol": protocol_configuration}
    if environment_variables:
        kwargs["environmentVariables"] = environment_variables

    return _call_with_ecr_validation_retry(
        "UpdateAgentRuntime", lambda: client.update_agent_runtime(**kwargs)
    )


def _wait_for_runtime_ready(
    client: Any,
    *,
    runtime_id: str,
    expected_image_uri: str | None = None,
    require_transition: bool = False,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
) -> dict:
    """Poll ``GetAgentRuntime`` until the resource reaches READY or fails.

    Returns the final describe response. Raises :class:`RuntimeError`
    if the resource reports a terminal failure state or we exceed the
    polling cap.

    Parameters
    ----------
    expected_image_uri:
        When supplied, ``READY`` is accepted only after the runtime's
        reported image URI matches. Prevents the update path from
        declaring victory on the *previous* revision's ``READY`` status
        before AgentCore has transitioned into ``UPDATING`` for the
        new image.
    require_transition:
        When ``True``, the loop first waits for ``status`` to leave
        ``READY`` / ``ACTIVE`` (i.e. enter ``CREATING`` / ``UPDATING``)
        before treating a subsequent ``READY`` as success. This is
        necessary on update paths because ``UpdateAgentRuntime``
        returns 202 and the service may still report the old ``READY``
        status for several seconds before transitioning. Without this
        the CR returned SUCCESS in ~11s for updates that actually took
        several minutes to roll out.
    """

    deadline = now() + _MAX_WAIT_SECONDS
    last_status = "CREATING"
    saw_transition = not require_transition
    while True:
        response = client.get_agent_runtime(agentRuntimeId=runtime_id)
        runtime = (response or {}).get("agentRuntime") or response or {}
        last_status = runtime.get("status") or last_status
        reported_image = _extract_image_uri(runtime)
        logger.info(
            "agentcore_runtime_wait_tick",
            extra={
                "runtime_id": runtime_id,
                "status": last_status,
                "reported_image": reported_image,
                "expected_image": expected_image_uri,
                "saw_transition": saw_transition,
            },
        )
        if last_status not in _SUCCESS_STATES:
            # Any non-success status counts as "transition observed".
            saw_transition = True
        if last_status in _SUCCESS_STATES and saw_transition:
            # Accept READY only when the deployed image matches what we
            # asked AgentCore to deploy. If it does not, the service is
            # still reporting the pre-update state; keep polling.
            if expected_image_uri and reported_image and reported_image != expected_image_uri:
                logger.info(
                    "agentcore_runtime_wait_stale_ready",
                    extra={
                        "runtime_id": runtime_id,
                        "reported_image": reported_image,
                        "expected_image": expected_image_uri,
                    },
                )
            else:
                return runtime
        elif last_status in _FAILURE_STATES:
            failure = runtime.get("failureReason") or "terminal status reported"
            raise RuntimeError(
                f"Runtime {runtime_id} entered terminal state {last_status!r}: {failure}"
            )
        if now() >= deadline:
            raise RuntimeError(
                f"Runtime {runtime_id} did not reach READY within "
                f"{_MAX_WAIT_SECONDS}s (last status: {last_status!r})"
            )
        sleep(_POLL_INTERVAL_SECONDS)


def _extract_image_uri(runtime: dict) -> str:
    """Return the ``containerUri`` from a ``GetAgentRuntime`` response."""

    artifact = runtime.get("agentRuntimeArtifact") or runtime.get("artifact") or {}
    container = artifact.get("containerConfiguration") or {}
    uri = container.get("containerUri") or container.get("imageUri")
    return uri if isinstance(uri, str) else ""


# --------------------------------------------------------------------------- #
# Dispatchers
# --------------------------------------------------------------------------- #


def _on_create(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Create the AgentCore Runtime resource and wait for READY."""

    props = _resource_properties(event)
    name = _required_str(props, "RuntimeName")
    image_uri = _required_str(props, "ImageUri")
    role_arn = _required_str(props, "RoleArn")
    description = _optional_str(
        props,
        "Description",
        "AgentCore Runtime for the M&A Due Diligence sample.",
    )
    network_mode = _optional_str(props, "NetworkMode", "PUBLIC")
    protocol_configuration = props.get("ProtocolConfiguration")
    if protocol_configuration is not None and not isinstance(protocol_configuration, str):
        raise ValueError("ResourceProperties.ProtocolConfiguration must be a string")
    environment_variables = _optional_env_vars(props)

    client = _make_client(boto3)
    response = _create_runtime(
        client,
        name=name,
        image_uri=image_uri,
        role_arn=role_arn,
        description=description,
        network_mode=network_mode,
        protocol_configuration=protocol_configuration,
        environment_variables=environment_variables,
    )

    runtime = (response or {}).get("agentRuntime") or response or {}
    runtime_id = (
        runtime.get("agentRuntimeId")
        or runtime.get("id")
        or (response or {}).get("agentRuntimeId")
    )
    runtime_arn = (
        runtime.get("agentRuntimeArn")
        or runtime.get("arn")
        or (response or {}).get("agentRuntimeArn")
        or ""
    )
    if not runtime_id:
        raise RuntimeError(
            f"CreateAgentRuntime response missing runtime identifier: {response!r}"
        )

    final = _wait_for_runtime_ready(client, runtime_id=runtime_id, expected_image_uri=image_uri)
    runtime_arn = (
        final.get("agentRuntimeArn") or final.get("arn") or runtime_arn or ""
    )

    data = {
        "AgentRuntimeId": runtime_id,
        "AgentRuntimeArn": runtime_arn,
        "ImageUri": image_uri,
        "Status": final.get("status") or "READY",
    }
    return runtime_id, data


def _on_update(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Update an existing Runtime in place and wait for the transition to finish."""

    props = _resource_properties(event)
    physical_id = event.get("PhysicalResourceId") or ""
    if not physical_id:
        raise ValueError("Update event missing PhysicalResourceId")

    image_uri = _required_str(props, "ImageUri")
    role_arn = _required_str(props, "RoleArn")
    description = _optional_str(
        props,
        "Description",
        "AgentCore Runtime for the M&A Due Diligence sample.",
    )
    network_mode = _optional_str(props, "NetworkMode", "PUBLIC")
    protocol_configuration = props.get("ProtocolConfiguration")
    if protocol_configuration is not None and not isinstance(protocol_configuration, str):
        raise ValueError("ResourceProperties.ProtocolConfiguration must be a string")
    environment_variables = _optional_env_vars(props)

    # If the caller changed the *name*, CloudFormation will already
    # have scheduled a replace (delete-old + create-new). This branch
    # runs only for in-place property changes: image URI, role,
    # env vars, description, etc.
    client = _make_client(boto3)
    _update_runtime(
        client,
        runtime_id=physical_id,
        image_uri=image_uri,
        role_arn=role_arn,
        description=description,
        network_mode=network_mode,
        protocol_configuration=protocol_configuration,
        environment_variables=environment_variables,
    )

    final = _wait_for_runtime_ready(client, runtime_id=physical_id, expected_image_uri=image_uri, require_transition=True)
    runtime_arn = final.get("agentRuntimeArn") or final.get("arn") or ""

    data = {
        "AgentRuntimeId": physical_id,
        "AgentRuntimeArn": runtime_arn,
        "ImageUri": image_uri,
        "Status": final.get("status") or "READY",
    }
    return physical_id, data


def _on_delete(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Delete the Runtime resource; missing resource treated as success."""

    physical_id = event.get("PhysicalResourceId") or ""
    if not physical_id or physical_id in {"init", ""}:
        logger.info("agentcore_runtime_delete_no_physical_id")
        return physical_id or "init", {}

    client = _make_client(boto3)
    try:
        client.delete_agent_runtime(agentRuntimeId=physical_id)
        logger.info("agentcore_runtime_deleted", extra={"runtime_id": physical_id})
    except Exception as exc:  # noqa: BLE001 - classifier decides outcome
        if is_missing_error(exc):
            logger.info(
                "agentcore_runtime_delete_already_gone",
                extra={
                    "runtime_id": physical_id,
                    "exception_type": type(exc).__name__,
                },
            )
            return physical_id, {}
        raise

    return physical_id, {}


handler = cr_handler(
    create=_on_create,
    update=_on_update,
    delete=_on_delete,
)(_on_create)


__all__ = ["handler"]


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    logger.info("agentcore_runtime_module_loaded")

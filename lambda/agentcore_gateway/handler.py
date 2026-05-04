"""AgentCore Gateway Custom Resource handler.

Manages the lifecycle of a Bedrock AgentCore Gateway **and** its
single MCP-protocol Lambda target via the
``bedrock-agentcore-control`` service (``CreateGateway`` /
``UpdateGateway`` / ``DeleteGateway`` and ``CreateGatewayTarget`` /
``UpdateGatewayTarget`` / ``DeleteGatewayTarget``). Used by
:class:`infra.stacks.gateway_stack.GatewayStack` whenever the
installed ``aws-cdk-lib`` does not yet expose native
``AWS::BedrockAgentCore::Gateway`` / ``GatewayTarget`` L1 resources
(the default state in the versions this sample pins).

The Gateway gives the Financial Analysis specialist (task 23) an
MCP-shaped entry point into the market-data Lambda (task 15). Instead
of invoking the Lambda directly, the agent speaks MCP to the Gateway,
which forwards the request to the Lambda target configured here. This
extra hop is a deliberate demonstration of the AgentCore Gateway
integration pattern (Req 3.1, design §Gateway Targets: Lambda) —
swapping the Lambda for a different target (external HTTP API,
another MCP server, etc.) is then a single CR property change.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Custom Resources Inventory* (item 5) and *Custom Resource
Safety Requirements*. Safety rules enforced by the shared CR base
(``lambda/_cr_common/send_response.py``):

* Rule 1 — no ``boto3`` at module scope.
* Rule 2 — guaranteed response via the shared ``cr_handler``.
* Rule 4 — ``Delete`` treats a missing Gateway *or* Target as success
  via the ``is_missing_error`` classifier; both delete calls are
  idempotent.
* Rule 6 — ``PhysicalResourceId`` is the Gateway ID so Update never
  triggers a replace cycle. A change to the Gateway *name* does
  require replacement (names are immutable on AgentCore Gateway);
  CloudFormation resolves this via the standard create-new +
  delete-old cycle, and the Create path below tolerates the case
  where a previous deployment left a Target behind.
* Rule 7 — returned ``Data`` is a handful of short strings (Gateway
  ID, ARN, Target ID) well under the 4 KB response cap.

Resource properties consumed (``event['ResourceProperties']``):

``GatewayName``
    Short human-readable name for the Gateway. Immutable — changing
    it triggers a replace cycle. Required.
``Description``
    Free-form description surfaced in the AgentCore console. Optional.
``ProtocolType``
    Protocol the Gateway exposes to agents. Defaults to ``MCP`` —
    the only protocol this sample exercises.
``RoleArn``
    ARN of the IAM role the Gateway assumes to invoke the downstream
    Lambda target. Required. Created by
    :class:`GatewayStack` and scoped to the single market-data Lambda.
``TargetName``
    Name of the single MCP target the Gateway exposes. Required.
    ``market_data`` by convention — agents call
    ``market_data.get_comparable_multiples`` through the Gateway.
``TargetLambdaArn``
    ARN of the Lambda backing the target. Required.
``ToolSchema``
    Optional JSON-serializable dict describing the tool surface the
    Gateway should expose on top of the Lambda (name, description,
    input schema, output schema). When absent, the Gateway falls
    back to the Lambda's own metadata.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Any

# --------------------------------------------------------------------------- #
# Shared CR base loader. Mirrors the pattern used by every other CR
# handler in this project — the ``lambda`` directory is not importable
# as a Python package (the name clashes with the keyword), so the
# handler loads ``_cr_common/send_response.py`` by filesystem path.
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
# Defaults and constants
# --------------------------------------------------------------------------- #

# Default protocol. MCP is the only protocol the sample exercises —
# it's the one called out in the design doc (§Gateway Targets: Lambda)
# and the one ``mna.tools.market_data`` (task 20) expects.
_DEFAULT_PROTOCOL_TYPE = "MCP"

# Default target description when the caller doesn't supply one.
_DEFAULT_TARGET_DESCRIPTION = (
    "MCP target routing to the M&A Due Diligence sample's market-data Lambda."
)


# --------------------------------------------------------------------------- #
# Resource-property helpers
# --------------------------------------------------------------------------- #


def _resource_properties(event: dict) -> dict:
    """Return ``ResourceProperties`` as a dict, even when absent."""

    props = event.get("ResourceProperties") or {}
    return props if isinstance(props, dict) else {}


def _required_str(props: dict, key: str) -> str:
    """Fetch a required string resource property or raise :class:`ValueError`."""

    value = props.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ResourceProperties.{key} is required")
    return value


def _optional_str(props: dict, key: str, default: str) -> str:
    value = props.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _optional_tool_schema(props: dict) -> dict | None:
    """Return the caller-supplied tool schema as a ``dict`` or ``None``.

    CloudFormation normalizes JSON objects into Python dicts when the
    CDK properties dict is rendered into the ``ResourceProperties``
    field, so we only need to accept ``dict`` (or treat anything else
    as absent). Returning ``None`` keeps the Create/Update kwargs
    clean when the caller did not supply a schema.
    """

    raw = props.get("ToolSchema")
    if isinstance(raw, dict) and raw:
        return raw
    return None


def _optional_credential_provider_configurations(props: dict) -> list | None:
    """Return the caller-supplied credential-provider list or ``None``.

    ``CreateGatewayTarget`` requires ``credentialProviderConfigurations``
    in practice even though the API reference marks it as optional —
    the service rejects the request with "Credential provider
    configurations is not defined" when it's missing. We accept a
    list of credential-provider dicts verbatim so the caller (the CDK
    stack) owns the contract.
    """

    raw = props.get("CredentialProviderConfigurations")
    if isinstance(raw, list) and raw:
        return raw
    return None


# --------------------------------------------------------------------------- #
# AgentCore Gateway client wrappers
# --------------------------------------------------------------------------- #


def _make_client(boto3: Any) -> Any:
    """Construct the ``bedrock-agentcore-control`` boto3 client.

    The control-plane service name is ``bedrock-agentcore-control`` —
    the same client used by the Memory and Runtime CRs. Using the
    dedicated control-plane client keeps this handler aligned with
    the ``CreateGateway`` / ``CreateGatewayTarget`` APIs rather than
    the dataplane ``bedrock-agentcore`` client used by runtime
    traffic.
    """

    return boto3.client("bedrock-agentcore-control")


def _lambda_target_configuration(
    lambda_arn: str, tool_schema: dict | None
) -> dict:
    """Return the ``targetConfiguration`` payload for a Lambda target.

    Centralized so the Create and Update paths share a single
    definition of the request shape. When a ``tool_schema`` is
    supplied it is embedded verbatim — the caller (the CDK stack)
    owns the schema contract.
    """

    mcp: dict[str, Any] = {"lambdaArn": lambda_arn}
    if tool_schema is not None:
        mcp["toolSchema"] = tool_schema
    return {"mcp": {"lambda": mcp}}


def _create_gateway(
    client: Any,
    *,
    name: str,
    description: str,
    protocol_type: str,
    role_arn: str,
    authorizer_type: str,
    authorizer_configuration: dict | None,
) -> dict:
    """Invoke ``CreateGateway`` and return the service response."""

    logger.info(
        "agentcore_gateway_create_start",
        extra={
            "gateway_name": name,
            "protocol_type": protocol_type,
            "role_arn": role_arn,
            "authorizer_type": authorizer_type,
            "authorizer_configuration_present": authorizer_configuration is not None,
        },
    )
    kwargs: dict[str, Any] = {
        "name": name,
        "protocolType": protocol_type,
        "roleArn": role_arn,
        # ``authorizerType`` is required by the CreateGateway API.
        # ``CUSTOM_JWT`` additionally requires ``authorizerConfiguration``;
        # ``AWS_IAM`` / ``NONE`` / ``AUTHENTICATE_ONLY`` do not.
        "authorizerType": authorizer_type,
    }
    if description:
        kwargs["description"] = description
    if authorizer_configuration is not None:
        kwargs["authorizerConfiguration"] = authorizer_configuration
    return client.create_gateway(**kwargs)


def _update_gateway(
    client: Any,
    *,
    gateway_id: str,
    description: str,
    protocol_type: str,
    role_arn: str,
    name: str,
    authorizer_type: str,
    authorizer_configuration: dict | None,
) -> dict:
    """Invoke ``UpdateGateway`` and return the service response.

    The ``name`` field is required by the API even for in-place
    updates (AgentCore surfaces it as the canonical identifier in
    the console). Since our Create path pins a stable name, passing
    the same value here is a no-op from the service's perspective.
    ``authorizerType`` is also required on Update for the same reason.
    """

    logger.info(
        "agentcore_gateway_update_start",
        extra={
            "gateway_id": gateway_id,
            "protocol_type": protocol_type,
            "role_arn": role_arn,
            "authorizer_type": authorizer_type,
        },
    )
    kwargs: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "name": name,
        "protocolType": protocol_type,
        "roleArn": role_arn,
        "authorizerType": authorizer_type,
    }
    if description:
        kwargs["description"] = description
    if authorizer_configuration is not None:
        kwargs["authorizerConfiguration"] = authorizer_configuration
    return client.update_gateway(**kwargs)


def _create_gateway_target(
    client: Any,
    *,
    gateway_id: str,
    target_name: str,
    description: str,
    lambda_arn: str,
    tool_schema: dict | None,
    credential_provider_configurations: list | None,
) -> dict:
    """Invoke ``CreateGatewayTarget`` and return the service response."""

    logger.info(
        "agentcore_gateway_target_create_start",
        extra={
            "gateway_id": gateway_id,
            "target_name": target_name,
            "lambda_arn": lambda_arn,
            "tool_schema_present": tool_schema is not None,
            "credential_providers_present": credential_provider_configurations is not None,
        },
    )
    kwargs: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "name": target_name,
        "targetConfiguration": _lambda_target_configuration(lambda_arn, tool_schema),
    }
    if description:
        kwargs["description"] = description
    if credential_provider_configurations is not None:
        kwargs["credentialProviderConfigurations"] = credential_provider_configurations
    return client.create_gateway_target(**kwargs)


def _update_gateway_target(
    client: Any,
    *,
    gateway_id: str,
    target_id: str,
    target_name: str,
    description: str,
    lambda_arn: str,
    tool_schema: dict | None,
    credential_provider_configurations: list | None,
) -> dict:
    """Invoke ``UpdateGatewayTarget`` and return the service response."""

    logger.info(
        "agentcore_gateway_target_update_start",
        extra={
            "gateway_id": gateway_id,
            "target_id": target_id,
            "target_name": target_name,
            "lambda_arn": lambda_arn,
            "credential_providers_present": credential_provider_configurations is not None,
        },
    )
    kwargs: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "targetId": target_id,
        "name": target_name,
        "targetConfiguration": _lambda_target_configuration(lambda_arn, tool_schema),
    }
    if description:
        kwargs["description"] = description
    if credential_provider_configurations is not None:
        kwargs["credentialProviderConfigurations"] = credential_provider_configurations
    return client.update_gateway_target(**kwargs)


def _extract_gateway_identifiers(response: dict | None) -> tuple[str, str]:
    """Return ``(gateway_id, gateway_arn)`` from a Gateway API response.

    The AgentCore API sometimes nests the resource under ``gateway``
    and sometimes returns it at the response root — both shapes show
    up in the boto3 stub definitions today. Accept either.
    """

    body = response or {}
    gateway = body.get("gateway") if isinstance(body.get("gateway"), dict) else body
    gateway_id = (
        gateway.get("gatewayId")
        or gateway.get("id")
        or body.get("gatewayId")
        or ""
    )
    gateway_arn = (
        gateway.get("gatewayArn")
        or gateway.get("arn")
        or body.get("gatewayArn")
        or ""
    )
    return gateway_id, gateway_arn


def _synthesize_gateway_arn(boto3: Any, gateway_id: str) -> str:
    """Construct an AgentCore Gateway ARN from its ID.

    Used as a fallback when the service response omits ``gatewayArn``
    (observed in early GA releases of the bedrock-agentcore-control
    API). The ARN shape is stable:

        arn:{partition}:bedrock-agentcore:{region}:{account}:gateway/{id}

    Partition, region, and account are resolved from the current
    STS/session context so this works in ``aws``, ``aws-cn``, and
    ``aws-us-gov`` without a code change.
    """

    session = boto3.session.Session()
    region = session.region_name or "us-east-1"
    partition = "aws"
    if region.startswith("cn-"):
        partition = "aws-cn"
    elif region.startswith("us-gov-"):
        partition = "aws-us-gov"
    try:
        account = boto3.client("sts").get_caller_identity()["Account"]
    except Exception:  # noqa: BLE001 - best-effort fallback
        account = ""
    if not account:
        return ""
    return f"arn:{partition}:bedrock-agentcore:{region}:{account}:gateway/{gateway_id}"


def _extract_target_identifier(response: dict | None) -> str:
    """Return the ``targetId`` from a Gateway Target API response."""

    body = response or {}
    target = body.get("target") if isinstance(body.get("target"), dict) else body
    return (
        target.get("targetId")
        or target.get("id")
        or body.get("targetId")
        or ""
    )


# --------------------------------------------------------------------------- #
# Dispatchers
# --------------------------------------------------------------------------- #


# Default authorizer type when the caller does not specify one. ``AWS_IAM``
# lets the Gateway rely on the agent's normal IAM-signed requests without
# requiring JWT infrastructure (AgentCore Identity / JWT validation is
# explicitly out of scope for this sample — Requirement Out-of-Scope list).
_DEFAULT_AUTHORIZER_TYPE = "AWS_IAM"


def _resolve_authorizer(
    props: dict,
) -> tuple[str, dict | None]:
    """Return ``(authorizerType, authorizerConfiguration)`` from the props.

    ``authorizerType`` defaults to ``AWS_IAM`` (see
    :data:`_DEFAULT_AUTHORIZER_TYPE`) and is required by the
    ``CreateGateway``/``UpdateGateway`` APIs.
    ``authorizerConfiguration`` is only required when
    ``authorizerType == 'CUSTOM_JWT'`` and must be a dict matching the
    ``AuthorizerConfiguration`` union shape
    (``{'customJWTAuthorizer': {'discoveryUrl': str, 'allowedAudience': [str]}}``).
    For any other authorizer type we pass ``None`` so the request
    omits the field entirely — the service rejects extra config on
    ``AWS_IAM`` / ``NONE`` / ``AUTHENTICATE_ONLY`` authorizers.
    """

    authorizer_type = _optional_str(props, "AuthorizerType", _DEFAULT_AUTHORIZER_TYPE)
    raw_config = props.get("AuthorizerConfiguration")
    authorizer_config: dict | None = (
        raw_config if isinstance(raw_config, dict) and raw_config else None
    )
    if authorizer_type == "CUSTOM_JWT" and authorizer_config is None:
        raise ValueError(
            "AuthorizerType=CUSTOM_JWT requires a non-empty AuthorizerConfiguration "
            "with a ``customJWTAuthorizer`` block (discoveryUrl + allowedAudience)."
        )
    # Strip configuration on non-JWT authorizers; the service rejects
    # ``authorizerConfiguration`` when type is ``AWS_IAM`` / ``NONE`` /
    # ``AUTHENTICATE_ONLY``. The Lambda ships vendored boto3 (see
    # ``lambda/requirements.txt``) whose service model correctly marks
    # this field as optional for non-JWT types, so no placeholder is
    # needed to satisfy the client-side validator.
    if authorizer_type != "CUSTOM_JWT":
        authorizer_config = None
    return authorizer_type, authorizer_config


def _find_gateway_by_name(client: Any, name: str) -> dict | None:
    """Return the gateway dict matching ``name`` from ``ListGateways``.

    Used by the Create path to recover from ``ConflictException`` when
    a previous failed deploy left a gateway with the same name in
    AgentCore. ``list_gateways`` returns a paginated response; this
    helper scans pages until a match is found or the pages run out.
    """

    paginator = client.get_paginator("list_gateways")
    for page in paginator.paginate():
        items = (
            page.get("items")
            or page.get("gateways")
            or page.get("gatewaySummaries")
            or []
        )
        for item in items:
            if isinstance(item, dict) and item.get("name") == name:
                return item
    return None


def _find_target_by_name(
    client: Any, *, gateway_id: str, name: str
) -> str | None:
    """Return the ``targetId`` of the matching target, or ``None``."""

    list_response = client.list_gateway_targets(gatewayIdentifier=gateway_id)
    items = (
        (list_response or {}).get("targets")
        or (list_response or {}).get("gatewayTargets")
        or (list_response or {}).get("items")
        or []
    )
    for item in items:
        if isinstance(item, dict) and item.get("name") == name:
            return item.get("targetId") or item.get("id")
    return None


# Upper bound on time spent polling for a gateway to reach READY.
# Kept under the 14-minute CR cap (Req 11a.5) with plenty of headroom
# for a later ``CreateGatewayTarget`` call and response upload.
_GATEWAY_READY_TIMEOUT_SECONDS = 600
_GATEWAY_READY_POLL_INTERVAL_SECONDS = 5


def _wait_for_gateway_ready(client: Any, gateway_id: str) -> None:
    """Poll ``GetGateway`` until the gateway reaches READY status.

    AgentCore ``CreateGateway`` returns 202 with status ``CREATING``
    and provisions asynchronously. ``CreateGatewayTarget`` rejects
    requests until the gateway status is ``READY``. This helper
    bridges the gap with a bounded poll loop.

    Raises :class:`RuntimeError` on terminal-failure statuses or when
    the timeout is hit. Polling is deliberately plain
    ``time.sleep`` — the CR's own timeout is the outer safety net
    (Req 11a.5: 14-minute cap).
    """

    import time  # noqa: PLC0415 - stdlib, imported lazily to keep module cold-start safe

    deadline = time.monotonic() + _GATEWAY_READY_TIMEOUT_SECONDS
    terminal_failure = {"FAILED", "UPDATE_UNSUCCESSFUL"}
    ready = {"READY"}

    while True:
        response = client.get_gateway(gatewayIdentifier=gateway_id)
        body = response or {}
        gateway = body.get("gateway") if isinstance(body.get("gateway"), dict) else body
        status = gateway.get("status") or body.get("status") or ""
        logger.info(
            "agentcore_gateway_wait_tick",
            extra={"gateway_id": gateway_id, "status": status},
        )
        if status in ready:
            return
        if status in terminal_failure:
            reasons = gateway.get("statusReasons") or body.get("statusReasons") or []
            raise RuntimeError(
                f"Gateway {gateway_id} reached terminal status {status!r}. "
                f"Reasons: {reasons!r}"
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Timed out waiting for gateway {gateway_id} to reach READY "
                f"after {_GATEWAY_READY_TIMEOUT_SECONDS}s. Last status: {status!r}"
            )
        time.sleep(_GATEWAY_READY_POLL_INTERVAL_SECONDS)


def _is_conflict_error(exc: BaseException) -> bool:
    """Return True when ``exc`` is an AgentCore ``ConflictException``."""

    if "ConflictException" in type(exc).__name__:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code") if isinstance(
            response.get("Error"), dict
        ) else None
        if isinstance(code, str) and "Conflict" in code:
            return True
    return False


def _on_create(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Create the Gateway and its single MCP Lambda target.

    If a gateway with the same name already exists (left over from a
    previous failed deploy that rolled back before the CR's Delete
    path could clean it up), the Create path adopts it and proceeds
    to create or update the target. This makes the CR resilient to
    failed-retry-failed-retry cycles — something the reader will
    hit a lot when iterating on a new sample.
    """

    props = _resource_properties(event)
    name = _required_str(props, "GatewayName")
    description = _optional_str(
        props,
        "Description",
        "AgentCore Gateway for the M&A Due Diligence sample.",
    )
    protocol_type = _optional_str(props, "ProtocolType", _DEFAULT_PROTOCOL_TYPE)
    role_arn = _required_str(props, "RoleArn")
    target_name = _required_str(props, "TargetName")
    target_lambda_arn = _required_str(props, "TargetLambdaArn")
    tool_schema = _optional_tool_schema(props)
    credential_provider_configurations = _optional_credential_provider_configurations(props)
    authorizer_type, authorizer_config = _resolve_authorizer(props)

    client = _make_client(boto3)

    try:
        gateway_response = _create_gateway(
            client,
            name=name,
            description=description,
            protocol_type=protocol_type,
            role_arn=role_arn,
            authorizer_type=authorizer_type,
            authorizer_configuration=authorizer_config,
        )
    except Exception as exc:  # noqa: BLE001 - classifier decides next step
        if not _is_conflict_error(exc):
            raise
        # Recover: adopt the existing gateway. This is the path we
        # take when an earlier deploy rolled back after CreateGateway
        # succeeded but before the target was wired, leaving the
        # gateway orphaned in AgentCore with our chosen name.
        logger.warning(
            "agentcore_gateway_create_conflict_adopting_existing",
            extra={"gateway_name": name},
        )
        existing = _find_gateway_by_name(client, name)
        if existing is None:
            # Conflict was real but we can't find the offending
            # gateway. Re-raise so the reader sees a clear error
            # instead of a confusing downstream failure.
            raise
        gateway_response = existing
    logger.info(
        "agentcore_gateway_create_response",
        extra={"keys": sorted((gateway_response or {}).keys())},
    )
    gateway_id, gateway_arn = _extract_gateway_identifiers(gateway_response)
    if not gateway_id:
        raise RuntimeError(
            f"CreateGateway response missing gateway identifier: {gateway_response!r}"
        )
    if not gateway_arn:
        # Service response omitted ``gatewayArn``. Synthesize it from
        # the stable ARN shape so downstream CFN GetAtt on
        # ``GatewayArn`` always resolves to a non-empty value.
        gateway_arn = _synthesize_gateway_arn(boto3, gateway_id)
        logger.warning(
            "agentcore_gateway_arn_synthesized",
            extra={"gateway_id": gateway_id, "arn_present": bool(gateway_arn)},
        )
    if not gateway_arn:
        raise RuntimeError(
            "Could not determine Gateway ARN — neither service response "
            f"nor ARN synthesis produced a value. Response: {gateway_response!r}"
        )

    # Wait for the gateway to reach READY before creating the target.
    # ``CreateGateway`` returns 202 with status ``CREATING`` and
    # provisions asynchronously — ``CreateGatewayTarget`` rejects
    # requests until the gateway is READY. Bounded poll so we stay
    # inside the CR's 14-minute safety cap (Req 11a.5).
    _wait_for_gateway_ready(client, gateway_id)

    try:
        target_response = _create_gateway_target(
            client,
            gateway_id=gateway_id,
            target_name=target_name,
            description=_DEFAULT_TARGET_DESCRIPTION,
            lambda_arn=target_lambda_arn,
            tool_schema=tool_schema,
            credential_provider_configurations=credential_provider_configurations,
        )
    except Exception as exc:  # noqa: BLE001 - classifier decides next step
        if not _is_conflict_error(exc):
            raise
        # Adopt-and-update an existing target left over from a prior
        # failed deploy. We list targets on the gateway, find the one
        # with our ``target_name``, and update it in place.
        logger.warning(
            "agentcore_gateway_target_create_conflict_adopting_existing",
            extra={"gateway_id": gateway_id, "target_name": target_name},
        )
        existing_target_id = _find_target_by_name(
            client, gateway_id=gateway_id, name=target_name
        )
        if not existing_target_id:
            raise
        target_response = _update_gateway_target(
            client,
            gateway_id=gateway_id,
            target_id=existing_target_id,
            target_name=target_name,
            description=_DEFAULT_TARGET_DESCRIPTION,
            lambda_arn=target_lambda_arn,
            tool_schema=tool_schema,
            credential_provider_configurations=credential_provider_configurations,
        )
    target_id = _extract_target_identifier(target_response)
    if not target_id:
        # Target creation failed silently — treat as a CR failure so
        # CloudFormation rolls back and the reader gets a clear error
        # rather than a half-wired Gateway.
        raise RuntimeError(
            f"CreateGatewayTarget response missing target identifier: {target_response!r}"
        )

    data = {
        "GatewayId": gateway_id,
        "GatewayArn": gateway_arn,
        "TargetId": target_id,
    }
    return gateway_id, data


def _on_update(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Update the Gateway and its Lambda target in place.

    Both updates are idempotent on the service side: re-applying the
    same description, protocol, role, Lambda ARN, or tool schema is a
    no-op. CloudFormation replaces the whole resource only when
    ``GatewayName`` changes (names are immutable on AgentCore
    Gateway); the replace cycle goes through this handler's Create
    and Delete paths.
    """

    props = _resource_properties(event)
    physical_id = event.get("PhysicalResourceId") or ""
    if not physical_id:
        raise ValueError("Update event missing PhysicalResourceId")

    name = _required_str(props, "GatewayName")
    description = _optional_str(
        props,
        "Description",
        "AgentCore Gateway for the M&A Due Diligence sample.",
    )
    protocol_type = _optional_str(props, "ProtocolType", _DEFAULT_PROTOCOL_TYPE)
    role_arn = _required_str(props, "RoleArn")
    target_name = _required_str(props, "TargetName")
    target_lambda_arn = _required_str(props, "TargetLambdaArn")
    tool_schema = _optional_tool_schema(props)
    credential_provider_configurations = _optional_credential_provider_configurations(props)
    authorizer_type, authorizer_config = _resolve_authorizer(props)

    client = _make_client(boto3)

    gateway_response = _update_gateway(
        client,
        gateway_id=physical_id,
        description=description,
        protocol_type=protocol_type,
        role_arn=role_arn,
        name=name,
        authorizer_type=authorizer_type,
        authorizer_configuration=authorizer_config,
    )
    _, gateway_arn = _extract_gateway_identifiers(gateway_response)
    if not gateway_arn:
        # Same synthesis fallback the Create path uses — keep Update
        # and Create in sync so CFN GetAtt never sees an empty ARN.
        gateway_arn = _synthesize_gateway_arn(boto3, physical_id)

    # Look up the existing target so we can update it in place.
    # AgentCore Gateway supports multiple targets per gateway, but
    # this sample only provisions one (``TargetName``). The service's
    # ``list_gateway_targets`` returns the whole set so we find the
    # match by name.
    list_response = client.list_gateway_targets(gatewayIdentifier=physical_id)
    existing_targets = (list_response or {}).get("targets") or (list_response or {}).get(
        "gatewayTargets"
    ) or []
    target_id = ""
    for item in existing_targets:
        if not isinstance(item, dict):
            continue
        if item.get("name") == target_name:
            target_id = (
                item.get("targetId") or item.get("id") or ""
            )
            break

    if target_id:
        target_response = _update_gateway_target(
            client,
            gateway_id=physical_id,
            target_id=target_id,
            target_name=target_name,
            description=_DEFAULT_TARGET_DESCRIPTION,
            lambda_arn=target_lambda_arn,
            tool_schema=tool_schema,
            credential_provider_configurations=credential_provider_configurations,
        )
        target_id = _extract_target_identifier(target_response) or target_id
    else:
        # Previous deploy left no target (partial failure, manual
        # edit, etc.) — recreate it so the Gateway is always in the
        # state the stack declares.
        target_response = _create_gateway_target(
            client,
            gateway_id=physical_id,
            target_name=target_name,
            description=_DEFAULT_TARGET_DESCRIPTION,
            lambda_arn=target_lambda_arn,
            tool_schema=tool_schema,
            credential_provider_configurations=credential_provider_configurations,
        )
        target_id = _extract_target_identifier(target_response)
        if not target_id:
            raise RuntimeError(
                "Update recovery failed: CreateGatewayTarget response missing target id: "
                f"{target_response!r}"
            )

    data = {
        "GatewayId": physical_id,
        "GatewayArn": gateway_arn,
        "TargetId": target_id,
    }
    return physical_id, data


def _on_delete(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Delete the Gateway (and its target first).

    The AgentCore API requires targets to be removed before their
    parent Gateway, so we list targets, delete each one, and only
    then delete the Gateway itself. Every call is wrapped so a
    missing resource is swallowed by the shared CR base's
    ``is_missing_error`` classifier.
    """

    physical_id = event.get("PhysicalResourceId") or ""
    if not physical_id or physical_id in {"init", ""}:
        logger.info("agentcore_gateway_delete_no_physical_id")
        return physical_id or "init", {}

    client = _make_client(boto3)

    # Step 1 — delete every target attached to the Gateway. Missing
    # targets (already cleaned up) are tolerated per rule 4.
    try:
        list_response = client.list_gateway_targets(gatewayIdentifier=physical_id)
    except Exception as exc:  # noqa: BLE001 - classifier decides outcome
        if is_missing_error(exc):
            logger.info(
                "agentcore_gateway_delete_already_gone",
                extra={
                    "gateway_id": physical_id,
                    "exception_type": type(exc).__name__,
                },
            )
            return physical_id, {}
        raise

    existing_targets = (list_response or {}).get("targets") or (list_response or {}).get(
        "gatewayTargets"
    ) or []
    for item in existing_targets:
        if not isinstance(item, dict):
            continue
        target_id = item.get("targetId") or item.get("id")
        if not target_id:
            continue
        try:
            client.delete_gateway_target(
                gatewayIdentifier=physical_id,
                targetId=target_id,
            )
            logger.info(
                "agentcore_gateway_target_deleted",
                extra={"gateway_id": physical_id, "target_id": target_id},
            )
        except Exception as exc:  # noqa: BLE001 - classifier decides outcome
            if is_missing_error(exc):
                logger.info(
                    "agentcore_gateway_target_delete_already_gone",
                    extra={
                        "gateway_id": physical_id,
                        "target_id": target_id,
                        "exception_type": type(exc).__name__,
                    },
                )
                continue
            raise

    # Step 2 — delete the Gateway itself. Missing Gateway tolerated
    # per rule 4 (delete idempotency).
    try:
        client.delete_gateway(gatewayIdentifier=physical_id)
        logger.info("agentcore_gateway_deleted", extra={"gateway_id": physical_id})
    except Exception as exc:  # noqa: BLE001 - classifier decides outcome
        if is_missing_error(exc):
            logger.info(
                "agentcore_gateway_delete_already_gone",
                extra={
                    "gateway_id": physical_id,
                    "exception_type": type(exc).__name__,
                },
            )
            return physical_id, {}
        raise

    return physical_id, {}


# Wire Create/Update/Delete through the shared CR base. The base
# handles rule 1 (lazy boto3 import), rule 2 (guaranteed response),
# rule 4 (delete-of-missing → success via ``is_missing`` classifier),
# and rules 7/8.
handler = cr_handler(
    create=_on_create,
    update=_on_update,
    delete=_on_delete,
)(_on_create)


__all__ = ["handler"]


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    logger.info("agentcore_gateway_module_loaded")

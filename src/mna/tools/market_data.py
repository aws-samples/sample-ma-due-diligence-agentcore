"""Market-data tool backed by the AgentCore Gateway.

The Financial Analysis specialist calls this tool when it needs
synthetic comparable multiples. Per design.md → "Components and
Interfaces" → "Tools" → ``tools/market_data.py``, the tool is a thin
client: it invokes the ``market_data.get_comparable_multiples`` tool
hosted on the AgentCore Gateway, which in turn routes to the market-
data Lambda at ``lambda/market_data/handler.py``.

The MCP hop happens at the Gateway layer — this module just posts the
arguments and unwraps the response.

The boto3 client is imported lazily so ``import mna`` stays cold-start
safe (same convention as the other tool modules).
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from mna.config import load_config
from mna.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from botocore.client import BaseClient

logger = get_logger(__name__)

#: Fully-qualified MCP tool name exposed by the Gateway target that
#: fronts ``lambda/market_data/handler.py``. Dots are the MCP tool
#: namespace separator; the ``market-data`` prefix is the on-AWS
#: target name (AgentCore rejects underscores in target names, so
#: the gateway target is ``market-data`` while the Python module
#: name stays ``market_data`` — Python forbids hyphens in module
#: identifiers). Matches ``_GATEWAY_TARGET_NAME`` in
#: :mod:`infra.stacks.gateway_stack`.
TOOL_NAME = "market-data.get_comparable_multiples"


class MarketDataError(RuntimeError):
    """Raised when the Gateway invocation or response parsing fails."""


def _build_agentcore_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for the AgentCore data plane (lazy import)."""

    import boto3  # Lazy import: never at module top level.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("bedrock-agentcore", **kwargs)


def _read_response_body(body: Any) -> bytes:
    """Collect a Gateway invocation response body into bytes.

    The AgentCore data plane can return either bytes (non-streaming)
    or a ``StreamingBody`` / iterable of chunk dicts (streaming). Same
    pattern we use in :func:`mna.client._read_response_body`.
    """

    if body is None:
        return b""
    if isinstance(body, bytes | bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")

    read = getattr(body, "read", None)
    if callable(read):
        data = read()
        if isinstance(data, str):
            return data.encode("utf-8")
        return bytes(data or b"")

    try:
        chunks: list[bytes] = []
        for event in body:
            if isinstance(event, dict):
                chunk = event.get("chunk") or event.get("payload") or {}
                if isinstance(chunk, dict):
                    part = chunk.get("bytes") or chunk.get("data") or b""
                else:
                    part = chunk
                if isinstance(part, str):
                    part = part.encode("utf-8")
                if part:
                    chunks.append(bytes(part))
            elif isinstance(event, bytes | bytearray):
                chunks.append(bytes(event))
            elif isinstance(event, str):
                chunks.append(event.encode("utf-8"))
        return b"".join(chunks)
    except TypeError:
        return b""


def _parse_payload(raw: bytes) -> dict[str, Any]:
    """Decode the gateway response body into a dict."""

    if not raw:
        return {}
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MarketDataError(f"Gateway returned non-JSON body: {exc}") from exc

    if isinstance(parsed, dict):
        return parsed
    raise MarketDataError(
        f"Gateway returned unexpected JSON type {type(parsed).__name__}; expected object"
    )


def _invoke_gateway_tool(
    client: BaseClient,
    gateway_arn: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Low-level Gateway tool invocation.

    The AgentCore data plane exposes the Gateway via an
    ``invoke_gateway`` operation that takes a JSON payload carrying the
    MCP tool name and arguments. Operation names in the SDK surface
    vary across preview versions, so we resolve the method by name and
    fall back to ``invoke`` if only that is available. Tests inject a
    mock client exposing ``invoke_gateway`` directly.
    """

    payload = json.dumps({"tool": tool_name, "arguments": arguments}).encode("utf-8")

    invoke = getattr(client, "invoke_gateway", None)
    if callable(invoke):
        response = invoke(gatewayArn=gateway_arn, payload=payload)
    else:
        # Fallback: some preview SDKs use the generic ``invoke`` call.
        invoke = getattr(client, "invoke", None)
        if not callable(invoke):
            raise MarketDataError(
                "AgentCore client does not expose an invoke_gateway / invoke method"
            )
        response = invoke(gatewayArn=gateway_arn, payload=payload)

    raw_body = _read_response_body(
        response.get("response") if isinstance(response, dict) else None
    )
    if not raw_body and isinstance(response, dict):
        raw_body = _read_response_body(response.get("payload"))
    return _parse_payload(raw_body)


def get_comparable_multiples(
    industry_code: str,
    deal_size_band: str,
    *,
    gateway_arn: str | None = None,
    agentcore_client: BaseClient | None = None,
    region_name: str | None = None,
) -> dict[str, Any]:
    """Fetch synthetic comparable multiples for a target industry / size band.

    Parameters
    ----------
    industry_code:
        Free-form industry identifier (e.g. ``"transportation"``).
    deal_size_band:
        Revenue band string (e.g. ``"100M-500M"``).
    gateway_arn:
        AgentCore Gateway ARN hosting the ``market_data`` MCP target.
        When omitted, resolved from :func:`mna.config.load_config`.
    agentcore_client:
        Optional boto3 client for dependency injection in tests.
    region_name:
        Override AWS region when the function builds its own client.
        Ignored when ``agentcore_client`` is provided.

    Returns
    -------
    dict
        The payload returned by the Gateway-backed Lambda. Per
        ``lambda/market_data/handler.py`` the shape is::

            {
                "synthetic": True,
                "disclaimer": "SYNTHETIC DATA - NOT REAL MARKET DATA",
                "industry_code": ...,
                "deal_size_band": ...,
                "comparables": [...],
                "median_ev_ebitda": ...,
                "median_ev_revenue": ...,
                "p25_ev_ebitda": ...,
                "p75_ev_ebitda": ...,
            }

        The caller (the Financial Analysis agent) receives the payload
        unchanged so downstream prompts can reference fields directly.
    """

    if not isinstance(industry_code, str) or not industry_code.strip():
        raise MarketDataError("industry_code must be a non-empty string")
    if not isinstance(deal_size_band, str) or not deal_size_band.strip():
        raise MarketDataError("deal_size_band must be a non-empty string")

    resolved_gateway_arn = gateway_arn
    if not resolved_gateway_arn:
        try:
            resolved_gateway_arn = load_config(region_name=region_name).gateway_arn
        except Exception as exc:
            raise MarketDataError(
                "gateway_arn was not provided and could not be resolved from SSM"
            ) from exc

    if not resolved_gateway_arn:
        raise MarketDataError("gateway_arn resolved to an empty string")

    client = agentcore_client or _build_agentcore_client(region_name=region_name)

    arguments = {
        "industry_code": industry_code,
        "deal_size_band": deal_size_band,
    }

    logger.info(
        "market_data_invocation_started",
        extra={
            "gateway_arn": resolved_gateway_arn,
            "tool": TOOL_NAME,
            "industry_code": industry_code,
            "deal_size_band": deal_size_band,
        },
    )

    try:
        payload = _invoke_gateway_tool(
            client,
            gateway_arn=resolved_gateway_arn,
            tool_name=TOOL_NAME,
            arguments=arguments,
        )
    except MarketDataError:
        raise
    except Exception as exc:
        logger.error(
            "market_data_invocation_failed",
            extra={
                "gateway_arn": resolved_gateway_arn,
                "error_type": type(exc).__name__,
            },
        )
        raise MarketDataError(f"Gateway invocation failed: {exc}") from exc

    logger.info(
        "market_data_invocation_completed",
        extra={
            "gateway_arn": resolved_gateway_arn,
            "comparables": len(payload.get("comparables") or []),
            "synthetic": bool(payload.get("synthetic")),
        },
    )

    return payload

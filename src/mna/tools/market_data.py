"""Market-data tool backed by the AgentCore Gateway.

The Financial Analysis specialist calls this tool when it needs
synthetic comparable multiples. Per design.md → "Components and
Interfaces" → "Tools" → ``tools/market_data.py``, the tool is a thin
client: it invokes the ``get_comparable_multiples`` tool hosted on the
AgentCore Gateway via the MCP protocol over HTTP, authenticated using
SigV4 (IAM-based inbound authorization).

The Gateway URL is resolved from SSM or the ``MNA_GATEWAY_URL``
environment variable. The boto3 auth signer provides the SigV4
credentials for the request.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from mna.config import load_config
from mna.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    pass

logger = get_logger(__name__)

#: MCP tool name as registered on the Gateway target.
#: Format is ``{target_name}___{tool_name}`` (three underscores).
TOOL_NAME = "market-data___get_comparable_multiples"


class MarketDataError(RuntimeError):
    """Raised when the Gateway invocation or response parsing fails."""


def _resolve_gateway_url(region_name: str | None = None) -> str:
    """Resolve the Gateway MCP endpoint URL.

    Resolution order:
    1. ``MNA_GATEWAY_URL`` environment variable (set by agent_stack.py)
    2. Derived from Gateway ARN
    """
    url = os.getenv("MNA_GATEWAY_URL")
    if url:
        return url

    gateway_arn = os.getenv("MNA_GATEWAY_ARN")
    if not gateway_arn:
        try:
            gateway_arn = load_config(region_name=region_name).gateway_arn
        except Exception as exc:
            raise MarketDataError(
                "Cannot resolve gateway URL: MNA_GATEWAY_URL not set and "
                "gateway ARN not resolvable from SSM"
            ) from exc

    if not gateway_arn:
        raise MarketDataError("gateway_arn resolved to an empty string")

    try:
        gateway_id = gateway_arn.rsplit("/", 1)[1]
    except (IndexError, AttributeError) as exc:
        raise MarketDataError(f"Cannot parse gateway ID from ARN: {gateway_arn}") from exc

    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    return f"https://{gateway_id}.gateway.bedrock-agentcore.{resolved_region}.amazonaws.com/mcp"


def _get_cognito_token(region_name: str | None = None) -> str:
    """Obtain a JWT access token via Cognito client_credentials grant.

    The Gateway was created with a default Cognito authorizer. The
    client ID / secret / domain are resolved from environment variables
    or from the Cognito user pool associated with the gateway.
    """
    import base64  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415

    client_id = os.getenv("MNA_GATEWAY_CLIENT_ID", "7qoor24cdnvvbqg3ged41feg05")
    client_secret = os.getenv("MNA_GATEWAY_CLIENT_SECRET", "17bulpnb445su0bd2hr2aputc5a1erbp78fkq423dm8m1pn5oo7i")
    token_domain = os.getenv(
        "MNA_GATEWAY_TOKEN_DOMAIN",
        "mnagatewaystack-agentgateway-57b81449"
    )
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"

    token_url = f"https://{token_domain}.auth.{resolved_region}.amazoncognito.com/oauth2/token"

    # client_credentials grant requires Basic auth header
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    body = "grant_type=client_credentials".encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {credentials}",
    }

    req = urllib.request.Request(token_url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_response = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise MarketDataError(f"Failed to obtain Cognito token: {exc}") from exc

    access_token = token_response.get("access_token")
    if not access_token:
        raise MarketDataError(f"Cognito token response missing access_token: {list(token_response.keys())}")
    return access_token


def _call_gateway_mcp(
    gateway_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    region_name: str | None = None,
) -> dict[str, Any]:
    """Call a tool on the Gateway via the MCP JSON-RPC protocol.

    Sends a POST to the Gateway's /mcp endpoint with a JSON-RPC 2.0
    payload using the ``tools/call`` method. Authentication is via
    a Cognito JWT Bearer token (client_credentials grant).
    """
    import urllib.request  # noqa: PLC0415
    import urllib.error  # noqa: PLC0415

    # Obtain a JWT token from Cognito
    access_token = _get_cognito_token(region_name=region_name)

    payload = {
        "jsonrpc": "2.0",
        "id": "market-data-call",
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    body = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    req = urllib.request.Request(
        gateway_url,
        data=body,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise MarketDataError(
            f"Gateway returned HTTP {exc.code}: {error_body[:500]}"
        ) from exc
    except Exception as exc:
        raise MarketDataError(f"Gateway request failed: {exc}") from exc

    try:
        rpc_response = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise MarketDataError(f"Gateway returned non-JSON: {response_body[:200]}") from exc

    # JSON-RPC error handling
    if "error" in rpc_response:
        err = rpc_response["error"]
        raise MarketDataError(
            f"Gateway MCP error {err.get('code')}: {err.get('message', 'unknown')}"
        )

    # Extract the tool result from the JSON-RPC response
    result = rpc_response.get("result", {})
    # MCP tools/call returns {"content": [{"type": "text", "text": "..."}]}
    content = result.get("content", [])
    if content and isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_content = item.get("text", "")
                try:
                    return json.loads(text_content)
                except json.JSONDecodeError:
                    return {"text": text_content}

    # Fallback: return the result as-is
    if isinstance(result, dict):
        return result
    return {"raw": response_body}


def get_comparable_multiples(
    industry_code: str,
    deal_size_band: str,
    *,
    gateway_url: str | None = None,
    region_name: str | None = None,
) -> dict[str, Any]:
    """Fetch synthetic comparable multiples for a target industry / size band.

    Parameters
    ----------
    industry_code:
        Free-form industry identifier (e.g. ``"transportation"``).
    deal_size_band:
        Revenue band string (e.g. ``"100M-500M"``).
    gateway_url:
        Full MCP endpoint URL. When omitted, resolved from env/SSM.
    region_name:
        Override AWS region for SigV4 signing.

    Returns
    -------
    dict
        The payload from the Lambda via the Gateway.
    """
    if not isinstance(industry_code, str) or not industry_code.strip():
        raise MarketDataError("industry_code must be a non-empty string")
    if not isinstance(deal_size_band, str) or not deal_size_band.strip():
        raise MarketDataError("deal_size_band must be a non-empty string")

    resolved_url = gateway_url or _resolve_gateway_url(region_name=region_name)

    arguments = {
        "industry_code": industry_code,
        "deal_size_band": deal_size_band,
    }

    logger.info(
        "market_data_invocation_started",
        extra={
            "gateway_url": resolved_url,
            "tool": TOOL_NAME,
            "industry_code": industry_code,
            "deal_size_band": deal_size_band,
        },
    )

    try:
        payload = _call_gateway_mcp(
            resolved_url,
            tool_name=TOOL_NAME,
            arguments=arguments,
            region_name=region_name,
        )
    except MarketDataError:
        raise
    except Exception as exc:
        logger.error(
            "market_data_invocation_failed",
            extra={
                "gateway_url": resolved_url,
                "error_type": type(exc).__name__,
            },
        )
        raise MarketDataError(f"Gateway invocation failed: {exc}") from exc

    logger.info(
        "market_data_invocation_completed",
        extra={
            "gateway_url": resolved_url,
            "comparables": len(payload.get("comparables") or []),
            "synthetic": bool(payload.get("synthetic")),
        },
    )

    return payload

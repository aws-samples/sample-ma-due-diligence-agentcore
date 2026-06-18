"""Custom Resource handler: attach a Policy Engine to an AgentCore Gateway.

This handler retries the ``update_gateway`` call until IAM permission
propagation completes. It is deterministic — it only signals SUCCESS
when the association is confirmed working by the API returning a
non-error response.

Environment / ResourceProperties:
- GATEWAY_ID: the Gateway identifier
- POLICY_ENGINE_ARN: the Policy Engine ARN to attach
- GATEWAY_NAME: the Gateway name (required by update_gateway)
- GATEWAY_ROLE_ARN: the Gateway service role ARN
"""

from __future__ import annotations

import json
import time
from typing import Any

import boto3
import urllib.request

# Retry config
MAX_ATTEMPTS = 12  # 12 * 5s = 60s max
RETRY_INTERVAL_SECONDS = 5


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """CloudFormation Custom Resource handler."""

    request_type = event.get("RequestType", "")
    props = event.get("ResourceProperties", {})
    response_url = event.get("ResponseURL", "")

    physical_id = props.get("GATEWAY_ID", "policy-attach")

    try:
        if request_type in ("Create", "Update"):
            _attach_policy_engine(props)
        elif request_type == "Delete":
            _detach_policy_engine(props)

        _send_response(response_url, event, "SUCCESS", physical_id)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        _send_response(
            response_url, event, "FAILED", physical_id,
            reason=f"{type(exc).__name__}: {exc}"
        )

    return {"PhysicalResourceId": physical_id}


def _attach_policy_engine(props: dict[str, Any]) -> None:
    """Retry update_gateway until IAM propagation completes."""

    gateway_id = props["GATEWAY_ID"]
    policy_engine_arn = props["POLICY_ENGINE_ARN"]
    gateway_name = props["GATEWAY_NAME"]
    gateway_role_arn = props["GATEWAY_ROLE_ARN"]

    client = boto3.client("bedrock-agentcore-control")

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            client.update_gateway(
                gatewayIdentifier=gateway_id,
                name=gateway_name,
                roleArn=gateway_role_arn,
                protocolType="MCP",
                authorizerType="AWS_IAM",
                policyEngineConfiguration={
                    "mode": "ENFORCE",
                    "arn": policy_engine_arn,
                },
            )
            print(f"SUCCESS: Policy engine attached on attempt {attempt}")
            return
        except Exception as exc:
            last_error = exc
            error_msg = str(exc)
            if "Access denied" in error_msg or "GetPolicyEngine" in error_msg:
                print(f"Attempt {attempt}/{MAX_ATTEMPTS}: IAM not propagated yet, retrying in {RETRY_INTERVAL_SECONDS}s...")
                time.sleep(RETRY_INTERVAL_SECONDS)
            else:
                # Non-IAM error — don't retry
                raise

    raise RuntimeError(
        f"Failed to attach policy engine after {MAX_ATTEMPTS} attempts. "
        f"Last error: {last_error}"
    )


def _detach_policy_engine(props: dict[str, Any]) -> None:
    """Remove policy engine association on stack deletion."""

    gateway_id = props["GATEWAY_ID"]
    gateway_name = props["GATEWAY_NAME"]
    gateway_role_arn = props["GATEWAY_ROLE_ARN"]

    client = boto3.client("bedrock-agentcore-control")

    try:
        client.update_gateway(
            gatewayIdentifier=gateway_id,
            name=gateway_name,
            roleArn=gateway_role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
        )
        print("Policy engine detached from gateway")
    except Exception as exc:
        # Best-effort on delete — don't fail the stack deletion
        print(f"WARNING: Failed to detach policy engine: {exc}")


def _send_response(
    response_url: str,
    event: dict[str, Any],
    status: str,
    physical_id: str,
    reason: str = "",
) -> None:
    """Send the CloudFormation Custom Resource response."""

    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch logs",
        "PhysicalResourceId": physical_id,
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
    }).encode("utf-8")

    req = urllib.request.Request(
        response_url,
        data=body,
        headers={"Content-Type": ""},
        method="PUT",
    )
    urllib.request.urlopen(req, timeout=30)

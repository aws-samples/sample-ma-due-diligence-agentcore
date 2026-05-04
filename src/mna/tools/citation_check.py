"""Citation-check tool — invokes the evaluator Lambda.

The Compliance Validation agent calls this tool to run the citation
evaluator against an earlier specialist response. Per design.md →
*Evaluator: Citation Check*, the canonical deployment is the Lambda at
``lambda/citation_check/handler.py``; this module is the thin boto3
wrapper the agent uses to invoke it.

A purely local check is also available via
:func:`mna.evaluators.citation_check.check_citations` — tests and the
notebook exercise that path when no deployed Lambda is reachable.

The boto3 client is imported lazily to keep ``import mna.tools``
cold-start safe (same convention as the other tool modules).
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


class CitationCheckError(RuntimeError):
    """Raised when the evaluator Lambda invocation fails."""


def _build_lambda_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 Lambda client (lazy import)."""

    import boto3  # Lazy import: never at module top level.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("lambda", **kwargs)


def _read_response_body(body: Any) -> bytes:
    """Collect a Lambda invocation ``Payload`` into bytes."""

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
    return b""


def _normalise_citations(citations: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Coerce a list of citation-like dicts into evaluator-shaped dicts."""

    if not citations:
        return []
    normalised: list[dict[str, Any]] = []
    for entry in citations:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text", "")
        source = entry.get("source", "")
        if not isinstance(text, str) or not isinstance(source, str):
            continue
        normalised.append(
            {
                "text": text,
                "source": source,
                "page": entry.get("page"),
                "score": entry.get("score"),
            }
        )
    return normalised


def check_citations_via_lambda(
    response_text: str,
    citations: list[dict[str, Any]],
    *,
    evaluator_arn: str | None = None,
    lambda_client: BaseClient | None = None,
    region_name: str | None = None,
) -> dict[str, Any]:
    """Invoke the citation-check evaluator Lambda and return the result.

    Parameters
    ----------
    response_text:
        The agent response body to evaluate. Must be a non-empty string.
    citations:
        List of citation dicts (``{"text", "source", "page", "score"}``)
        that grounded the response. May be empty — the Lambda will then
        flag every claim as unsupported.
    evaluator_arn:
        Citation-check Lambda ARN. When omitted, resolved from
        :func:`mna.config.load_config` (SSM ``/mna/evaluator/arn``).
    lambda_client:
        Optional boto3 Lambda client for dependency injection in tests.
    region_name:
        AWS region override when the function builds its own client.

    Returns
    -------
    dict
        The :class:`~mna.types.EvaluationResult`-shaped payload
        returned by the Lambda::

            {"passed": bool, "unsupported_claims": [str, ...], "total_claims": int}
    """

    if not isinstance(response_text, str) or not response_text.strip():
        raise CitationCheckError("response_text must be a non-empty string")

    if citations is not None and not isinstance(citations, list):
        raise CitationCheckError("citations must be a list when provided")

    resolved_arn = evaluator_arn
    if not resolved_arn:
        try:
            resolved_arn = load_config(region_name=region_name).evaluator_arn
        except Exception as exc:
            raise CitationCheckError(
                "evaluator_arn was not provided and could not be resolved from SSM"
            ) from exc

    if not resolved_arn:
        raise CitationCheckError("evaluator_arn resolved to an empty string")

    client = lambda_client or _build_lambda_client(region_name=region_name)

    payload = json.dumps(
        {
            "response_text": response_text,
            "citations": _normalise_citations(citations or []),
        }
    ).encode("utf-8")

    logger.info(
        "citation_check_invocation_started",
        extra={
            "evaluator_arn": resolved_arn,
            "response_length": len(response_text),
            "citation_count": len(citations or []),
        },
    )

    try:
        response = client.invoke(
            FunctionName=resolved_arn,
            InvocationType="RequestResponse",
            Payload=payload,
        )
    except Exception as exc:
        logger.error(
            "citation_check_invocation_failed",
            extra={
                "evaluator_arn": resolved_arn,
                "error_type": type(exc).__name__,
            },
        )
        raise CitationCheckError(f"Lambda invoke failed: {exc}") from exc

    function_error = response.get("FunctionError") if isinstance(response, dict) else None
    raw_body = _read_response_body(response.get("Payload")) if isinstance(response, dict) else b""

    if not raw_body:
        raise CitationCheckError("Evaluator Lambda returned an empty payload")

    text = raw_body.decode("utf-8", errors="replace").strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CitationCheckError(f"Evaluator Lambda returned non-JSON body: {exc}") from exc

    if function_error:
        # Lambda surfaces handler exceptions as a dict with
        # ``errorType`` / ``errorMessage``. Re-raise so the agent can
        # fall back to the local evaluator per design §Runtime Errors.
        raise CitationCheckError(
            f"Evaluator Lambda error ({function_error}): "
            f"{parsed.get('errorType', '')} {parsed.get('errorMessage', '')}"
        )

    if not isinstance(parsed, dict):
        raise CitationCheckError(
            f"Evaluator Lambda returned unexpected payload type {type(parsed).__name__}"
        )

    logger.info(
        "citation_check_invocation_completed",
        extra={
            "evaluator_arn": resolved_arn,
            "passed": bool(parsed.get("passed")),
            "total_claims": parsed.get("total_claims"),
            "unsupported": len(parsed.get("unsupported_claims") or []),
        },
    )

    return parsed

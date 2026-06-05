"""Shared agent invocation layer for the M&A Due Diligence sample.

This module is the single entry point used by the notebook, the CLI, and
the smoke test so that every reader surface exercises the same path
through to the deployed AgentCore Runtime.

Public API (matching design.md → "Components and Interfaces" → "Shared
Python Package: mna"):

* :func:`invoke_agent` — call the runtime and get a typed
  :class:`~mna.types.AgentResponse`.
* :func:`list_agents` — enumerate the five agent names.
* :func:`get_last_trace` — hydrate an X-Ray trace for notebook display.

The ``bedrock-agentcore`` and X-Ray boto3 clients are imported lazily so
that ``import mna`` stays cold-start safe (same pattern used by
:mod:`mna.config`).
"""

from __future__ import annotations

import json
import os
import uuid
from typing import TYPE_CHECKING, Any

from mna.config import load_config
from mna.logging_config import get_logger
from mna.types import AgentResponse, Citation

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from botocore.client import BaseClient

logger = get_logger(__name__)

# Canonical list of agents exposed by the runtime. The first entry is
# the supervisor (default qualifier when none is specified by the caller).
_SUPERVISOR = "supervisor"
_AGENT_NAMES: tuple[str, ...] = (
    _SUPERVISOR,
    "target_screening",
    "financial_analysis",
    "strategic_fit",
    "compliance_validation",
)


class ClientError(RuntimeError):
    """Raised when the shared invocation layer cannot complete a call."""


def list_agents() -> list[str]:
    """Return the static list of agent names hosted on the runtime.

    Order is stable: supervisor first, then the four specialists in the
    order documented in ``design.md``.
    """

    return list(_AGENT_NAMES)


def _build_agentcore_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for the Amazon Bedrock AgentCore data plane (lazy import)."""

    import boto3  # Lazy import keeps the package cold-start safe.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("bedrock-agentcore", **kwargs)


def _build_xray_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for AWS X-Ray (lazy import)."""

    import boto3  # Lazy import.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("xray", **kwargs)


def _build_dynamodb_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for DynamoDB (lazy import)."""

    import boto3  # Lazy import.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("dynamodb", **kwargs)


# Retention on ``mna-sessions`` rows matches the 7-day TTL the DataStack
# sets on the table (Req 2.6, 14.4). Kept as a module constant so the
# value is obvious to maintainers and easy to change in one place.
_SESSIONS_TTL_SECONDS = 7 * 24 * 60 * 60


def _resolve_sessions_table_name(region_name: str | None) -> str | None:
    """Return the DynamoDB ``mna-sessions`` table name, or ``None``.

    Resolution is **explicit opt-in**:

    1. ``MNA_SESSIONS_TABLE`` environment variable. This is the path
       taken inside the agent runtime container (``agent_stack.py``
       injects the value) and by the smoke test (which exports it
       before exercising the client).
    2. ``None`` -- meaning audit persistence is silently disabled.
       This is the path the reader's local CLI takes by default
       (no env var set), which keeps ``mna invoke`` from making an
       extra SSM round-trip on every call. Readers who want
       audit-writes from the CLI can export the env var manually
       (``export MNA_SESSIONS_TABLE=mna-sessions``) or pass a
       ``dynamodb_client`` explicitly.

    ``region_name`` is accepted for future use but not consulted
    today; kept in the signature so callers don't need to change
    when we wire richer resolution later.
    """

    _ = region_name  # Intentionally unused for now.
    env_value = os.getenv("MNA_SESSIONS_TABLE")
    if env_value:
        return env_value
    return None


def _build_sessions_item(
    *,
    session_id: str,
    agent: str,
    prompt: str,
    response: AgentResponse,
    now_epoch: int,
) -> dict[str, Any]:
    """Build the DynamoDB put_item attribute map for an invocation turn.

    Schema matches ``design.md`` → Data Model → DynamoDB:
    ``session_id`` (PK), ``turn_id`` (SK), ``prompt``, ``response``,
    ``citations``, ``trace_id``, ``evaluation``, plus the ``expires_at``
    attribute the table's TTL references.
    """

    import datetime  # noqa: PLC0415 - stdlib, lazy import for cold-start parity

    turn_id = datetime.datetime.fromtimestamp(
        now_epoch, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    citations_attr = {
        "L": [
            {
                "M": {
                    "text": {"S": c.text or ""},
                    "source": {"S": c.source or ""},
                    **({"page": {"N": str(c.page)}} if c.page is not None else {}),
                    **(
                        {"score": {"N": format(c.score, "f")}}
                        if c.score is not None
                        else {}
                    ),
                }
            }
            for c in response.citations
        ]
    }
    item: dict[str, Any] = {
        "session_id": {"S": session_id},
        "turn_id": {"S": turn_id},
        "agent": {"S": agent},
        "prompt": {"S": prompt},
        "response": {"S": response.text or ""},
        "citations": citations_attr,
        "expires_at": {"N": str(now_epoch + _SESSIONS_TTL_SECONDS)},
    }
    if response.trace_id:
        item["trace_id"] = {"S": response.trace_id}
    return item


def _record_turn(
    *,
    agent: str,
    prompt: str,
    response: AgentResponse,
    session_id: str,
    dynamodb_client: BaseClient | None,
    region_name: str | None,
) -> None:
    """Persist an invocation turn to the ``mna-sessions`` DynamoDB table.

    Best-effort: any failure is logged and swallowed. The audit write
    is not on the critical path of an agent invocation — a DynamoDB
    outage must never fail an otherwise-successful response.

    The table is write-only in v1: nothing in the sample reads back
    from it. Readers extending the sample can query directly
    (``aws dynamodb query --table-name mna-sessions ...``), export to
    S3, or add a GSI later without changing this write shape.
    """

    table_name = _resolve_sessions_table_name(region_name)
    if not table_name:
        logger.debug(
            "sessions_audit_skipped_no_table",
            extra={"agent": agent, "session_id": session_id},
        )
        return

    import time  # noqa: PLC0415 - stdlib, imported lazily to keep ``import mna`` cheap

    now_epoch = int(time.time())
    item = _build_sessions_item(
        session_id=session_id,
        agent=agent,
        prompt=prompt,
        response=response,
        now_epoch=now_epoch,
    )

    client = dynamodb_client or _build_dynamodb_client(region_name=region_name)
    try:
        client.put_item(TableName=table_name, Item=item)
        logger.info(
            "sessions_audit_recorded",
            extra={
                "table": table_name,
                "agent": agent,
                "session_id": session_id,
                "turn_id": item["turn_id"]["S"],
                "citations": len(response.citations),
            },
        )
    except Exception as exc:  # noqa: BLE001 - audit failure must never fail invocation
        logger.warning(
            "sessions_audit_write_failed",
            extra={
                "table": table_name,
                "agent": agent,
                "session_id": session_id,
                "error_type": type(exc).__name__,
            },
        )


def _read_response_body(body: Any) -> bytes:
    """Collect the runtime response body, whether streaming or not.

    ``bedrock-agentcore:InvokeAgentRuntime`` can return either a bytes
    payload (non-streaming) or a botocore ``EventStream`` / file-like
    object (streaming). This helper normalises both into a single bytes
    blob for JSON decoding.
    """

    if body is None:
        return b""
    if isinstance(body, bytes | bytearray):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")

    # botocore StreamingBody exposes .read()
    read = getattr(body, "read", None)
    if callable(read):
        data = read()
        if isinstance(data, str):
            return data.encode("utf-8")
        return bytes(data or b"")

    # EventStream-style: iterable of chunk dicts with a 'chunk' field.
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
        # Not iterable either; fall back to empty bytes.
        return b""


def _parse_response_payload(raw_body: bytes) -> dict[str, Any]:
    """Decode the runtime response body into a dict.

    Accepts either a JSON object, a JSON array of event fragments, or
    plain text. Always returns a dict so downstream parsing is uniform.
    """

    if not raw_body:
        return {}
    text = raw_body.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}

    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        # Flatten a streaming-style list of chunks into a single payload.
        aggregated_text: list[str] = []
        citations: list[Any] = []
        trace_id: str | None = None
        session_id: str | None = None
        for item in parsed:
            if not isinstance(item, dict):
                continue
            aggregated_text.append(_extract_text(item))
            citations.extend(_extract_raw_citations(item))
            trace_id = trace_id or item.get("trace_id") or item.get("traceId")
            session_id = session_id or item.get("session_id") or item.get("sessionId")
        return {
            "text": "".join(aggregated_text),
            "citations": citations,
            "trace_id": trace_id,
            "session_id": session_id,
            "events": parsed,
        }
    return {"text": str(parsed)}


def _extract_text(payload: dict[str, Any]) -> str:
    """Pull the assistant text out of a runtime payload fragment."""

    for key in ("text", "output_text", "completion", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    # Bedrock-style ``{"output": {"message": {"content": [{"text": "..."}]}}}``.
    output = payload.get("output")
    if isinstance(output, dict):
        message = output.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                return "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
        if isinstance(output.get("text"), str):
            return output["text"]
    return ""


def _extract_raw_citations(payload: dict[str, Any]) -> list[Any]:
    """Pull the list of citation-shaped dicts out of a runtime payload fragment."""

    citations = payload.get("citations")
    if isinstance(citations, list):
        return citations
    # Some runtimes nest citations under ``output`` or ``metadata``.
    for parent_key in ("output", "metadata"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict):
            nested = parent.get("citations")
            if isinstance(nested, list):
                return nested
    return []


def _coerce_citations(raw_citations: list[Any]) -> list[Citation]:
    """Convert a list of raw citation dicts into :class:`Citation` objects."""

    result: list[Citation] = []
    for entry in raw_citations:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text") or entry.get("content") or ""
        source = entry.get("source") or entry.get("uri") or entry.get("location") or ""
        if not isinstance(text, str) or not isinstance(source, str):
            continue
        if not text and not source:
            continue
        page_value = entry.get("page")
        page = int(page_value) if isinstance(page_value, int) else None
        score_value = entry.get("score")
        score = float(score_value) if isinstance(score_value, int | float) else None
        result.append(Citation(text=text, source=source, page=page, score=score))
    return result


def _resolve_qualifier(agent_name: str) -> str:
    """Validate ``agent_name`` and return it as the AgentCore qualifier.

    Raises :class:`ClientError` for unknown agents so callers get a clear
    error up-front instead of a 4xx from the runtime.
    """

    if agent_name not in _AGENT_NAMES:
        raise ClientError(f"Unknown agent '{agent_name}'. Valid names: {list(_AGENT_NAMES)}")
    return agent_name


def invoke_agent(
    agent_name: str,
    prompt: str,
    session_id: str | None = None,
    *,
    agentcore_client: BaseClient | None = None,
    region_name: str | None = None,
    runtime_arn: str | None = None,
    dynamodb_client: BaseClient | None = None,
) -> AgentResponse:
    """Invoke an agent hosted on Amazon Bedrock AgentCore Runtime.

    Parameters mirror the design doc: ``agent_name`` selects the
    specialist via the runtime ``qualifier`` (or ``"supervisor"`` when
    unspecified), ``prompt`` is the user text, and ``session_id``
    preserves AgentCore Memory continuity across turns. When ``None`` a
    fresh UUID is generated.

    The ``agentcore_client`` and ``runtime_arn`` parameters exist for
    dependency injection in tests; production callers should let the
    function resolve them from SSM.
    """

    if not agent_name:
        raise ClientError("agent_name is required")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ClientError("prompt must be a non-empty string")

    qualifier = _resolve_qualifier(agent_name)
    resolved_session_id = session_id or str(uuid.uuid4())

    if runtime_arn is None:
        runtime_arn = load_config(region_name=region_name).runtime_arn

    client = agentcore_client or _build_agentcore_client(region_name=region_name)

    # Every call lands on the supervisor's AgentCore endpoint (the
    # runtime hosts a single entrypoint: ``mna.agents.supervisor``).
    # The supervisor handler reads ``agent_name`` from the payload and
    # dispatches directly to the named specialist when set, or runs
    # the supervisor routing LLM otherwise. This avoids the need for
    # a separate AgentCore endpoint per specialist.
    payload = json.dumps(
        {
            "prompt": prompt,
            "session_id": resolved_session_id,
            "agent_name": qualifier,
        }
    ).encode("utf-8")

    logger.info(
        "agent_invocation_started",
        extra={
            "agent": qualifier,
            "session_id": resolved_session_id,
            "runtime_arn": runtime_arn,
        },
    )

    try:
        response = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=resolved_session_id,
            payload=payload,
        )
    except Exception as exc:
        logger.error(
            "agent_invocation_failed",
            extra={
                "agent": qualifier,
                "session_id": resolved_session_id,
                "error_type": type(exc).__name__,
            },
        )
        raise ClientError(f"InvokeAgentRuntime failed for {qualifier}: {exc}") from exc

    raw_body = _read_response_body(response.get("response") or response.get("payload"))
    parsed = _parse_response_payload(raw_body)

    text = _extract_text(parsed) or parsed.get("text") or ""
    citations = _coerce_citations(_extract_raw_citations(parsed) or parsed.get("citations") or [])
    trace_id = (
        parsed.get("trace_id")
        or parsed.get("traceId")
        or response.get("traceId")
        or response.get("TraceId")
    )
    resolved_response_session = (
        parsed.get("session_id")
        or parsed.get("sessionId")
        or response.get("runtimeSessionId")
        or resolved_session_id
    )

    raw = {
        # ResponseMetadata is often huge and not JSON-serialisable.
        k: v
        for k, v in response.items()
        if k not in {"ResponseMetadata", "response", "payload"}
    }
    raw["payload"] = parsed

    logger.info(
        "agent_invocation_completed",
        extra={
            "agent": qualifier,
            "session_id": resolved_response_session,
            "trace_id": trace_id,
            "citations": len(citations),
        },
    )

    agent_response = AgentResponse(
        text=text if isinstance(text, str) else str(text),
        citations=citations,
        trace_id=trace_id if isinstance(trace_id, str) else None,
        session_id=(
            resolved_response_session if isinstance(resolved_response_session, str) else None
        ),
        raw=raw,
    )

    # Best-effort audit write to ``mna-sessions``. Failure is logged
    # and swallowed so a transient DynamoDB issue never fails a good
    # invocation (Req 4.4: evaluator results stored alongside the
    # agent response for auditability; Req 2.6: DynamoDB holds
    # session + cache rows).
    _record_turn(
        agent=qualifier,
        prompt=prompt,
        response=agent_response,
        session_id=(
            resolved_response_session
            if isinstance(resolved_response_session, str) and resolved_response_session
            else resolved_session_id
        ),
        dynamodb_client=dynamodb_client,
        region_name=region_name,
    )

    return agent_response


def get_last_trace(
    trace_id: str,
    *,
    xray_client: BaseClient | None = None,
    region_name: str | None = None,
) -> dict[str, Any]:
    """Fetch the X-Ray trace data for ``trace_id``.

    The notebook's trace-inspection cell calls this to display the
    supervisor → specialist → tool call hierarchy. Returns a dict with
    two keys:

    * ``summary`` — the first (and only) ``TraceSummary`` from
      ``xray:GetTraceSummaries``.
    * ``segments`` — parsed ``Segments`` documents from
      ``xray:BatchGetTraces``, or an empty list if the trace has not yet
      been indexed.
    """

    if not trace_id:
        raise ClientError("trace_id is required")

    client = xray_client or _build_xray_client(region_name=region_name)

    try:
        summary_response = client.get_trace_summaries(TraceIds=[trace_id])
    except Exception as exc:
        logger.error(
            "xray_get_trace_summaries_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
        )
        raise ClientError(f"GetTraceSummaries failed for {trace_id}: {exc}") from exc

    summaries = list(summary_response.get("TraceSummaries") or [])
    summary = summaries[0] if summaries else {}

    try:
        batch_response = client.batch_get_traces(TraceIds=[trace_id])
    except Exception as exc:
        logger.error(
            "xray_batch_get_traces_failed",
            extra={"trace_id": trace_id, "error_type": type(exc).__name__},
        )
        raise ClientError(f"BatchGetTraces failed for {trace_id}: {exc}") from exc

    segments: list[dict[str, Any]] = []
    for trace in batch_response.get("Traces") or []:
        for segment in trace.get("Segments") or []:
            doc = segment.get("Document")
            if isinstance(doc, str):
                try:
                    segments.append(json.loads(doc))
                except json.JSONDecodeError:
                    segments.append({"raw": doc})
            elif isinstance(doc, dict):
                segments.append(doc)

    return {
        "trace_id": trace_id,
        "summary": summary,
        "segments": segments,
    }

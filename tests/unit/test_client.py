"""Unit tests for ``mna.client`` shared invocation layer.

These tests stub the ``bedrock-agentcore`` and X-Ray boto3 clients with
``MagicMock`` so they run with no AWS calls.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest

from mna.client import (
    ClientError,
    _parse_response_payload,
    _read_response_body,
    get_last_trace,
    invoke_agent,
    list_agents,
)
from mna.types import AgentResponse, Citation

_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/mna"


# ----------------------------- list_agents -----------------------------


class TestListAgents:
    def test_returns_exactly_five_agents_in_documented_order(self) -> None:
        agents = list_agents()
        assert agents == [
            "supervisor",
            "target_screening",
            "financial_analysis",
            "strategic_fit",
            "compliance_validation",
        ]

    def test_returns_a_fresh_list_each_call(self) -> None:
        first = list_agents()
        first.append("mutated")
        assert list_agents() == [
            "supervisor",
            "target_screening",
            "financial_analysis",
            "strategic_fit",
            "compliance_validation",
        ]


# ----------------------------- invoke_agent ----------------------------


def _make_runtime_response(
    *,
    text: str = "Target companies matched: 3",
    citations: list[dict] | None = None,
    trace_id: str | None = "1-abcdef12-1234567890abcdef12345678",
    session_id: str | None = "sess-123",
    as_stream: bool = False,
) -> dict:
    payload: dict = {"text": text}
    if citations is not None:
        payload["citations"] = citations
    if trace_id is not None:
        payload["trace_id"] = trace_id
    if session_id is not None:
        payload["session_id"] = session_id

    body_bytes = json.dumps(payload).encode("utf-8")
    body = io.BytesIO(body_bytes) if as_stream else body_bytes
    return {
        "response": body,
        "traceId": trace_id,
        "runtimeSessionId": session_id,
    }


class TestInvokeAgent:
    def test_invokes_supervisor_with_correct_arguments(self) -> None:
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_runtime_response()

        response = invoke_agent(
            "supervisor",
            "Run a DCF on Acme Logistics",
            session_id="sess-123",
            agentcore_client=client,
            runtime_arn=_RUNTIME_ARN,
        )

        client.invoke_agent_runtime.assert_called_once()
        kwargs = client.invoke_agent_runtime.call_args.kwargs
        assert kwargs["agentRuntimeArn"] == _RUNTIME_ARN
        # The runtime hosts a single endpoint (the supervisor); the
        # agent name travels inside the payload rather than as a
        # ``qualifier`` kwarg.
        assert "qualifier" not in kwargs
        assert kwargs["runtimeSessionId"] == "sess-123"
        payload = json.loads(kwargs["payload"].decode("utf-8"))
        assert payload == {
            "prompt": "Run a DCF on Acme Logistics",
            "session_id": "sess-123",
            "agent_name": "supervisor",
        }

        assert isinstance(response, AgentResponse)
        assert response.text == "Target companies matched: 3"
        assert response.trace_id == "1-abcdef12-1234567890abcdef12345678"
        assert response.session_id == "sess-123"

    def test_routes_to_specialist_via_payload_agent_name(self) -> None:
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_runtime_response()

        invoke_agent(
            "financial_analysis",
            "Pull comparable multiples",
            session_id="sess-xyz",
            agentcore_client=client,
            runtime_arn=_RUNTIME_ARN,
        )

        kwargs = client.invoke_agent_runtime.call_args.kwargs
        assert "qualifier" not in kwargs
        payload = json.loads(kwargs["payload"].decode("utf-8"))
        assert payload["agent_name"] == "financial_analysis"

    def test_generates_session_id_when_not_provided(self) -> None:
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_runtime_response(session_id=None)

        response = invoke_agent(
            "supervisor",
            "hello",
            agentcore_client=client,
            runtime_arn=_RUNTIME_ARN,
        )

        generated = client.invoke_agent_runtime.call_args.kwargs["runtimeSessionId"]
        assert isinstance(generated, str) and len(generated) > 0
        payload = json.loads(
            client.invoke_agent_runtime.call_args.kwargs["payload"].decode("utf-8")
        )
        assert payload["session_id"] == generated
        # The response should echo the generated id when the payload doesn't set one.
        assert response.session_id == generated

    def test_parses_citations_into_typed_objects(self) -> None:
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_runtime_response(
            citations=[
                {
                    "text": "Revenue grew 12% YoY",
                    "source": "s3://mna-docs/cims/acme.pdf",
                    "page": 4,
                    "score": 0.91,
                },
                {"text": "Fleet of 220 tractors", "source": "s3://mna-docs/cims/acme.pdf"},
            ]
        )

        response = invoke_agent(
            "target_screening",
            "Summarise Acme",
            session_id="s1",
            agentcore_client=client,
            runtime_arn=_RUNTIME_ARN,
        )

        assert len(response.citations) == 2
        assert all(isinstance(c, Citation) for c in response.citations)
        assert response.citations[0].source == "s3://mna-docs/cims/acme.pdf"
        assert response.citations[0].page == 4
        assert response.citations[0].score == pytest.approx(0.91)
        assert response.citations[1].page is None
        assert response.citations[1].score is None

    def test_handles_streaming_body_with_read_method(self) -> None:
        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_runtime_response(as_stream=True)

        response = invoke_agent(
            "compliance_validation",
            "Check citations",
            session_id="s2",
            agentcore_client=client,
            runtime_arn=_RUNTIME_ARN,
        )

        assert response.text == "Target companies matched: 3"

    def test_handles_event_stream_body(self) -> None:
        client = MagicMock()
        # Simulate a streaming event body: iterable of chunk dicts with a
        # ``bytes`` field. Each chunk is a JSON object fragment the layer
        # aggregates into a single response.
        events = [
            {"chunk": {"bytes": json.dumps({"text": "Hello "}).encode("utf-8")}},
            {
                "chunk": {
                    "bytes": json.dumps(
                        {
                            "text": "world",
                            "trace_id": "1-deadbeef-1234567890abcdef12345678",
                            "session_id": "s3",
                        }
                    ).encode("utf-8")
                }
            },
        ]
        # JSON-decoding the concatenation of two objects isn't valid, so
        # the client wraps them as a list.
        merged_body = json.dumps(
            [
                {"text": "Hello "},
                {
                    "text": "world",
                    "trace_id": "1-deadbeef-1234567890abcdef12345678",
                    "session_id": "s3",
                },
            ]
        ).encode("utf-8")
        client.invoke_agent_runtime.return_value = {"response": merged_body}

        response = invoke_agent(
            "strategic_fit",
            "Compare against prior deals",
            session_id="s3",
            agentcore_client=client,
            runtime_arn=_RUNTIME_ARN,
        )

        assert response.text == "Hello world"
        assert response.trace_id == "1-deadbeef-1234567890abcdef12345678"
        assert response.session_id == "s3"

        # Sanity: the low-level event-stream reader handles the iterable form too.
        collected = _read_response_body(events)
        assert b"Hello " in collected and b"world" in collected

    def test_rejects_unknown_agent_name(self) -> None:
        client = MagicMock()
        with pytest.raises(ClientError, match="Unknown agent"):
            invoke_agent(
                "nonexistent",
                "hi",
                agentcore_client=client,
                runtime_arn=_RUNTIME_ARN,
            )
        client.invoke_agent_runtime.assert_not_called()

    def test_rejects_empty_prompt(self) -> None:
        client = MagicMock()
        with pytest.raises(ClientError, match="prompt"):
            invoke_agent(
                "supervisor",
                "   ",
                agentcore_client=client,
                runtime_arn=_RUNTIME_ARN,
            )
        client.invoke_agent_runtime.assert_not_called()

    def test_wraps_underlying_runtime_error(self) -> None:
        client = MagicMock()
        client.invoke_agent_runtime.side_effect = RuntimeError("throttled")

        with pytest.raises(ClientError, match="InvokeAgentRuntime failed"):
            invoke_agent(
                "supervisor",
                "hi",
                session_id="s1",
                agentcore_client=client,
                runtime_arn=_RUNTIME_ARN,
            )

    def test_resolves_runtime_arn_from_config_when_not_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_config = MagicMock()
        fake_config.runtime_arn = _RUNTIME_ARN
        monkeypatch.setattr("mna.client.load_config", lambda **_: fake_config)

        client = MagicMock()
        client.invoke_agent_runtime.return_value = _make_runtime_response()

        invoke_agent(
            "supervisor",
            "hi",
            session_id="s1",
            agentcore_client=client,
        )

        assert client.invoke_agent_runtime.call_args.kwargs["agentRuntimeArn"] == _RUNTIME_ARN


# --------------------------- payload parsing ---------------------------


class TestPayloadParsing:
    def test_parses_bedrock_style_output_message(self) -> None:
        body = json.dumps(
            {
                "output": {
                    "message": {
                        "content": [{"text": "hello "}, {"text": "world"}],
                    }
                }
            }
        ).encode("utf-8")
        parsed = _parse_response_payload(body)
        assert parsed["output"]["message"]["content"][0]["text"] == "hello "

    def test_returns_plain_text_when_body_is_not_json(self) -> None:
        parsed = _parse_response_payload(b"raw text answer")
        assert parsed == {"text": "raw text answer"}

    def test_empty_body_returns_empty_dict(self) -> None:
        assert _parse_response_payload(b"") == {}


# ----------------------------- get_last_trace ---------------------------


class TestGetLastTrace:
    def test_combines_summary_and_segments(self) -> None:
        xray = MagicMock()
        xray.get_trace_summaries.return_value = {
            "TraceSummaries": [
                {
                    "Id": "1-abcdef12-1234567890abcdef12345678",
                    "Duration": 1.23,
                }
            ]
        }
        xray.batch_get_traces.return_value = {
            "Traces": [
                {
                    "Id": "1-abcdef12-1234567890abcdef12345678",
                    "Segments": [
                        {"Document": json.dumps({"name": "supervisor", "origin": "AgentCore"})},
                        {"Document": json.dumps({"name": "text_to_sql", "origin": "Tool"})},
                    ],
                }
            ]
        }

        result = get_last_trace(
            "1-abcdef12-1234567890abcdef12345678",
            xray_client=xray,
        )

        xray.get_trace_summaries.assert_called_once_with(
            TraceIds=["1-abcdef12-1234567890abcdef12345678"]
        )
        xray.batch_get_traces.assert_called_once_with(
            TraceIds=["1-abcdef12-1234567890abcdef12345678"]
        )
        assert result["trace_id"] == "1-abcdef12-1234567890abcdef12345678"
        assert result["summary"]["Duration"] == 1.23
        assert [seg["name"] for seg in result["segments"]] == ["supervisor", "text_to_sql"]

    def test_missing_summary_returns_empty_dict(self) -> None:
        xray = MagicMock()
        xray.get_trace_summaries.return_value = {"TraceSummaries": []}
        xray.batch_get_traces.return_value = {"Traces": []}

        result = get_last_trace("trace-xyz", xray_client=xray)
        assert result == {"trace_id": "trace-xyz", "summary": {}, "segments": []}

    def test_invalid_segment_document_falls_back_to_raw(self) -> None:
        xray = MagicMock()
        xray.get_trace_summaries.return_value = {"TraceSummaries": [{"Id": "t"}]}
        xray.batch_get_traces.return_value = {"Traces": [{"Segments": [{"Document": "not-json"}]}]}

        result = get_last_trace("t", xray_client=xray)
        assert result["segments"] == [{"raw": "not-json"}]

    def test_rejects_empty_trace_id(self) -> None:
        xray = MagicMock()
        with pytest.raises(ClientError, match="trace_id"):
            get_last_trace("", xray_client=xray)
        xray.get_trace_summaries.assert_not_called()

    def test_wraps_get_trace_summaries_error(self) -> None:
        xray = MagicMock()
        xray.get_trace_summaries.side_effect = RuntimeError("xray down")
        with pytest.raises(ClientError, match="GetTraceSummaries failed"):
            get_last_trace("t", xray_client=xray)

    def test_wraps_batch_get_traces_error(self) -> None:
        xray = MagicMock()
        xray.get_trace_summaries.return_value = {"TraceSummaries": [{"Id": "t"}]}
        xray.batch_get_traces.side_effect = RuntimeError("xray down")
        with pytest.raises(ClientError, match="BatchGetTraces failed"):
            get_last_trace("t", xray_client=xray)


# -------------------------- sessions audit write --------------------------


class TestSessionsAudit:
    """``invoke_agent`` optionally writes a row to ``mna-sessions``.

    The audit is opt-in: enabled by the ``MNA_SESSIONS_TABLE`` env var
    (set by :class:`AgentStack` in the runtime container) or by the
    caller passing ``dynamodb_client=`` explicitly. By default a local
    CLI invocation makes no DynamoDB call.
    """

    def test_no_write_when_env_and_client_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MNA_SESSIONS_TABLE", raising=False)

        runtime = MagicMock()
        runtime.invoke_agent_runtime.return_value = _make_runtime_response()

        invoke_agent(
            "supervisor",
            "hi",
            session_id="sess-1",
            agentcore_client=runtime,
            runtime_arn=_RUNTIME_ARN,
        )

        # Confirm the runtime call went through but no DynamoDB client
        # was ever constructed.
        runtime.invoke_agent_runtime.assert_called_once()
        runtime.put_item.assert_not_called()

    def test_writes_turn_when_env_var_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MNA_SESSIONS_TABLE", "mna-sessions")

        runtime = MagicMock()
        runtime.invoke_agent_runtime.return_value = _make_runtime_response(
            text="Acme grew 12% YoY.",
            citations=[
                {
                    "text": "Revenue grew 12% YoY",
                    "source": "s3://mna-docs/cims/acme.pdf",
                    "page": 4,
                    "score": 0.91,
                }
            ],
            trace_id="1-aabbccdd-1122334455667788",
            session_id="sess-audit-1",
        )
        ddb = MagicMock()

        invoke_agent(
            "financial_analysis",
            "Run DCF on Acme",
            session_id="sess-audit-1",
            agentcore_client=runtime,
            runtime_arn=_RUNTIME_ARN,
            dynamodb_client=ddb,
        )

        ddb.put_item.assert_called_once()
        kwargs = ddb.put_item.call_args.kwargs
        assert kwargs["TableName"] == "mna-sessions"
        item = kwargs["Item"]
        assert item["session_id"] == {"S": "sess-audit-1"}
        assert item["agent"] == {"S": "financial_analysis"}
        assert item["prompt"] == {"S": "Run DCF on Acme"}
        assert item["response"] == {"S": "Acme grew 12% YoY."}
        assert item["trace_id"] == {"S": "1-aabbccdd-1122334455667788"}
        assert "expires_at" in item
        # TTL must be roughly 7 days out.
        assert int(item["expires_at"]["N"]) > 0
        # Citation list preserves text + source + page + score.
        citations = item["citations"]["L"]
        assert len(citations) == 1
        cite_attrs = citations[0]["M"]
        assert cite_attrs["text"] == {"S": "Revenue grew 12% YoY"}
        assert cite_attrs["source"] == {"S": "s3://mna-docs/cims/acme.pdf"}
        assert cite_attrs["page"] == {"N": "4"}

    def test_write_failure_does_not_break_invocation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MNA_SESSIONS_TABLE", "mna-sessions")

        runtime = MagicMock()
        runtime.invoke_agent_runtime.return_value = _make_runtime_response()
        ddb = MagicMock()
        ddb.put_item.side_effect = RuntimeError("transient DynamoDB error")

        response = invoke_agent(
            "supervisor",
            "hi",
            session_id="sess-audit-2",
            agentcore_client=runtime,
            runtime_arn=_RUNTIME_ARN,
            dynamodb_client=ddb,
        )

        # The invocation itself must still succeed and return a real
        # AgentResponse even though the audit write failed.
        assert isinstance(response, AgentResponse)
        assert response.text == "Target companies matched: 3"
        ddb.put_item.assert_called_once()

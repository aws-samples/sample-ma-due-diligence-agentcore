"""Unit tests for ``mna.tools.citation_check`` (Lambda invocation wrapper).

Distinct from ``tests/unit/test_citation_check.py`` which exercises
the evaluator logic itself. This file verifies the thin boto3 Lambda
wrapper used by the Compliance Validation agent: payload shape,
``FunctionError`` handling, and error translation.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest

from mna.tools.citation_check import (
    CitationCheckError,
    check_citations_via_lambda,
)

_EVALUATOR_ARN = (
    "arn:aws:lambda:us-west-2:123456789012:function:mna-citation-check"
)


def _lambda_response(body: dict, function_error: str | None = None) -> dict:
    payload = json.dumps(body).encode("utf-8")
    response: dict = {
        "StatusCode": 200,
        "Payload": io.BytesIO(payload),
    }
    if function_error:
        response["FunctionError"] = function_error
    return response


def test_invokes_lambda_with_expected_payload():
    client = MagicMock()
    client.invoke.return_value = _lambda_response(
        {"passed": True, "unsupported_claims": [], "total_claims": 2}
    )

    citations = [
        {"text": "Acme had $200M revenue", "source": "s3://doc.pdf", "page": 3, "score": 0.9},
        {"text": "Acme operates in the PNW", "source": "s3://doc.pdf", "page": 4, "score": 0.8},
    ]

    result = check_citations_via_lambda(
        "Acme had $200M revenue. Acme operates in the PNW.",
        citations,
        evaluator_arn=_EVALUATOR_ARN,
        lambda_client=client,
    )

    assert result == {"passed": True, "unsupported_claims": [], "total_claims": 2}

    client.invoke.assert_called_once()
    kwargs = client.invoke.call_args.kwargs
    assert kwargs["FunctionName"] == _EVALUATOR_ARN
    assert kwargs["InvocationType"] == "RequestResponse"
    sent_payload = json.loads(kwargs["Payload"])
    assert sent_payload["response_text"].startswith("Acme had")
    assert len(sent_payload["citations"]) == 2
    assert sent_payload["citations"][0]["source"] == "s3://doc.pdf"


def test_raises_on_empty_response_text():
    with pytest.raises(CitationCheckError):
        check_citations_via_lambda("", [], evaluator_arn=_EVALUATOR_ARN)


def test_raises_on_non_list_citations():
    with pytest.raises(CitationCheckError):
        check_citations_via_lambda(
            "something",
            citations={"bad": "shape"},  # type: ignore[arg-type]
            evaluator_arn=_EVALUATOR_ARN,
        )


def test_raises_when_function_error_present():
    client = MagicMock()
    client.invoke.return_value = _lambda_response(
        {"errorType": "RuntimeError", "errorMessage": "boom"},
        function_error="Unhandled",
    )
    with pytest.raises(CitationCheckError) as excinfo:
        check_citations_via_lambda(
            "some response text", [], evaluator_arn=_EVALUATOR_ARN, lambda_client=client
        )
    assert "boom" in str(excinfo.value)


def test_raises_on_non_json_body():
    client = MagicMock()
    client.invoke.return_value = {
        "StatusCode": 200,
        "Payload": io.BytesIO(b"not-json-body"),
    }
    with pytest.raises(CitationCheckError) as excinfo:
        check_citations_via_lambda(
            "text", [], evaluator_arn=_EVALUATOR_ARN, lambda_client=client
        )
    assert "non-JSON" in str(excinfo.value)


def test_raises_on_empty_payload():
    client = MagicMock()
    client.invoke.return_value = {"StatusCode": 200, "Payload": io.BytesIO(b"")}
    with pytest.raises(CitationCheckError) as excinfo:
        check_citations_via_lambda(
            "text", [], evaluator_arn=_EVALUATOR_ARN, lambda_client=client
        )
    assert "empty payload" in str(excinfo.value)


def test_normalises_malformed_citation_entries():
    """Malformed entries are dropped; good ones forwarded."""

    client = MagicMock()
    client.invoke.return_value = _lambda_response(
        {"passed": True, "unsupported_claims": [], "total_claims": 0}
    )

    citations = [
        "not-a-dict",  # malformed, dropped
        {"text": "good", "source": "s3://x"},
        {"text": 123, "source": "s3://y"},  # non-string text, dropped
    ]

    check_citations_via_lambda(
        "response text",
        citations,  # type: ignore[arg-type]
        evaluator_arn=_EVALUATOR_ARN,
        lambda_client=client,
    )

    sent_payload = json.loads(client.invoke.call_args.kwargs["Payload"])
    assert sent_payload["citations"] == [
        {"text": "good", "source": "s3://x", "page": None, "score": None}
    ]


def test_wraps_client_exception():
    """Client errors are wrapped in CitationCheckError."""

    client = MagicMock()
    client.invoke.side_effect = RuntimeError("client blew up")
    with pytest.raises(CitationCheckError) as excinfo:
        check_citations_via_lambda(
            "text", [], evaluator_arn=_EVALUATOR_ARN, lambda_client=client
        )
    assert "Lambda invoke failed" in str(excinfo.value)

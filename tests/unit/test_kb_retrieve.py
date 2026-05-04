"""Unit tests for ``mna.tools.kb_retrieve``.

These tests stub the ``bedrock-agent-runtime`` boto3 client with
``MagicMock`` so they run with no AWS calls and no network IO.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from mna.tools.kb_retrieve import (
    KBRetrieveError,
    retrieve,
)
from mna.types import Citation

_KB_ID = "KB1234567"


# ------------------------------ helpers --------------------------------


def _s3_passage(
    *,
    text: str,
    uri: str,
    page: int | None = None,
    score: float | None = None,
    metadata_style: str = "top_level",
) -> dict:
    """Build a Retrieve ``retrievalResults`` entry with the S3 location shape.

    ``metadata_style`` toggles between the two page-number payload
    shapes Bedrock emits so the tests exercise both branches of the
    parser.
    """

    passage: dict = {
        "content": {"text": text},
        "location": {
            "type": "S3",
            "s3Location": {"uri": uri},
        },
    }
    if score is not None:
        passage["score"] = score
    if page is not None:
        if metadata_style == "document_attributes":
            passage["metadata"] = {
                "documentAttributes": [
                    {"key": "page", "value": {"numberValue": page}},
                ]
            }
        else:
            passage["metadata"] = {"page": page}
    return passage


# ----------------------- two-passage happy path ------------------------


class TestRetrieveHappyPath:
    def test_returns_citation_list_matching_retrieval_results(self) -> None:
        client = MagicMock()
        client.retrieve.return_value = {
            "retrievalResults": [
                _s3_passage(
                    text="Acme grew revenue 12% YoY to $320M.",
                    uri="s3://mna-docs/cims/acme.pdf",
                    page=4,
                    score=0.91,
                ),
                _s3_passage(
                    text="Fleet size reached 220 tractors by Q4 2024.",
                    uri="s3://mna-docs/cims/acme.pdf",
                    page=7,
                    score=0.82,
                    metadata_style="document_attributes",
                ),
            ]
        }

        citations = retrieve(
            "How fast is Acme growing?",
            kb_id=_KB_ID,
            top_k=5,
            bedrock_agent_runtime_client=client,
        )

        # Call shape matches the Bedrock API contract.
        client.retrieve.assert_called_once_with(
            knowledgeBaseId=_KB_ID,
            retrievalQuery={"text": "How fast is Acme growing?"},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": 5},
            },
        )

        assert len(citations) == 2
        assert all(isinstance(c, Citation) for c in citations)

        first, second = citations
        assert first.text == "Acme grew revenue 12% YoY to $320M."
        assert first.source == "s3://mna-docs/cims/acme.pdf"
        assert first.page == 4
        assert first.score == pytest.approx(0.91)

        assert second.text == "Fleet size reached 220 tractors by Q4 2024."
        assert second.source == "s3://mna-docs/cims/acme.pdf"
        assert second.page == 7
        assert second.score == pytest.approx(0.82)

    def test_passage_missing_score_or_page_still_returns_citation(self) -> None:
        client = MagicMock()
        client.retrieve.return_value = {
            "retrievalResults": [
                {
                    "content": {"text": "Synthetic company description."},
                    "location": {"type": "S3", "s3Location": {"uri": "s3://bkt/x.pdf"}},
                },
            ]
        }

        citations = retrieve("describe x", kb_id=_KB_ID, bedrock_agent_runtime_client=client)

        assert len(citations) == 1
        assert citations[0].text == "Synthetic company description."
        assert citations[0].source == "s3://bkt/x.pdf"
        assert citations[0].page is None
        assert citations[0].score is None

    def test_uses_default_top_k_when_not_provided(self) -> None:
        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": []}

        retrieve("anything", kb_id=_KB_ID, bedrock_agent_runtime_client=client)

        config = client.retrieve.call_args.kwargs["retrievalConfiguration"]
        assert config["vectorSearchConfiguration"]["numberOfResults"] == 5


# ----------------------------- empty case ------------------------------


class TestEmptyRetrieval:
    def test_returns_empty_list_and_logs_info(self, caplog: pytest.LogCaptureFixture) -> None:
        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": []}

        # ``mna`` configures its own handler with ``propagate=False`` so the
        # default caplog handler never sees records. Attach caplog's handler
        # directly to the tool's logger for the duration of the test.
        tool_logger = logging.getLogger("mna.tools.kb_retrieve")
        tool_logger.addHandler(caplog.handler)
        previous_level = tool_logger.level
        tool_logger.setLevel(logging.INFO)
        try:
            with caplog.at_level(logging.INFO, logger="mna.tools.kb_retrieve"):
                citations = retrieve(
                    "no matches please",
                    kb_id=_KB_ID,
                    bedrock_agent_runtime_client=client,
                )
        finally:
            tool_logger.removeHandler(caplog.handler)
            tool_logger.setLevel(previous_level)

        assert citations == []
        # The empty case must be surfaced explicitly for Requirement 1.6.
        messages = [r.getMessage() for r in caplog.records]
        assert any(m == "kb_retrieve_empty" for m in messages), (
            f"Expected 'kb_retrieve_empty' log; got: {messages}"
        )

    def test_missing_retrieval_results_key_treated_as_empty(self) -> None:
        client = MagicMock()
        client.retrieve.return_value = {}

        citations = retrieve("hello", kb_id=_KB_ID, bedrock_agent_runtime_client=client)
        assert citations == []

    def test_passages_without_text_or_source_are_skipped(self) -> None:
        client = MagicMock()
        client.retrieve.return_value = {
            "retrievalResults": [
                {},  # totally empty
                {"content": {}, "location": {}},  # no text, no uri
                _s3_passage(text="kept", uri="s3://b/x.pdf"),
            ]
        }

        citations = retrieve("q", kb_id=_KB_ID, bedrock_agent_runtime_client=client)

        assert len(citations) == 1
        assert citations[0].text == "kept"


# ------------------------- kb_id resolution ----------------------------


class TestKbIdResolution:
    def test_falls_back_to_config_when_kb_id_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_config = MagicMock()
        fake_config.kb_id = _KB_ID
        monkeypatch.setattr("mna.tools.kb_retrieve.load_config", lambda **_: fake_config)

        client = MagicMock()
        client.retrieve.return_value = {"retrievalResults": []}

        retrieve("q", bedrock_agent_runtime_client=client)

        assert client.retrieve.call_args.kwargs["knowledgeBaseId"] == _KB_ID

    def test_raises_when_config_lookup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**_: object) -> object:
            raise RuntimeError("SSM parameter /mna/kb/id not found")

        monkeypatch.setattr("mna.tools.kb_retrieve.load_config", _boom)

        client = MagicMock()
        with pytest.raises(KBRetrieveError, match="could not be resolved"):
            retrieve("q", bedrock_agent_runtime_client=client)
        client.retrieve.assert_not_called()

    def test_raises_when_resolved_kb_id_is_empty_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_config = MagicMock()
        fake_config.kb_id = ""
        monkeypatch.setattr("mna.tools.kb_retrieve.load_config", lambda **_: fake_config)

        client = MagicMock()
        with pytest.raises(KBRetrieveError, match="empty string"):
            retrieve("q", bedrock_agent_runtime_client=client)
        client.retrieve.assert_not_called()


# --------------------------- input validation --------------------------


class TestInputValidation:
    def test_rejects_empty_query(self) -> None:
        client = MagicMock()
        with pytest.raises(KBRetrieveError, match="non-empty string"):
            retrieve("   ", kb_id=_KB_ID, bedrock_agent_runtime_client=client)
        client.retrieve.assert_not_called()

    def test_rejects_non_string_query(self) -> None:
        client = MagicMock()
        with pytest.raises(KBRetrieveError, match="non-empty string"):
            retrieve(None, kb_id=_KB_ID, bedrock_agent_runtime_client=client)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad_top_k", [0, -1, 2.5, True, "5"])
    def test_rejects_invalid_top_k(self, bad_top_k: object) -> None:
        client = MagicMock()
        with pytest.raises(KBRetrieveError, match="top_k"):
            retrieve(
                "query",
                kb_id=_KB_ID,
                top_k=bad_top_k,  # type: ignore[arg-type]
                bedrock_agent_runtime_client=client,
            )
        client.retrieve.assert_not_called()


# ----------------------------- error wrapping --------------------------


class TestClientError:
    def test_wraps_underlying_client_error(self) -> None:
        client = MagicMock()
        client.retrieve.side_effect = RuntimeError("ValidationException")

        with pytest.raises(KBRetrieveError, match="Bedrock Retrieve failed"):
            retrieve("q", kb_id=_KB_ID, bedrock_agent_runtime_client=client)

"""Unit tests for ``mna.tools.memory``.

Tests stub the AgentCore boto3 client with ``MagicMock`` so they run
with no AWS calls. The primary concern beyond basic wiring is
namespace isolation (Requirements 2.3, 2.4): different namespaces must
never cross-contaminate across calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mna.tools.memory import (
    PRIOR_DEALS_NAMESPACE,
    MemoryError,
    create_memory_record,
    retrieve_memory,
    session_namespace,
)

_MEMORY_ID = "mem-abc123"


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------


class TestSessionNamespace:
    def test_builds_session_prefixed_namespace(self) -> None:
        assert session_namespace("abc") == "session_abc"
        assert session_namespace("  abc  ") == "session_abc"

    def test_rejects_empty_or_whitespace(self) -> None:
        with pytest.raises(MemoryError):
            session_namespace("")
        with pytest.raises(MemoryError):
            session_namespace("   ")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(MemoryError):
            session_namespace(None)  # type: ignore[arg-type]

    def test_prior_deals_constant_is_literal_string(self) -> None:
        # Not a method call; imported as a constant so callers can
        # reference it without worrying about typos.
        assert PRIOR_DEALS_NAMESPACE == "prior_deals"


# ---------------------------------------------------------------------------
# retrieve_memory
# ---------------------------------------------------------------------------


class TestRetrieveMemory:
    def test_calls_api_with_namespace_and_limit(self) -> None:
        client = MagicMock()
        client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "rec-1",
                    "namespace": PRIOR_DEALS_NAMESPACE,
                    "content": {"text": "Prior deal memo 1"},
                    "metadata": {"deal_id": "d-001"},
                    "score": 0.91,
                },
            ]
        }

        records = retrieve_memory(
            PRIOR_DEALS_NAMESPACE,
            query="integration risk",
            limit=5,
            memory_id=_MEMORY_ID,
            client=client,
        )

        call = client.retrieve_memory_records.call_args
        assert call.kwargs["memoryId"] == _MEMORY_ID
        assert call.kwargs["namespace"] == PRIOR_DEALS_NAMESPACE
        assert call.kwargs["maxResults"] == 5
        assert call.kwargs["searchCriteria"] == {"searchQuery": "integration risk"}

        assert len(records) == 1
        assert records[0]["id"] == "rec-1"
        assert records[0]["namespace"] == PRIOR_DEALS_NAMESPACE
        assert records[0]["content"] == "Prior deal memo 1"
        assert records[0]["metadata"] == {"deal_id": "d-001"}
        assert records[0]["score"] == pytest.approx(0.91)

    def test_omits_search_criteria_when_query_none(self) -> None:
        client = MagicMock()
        client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}

        retrieve_memory(
            session_namespace("sess-42"),
            memory_id=_MEMORY_ID,
            client=client,
        )

        kwargs = client.retrieve_memory_records.call_args.kwargs
        assert "searchCriteria" not in kwargs
        assert kwargs["namespace"] == "session_sess-42"

    def test_returns_empty_list_when_namespace_empty(self) -> None:
        client = MagicMock()
        client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}

        records = retrieve_memory(
            PRIOR_DEALS_NAMESPACE,
            memory_id=_MEMORY_ID,
            client=client,
        )
        assert records == []

    def test_drops_cross_namespace_records(self) -> None:
        # Even if the API ever leaks rows from another namespace, the
        # tool must filter them out. This is the namespace-isolation
        # guarantee (Requirements 2.3 / 2.4).
        client = MagicMock()
        client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "rec-good",
                    "namespace": PRIOR_DEALS_NAMESPACE,
                    "content": {"text": "belongs here"},
                },
                {
                    "memoryRecordId": "rec-leaked",
                    "namespace": "session_other",
                    "content": {"text": "does not belong"},
                },
            ]
        }

        records = retrieve_memory(
            PRIOR_DEALS_NAMESPACE,
            memory_id=_MEMORY_ID,
            client=client,
        )
        assert [r["id"] for r in records] == ["rec-good"]

    def test_accepts_alternative_payload_key(self) -> None:
        # Different preview SDK revisions use ``memoryRecords`` instead
        # of ``memoryRecordSummaries``. Both must be accepted.
        client = MagicMock()
        client.retrieve_memory_records.return_value = {
            "memoryRecords": [
                {
                    "id": "rec-1",
                    "namespace": "session_sess-1",
                    "content": "legacy shape",
                    "metadata": {},
                }
            ]
        }

        records = retrieve_memory(
            session_namespace("sess-1"),
            memory_id=_MEMORY_ID,
            client=client,
        )
        assert records[0]["content"] == "legacy shape"

    @pytest.mark.parametrize("bad_limit", [0, -1, 2.5, True, "5"])
    def test_rejects_invalid_limit(self, bad_limit: object) -> None:
        client = MagicMock()
        with pytest.raises(MemoryError, match="limit"):
            retrieve_memory(
                PRIOR_DEALS_NAMESPACE,
                limit=bad_limit,  # type: ignore[arg-type]
                memory_id=_MEMORY_ID,
                client=client,
            )
        client.retrieve_memory_records.assert_not_called()

    def test_rejects_empty_namespace(self) -> None:
        client = MagicMock()
        with pytest.raises(MemoryError, match="namespace"):
            retrieve_memory(
                "   ",
                memory_id=_MEMORY_ID,
                client=client,
            )
        client.retrieve_memory_records.assert_not_called()

    def test_rejects_empty_query_when_provided(self) -> None:
        client = MagicMock()
        with pytest.raises(MemoryError, match="query"):
            retrieve_memory(
                PRIOR_DEALS_NAMESPACE,
                query="   ",
                memory_id=_MEMORY_ID,
                client=client,
            )
        client.retrieve_memory_records.assert_not_called()

    def test_wraps_underlying_api_error(self) -> None:
        client = MagicMock()
        client.retrieve_memory_records.side_effect = RuntimeError("ResourceNotFoundException")

        with pytest.raises(MemoryError, match="RetrieveMemoryRecords failed"):
            retrieve_memory(
                PRIOR_DEALS_NAMESPACE,
                memory_id=_MEMORY_ID,
                client=client,
            )


# ---------------------------------------------------------------------------
# create_memory_record
# ---------------------------------------------------------------------------


class TestCreateMemoryRecord:
    def test_calls_api_with_namespace_content_and_metadata(self) -> None:
        client = MagicMock()
        client.create_memory_record.return_value = {"memoryRecordId": "rec-42"}

        result = create_memory_record(
            session_namespace("sess-1"),
            "User asked about Acme Logistics",
            metadata={"turn": 1},
            memory_id=_MEMORY_ID,
            client=client,
        )

        call = client.create_memory_record.call_args
        assert call.kwargs["memoryId"] == _MEMORY_ID
        assert call.kwargs["namespace"] == "session_sess-1"
        assert call.kwargs["content"] == {"text": "User asked about Acme Logistics"}
        assert call.kwargs["metadata"] == {"turn": 1}

        assert result == {"id": "rec-42", "namespace": "session_sess-1"}

    def test_omits_metadata_when_none(self) -> None:
        client = MagicMock()
        client.create_memory_record.return_value = {"memoryRecordId": "rec-1"}

        create_memory_record(
            PRIOR_DEALS_NAMESPACE,
            "Long-term memo body",
            memory_id=_MEMORY_ID,
            client=client,
        )

        kwargs = client.create_memory_record.call_args.kwargs
        assert "metadata" not in kwargs

    def test_rejects_empty_content(self) -> None:
        client = MagicMock()
        with pytest.raises(MemoryError, match="content"):
            create_memory_record(
                PRIOR_DEALS_NAMESPACE,
                "   ",
                memory_id=_MEMORY_ID,
                client=client,
            )
        client.create_memory_record.assert_not_called()

    def test_rejects_non_dict_metadata(self) -> None:
        client = MagicMock()
        with pytest.raises(MemoryError, match="metadata"):
            create_memory_record(
                PRIOR_DEALS_NAMESPACE,
                "content",
                metadata=["not", "a", "dict"],  # type: ignore[arg-type]
                memory_id=_MEMORY_ID,
                client=client,
            )
        client.create_memory_record.assert_not_called()

    def test_rejects_empty_namespace(self) -> None:
        client = MagicMock()
        with pytest.raises(MemoryError, match="namespace"):
            create_memory_record(
                "",
                "content",
                memory_id=_MEMORY_ID,
                client=client,
            )
        client.create_memory_record.assert_not_called()

    def test_wraps_underlying_api_error(self) -> None:
        client = MagicMock()
        client.create_memory_record.side_effect = RuntimeError("ThrottlingException")

        with pytest.raises(MemoryError, match="CreateMemoryRecord failed"):
            create_memory_record(
                PRIOR_DEALS_NAMESPACE,
                "content",
                memory_id=_MEMORY_ID,
                client=client,
            )


# ---------------------------------------------------------------------------
# Namespace isolation across reads and writes
# ---------------------------------------------------------------------------


class TestNamespaceIsolation:
    def test_session_and_prior_deals_calls_use_different_namespaces(self) -> None:
        client = MagicMock()
        client.retrieve_memory_records.return_value = {"memoryRecordSummaries": []}

        retrieve_memory(
            PRIOR_DEALS_NAMESPACE, memory_id=_MEMORY_ID, client=client
        )
        retrieve_memory(
            session_namespace("sess-1"), memory_id=_MEMORY_ID, client=client
        )

        namespaces = [
            call.kwargs["namespace"] for call in client.retrieve_memory_records.call_args_list
        ]
        assert namespaces == [PRIOR_DEALS_NAMESPACE, "session_sess-1"]

    def test_create_respects_caller_namespace(self) -> None:
        client = MagicMock()
        client.create_memory_record.return_value = {"memoryRecordId": "rec-x"}

        create_memory_record(
            PRIOR_DEALS_NAMESPACE, "memo", memory_id=_MEMORY_ID, client=client
        )
        create_memory_record(
            session_namespace("sess-9"), "turn note", memory_id=_MEMORY_ID, client=client
        )

        namespaces = [
            call.kwargs["namespace"] for call in client.create_memory_record.call_args_list
        ]
        assert namespaces == [PRIOR_DEALS_NAMESPACE, "session_sess-9"]

    def test_different_sessions_isolate(self) -> None:
        # Two sessions must never share a namespace string.
        ns_a = session_namespace("sess-a")
        ns_b = session_namespace("sess-b")
        assert ns_a != ns_b
        assert ns_a != PRIOR_DEALS_NAMESPACE
        assert ns_b != PRIOR_DEALS_NAMESPACE

    def test_retrieve_filters_records_across_interleaved_sessions(self) -> None:
        """Interleaved reads from two sessions never leak records across them.

        Simulates the real-world scenario where two concurrent readers
        hit the same Memory resource. Each ``retrieve_memory`` call
        requests its own namespace, and the client-side filter must
        drop any record whose ``namespace`` field doesn't match the
        request — even if the API response erroneously includes one.
        """

        client = MagicMock()
        # Every call returns records from BOTH sessions; the filter
        # must drop the mismatched ones based on the requested ns.
        client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "rec-a",
                    "namespace": "session_sess-a",
                    "content": {"text": "a1"},
                },
                {
                    "memoryRecordId": "rec-b",
                    "namespace": "session_sess-b",
                    "content": {"text": "b1"},
                },
                {
                    "memoryRecordId": "rec-prior",
                    "namespace": PRIOR_DEALS_NAMESPACE,
                    "content": {"text": "prior memo"},
                },
            ]
        }

        records_a = retrieve_memory(
            session_namespace("sess-a"), memory_id=_MEMORY_ID, client=client
        )
        records_b = retrieve_memory(
            session_namespace("sess-b"), memory_id=_MEMORY_ID, client=client
        )
        records_prior = retrieve_memory(
            PRIOR_DEALS_NAMESPACE, memory_id=_MEMORY_ID, client=client
        )

        assert [r["id"] for r in records_a] == ["rec-a"]
        assert [r["id"] for r in records_b] == ["rec-b"]
        assert [r["id"] for r in records_prior] == ["rec-prior"]

    def test_prior_deals_reads_never_surface_session_data(self) -> None:
        """Guarantees Strategic Fit agent can't read another reader's session.

        The agent is wired to read only ``prior_deals`` (see
        ``mna.agents.strategic_fit.retrieve_memory``). Even when the
        Memory API returns session-scoped records, the namespace
        filter must strip them before the agent sees them.
        """

        client = MagicMock()
        client.retrieve_memory_records.return_value = {
            "memoryRecordSummaries": [
                {
                    "memoryRecordId": "rec-session-leak",
                    "namespace": "session_other",
                    "content": {"text": "private session data"},
                },
                {
                    "memoryRecordId": "rec-prior",
                    "namespace": PRIOR_DEALS_NAMESPACE,
                    "content": {"text": "Northwind prior deal memo"},
                },
            ]
        }

        records = retrieve_memory(
            PRIOR_DEALS_NAMESPACE,
            query="Northwind",
            memory_id=_MEMORY_ID,
            client=client,
        )
        # Only the prior_deals record survives the filter.
        assert len(records) == 1
        assert records[0]["namespace"] == PRIOR_DEALS_NAMESPACE
        assert "private session data" not in records[0]["content"]


# ---------------------------------------------------------------------------
# Memory ID resolution
# ---------------------------------------------------------------------------


class TestMemoryIdResolution:
    def test_raises_clear_error_when_memory_id_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The config doesn't currently publish a memory id, so an
        # explicit memory_id is required for now.
        fake_config = MagicMock(spec=[])  # no attributes at all
        monkeypatch.setattr("mna.tools.memory.load_config", lambda **_: fake_config)

        client = MagicMock()
        with pytest.raises(MemoryError, match="memory_id"):
            retrieve_memory(PRIOR_DEALS_NAMESPACE, client=client)
        client.retrieve_memory_records.assert_not_called()

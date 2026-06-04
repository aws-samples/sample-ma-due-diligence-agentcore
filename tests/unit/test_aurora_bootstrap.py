"""Unit tests for ``lambda/aurora_bootstrap/handler.py``.

Covers the CR safety scenarios from task 12's acceptance criteria:

* simulated ``ImportError`` (rule 1 — handler still responds FAILED)
* successful ``Create`` with every DDL statement executed in order
* schema-drift ``Update`` (idempotent reapplication, stable physical ID)
* ``Delete`` of a missing resource (no RDS Data API calls, SUCCESS)
* missing required resource property → FAILED

Tests run without touching AWS: ``boto3`` is replaced with a fake
client that records every ``execute_statement`` invocation, and
``urllib.request.urlopen`` is patched so we can inspect the body
CloudFormation would receive.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any
from unittest import mock

import pytest

# --------------------------------------------------------------------------- #
# Module loading
#
# Mirrors the pattern in ``tests/unit/test_build_trigger.py``: the
# repo's ``lambda/`` directory isn't importable as a package (the name
# clashes with the keyword), so we load the shared base + the handler
# by file path and register them under benign ``sys.modules`` names.
# --------------------------------------------------------------------------- #

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CR_COMMON_PATH = _REPO_ROOT / "lambda" / "_cr_common" / "send_response.py"
_AURORA_BOOTSTRAP_HANDLER_PATH = _REPO_ROOT / "lambda" / "aurora_bootstrap" / "handler.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def send_response_module():
    module = _load_module("cr_common_send_response_ab", _CR_COMMON_PATH)
    # Expose under the name the handler's fallback loader expects.
    sys.modules["_cr_common_send_response"] = module
    yield module
    sys.modules.pop("cr_common_send_response_ab", None)
    sys.modules.pop("_cr_common_send_response", None)


@pytest.fixture
def aurora_bootstrap_module(send_response_module):  # noqa: ARG001 - load ordering
    """Load the handler fresh per-test so decorator state does not leak."""

    module = _load_module("aurora_bootstrap_handler", _AURORA_BOOTSTRAP_HANDLER_PATH)
    yield module
    sys.modules.pop("aurora_bootstrap_handler", None)


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #


class FakeContext:
    def __init__(self) -> None:
        self.log_stream_name = "2024/01/01/[$LATEST]aurora-bootstrap"


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


class _FakeRdsDataClient:
    """Records every ``execute_statement`` call and returns a canned response."""

    def __init__(self, *, exc: Exception | None = None) -> None:
        self._exc = exc
        self.calls: list[dict] = []

    def execute_statement(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        # RDS Data API returns ``numberOfRecordsUpdated`` and related
        # fields; DDL doesn't care so we return an empty-ish response.
        return {"numberOfRecordsUpdated": 0}


def _fake_boto3_module(rds_client: _FakeRdsDataClient):
    """Return a mock exposing ``boto3.client('rds-data')`` → ``rds_client``."""

    module = mock.MagicMock()
    module.client.return_value = rds_client
    return module


_CLUSTER_ARN = (
    "arn:aws:rds:us-east-1:111122223333:cluster:mna-auroracluster-abc123"
)
_SECRET_ARN = (  # noqa: S105 - test fixture ARN, not a credential
    "arn:aws:secretsmanager:us-east-1:111122223333:secret:mna/aurora/admin-xyz"
)


_SCHEMA_SQL = """
-- SYNTHETIC DATA - NOT REAL
CREATE SCHEMA IF NOT EXISTS mna;

CREATE TABLE IF NOT EXISTS mna.target_companies (
    company_id TEXT PRIMARY KEY,
    legal_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tc_revenue
    ON mna.target_companies (company_id);
""".strip()


def _default_properties(**overrides: Any) -> dict:
    props: dict[str, Any] = {
        "ClusterArn": _CLUSTER_ARN,
        "SecretArn": _SECRET_ARN,
        "DatabaseName": "mna",
        "SchemaSql": _SCHEMA_SQL,
        "KbTableName": "mna.kb_chunks",
        "EmbeddingDim": 1024,
        "ReadOnlyRoleName": "mna_readonly",
    }
    props.update(overrides)
    return props


def _event(
    request_type: str = "Create",
    *,
    physical_id: str = "",
    properties: dict | None = None,
) -> dict:
    event: dict[str, Any] = {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource-response.example.com/presigned",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/test/guid",
        "RequestId": "req-guid",
        "LogicalResourceId": "AuroraBootstrap",
        "ResourceProperties": properties if properties is not None else _default_properties(),
    }
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    return event


def _captured_body(mock_urlopen: mock.MagicMock) -> dict:
    assert mock_urlopen.called, "expected a CloudFormation response upload"
    args, _kwargs = mock_urlopen.call_args
    request = args[0]
    return json.loads(request.data.decode("utf-8"))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestImportErrorPath:
    """Rule 1 — ImportError on ``boto3`` still produces a FAILED response."""

    def test_import_error_sends_failed(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "boto3":
                raise ImportError("simulated boto3 missing from deployment")
            return real_import(name, globals, locals, fromlist, level)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch("builtins.__import__", side_effect=fake_import),
        ):
            aurora_bootstrap_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImportError" in body["Reason"]
        assert body["PhysicalResourceId"]


class TestCreateSuccess:
    def test_create_runs_statements_in_required_order(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        fake_client = _FakeRdsDataClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]
        assert body["Data"]["ClusterArn"] == _CLUSTER_ARN
        assert body["Data"]["KbTableName"] == "mna.kb_chunks"
        assert body["Data"]["EmbeddingDim"] == "1024"
        assert body["Data"]["ReadOnlyRoleName"] == "mna_readonly"
        # Physical ID should combine cluster + database and stay short.
        assert _CLUSTER_ARN in body["PhysicalResourceId"]
        assert body["PhysicalResourceId"].endswith("-mna")

        # Every call must target the cluster + secret the caller asked for.
        assert fake_client.calls, "expected at least one RDS Data API call"
        for call in fake_client.calls:
            assert call["resourceArn"] == _CLUSTER_ARN
            assert call["secretArn"] == _SECRET_ARN
            assert call["database"] == "mna"

        # Ordering check: pgvector extension first, then schema DDL,
        # then KB table, then the read-only role DDL.
        sql_sequence = [call["sql"] for call in fake_client.calls]
        assert sql_sequence[0] == "CREATE EXTENSION IF NOT EXISTS vector"

        def _index_of(substring: str) -> int:
            for idx, stmt in enumerate(sql_sequence):
                if substring in stmt:
                    return idx
            raise AssertionError(
                f"no statement contained {substring!r}; got: {sql_sequence!r}"
            )

        schema_idx = _index_of("mna.target_companies")
        kb_table_idx = _index_of("mna.kb_chunks")
        hnsw_idx = _index_of("USING hnsw")
        fts_idx = _index_of("USING gin (to_tsvector")
        role_idx = _index_of("CREATE ROLE mna_readonly")
        grant_select_idx = _index_of("GRANT SELECT ON ALL TABLES IN SCHEMA mna")
        grant_rds_iam_idx = _index_of("GRANT rds_iam TO mna_readonly")

        # Extension must come before the table that depends on the
        # ``vector`` type. Both indexes must follow the table, and
        # role-level statements follow every index.
        assert 0 < schema_idx < kb_table_idx < hnsw_idx
        assert kb_table_idx < fts_idx
        assert max(hnsw_idx, fts_idx) < role_idx
        # Every role-level statement must follow the role's creation.
        assert role_idx < grant_rds_iam_idx
        assert role_idx < grant_select_idx

        # ``StatementsExecuted`` should match the number of API calls.
        assert body["Data"]["StatementsExecuted"] == str(len(fake_client.calls))

    def test_create_with_inline_schema_missing_still_creates_kb_table(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        """Operators can pre-apply the schema and pass an empty ``SchemaSql``."""

        fake_client = _FakeRdsDataClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties(SchemaSql="")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(
                _event("Create", properties=props),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]

        sql_sequence = [call["sql"] for call in fake_client.calls]
        assert "CREATE EXTENSION IF NOT EXISTS vector" in sql_sequence
        # KB table + HNSW vector index + GIN full-text index + role
        # statements must still be present.
        assert any("mna.kb_chunks" in sql for sql in sql_sequence)
        assert any("USING hnsw" in sql for sql in sql_sequence)
        assert any("USING gin (to_tsvector" in sql for sql in sql_sequence)
        assert any("CREATE ROLE mna_readonly" in sql for sql in sql_sequence)
        # target_companies DDL should NOT be executed when SchemaSql is empty.
        assert not any("target_companies" in sql for sql in sql_sequence)


class TestUpdateIdempotent:
    def test_update_reapplies_schema_with_stable_physical_id(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        fake_client = _FakeRdsDataClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(_event("Create"), lambda_context)
            first_body = _captured_body(mock_urlopen)
            first_id = first_body["PhysicalResourceId"]
            first_count = len(fake_client.calls)

            mock_urlopen.reset_mock()
            # Simulate a schema drift — caller adds an extra ``CREATE
            # INDEX`` to the SQL on redeploy.
            drifted_sql = _SCHEMA_SQL + (
                "\nCREATE INDEX IF NOT EXISTS idx_tc_legal_name "
                "ON mna.target_companies (legal_name);"
            )
            updated_props = _default_properties(SchemaSql=drifted_sql)
            aurora_bootstrap_module.handler(
                _event("Update", physical_id=first_id, properties=updated_props),
                lambda_context,
            )

        second_body = _captured_body(mock_urlopen)
        assert second_body["Status"] == "SUCCESS", second_body["Reason"]
        # Physical ID must not change — otherwise CloudFormation would
        # delete-old + create-new and potentially orphan state.
        assert second_body["PhysicalResourceId"] == first_id

        # The Update should have executed the drifted statement.
        update_only_calls = fake_client.calls[first_count:]
        assert any(
            "idx_tc_legal_name" in call["sql"] for call in update_only_calls
        )
        # Every statement is guarded by ``IF NOT EXISTS`` / ``IF NOT
        # EXISTS`` conditionals so reapplying the full set is safe.
        # Spot-check one of the previously-applied statements appears
        # in the Update run as well.
        assert any(
            "mna.target_companies" in call["sql"] for call in update_only_calls
        )


class TestDeleteOfMissing:
    def test_delete_is_a_no_op_success(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        fake_client = _FakeRdsDataClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(
                _event(
                    "Delete",
                    physical_id=f"aurora-bootstrap-{_CLUSTER_ARN}-mna",
                ),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # Delete must not issue any RDS Data API calls — Aurora
        # teardown is handled by the native CDK resource, not by this
        # CR.
        assert fake_client.calls == []
        assert body["PhysicalResourceId"] == f"aurora-bootstrap-{_CLUSTER_ARN}-mna"


class TestMissingProperties:
    def test_missing_cluster_arn_fails_fast(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        fake_client = _FakeRdsDataClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties()
        props.pop("ClusterArn")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(
                _event("Create", properties=props),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ClusterArn" in body["Reason"]
        # No DDL should have executed.
        assert fake_client.calls == []

    def test_missing_secret_arn_fails_fast(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        fake_client = _FakeRdsDataClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties()
        props.pop("SecretArn")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(
                _event("Create", properties=props),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "SecretArn" in body["Reason"]
        assert fake_client.calls == []

    def test_unsafe_role_name_is_rejected(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        fake_client = _FakeRdsDataClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties(ReadOnlyRoleName="evil; DROP TABLE users;--")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(
                _event("Create", properties=props),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ReadOnlyRoleName" in body["Reason"]
        # No statement should have been sent at all.
        assert fake_client.calls == []

    def test_rds_data_error_surfaces_as_failed(
        self, aurora_bootstrap_module, lambda_context
    ) -> None:
        """A mid-stream RDS Data API failure must yield a FAILED response."""

        class RdsDataClientError(Exception):
            pass

        fake_client = _FakeRdsDataClient(
            exc=RdsDataClientError("BadRequestException: syntax error at or near"),
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            aurora_bootstrap_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "RdsDataClientError" in body["Reason"]

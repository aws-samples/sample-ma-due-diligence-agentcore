"""Unit tests for ``mna.tools.text_to_sql``.

These tests stub the Bedrock runtime and RDS Data API boto3 clients
with ``MagicMock`` so they execute with no AWS calls and no network IO.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mna.tools.text_to_sql import (
    TextToSqlError,
    query,
    validate_sql,
)

_CLUSTER_ARN = "arn:aws:rds:us-east-1:123456789012:cluster:mna-aurora"
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mna-aurora-creds"


def _converse_response(sql: str) -> dict:
    """Build a Bedrock ``Converse`` response containing ``sql``."""

    return {
        "output": {
            "message": {
                "content": [{"text": sql}],
            }
        }
    }


def _execute_statement_response() -> dict:
    """Build a canonical RDS Data API ``ExecuteStatement`` response."""

    return {
        "columnMetadata": [
            {"label": "company_id"},
            {"label": "legal_name"},
            {"label": "revenue_usd"},
            {"label": "service_lines"},
        ],
        "records": [
            [
                {"stringValue": "co-001"},
                {"stringValue": "Acme Logistics"},
                {"doubleValue": 250_000_000.0},
                {"arrayValue": {"stringValues": ["ltl", "ftl"]}},
            ],
            [
                {"stringValue": "co-002"},
                {"stringValue": "Bluewave Freight"},
                {"doubleValue": 180_000_000.0},
                {"arrayValue": {"stringValues": ["ocean"]}},
            ],
        ],
    }


# ---------------------------------------------------------------------------
# validate_sql
# ---------------------------------------------------------------------------


class TestValidateSql:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM mna.target_companies",
            "  SELECT company_id FROM mna.target_companies LIMIT 5;",
            "select legal_name from mna.target_companies where revenue_usd > 100000000",
            "WITH top AS (SELECT * FROM mna.target_companies) SELECT * FROM top",
            "-- find top targets\nSELECT * FROM mna.target_companies",
            "/* comment block */ SELECT 1",
        ],
    )
    def test_accepts_select_statements(self, sql: str) -> None:
        # Should not raise and should echo back the trimmed form.
        result = validate_sql(sql)
        assert result.upper().startswith(("SELECT", "WITH"))

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO mna.target_companies VALUES ('x', 'y')",
            "UPDATE mna.target_companies SET revenue_usd = 0",
            "DELETE FROM mna.target_companies",
        ],
    )
    def test_rejects_mutations(self, sql: str) -> None:
        with pytest.raises(TextToSqlError):
            validate_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE mna.target_companies",
            "CREATE TABLE mna.evil (id TEXT)",
            "ALTER TABLE mna.target_companies ADD COLUMN evil TEXT",
            "TRUNCATE mna.target_companies",
            "GRANT ALL ON mna.target_companies TO PUBLIC",
            "REVOKE SELECT ON mna.target_companies FROM PUBLIC",
        ],
    )
    def test_rejects_ddl_and_permission_changes(self, sql: str) -> None:
        with pytest.raises(TextToSqlError):
            validate_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "BEGIN; SELECT 1; COMMIT;",
            "SELECT 1; DROP TABLE mna.target_companies;",
        ],
    )
    def test_rejects_multi_statement_bodies(self, sql: str) -> None:
        with pytest.raises(TextToSqlError):
            validate_sql(sql)

    def test_rejects_embedded_ddl_keywords(self) -> None:
        # Even when the leading keyword is SELECT, a trailing DROP must
        # be rejected because it is still a forbidden keyword.
        with pytest.raises(TextToSqlError, match="DROP"):
            validate_sql("SELECT * FROM mna.target_companies DROP TABLE foo")

    def test_rejects_empty_and_whitespace(self) -> None:
        with pytest.raises(TextToSqlError):
            validate_sql("")
        with pytest.raises(TextToSqlError):
            validate_sql("   \n  ")
        with pytest.raises(TextToSqlError):
            validate_sql("-- only a comment")

    def test_rejects_non_string_input(self) -> None:
        with pytest.raises(TextToSqlError):
            validate_sql(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# query (full flow)
# ---------------------------------------------------------------------------


class TestQueryHappyPath:
    def test_returns_sql_rows_row_count_columns(self) -> None:
        bedrock = MagicMock()
        bedrock.converse.return_value = _converse_response(
            "SELECT company_id, legal_name, revenue_usd, service_lines FROM mna.target_companies"
        )

        rds = MagicMock()
        rds.execute_statement.return_value = _execute_statement_response()

        result = query(
            "list target companies and revenue",
            cluster_arn=_CLUSTER_ARN,
            secret_arn=_SECRET_ARN,
            bedrock_client=bedrock,
            rds_data_client=rds,
        )

        assert set(result.keys()) == {"sql", "rows", "row_count", "columns"}
        assert result["sql"].upper().startswith("SELECT")
        assert result["row_count"] == 2
        assert result["columns"] == ["company_id", "legal_name", "revenue_usd", "service_lines"]

        first_row = result["rows"][0]
        assert first_row["company_id"] == "co-001"
        assert first_row["legal_name"] == "Acme Logistics"
        assert first_row["revenue_usd"] == pytest.approx(250_000_000.0)
        assert first_row["service_lines"] == ["ltl", "ftl"]

    def test_calls_rds_with_validated_sql_and_cluster_arn(self) -> None:
        bedrock = MagicMock()
        bedrock.converse.return_value = _converse_response(
            "SELECT company_id FROM mna.target_companies LIMIT 5"
        )

        rds = MagicMock()
        rds.execute_statement.return_value = {"columnMetadata": [], "records": []}

        query(
            "anything",
            cluster_arn=_CLUSTER_ARN,
            secret_arn=_SECRET_ARN,
            bedrock_client=bedrock,
            rds_data_client=rds,
        )

        call = rds.execute_statement.call_args
        assert call.kwargs["resourceArn"] == _CLUSTER_ARN
        assert call.kwargs["secretArn"] == _SECRET_ARN
        assert call.kwargs["database"] == "mna"
        assert call.kwargs["sql"].upper().startswith("SELECT")
        assert call.kwargs["includeResultMetadata"] is True

    def test_strips_markdown_fences_from_generated_sql(self) -> None:
        bedrock = MagicMock()
        bedrock.converse.return_value = _converse_response(
            "```sql\nSELECT * FROM mna.target_companies LIMIT 3\n```"
        )
        rds = MagicMock()
        rds.execute_statement.return_value = {"columnMetadata": [], "records": []}

        result = query(
            "first three companies",
            cluster_arn=_CLUSTER_ARN,
            secret_arn=_SECRET_ARN,
            bedrock_client=bedrock,
            rds_data_client=rds,
        )

        assert result["sql"].startswith("SELECT")
        assert "```" not in result["sql"]


class TestQueryRejectsGeneratedMutations:
    @pytest.mark.parametrize(
        "generated_sql",
        [
            "INSERT INTO mna.target_companies VALUES ('x','y')",
            "UPDATE mna.target_companies SET revenue_usd = 0",
            "DELETE FROM mna.target_companies",
            "DROP TABLE mna.target_companies",
            "CREATE TABLE evil (x TEXT)",
        ],
    )
    def test_raises_text_to_sql_error_when_llm_returns_non_select(
        self, generated_sql: str
    ) -> None:
        bedrock = MagicMock()
        bedrock.converse.return_value = _converse_response(generated_sql)

        rds = MagicMock()

        with pytest.raises(TextToSqlError):
            query(
                "please destroy the table",
                cluster_arn=_CLUSTER_ARN,
                secret_arn=_SECRET_ARN,
                bedrock_client=bedrock,
                rds_data_client=rds,
            )

        # RDS Data API must never be reached on a validation failure.
        rds.execute_statement.assert_not_called()

    def test_raises_when_bedrock_returns_empty_text(self) -> None:
        bedrock = MagicMock()
        bedrock.converse.return_value = {"output": {"message": {"content": [{"text": ""}]}}}

        rds = MagicMock()

        with pytest.raises(TextToSqlError, match="empty SQL response"):
            query(
                "anything",
                cluster_arn=_CLUSTER_ARN,
                secret_arn=_SECRET_ARN,
                bedrock_client=bedrock,
                rds_data_client=rds,
            )
        rds.execute_statement.assert_not_called()


class TestQueryInputValidation:
    def test_rejects_empty_natural_language(self) -> None:
        with pytest.raises(TextToSqlError, match="non-empty string"):
            query(
                "   ",
                cluster_arn=_CLUSTER_ARN,
                secret_arn=_SECRET_ARN,
                bedrock_client=MagicMock(),
                rds_data_client=MagicMock(),
            )

    def test_requires_secret_arn(self) -> None:
        with pytest.raises(TextToSqlError, match="secret_arn is required"):
            query(
                "list targets",
                cluster_arn=_CLUSTER_ARN,
                secret_arn=None,
                bedrock_client=MagicMock(),
                rds_data_client=MagicMock(),
            )

    def test_falls_back_to_config_for_cluster_arn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_config = MagicMock()
        fake_config.aurora_cluster_arn = _CLUSTER_ARN
        monkeypatch.setattr("mna.tools.text_to_sql.load_config", lambda **_: fake_config)

        bedrock = MagicMock()
        bedrock.converse.return_value = _converse_response(
            "SELECT company_id FROM mna.target_companies"
        )
        rds = MagicMock()
        rds.execute_statement.return_value = {"columnMetadata": [], "records": []}

        query(
            "list targets",
            secret_arn=_SECRET_ARN,
            bedrock_client=bedrock,
            rds_data_client=rds,
        )

        call = rds.execute_statement.call_args
        assert call.kwargs["resourceArn"] == _CLUSTER_ARN


class TestQueryExecutionErrors:
    def test_wraps_rds_execute_failure(self) -> None:
        bedrock = MagicMock()
        bedrock.converse.return_value = _converse_response(
            "SELECT company_id FROM mna.target_companies"
        )
        rds = MagicMock()
        rds.execute_statement.side_effect = RuntimeError("BadRequestException")

        with pytest.raises(TextToSqlError, match="execute_statement failed"):
            query(
                "list targets",
                cluster_arn=_CLUSTER_ARN,
                secret_arn=_SECRET_ARN,
                bedrock_client=bedrock,
                rds_data_client=rds,
            )

    def test_wraps_bedrock_runtime_failure(self) -> None:
        bedrock = MagicMock()
        bedrock.converse.side_effect = RuntimeError("ThrottlingException")

        rds = MagicMock()

        with pytest.raises(TextToSqlError, match="generation failed"):
            query(
                "list targets",
                cluster_arn=_CLUSTER_ARN,
                secret_arn=_SECRET_ARN,
                bedrock_client=bedrock,
                rds_data_client=rds,
            )
        rds.execute_statement.assert_not_called()


# ---------------------------------------------------------------------------
# RDS field coercion
# ---------------------------------------------------------------------------


class TestRdsCoercion:
    def test_null_long_and_boolean_fields_are_normalised(self) -> None:
        bedrock = MagicMock()
        bedrock.converse.return_value = _converse_response(
            "SELECT company_id, employee_count, is_active FROM mna.target_companies"
        )
        rds = MagicMock()
        rds.execute_statement.return_value = {
            "columnMetadata": [
                {"label": "company_id"},
                {"label": "employee_count"},
                {"label": "is_active"},
            ],
            "records": [
                [
                    {"stringValue": "co-003"},
                    {"longValue": 420},
                    {"booleanValue": True},
                ],
                [
                    {"stringValue": "co-004"},
                    {"isNull": True},
                    {"booleanValue": False},
                ],
            ],
        }

        result = query(
            "edge cases",
            cluster_arn=_CLUSTER_ARN,
            secret_arn=_SECRET_ARN,
            bedrock_client=bedrock,
            rds_data_client=rds,
        )

        assert result["rows"][0]["employee_count"] == 420
        assert result["rows"][0]["is_active"] is True
        assert result["rows"][1]["employee_count"] is None
        assert result["rows"][1]["is_active"] is False

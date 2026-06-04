"""Natural-language → SQL → Aurora tool for the Target Screening agent.

Implements the three-step pattern described in design.md → "Components
and Interfaces" → "Tools" → ``tools/text_to_sql.py``:

1. Ask a small Bedrock model to translate a natural-language question
   into a SQL statement, using the ``mna.target_companies`` schema as
   part of the system prompt so the LLM has the field names and types.
2. Validate the generated SQL is SELECT-only. We deliberately avoid
   pulling in a third-party SQL parser (``sqlparse`` is not pinned in
   ``requirements.txt``) and use a conservative string-based check
   instead: comment stripping + first-keyword check + rejection of any
   mutating or DDL keyword as a whole word anywhere in the body.
3. Run the validated SQL through the Amazon RDS Data API so no persistent
   DB connection is held by the agent runtime.

The generated SQL is returned in the response envelope and logged so
it appears in the X-Ray trace for auditability (Requirement 2a.3).

All AWS SDK clients are imported lazily to keep ``import mna`` cold-
start safe — same convention as :mod:`mna.tools.kb_retrieve` and
:mod:`mna.config`.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mna.config import load_config
from mna.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from botocore.client import BaseClient

logger = get_logger(__name__)

#: Default Bedrock model used to generate SQL from natural language.
#: Uses the Haiku 4.5 inference profile — same as the specialist
#: agents. The original Haiku 3.5 direct model ID was flagged Legacy
#: and the runtime role's IAM policy only covers inference-profile
#: ARNs (with ``*`` region for cross-region routing).
#: Overridable via ``MNA_TEXT_TO_SQL_MODEL`` so a reader can swap in
#: a cheaper or faster model without editing source.
DEFAULT_MODEL_ID = os.getenv(
    "MNA_TEXT_TO_SQL_MODEL",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)

#: Default logical database name. Matches the name created by the
#: Aurora schema bootstrap Custom Resource.
DEFAULT_DATABASE = "mna"

#: Path to the canonical target-company schema. Loaded once and cached
#: so every call doesn't hit the filesystem.
#:
#: Resolution differs between local dev and the container:
#:   Local:     ``<repo>/src/mna/tools/text_to_sql.py`` → parents[3] = repo root
#:   Container: ``/app/mna/tools/text_to_sql.py``       → parents[2] = /app
#: We try both and pick whichever exists.
_SCHEMA_CANDIDATES = [
    Path(__file__).resolve().parents[3] / "data" / "schemas" / "target_companies.sql",
    Path(__file__).resolve().parents[2] / "data" / "schemas" / "target_companies.sql",
]
_SCHEMA_PATH = next((p for p in _SCHEMA_CANDIDATES if p.is_file()), _SCHEMA_CANDIDATES[0])

#: Mutating / DDL / transaction keywords rejected by :func:`validate_sql`.
#: Matched as whole words (case-insensitive) anywhere in the comment-
#: stripped statement.
_FORBIDDEN_KEYWORDS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
)

#: Keywords permitted as the first token of a statement.
_ALLOWED_LEADING_KEYWORDS: tuple[str, ...] = ("SELECT", "WITH")

# Matches a single ``--`` line comment through end-of-line.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
# Matches a ``/* ... */`` block comment, non-greedy, dot-all.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
# Matches a leading ``WITH ... SELECT`` to confirm a CTE ultimately
# produces a SELECT. Checked case-insensitively on the stripped text.
_WITH_SELECT_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)


class TextToSqlError(RuntimeError):
    """Raised when SQL generation, validation, or execution fails.

    Kept separate from :class:`mna.client.ClientError` so the agent can
    surface a specific rephrase prompt when a non-SELECT is generated
    (see design.md → "Error Handling" → "Runtime Errors").
    """


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_schema() -> str:
    """Read the target-company DDL file once and cache the string.

    Returns an empty string if the file is missing rather than raising
    — the LLM still receives the tool contract in the system prompt,
    and a deployment-time schema mismatch is visible through the
    generated SQL in the trace.
    """

    try:
        return _SCHEMA_PATH.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as exc:  # pragma: no cover - defensive
        logger.warning(
            "text_to_sql_schema_missing",
            extra={"schema_path": str(_SCHEMA_PATH), "error_type": type(exc).__name__},
        )
        return ""


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------


def _strip_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments."""

    without_block = _BLOCK_COMMENT_RE.sub(" ", sql)
    without_line = _LINE_COMMENT_RE.sub(" ", without_block)
    return without_line


def _first_keyword(sql: str) -> str:
    """Return the first bare keyword of ``sql`` uppercased.

    Leading whitespace and an opening parenthesis (for ``(SELECT ...)``
    style statements) are tolerated.
    """

    stripped = sql.lstrip().lstrip("(").lstrip()
    match = re.match(r"[A-Za-z_]+", stripped)
    return match.group(0).upper() if match else ""


def validate_sql(sql: str) -> str:
    """Validate ``sql`` is a read-only SELECT statement.

    Raises :class:`TextToSqlError` for any mutation, DDL, or multi-
    statement attempt. Returns the trimmed SQL on success so callers
    can persist / log exactly what was validated.

    The check is deliberately conservative: it strips comments, rejects
    multi-statement bodies (any semicolon that isn't the terminating
    one), enforces ``SELECT`` or ``WITH ... SELECT`` as the leading
    construct, and rejects any forbidden keyword as a whole word.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise TextToSqlError("SQL must be a non-empty string")

    stripped = _strip_comments(sql).strip()
    if not stripped:
        raise TextToSqlError("SQL is empty after stripping comments")

    # Allow exactly one trailing semicolon; reject embedded ones to
    # block multi-statement attempts like ``SELECT 1; DROP TABLE ...``.
    trimmed = stripped.rstrip().rstrip(";").rstrip()
    if ";" in trimmed:
        raise TextToSqlError("SQL must not contain multiple statements")

    leading = _first_keyword(trimmed)
    if leading not in _ALLOWED_LEADING_KEYWORDS:
        raise TextToSqlError(
            f"Only SELECT/WITH statements are allowed (got {leading or '<empty>'})"
        )

    # ``WITH`` is permitted only when a SELECT follows at some point.
    if leading == "WITH" and not _WITH_SELECT_RE.search(trimmed):
        raise TextToSqlError("WITH statements must ultimately SELECT")

    # Reject forbidden keywords anywhere in the body as whole words.
    upper = trimmed.upper()
    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", upper):
            raise TextToSqlError(f"SQL contains forbidden keyword: {keyword}")

    return trimmed


# ---------------------------------------------------------------------------
# Bedrock SQL generation
# ---------------------------------------------------------------------------


def _build_bedrock_runtime_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for the Bedrock runtime (lazy import)."""

    import boto3  # Lazy import: never at module top level.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("bedrock-runtime", **kwargs)


def _build_rds_data_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 client for the RDS Data API (lazy import)."""

    import boto3  # Lazy import.

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if resolved_region:
        kwargs["region_name"] = resolved_region
    return boto3.client("rds-data", **kwargs)


_SYSTEM_PROMPT_TEMPLATE = """You are a read-only SQL assistant for the M&A Due Diligence sample.
Translate the user's natural-language question into a single PostgreSQL SELECT
statement against the schema below. Follow these rules without exception:

* Return ONLY the SQL statement. Do not wrap it in markdown fences, do not
  explain, do not prepend "SQL:" or any label.
* The statement MUST begin with SELECT or WITH. Do not emit INSERT, UPDATE,
  DELETE, DROP, CREATE, ALTER, TRUNCATE, GRANT, REVOKE, BEGIN, COMMIT, or
  ROLLBACK.
* The statement MUST reference only the ``mna`` schema objects defined below.
* Use explicit column lists rather than ``SELECT *`` when practical.
* Apply sensible LIMITs (default 50) when the user does not specify one.
* Do not use parameter placeholders; inline literals are acceptable because
  the RDS Data API quotes them on execution.

Schema:
{schema}
""".strip()


def _extract_sql_from_response(response: dict[str, Any]) -> str:
    """Pull the first text chunk out of a Bedrock ``Converse`` / ``InvokeModel`` response."""

    # Converse API shape: {"output": {"message": {"content": [{"text": "..."}]}}}
    output = response.get("output")
    if isinstance(output, dict):
        message = output.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            return text

    # InvokeModel (Anthropic messages API) shape: bytes body containing
    # ``{"content": [{"type": "text", "text": "..."}]}``.
    body = response.get("body")
    if body is not None:
        read = getattr(body, "read", None)
        raw = read() if callable(read) else body
        if isinstance(raw, bytes | bytearray):
            raw = bytes(raw).decode("utf-8", errors="replace")
        if isinstance(raw, str) and raw.strip():
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            content = decoded.get("content") if isinstance(decoded, dict) else None
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            return text

    return ""


def _normalise_generated_sql(raw: str) -> str:
    """Strip markdown fences and surrounding whitespace from generated SQL."""

    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening fence (```sql or ```).
        text = text.split("\n", 1)[1] if "\n" in text else ""
        # Drop the closing fence.
        if text.endswith("```"):
            text = text[: -len("```")]
    return text.strip().rstrip(";").strip()


def _generate_sql(
    natural_language: str,
    *,
    bedrock_client: BaseClient,
    model_id: str,
) -> str:
    """Ask Bedrock to translate ``natural_language`` into a SELECT statement."""

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(schema=_load_schema() or "(schema unavailable)")

    response = bedrock_client.converse(
        modelId=model_id,
        system=[{"text": system_prompt}],
        messages=[
            {
                "role": "user",
                "content": [{"text": natural_language}],
            }
        ],
        inferenceConfig={"maxTokens": 512, "temperature": 0.0},
    )

    text = _extract_sql_from_response(response)
    if not text:
        raise TextToSqlError("Bedrock returned an empty SQL response")

    return _normalise_generated_sql(text)


# ---------------------------------------------------------------------------
# RDS Data API execution
# ---------------------------------------------------------------------------


def _rds_field_to_python(field: dict[str, Any]) -> Any:
    """Convert a single RDS Data API ``Field`` into a Python value."""

    if not isinstance(field, dict):
        return field
    if field.get("isNull"):
        return None
    # Ordered by how frequently each variant appears in PostgreSQL results.
    for key in (
        "stringValue",
        "longValue",
        "doubleValue",
        "booleanValue",
        "blobValue",
    ):
        if key in field:
            return field[key]
    # Array values come back as ``{"arrayValue": {"stringValues": [...]}}``.
    array_value = field.get("arrayValue")
    if isinstance(array_value, dict):
        for key in (
            "stringValues",
            "longValues",
            "doubleValues",
            "booleanValues",
        ):
            if key in array_value:
                return list(array_value[key])
        return []
    return None


def _execute_sql(
    sql: str,
    *,
    rds_data_client: BaseClient,
    cluster_arn: str,
    secret_arn: str,
    database: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run ``sql`` via the RDS Data API and return rows + column names."""

    response = rds_data_client.execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=sql,
        includeResultMetadata=True,
    )

    metadata = response.get("columnMetadata") or []
    columns = [str(col.get("label") or col.get("name") or "") for col in metadata]

    rows: list[dict[str, Any]] = []
    for record in response.get("records") or []:
        if not isinstance(record, list):
            continue
        row = {
            columns[i] if i < len(columns) else f"col_{i}": _rds_field_to_python(field)
            for i, field in enumerate(record)
        }
        rows.append(row)

    return rows, columns


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def query(
    natural_language: str,
    *,
    cluster_arn: str | None = None,
    secret_arn: str | None = None,
    database: str = DEFAULT_DATABASE,
    bedrock_client: BaseClient | None = None,
    rds_data_client: BaseClient | None = None,
    model_id: str = DEFAULT_MODEL_ID,
    region_name: str | None = None,
) -> dict[str, Any]:
    """Translate a natural-language question into SQL and run it on Aurora.

    Parameters
    ----------
    natural_language:
        The user question. Must be a non-empty string.
    cluster_arn:
        Aurora cluster ARN. Resolved from :func:`mna.config.load_config`
        when omitted.
    secret_arn:
        Secrets Manager ARN holding the Aurora credentials. Resolved
        from the same SSM-backed config when omitted — note the config
        does not currently publish the secret ARN, so callers invoking
        against a real cluster must pass it explicitly. Tests inject a
        mock client and supply the value directly.
    database:
        Logical database name. Defaults to ``mna``.
    bedrock_client, rds_data_client:
        Optional boto3 clients for dependency injection. Production
        callers should let the function construct its own.
    model_id:
        Bedrock model used for SQL generation. Defaults to the small
        Haiku model.

    Returns
    -------
    dict
        ``{"sql": str, "rows": list[dict], "row_count": int,
        "columns": list[str]}``. The ``sql`` field contains the
        validated statement as it was executed; echoing it back lets
        the agent narrate its reasoning and appears in the X-Ray trace.
    """

    if not isinstance(natural_language, str) or not natural_language.strip():
        raise TextToSqlError("natural_language must be a non-empty string")

    resolved_cluster_arn = cluster_arn
    if not resolved_cluster_arn:
        try:
            resolved_cluster_arn = load_config(region_name=region_name).aurora_cluster_arn
        except Exception as exc:
            raise TextToSqlError(
                "cluster_arn was not provided and could not be resolved from SSM"
            ) from exc

    if not resolved_cluster_arn:
        raise TextToSqlError("cluster_arn resolved to an empty string")

    if not secret_arn:
        # Fall back to the env var the AgentStack injects into the
        # runtime container, then to the SSM-backed config.
        secret_arn = os.getenv("MNA_AURORA_SECRET_ARN")
    if not secret_arn:
        try:
            secret_arn = load_config(region_name=region_name).aurora_secret_arn
        except Exception:
            pass
    if not secret_arn:
        raise TextToSqlError(
            "secret_arn is required; pass it explicitly, set "
            "MNA_AURORA_SECRET_ARN, or confirm /mna/aurora/secret_arn "
            "is populated in SSM"
        )

    bedrock = bedrock_client or _build_bedrock_runtime_client(region_name=region_name)
    rds_data = rds_data_client or _build_rds_data_client(region_name=region_name)

    logger.info(
        "text_to_sql_started",
        extra={"query_length": len(natural_language), "model_id": model_id},
    )

    try:
        generated_sql = _generate_sql(
            natural_language, bedrock_client=bedrock, model_id=model_id
        )
    except TextToSqlError:
        raise
    except Exception as exc:
        logger.error(
            "text_to_sql_generation_failed",
            extra={"error_type": type(exc).__name__, "model_id": model_id},
        )
        raise TextToSqlError(f"Bedrock SQL generation failed: {exc}") from exc

    try:
        validated_sql = validate_sql(generated_sql)
    except TextToSqlError:
        # Emit the offending SQL into the trace so the agent can rephrase.
        logger.warning(
            "text_to_sql_validation_rejected",
            extra={"generated_sql": generated_sql},
        )
        raise

    logger.info(
        "text_to_sql_generated",
        extra={"sql": validated_sql, "model_id": model_id},
    )

    try:
        rows, columns = _execute_sql(
            validated_sql,
            rds_data_client=rds_data,
            cluster_arn=resolved_cluster_arn,
            secret_arn=secret_arn,
            database=database,
        )
    except Exception as exc:
        logger.error(
            "text_to_sql_execution_failed",
            extra={"error_type": type(exc).__name__, "sql": validated_sql},
        )
        raise TextToSqlError(f"RDS Data API execute_statement failed: {exc}") from exc

    logger.info(
        "text_to_sql_completed",
        extra={"row_count": len(rows), "columns": columns},
    )

    return {
        "sql": validated_sql,
        "rows": rows,
        "row_count": len(rows),
        "columns": columns,
    }

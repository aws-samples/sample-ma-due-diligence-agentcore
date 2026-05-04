"""Aurora schema bootstrap Custom Resource handler.

Brings the Aurora Serverless v2 cluster up to the state the rest of
the sample expects before any downstream resource touches it:

1. Install the ``pgvector`` extension in the ``mna`` database so the
   Bedrock Knowledge Base can persist embeddings produced by the
   Titan model (design §Data Model - Bedrock Knowledge Base).
2. Apply the structured target-company schema shipped in
   ``data/schemas/target_companies.sql`` (design §Data Model - Aurora
   PostgreSQL Schema).
3. Create the ``mna.kb_chunks`` table + HNSW index that backs the
   Knowledge Base's pgvector store (design §Data Model - Bedrock
   Knowledge Base - the exact columns the KB's ``RdsConfiguration``
   references in :mod:`infra.stacks.data_stack`).
4. Create a read-only IAM-authenticated PostgreSQL role so the agent
   runtime's text-to-SQL tool can connect with least privilege (Req
   2a.5, 14.7).

All SQL runs through the RDS Data API via
``boto3.client("rds-data").execute_statement``; no database driver
(psycopg, psycopg2) is bundled into the Lambda. The handler is
idempotent end-to-end so an Update — which CloudFormation fires on
any change to the resource properties — safely reapplies the schema
without failing on objects that already exist.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Data Model*, *Custom Resources Inventory*, and *Custom
Resource Safety Requirements*. Safety rules enforced by the shared CR
base (``lambda/_cr_common/send_response.py``):

* Rule 1 — no ``boto3`` at module scope. The handler only reaches for
  ``boto3`` after the shared base has lazy-imported it.
* Rule 2 — guaranteed response via the shared ``cr_handler`` wrapper.
* Rule 4 — ``Delete`` is a no-op success. Aurora cleanup is handled by
  the native ``aws_cdk.aws_rds.DatabaseCluster`` removal policy
  (``DESTROY``); there is nothing for this CR to tear down separately.
* Rule 6 — ``PhysicalResourceId`` is stable across ``Create``/``Update``
  so CloudFormation never treats a schema refresh as a resource
  replacement.
* Rule 7 — returned ``Data`` is limited to a handful of short strings
  (statement count, role name, cluster ARN echo) well under the 4 KB
  response cap.

Resource properties consumed (``event['ResourceProperties']``):

``ClusterArn``
    Aurora Serverless v2 cluster ARN. Required.
``SecretArn``
    Secrets Manager ARN for the cluster's admin credentials. Required.
``DatabaseName``
    PostgreSQL database name to bootstrap. Defaults to ``"mna"``.
``SchemaSql``
    Full contents of ``data/schemas/target_companies.sql``. Passed
    inline as a resource property rather than re-read from S3 so this
    CR stays self-contained (one fewer IAM policy, one fewer failure
    mode). Required, may be an empty string if the caller has already
    applied the schema out-of-band (the handler will still create the
    KB table and the read-only role).
``KbTableName``
    Fully qualified (``schema.table``) name for the Bedrock Knowledge
    Base's vector table. Defaults to ``"mna.kb_chunks"``.
``EmbeddingDim``
    Dimensionality of the embedding column. Defaults to ``1024`` to
    match Titan Text Embeddings v2.
``ReadOnlyRoleName``
    Name of the read-only PostgreSQL role to create for the agent
    runtime. Defaults to ``"mna_readonly"``.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Any

# --------------------------------------------------------------------------- #
# Shared CR base loader.
#
# Mirrors the pattern used by ``lambda/build_trigger/handler.py`` and
# ``lambda/build_waiter/handler.py``. The ``lambda`` directory is not a
# normal Python package because ``lambda`` is a keyword, so each CR
# handler loads the shared ``_cr_common/send_response.py`` module by
# explicit filesystem path. At runtime inside AWS Lambda the
# ``_cr_common`` package is deployed alongside this handler so the
# import works; the path-based fallback keeps the handler runnable in
# local tests that drive it directly.
# --------------------------------------------------------------------------- #

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_cr_common() -> Any:
    """Return the shared ``send_response`` module.

    Tries the normal package import first (works in Lambda where
    ``_cr_common/`` sits on ``sys.path``) and falls back to a
    path-based load for local unit tests.
    """

    try:
        from _cr_common import send_response as module  # type: ignore[import-not-found]

        return module
    except ImportError:
        import importlib.util

        here = pathlib.Path(__file__).resolve().parent
        candidates = [
            here.parent / "_cr_common" / "send_response.py",
            here / "_cr_common" / "send_response.py",
        ]
        for candidate in candidates:
            if candidate.is_file():
                spec = importlib.util.spec_from_file_location(
                    "_cr_common_send_response", candidate
                )
                if spec is None or spec.loader is None:  # pragma: no cover - defensive
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules.setdefault("_cr_common_send_response", module)
                spec.loader.exec_module(module)
                return module
        raise


_cr_common = _load_cr_common()
cr_handler = _cr_common.cr_handler


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# Database name. Matches the ``default_database_name`` on the Aurora
# cluster in :class:`infra.stacks.data_stack.DataStack`.
_DEFAULT_DATABASE_NAME = "mna"

# KB vector table. Must match the ``table_name`` passed to
# ``CfnKnowledgeBase.RdsConfigurationProperty`` in the DataStack.
_DEFAULT_KB_TABLE_NAME = "mna.kb_chunks"

# Titan Text Embeddings v2 produces 1024-dimensional vectors. Keep the
# ``vector(N)`` column sized to match — a mismatch causes KB
# ingestion to fail at insert time (harder to debug than a static
# template error) so the default encodes the expected contract.
_DEFAULT_EMBEDDING_DIM = 1024

# Read-only PostgreSQL role for the agent runtime's text-to-SQL tool
# (Req 2a.5). The name is deliberately short and lowercase; PostgreSQL
# folds unquoted identifiers to lowercase and the agent runtime's
# connection logic does not quote the role name.
_DEFAULT_READ_ONLY_ROLE_NAME = "mna_readonly"

# PostgreSQL identifier character class — letters, digits, and
# underscores. Used to reject role names that could be weaponized for
# SQL injection via an unquoted identifier. Matches the subset of
# valid identifiers PostgreSQL accepts without quoting; any caller
# needing a richer name can quote it themselves upstream.
_IDENTIFIER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


# --------------------------------------------------------------------------- #
# Resource-property helpers
# --------------------------------------------------------------------------- #


def _resource_properties(event: dict) -> dict:
    """Return ``ResourceProperties`` as a dict, even when absent."""

    props = event.get("ResourceProperties") or {}
    return props if isinstance(props, dict) else {}


def _required_str(props: dict, key: str) -> str:
    """Fetch a required string resource property or raise :class:`ValueError`."""

    value = props.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ResourceProperties.{key} is required")
    return value


def _optional_str(props: dict, key: str, default: str) -> str:
    """Fetch an optional string resource property, falling back to ``default``."""

    value = props.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _optional_int(props: dict, key: str, default: int) -> int:
    """Fetch an optional int-like resource property, falling back to ``default``."""

    raw = props.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"ResourceProperties.{key} must be an integer (got {raw!r})"
        ) from exc


def _validate_identifier(name: str, *, purpose: str) -> str:
    """Return ``name`` if it is a safe PostgreSQL identifier, else raise.

    This is a defense-in-depth check against CR callers that might
    pass unsanitized input into a DDL string. The DDL ``CREATE ROLE``
    and ``GRANT`` statements below interpolate the identifier directly
    (role names cannot be parameterized via ``:placeholder`` in the
    RDS Data API, unlike values), so we enforce a conservative
    allow-list here.
    """

    if not name or len(name) > 63:
        # 63 chars is the PostgreSQL default ``NAMEDATALEN - 1`` limit.
        raise ValueError(f"{purpose} {name!r} must be 1–63 characters")
    if name[0].isdigit():
        raise ValueError(f"{purpose} {name!r} must not start with a digit")
    bad = [ch for ch in name if ch not in _IDENTIFIER_CHARS]
    if bad:
        raise ValueError(
            f"{purpose} {name!r} contains disallowed characters "
            "(only ASCII letters, digits, and underscores are accepted)"
        )
    return name


def _validate_qualified_table(name: str) -> tuple[str, str]:
    """Split ``schema.table`` into validated parts.

    Raises :class:`ValueError` if the name is not of the form
    ``schema.table`` where both halves are valid identifiers.
    """

    parts = name.split(".")
    if len(parts) != 2:
        raise ValueError(
            f"KbTableName {name!r} must be 'schema.table' (got {len(parts)} parts)"
        )
    schema, table = parts
    _validate_identifier(schema, purpose="KbTableName schema")
    _validate_identifier(table, purpose="KbTableName table")
    return schema, table


# --------------------------------------------------------------------------- #
# SQL generation
# --------------------------------------------------------------------------- #


def _split_statements(sql_text: str) -> list[str]:
    """Split a semicolon-separated SQL blob into individual statements.

    Naive splitter — sufficient for the DDL we ship (no string
    literals containing semicolons, no dollar-quoted function bodies).
    Keeping the splitter simple avoids pulling in a SQL-parser
    dependency for a handful of ``CREATE TABLE`` / ``CREATE INDEX``
    statements.

    Lines beginning with ``--`` (single-line comments) are dropped
    before splitting so inline comments that happen to contain
    semicolons do not create phantom statements.
    """

    cleaned_lines: list[str] = []
    for raw in sql_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("--"):
            continue
        cleaned_lines.append(raw)
    joined = "\n".join(cleaned_lines)

    statements: list[str] = []
    for chunk in joined.split(";"):
        candidate = chunk.strip()
        if candidate:
            statements.append(candidate)
    return statements


def _kb_table_statements(
    *,
    schema: str,
    table: str,
    embedding_dim: int,
) -> list[str]:
    """Return the idempotent DDL that creates the KB vector table + index.

    The columns must match the ``field_mapping`` the Bedrock KB uses
    (see :mod:`infra.stacks.data_stack`): ``id`` (UUID primary key),
    ``chunks`` (text), ``embedding`` (``vector(N)``), and ``metadata``
    (JSONB).
    """

    fq_table = f"{schema}.{table}"
    hnsw_index_name = f"{table}_embedding_idx"
    fts_index_name = f"{table}_chunks_fts_idx"
    return [
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        (
            f"CREATE TABLE IF NOT EXISTS {fq_table} ("
            "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
            "chunks TEXT, "
            f"embedding vector({embedding_dim}), "
            "metadata JSONB"
            ")"
        ),
        (
            # HNSW is the Bedrock-recommended index type for pgvector
            # KB backing stores. ``IF NOT EXISTS`` keeps reapplication
            # idempotent across Update invocations.
            f"CREATE INDEX IF NOT EXISTS {hnsw_index_name} "
            f"ON {fq_table} USING hnsw (embedding vector_cosine_ops)"
        ),
        (
            # GIN full-text-search index on the ``chunks`` column. The
            # Bedrock KB storage-configuration validator rejects the
            # table without it ("chunks column must be indexed"). We
            # use ``simple`` rather than a language-specific
            # dictionary because the synthetic corpus mixes plain
            # English, proper nouns, and numeric tokens — ``simple``
            # keeps the index behaviour deterministic across locales.
            f"CREATE INDEX IF NOT EXISTS {fts_index_name} "
            f"ON {fq_table} USING gin (to_tsvector('simple', chunks))"
        ),
    ]


def _read_only_role_statements(*, role_name: str, schema: str) -> list[str]:
    """Return the idempotent DDL that creates the read-only DB role.

    PostgreSQL's ``CREATE ROLE`` does not support ``IF NOT EXISTS``
    directly, so the statements are wrapped in a ``DO $$ ... $$`` block
    that checks ``pg_roles`` first. All other statements
    (``GRANT``/``ALTER DEFAULT PRIVILEGES``) are naturally idempotent.

    Grants ``rds_iam`` so the role can be assumed via IAM database
    authentication (Req 14.7) — no password ever needs to travel to
    the agent runtime at invocation time.
    """

    return [
        # Create the role if it doesn't already exist. ``WITH LOGIN``
        # is required so the agent runtime can open a session.
        (
            "DO $do$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN "
            f"CREATE ROLE {role_name} WITH LOGIN; "
            "END IF; "
            "END $do$"
        ),
        # ``rds_iam`` is the AWS-managed role that unlocks IAM DB auth.
        # Granting is idempotent — redundant grants are a no-op.
        f"GRANT rds_iam TO {role_name}",
        # Schema + table read grants. ``GRANT USAGE`` is what permits
        # the role to see objects inside the schema; ``GRANT SELECT``
        # covers the current set of tables.
        f"GRANT USAGE ON SCHEMA {schema} TO {role_name}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role_name}",
        # Future tables created in the schema inherit the SELECT grant
        # automatically so the text-to-SQL tool keeps working after
        # schema evolutions.
        (
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT SELECT ON TABLES TO {role_name}"
        ),
    ]


# --------------------------------------------------------------------------- #
# RDS Data API execution
# --------------------------------------------------------------------------- #


def _execute_statements(
    boto3: Any,
    *,
    cluster_arn: str,
    secret_arn: str,
    database: str,
    statements: list[str],
) -> int:
    """Execute each DDL statement via the RDS Data API.

    Returns the number of statements that executed successfully.
    Statements run one at a time (no transaction) because PostgreSQL
    forbids ``CREATE INDEX CONCURRENTLY`` and several DDL variants
    inside a transaction — and the surrounding CR wrapper already
    provides all-or-nothing semantics from CloudFormation's
    perspective: any failure raises and the shared CR base converts
    it into a ``FAILED`` response.
    """

    client = boto3.client("rds-data")
    count = 0
    for sql in statements:
        logger.info(
            "aurora_bootstrap_executing",
            # Log only the first 200 chars so secrets that somehow
            # end up inside a DDL string (e.g., a future
            # ``CREATE ROLE ... PASSWORD '...'`` revision) do not
            # surface in CloudWatch at full fidelity.
            extra={"sql_preview": sql[:200], "database": database},
        )
        client.execute_statement(
            resourceArn=cluster_arn,
            secretArn=secret_arn,
            database=database,
            sql=sql,
        )
        count += 1
    return count


# --------------------------------------------------------------------------- #
# Dispatchers
# --------------------------------------------------------------------------- #


def _physical_id(cluster_arn: str, database: str) -> str:
    """Stable physical ID keyed to the cluster + database combination.

    CloudFormation will re-invoke the CR on any property change, but
    the physical ID stays stable as long as the target database
    doesn't change — satisfying rule 6 (physical ID stability).
    """

    # CloudFormation physical IDs are bounded at ~1 KB; an ARN plus a
    # short suffix is well under the cap. We also avoid embedding the
    # schema SQL or the role name here so a cosmetic edit to either
    # does not trigger a CloudFormation replace cycle.
    return f"aurora-bootstrap-{cluster_arn}-{database}"


def _on_create_or_update(
    event: dict,
    _context: Any,
    boto3: Any,
) -> tuple[str, dict]:
    """Apply the full bootstrap DDL idempotently.

    The same code path serves ``Create`` and ``Update`` because every
    statement is designed to be safely reapplied (``IF NOT EXISTS``,
    ``DO $$ ... END IF; END $$`` guards, grants that silently no-op
    on repeat).
    """

    props = _resource_properties(event)
    cluster_arn = _required_str(props, "ClusterArn")
    secret_arn = _required_str(props, "SecretArn")
    database = _optional_str(props, "DatabaseName", _DEFAULT_DATABASE_NAME)
    schema_sql = props.get("SchemaSql", "")
    if not isinstance(schema_sql, str):
        raise ValueError("ResourceProperties.SchemaSql must be a string")

    kb_table_fq = _optional_str(props, "KbTableName", _DEFAULT_KB_TABLE_NAME)
    embedding_dim = _optional_int(props, "EmbeddingDim", _DEFAULT_EMBEDDING_DIM)
    role_name = _optional_str(props, "ReadOnlyRoleName", _DEFAULT_READ_ONLY_ROLE_NAME)

    # Validate identifiers that we interpolate directly into DDL. The
    # RDS Data API does not support parameter placeholders for
    # identifiers (only values), so the only defense against injection
    # is this allow-list — parameters are set by CDK from hardcoded
    # strings so a malicious value here would already require an
    # account compromise, but defense-in-depth is cheap.
    kb_schema, kb_table = _validate_qualified_table(kb_table_fq)
    _validate_identifier(role_name, purpose="ReadOnlyRoleName")
    _validate_identifier(database, purpose="DatabaseName")
    if embedding_dim <= 0 or embedding_dim > 16000:
        # 16000 is the current pgvector upper bound; enforcing it here
        # produces a clear error at CloudFormation time instead of a
        # cryptic pgvector error at INSERT time.
        raise ValueError(
            f"EmbeddingDim must be between 1 and 16000 (got {embedding_dim})"
        )

    # Assemble the statement list in the required order:
    # 1. pgvector extension.
    # 2. Schema bootstrap (optional — caller can pre-apply).
    # 3. KB vector table + index.
    # 4. Read-only role + grants.
    statements: list[str] = ["CREATE EXTENSION IF NOT EXISTS vector"]
    statements.extend(_split_statements(schema_sql))
    statements.extend(
        _kb_table_statements(
            schema=kb_schema,
            table=kb_table,
            embedding_dim=embedding_dim,
        )
    )
    statements.extend(
        _read_only_role_statements(role_name=role_name, schema=kb_schema)
    )

    logger.info(
        "aurora_bootstrap_start",
        extra={
            "cluster_arn": cluster_arn,
            "database": database,
            "statement_count": len(statements),
            "kb_table": kb_table_fq,
            "embedding_dim": embedding_dim,
            "read_only_role": role_name,
        },
    )

    executed = _execute_statements(
        boto3,
        cluster_arn=cluster_arn,
        secret_arn=secret_arn,
        database=database,
        statements=statements,
    )

    data = {
        "ClusterArn": cluster_arn,
        "Database": database,
        "StatementsExecuted": str(executed),
        "KbTableName": kb_table_fq,
        "EmbeddingDim": str(embedding_dim),
        "ReadOnlyRoleName": role_name,
    }
    return _physical_id(cluster_arn, database), data


# ``Delete`` intentionally falls through to the shared CR base's
# default (no-op success, rule 4). Aurora cleanup is handled by
# ``aws_cdk.aws_rds.DatabaseCluster`` with ``RemovalPolicy.DESTROY``,
# which also removes the database, the schemas, and every object
# we created — so there is nothing for this CR to undo separately.
handler = cr_handler(
    create=_on_create_or_update,
    update=_on_create_or_update,
    delete=None,
)(_on_create_or_update)


__all__ = ["handler"]


# Match the startup-log pattern used by the build-trigger and
# build-waiter handlers so an operator tailing the log group sees a
# predictable "module loaded" marker on cold start.
if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    logger.info("aurora_bootstrap_module_loaded")

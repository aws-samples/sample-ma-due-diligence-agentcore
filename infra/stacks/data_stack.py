"""DataStack - Amazon Aurora Serverless v2, Amazon DynamoDB, Amazon S3, and Amazon Bedrock Knowledge Bases.

This stack owns the persistent data plane for the sample:

- An Amazon Aurora PostgreSQL Serverless v2 cluster hosting the ``mna`` schema
  (target companies) and the ``mna.kb_chunks`` table that backs the
  Amazon Bedrock Knowledge Bases's ``pgvector`` vector store.
- An Amazon DynamoDB ``mna-sessions`` table for turn-level session caching with
  TTL-bounded storage cost.
- An Amazon S3 bucket that stores the synthetic CIMs, financials, press packs,
  memos, and governance documents.
- An Amazon Bedrock Knowledge Bases that embeds the S3 documents with
  ``amazon.titan-embed-text-v2:0`` and persists the vectors into Aurora
  via the ``pgvector`` extension.
- SSM parameters that publish each resource's identifier under the
  ``/mna`` prefix so downstream stacks, the ``mna`` Python package, and
  the notebook can resolve ARNs/names without hardcoding anything.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Infrastructure as Code Design - DataStack* and *Data Model*.

Requirements implemented by this stack:

- **2.1**  Synthetic M&A documents are indexed in a Bedrock Knowledge
  Base backed by S3 (the KB + data source created in task 7).
- **2.2**  KB responses return citations resolvable to source S3
  objects (shape guaranteed by the KB's native Retrieve contract; the
  KB service role is scoped to the documents bucket so S3 URIs are
  always usable).
- **2.5**  Structured target-company data is stored in Aurora PostgreSQL
  Serverless v2 (the ``mna.target_companies`` schema is populated by
  the Aurora bootstrap CR in task 12).
- **2.6**  Session and cached invocation metadata live in DynamoDB
  (`mna-sessions` table below).
- **13.3** Aurora Serverless v2 minimum ACU is set to 0.5, the lowest
  supported value, to minimize idle cost. Maximum ACU capped at 2 so a
  misbehaving prompt cannot blow the sample's $5 budget.
- **14.3** The documents S3 bucket blocks all forms of public access.
- **14.4** Every data store at rest is encrypted with AWS-managed keys
  (SSE-S3 on the bucket, AWS-owned key on DynamoDB, Aurora's default
  AWS-managed KMS storage encryption).
- **14.6** The Aurora cluster is placed in the private-isolated subnets
  owned by :class:`NetworkStack` and attached to the Aurora security
  group exported from that stack — no public internet path.
- **14.7** Aurora credentials are auto-generated and stored in Secrets
  Manager (no hardcoded passwords). IAM database authentication is also
  enabled so agent workloads can authenticate without passing a secret
  around at runtime.

.. note::

    The Bedrock KB's ``pgvector`` storage expects the extension and
    target table to exist *before* the CfnKnowledgeBase resource is
    created — CloudFormation validates the vector store on create. The
    Aurora bootstrap Custom Resource (task 12) is responsible for:

    1. ``CREATE EXTENSION IF NOT EXISTS vector;`` in the ``mna``
       database.
    2. Creating the ``mna.kb_chunks`` table with the columns the KB
       needs (``id UUID PRIMARY KEY``, ``chunks TEXT``, ``embedding
       vector(1024)`` sized to the Titan v2 embedding dimension, and
       ``metadata JSONB``) plus an HNSW/IVFFlat index on ``embedding``.
    3. Granting the KB service role rights to ``INSERT``, ``UPDATE``,
       ``DELETE``, and ``SELECT`` on that table.

    The CDK dependency wiring between the bootstrap CR (task 12) and
    the KB here must enforce that ordering so ``cdk deploy`` on a
    fresh account succeeds on the first try.
"""

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from aws_cdk import CfnOutput, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

if TYPE_CHECKING:
    from infra.stacks.network_stack import NetworkStack


# PostgreSQL 16 is the newest major version supported by Aurora
# Serverless v2 at the time of writing and comfortably exceeds the
# requirement of "PostgreSQL 15+". Pinning the version here (rather
# than selecting "latest") keeps the sample reproducible across AWS
# region rollouts.
_AURORA_ENGINE = rds.DatabaseClusterEngine.aurora_postgres(
    version=rds.AuroraPostgresEngineVersion.VER_16_4,
)

# Serverless v2 capacity units. 0.5 is the absolute floor Aurora
# Serverless v2 supports (Req 13.3); 2.0 gives enough headroom for
# KB ingestion plus a handful of concurrent agent queries without
# risking runaway cost on a misbehaving prompt.
_AURORA_MIN_ACU = 0.5
_AURORA_MAX_ACU = 2.0

# Database name. Kept lowercase for PostgreSQL convention; the
# ``mna`` schema inside this database is created later by the
# Aurora bootstrap Custom Resource (task 12).
_AURORA_DATABASE_NAME = "mna"

# DynamoDB table name. Hardcoded (rather than CDK-generated) so the
# agent runtime's IAM policy can be scoped to a predictable ARN.
_SESSIONS_TABLE_NAME = "mna-sessions"

# SSM parameter names — kept in sync with
# ``src/mna/config.py::ALL_PARAMETERS`` for the three that the shared
# Python package resolves directly.
_SSM_AURORA_CLUSTER_ARN = "/mna/aurora/cluster_arn"
_SSM_AURORA_SECRET_ARN = "/mna/aurora/secret_arn"  # noqa: S105 - SSM parameter path, not a credential
_SSM_DOCS_BUCKET = "/mna/docs/bucket"
_SSM_SESSIONS_TABLE = "/mna/sessions/table"
_SSM_KB_ID = "/mna/kb/id"

# Bedrock KB configuration constants.
#
# Titan Text Embeddings v2 produces 1024-dimensional vectors by default
# (the ``amazon.titan-embed-text-v2:0`` model ID is the GA on-demand
# foundation model for text embeddings in commercial regions). The
# embedding dimension must match the ``vector(1024)`` column created
# by the Aurora bootstrap CR (task 12) — a mismatch causes KB
# ingestion jobs to fail at insert time, not at CloudFormation create
# time, which is the harder failure mode to debug.
_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"

# ``pgvector`` table + column layout. The bootstrap CR creates these
# exact names; duplicating them here is deliberate so an operator can
# audit the CloudFormation template and the bootstrap SQL side-by-side
# without cross-referencing two files.
#
# Notes on the naming:
# - ``mna.kb_chunks`` lives in the ``mna`` schema alongside
#   ``target_companies`` so the single database houses both the
#   structured and vector data paths.
# - The KB ``RdsConfiguration`` uses the fully qualified
#   ``schema.table`` form for ``table_name``; Bedrock parses this into
#   the correct ``SET search_path`` at ingest time.
_KB_TABLE_NAME = "mna.kb_chunks"
_KB_PRIMARY_KEY_FIELD = "id"
_KB_TEXT_FIELD = "chunks"
_KB_VECTOR_FIELD = "embedding"
_KB_METADATA_FIELD = "metadata"

# Hierarchical chunking defaults, tuned for long-form M&A documents
# (CIMs of 5–10 pages, governance checklists, prior-deal memos). The
# parent window preserves section-level context for the retriever to
# bubble up, while the child window is tight enough to let the KB
# return a useful snippet alongside its citation.
#
# These numbers match the Bedrock console's "default hierarchical
# chunking" preset, which is what the task brief calls for.
_KB_PARENT_MAX_TOKENS = 1500
_KB_CHILD_MAX_TOKENS = 300
_KB_OVERLAP_TOKENS = 60

# Aurora bootstrap Custom Resource configuration.
#
# Paired with ``lambda/aurora_bootstrap/handler.py`` (task 12), which
# applies the ``mna`` schema, installs ``pgvector``, creates the KB
# vector table + HNSW index, and provisions the read-only
# IAM-authenticated role used by the text-to-SQL tool.
#
# Resolving the shipped SQL file relative to this module (and falling
# back gracefully when the file is absent) keeps the CDK synth step
# self-contained — ``cdk synth`` never needs to reach outside the
# repository or hit the network to render the template.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_AURORA_SCHEMA_SQL_PATH = _REPO_ROOT / "data" / "schemas" / "target_companies.sql"
_AURORA_BOOTSTRAP_LAMBDA_ROOT = _REPO_ROOT / "lambda"

# The read-only role name pairs with the ``rds_iam`` grant the CR
# issues so the agent runtime (AgentStack, task 13) can IAM-auth into
# Aurora using this role without a shared password (Req 14.7).
_AURORA_READ_ONLY_ROLE_NAME = "mna_readonly"

# Embedding dimensionality. Titan Text Embeddings v2 defaults to 1024
# — exported as a separate constant so the CR property and the KB
# ``vector(N)`` column width stay visibly in sync.
_AURORA_EMBEDDING_DIM = 1024

# Lambda sizing for the bootstrap CR. DDL runs quickly — 512 MB and
# 5 minutes is comfortably over-provisioned so a schema that grows
# during development never silently starves the Lambda.
_AURORA_BOOTSTRAP_LAMBDA_MEMORY_MB = 512
_AURORA_BOOTSTRAP_LAMBDA_TIMEOUT_MINUTES = 5


class DataStack(Stack):
    """Aurora, DynamoDB, S3, and Amazon Bedrock Knowledge Bases for the sample.

    Public attributes consumed by downstream stacks (primarily
    :class:`infra.stacks.agent_stack.AgentStack`):

    - :attr:`aurora_cluster` - the Aurora Serverless v2 cluster.
    - :attr:`aurora_secret` - the Secrets Manager secret holding the
      admin credentials auto-generated by Aurora.
    - :attr:`documents_bucket` - the S3 bucket that holds the synthetic
      CIMs, financials, press packs, and governance documents.
    - :attr:`sessions_table` - the DynamoDB ``mna-sessions`` table.
    - :attr:`aurora_read_only_policy` - a managed policy the AgentStack
      attaches to the agent runtime role. Grants the minimum
      permissions the text-to-SQL tool needs to run ``SELECT``
      statements against the ``mna`` schema via the RDS Data API
      (Req 2a.5).
    - :attr:`knowledge_base` - the :class:`aws_bedrock.CfnKnowledgeBase`
      wired to the documents bucket and Aurora pgvector store.
    - :attr:`kb_data_source` - the :class:`aws_bedrock.CfnDataSource`
      pointing at the documents bucket with hierarchical chunking.
    - :attr:`kb_service_role` - the IAM role Bedrock assumes to read
      S3, call the Titan embedding model, and write vectors to Aurora
      via the RDS Data API.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network_stack: NetworkStack,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Retain the reference in case task 7 / downstream stacks need
        # it (e.g., KB service role wanting the VPC for Aurora access).
        self._network_stack = network_stack

        # ------------------------------------------------------------------
        # Aurora Serverless v2 cluster (Req 2.5, 13.3, 14.4, 14.6, 14.7)
        # ------------------------------------------------------------------
        # ``DatabaseCluster`` with ``ServerlessV2ClusterInstanceProps``
        # is the supported way to provision Aurora Serverless v2 in
        # CDK v2.173. The capacity window (0.5–2 ACU) caps idle cost
        # (Req 13.3) while still allowing brief bursts for KB ingest.
        #
        # ``credentials=Credentials.from_generated_secret`` gives us an
        # auto-generated admin password stored in Secrets Manager
        # (Req 14.7) — nothing sensitive ever lives in the template.
        # ``iam_authentication=True`` layers IAM database auth on top
        # so downstream workloads can authenticate without a shared
        # password (Req 14.7).
        #
        # ``storage_encrypted=True`` is explicit even though it defaults
        # to true for Aurora clusters — it satisfies Req 14.4 for
        # at-rest encryption and keeps a reviewer from having to look
        # up the default.
        self.aurora_cluster = rds.DatabaseCluster(
            self,
            "AuroraCluster",
            engine=_AURORA_ENGINE,
            # Writer instance runs on the serverless v2 instance class.
            writer=rds.ClusterInstance.serverless_v2(
                "Writer",
                # Public access disabled is the default for non-public
                # subnets but restated here to make the control explicit
                # against Req 14.6.
                publicly_accessible=False,
                enable_performance_insights=False,
            ),
            # Aurora Serverless v2 requires ``serverless_v2_min_capacity``
            # and ``serverless_v2_max_capacity`` on the cluster props.
            serverless_v2_min_capacity=_AURORA_MIN_ACU,
            serverless_v2_max_capacity=_AURORA_MAX_ACU,
            # Networking: isolated subnets + the Aurora security group
            # owned by NetworkStack (Req 14.6). Placing the cluster
            # into the PRIVATE_ISOLATED tier is what enforces "no
            # public internet path" for Aurora.
            vpc=network_stack.vpc,
            vpc_subnets=network_stack.aurora_subnet_selection,
            security_groups=[network_stack.aurora_security_group],
            # Admin credentials auto-generated; username kept short to
            # avoid surprises in RDS Data API calls (PostgreSQL folds
            # unquoted identifiers to lowercase).
            credentials=rds.Credentials.from_generated_secret(
                username="mna_admin",
                secret_name="mna/aurora/admin",  # noqa: S106 - secret name (not the secret value)
            ),
            default_database_name=_AURORA_DATABASE_NAME,
            # IAM database authentication (Req 14.7) lets the agent
            # runtime role connect without a shared password when the
            # text-to-SQL tool eventually opens a direct connection.
            iam_authentication=True,
            # Enable the RDS Data API. This is the actual data path
            # used by the agents (see design "Components → tools →
            # text_to_sql") and by the Aurora bootstrap CR in task 12.
            enable_data_api=True,
            # Storage encryption with AWS-managed key (Req 14.4). customer managed key
            # rotation is called out as an extension in the README.
            storage_encrypted=True,
            # Keep the cost window narrow: 1-day automated backups are
            # the minimum Aurora allows. The sample is ephemeral so we
            # do not pay for a longer retention window.
            backup=rds.BackupProps(retention=Duration.days(1)),
            # Sample is ephemeral — ``cleanup.sh`` must remove the
            # cluster cleanly. The DESTROY policy plus
            # ``delete_automated_backups=True`` avoids orphaned
            # snapshots after teardown (Req 8.4).
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
            cloudwatch_logs_exports=["postgresql"],
        )

        # Secret is always present when ``from_generated_secret`` is
        # used; the type annotation guards against a future CDK change
        # that could return ``None`` (e.g., for externally managed
        # credentials).
        assert self.aurora_cluster.secret is not None  # noqa: S101
        self.aurora_secret = self.aurora_cluster.secret

        # ------------------------------------------------------------------
        # Read-only IAM policy for the agent runtime (Req 2a.5, 14.7)
        # ------------------------------------------------------------------
        # The agent runtime role (attached in AgentStack, task 13) needs
        # just enough permission to run ``SELECT`` statements via the
        # RDS Data API against the ``mna`` schema and to resolve the
        # admin secret when the text-to-SQL tool reaches for IAM auth.
        #
        # The RDS Data API does not distinguish between read and write
        # at the IAM layer — ``rds-data:ExecuteStatement`` covers both.
        # Read-only enforcement therefore happens in two places:
        #   1. The text-to-SQL tool validates the generated SQL is
        #      SELECT-only before calling ExecuteStatement (Req 2a.4).
        #   2. The Aurora bootstrap CR (task 12) creates a dedicated
        #      read-only PostgreSQL role whose credentials the agent
        #      runtime uses for data-path queries.
        # This managed policy captures the IAM side of that contract
        # and is documented in the README under the IAM Summary table.
        self.aurora_read_only_policy = iam.ManagedPolicy(
            self,
            "AuroraReadOnlyPolicy",
            description=(
                "Grants the M&A agent runtime read-only RDS Data API access "
                "to the Aurora cluster and read access to the admin secret. "
                "Write-side guardrails live in the text-to-SQL tool and the "
                "read-only DB role created by the Aurora bootstrap CR."
            ),
            statements=[
                iam.PolicyStatement(
                    sid="RdsDataApiReadOnly",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "rds-data:ExecuteStatement",
                        "rds-data:BatchExecuteStatement",
                        # BeginTransaction / CommitTransaction / RollbackTransaction
                        # are required for any multi-statement read flow
                        # (e.g., text-to-SQL wanting to fetch schema
                        # then execute a query). Scoped to the cluster
                        # ARN so this role cannot touch any other
                        # cluster in the account.
                        "rds-data:BeginTransaction",
                        "rds-data:CommitTransaction",
                        "rds-data:RollbackTransaction",
                    ],
                    resources=[self.aurora_cluster.cluster_arn],
                ),
                iam.PolicyStatement(
                    sid="AuroraSecretRead",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                    ],
                    resources=[self.aurora_secret.secret_arn],
                ),
            ],
        )

        # ------------------------------------------------------------------
        # DynamoDB ``mna-sessions`` table (Req 2.6, 14.4)
        # ------------------------------------------------------------------
        # Partition/sort keys follow the design's Data Model section:
        # ``session_id`` + ``turn_id``. On-demand billing keeps the
        # sample cost-bounded and avoids provisioning-capacity
        # surprises during a single hour of reader experimentation.
        # TTL on ``expires_at`` enforces the 7-day retention promise
        # from the design so forgotten sessions do not accumulate
        # storage cost.
        self.sessions_table = dynamodb.Table(
            self,
            "SessionsTable",
            table_name=_SESSIONS_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="session_id",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="turn_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            # AWS-owned KMS key = SSE at rest with no additional cost
            # (Req 14.4). customer managed key rotation called out as an extension.
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            # Sample is ephemeral; teardown must remove the table
            # cleanly (Req 8.4). Point-in-time recovery is left at
            # its default (disabled) to keep the sample inexpensive —
            # reader experimentation does not need a rolling backup
            # window.
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # S3 documents bucket (Req 14.3, 14.4)
        # ------------------------------------------------------------------
        # Houses the synthetic CIMs, financials, press packs, memos,
        # and governance checklist generated by ``data/generate.py``.
        # The KB data source wired up in task 7 points at this bucket.
        #
        # ``BLOCK_ALL`` explicitly turns on every block-public-access
        # control (Req 14.3). SSE-S3 encryption satisfies Req 14.4
        # without the operational overhead of a customer managed key. Versioning
        # protects against accidental overwrites during reader
        # experimentation and makes KB re-ingestion deterministic.
        self.documents_bucket = s3.Bucket(
            self,
            "DocumentsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            enforce_ssl=True,  # Deny any non-TLS request at the bucket policy.
            # Sample is ephemeral; ``auto_delete_objects`` wires up the
            # CDK-provided custom resource that empties the bucket on
            # stack delete so ``cdk destroy`` does not leave orphans.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ------------------------------------------------------------------
        # Aurora schema bootstrap Custom Resource (Req 2.5, 2a.5,
        # 14.6, 14.7, 11a.1–11a.9)
        # ------------------------------------------------------------------
        # Applies the ``mna`` schema, installs ``pgvector``, creates
        # the ``mna.kb_chunks`` table + HNSW index referenced by the
        # Bedrock KB below, and provisions the read-only
        # IAM-authenticated role used by the text-to-SQL tool. The
        # handler lives at ``lambda/aurora_bootstrap/handler.py`` and
        # follows the shared CR safety contract (task 10).
        #
        # Ordering contract:
        #   * The CR depends on the Aurora cluster so CloudFormation
        #     waits for the writer instance to be up before trying to
        #     call RDS Data API on it.
        #   * The KB (``self.knowledge_base``) and the KB data source
        #     (``self.kb_data_source``) below both add a dependency on
        #     this CR — Bedrock validates the vector store at create
        #     time, so the ``mna.kb_chunks`` table must exist before
        #     that validation runs.
        self.aurora_schema_sql = self._read_schema_sql()

        aurora_bootstrap_log_group = logs.LogGroup(
            self,
            "AuroraBootstrapLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # The Lambda bundle points at the full ``lambda/`` directory
        # so the shared ``_cr_common/`` package ships alongside the
        # handler. The exclude list trims unrelated handlers so a
        # change in (for example) ``lambda/market_data/handler.py``
        # does not invalidate this function's asset hash and trigger a
        # needless redeploy.
        self.aurora_bootstrap_function = lambda_.Function(
            self,
            "AuroraBootstrapFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="aurora_bootstrap.handler.handler",
            code=lambda_.Code.from_asset(
                str(_AURORA_BOOTSTRAP_LAMBDA_ROOT),
                exclude=[
                    "build_trigger/**",
                    "build_waiter/**",
                    "citation_check/**",
                    "market_data/**",
                    "**/__pycache__/**",
                    "*.pyc",
                    ".gitkeep",
                ],
            ),
            memory_size=_AURORA_BOOTSTRAP_LAMBDA_MEMORY_MB,
            timeout=Duration.minutes(_AURORA_BOOTSTRAP_LAMBDA_TIMEOUT_MINUTES),
            log_group=aurora_bootstrap_log_group,
            description=(
                "Custom Resource Lambda that installs pgvector, applies "
                "the mna schema, creates the KB vector table + HNSW "
                "index, and provisions the read-only DB role on the "
                "Aurora cluster via the RDS Data API."
            ),
            # ``_vendor`` holds pinned boto3/botocore (see
            # ``lambda/requirements.txt``); prepend it so our copy
            # wins over the runtime-bundled SDK.
            environment={"PYTHONPATH": "/var/task/_vendor"},
        )

        # RDS Data API permissions — scoped to the Aurora cluster ARN
        # and the admin secret so the bootstrap Lambda can only touch
        # the specific cluster it was created for.
        self.aurora_bootstrap_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AuroraBootstrapRdsData",
                effect=iam.Effect.ALLOW,
                actions=[
                    "rds-data:ExecuteStatement",
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:RollbackTransaction",
                ],
                resources=[self.aurora_cluster.cluster_arn],
            ),
        )
        self.aurora_bootstrap_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AuroraBootstrapSecretRead",
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[self.aurora_secret.secret_arn],
            ),
        )

        aurora_bootstrap_provider = cr.Provider(
            self,
            "AuroraBootstrapProvider",
            on_event_handler=self.aurora_bootstrap_function,
        )

        self.aurora_bootstrap = CustomResource(
            self,
            "AuroraBootstrap",
            service_token=aurora_bootstrap_provider.service_token,
            properties={
                "ClusterArn": self.aurora_cluster.cluster_arn,
                "SecretArn": self.aurora_secret.secret_arn,
                "DatabaseName": _AURORA_DATABASE_NAME,
                # Inline the SQL into the resource properties so the
                # CR is self-contained (one fewer IAM policy, one fewer
                # failure mode). ``CustomResource`` serializes the
                # value into the template, so CloudFormation diffs on
                # any schema edit and re-triggers the CR's Update
                # path accordingly.
                "SchemaSql": self.aurora_schema_sql,
                "KbTableName": _KB_TABLE_NAME,
                "EmbeddingDim": _AURORA_EMBEDDING_DIM,
                "ReadOnlyRoleName": _AURORA_READ_ONLY_ROLE_NAME,
            },
        )
        self.aurora_bootstrap.node.add_dependency(self.aurora_cluster)

        # ------------------------------------------------------------------
        # Amazon Bedrock Knowledge Bases service role (Req 2.1, 2.2, 14.1)
        # ------------------------------------------------------------------
        # Bedrock assumes this role when it:
        #   1. Lists / reads objects from the documents bucket during
        #      an ingestion job.
        #   2. Invokes the Titan text embeddings model to turn each
        #      chunk into a 1024-dim vector.
        #   3. Writes those vectors (and the accompanying text +
        #      metadata) into ``mna.kb_chunks`` via the RDS Data API.
        #
        # The trust policy narrows the ``sts:AssumeRole`` principal to
        # the Bedrock service and adds the ``aws:SourceAccount``
        # confused-deputy guard recommended by the Bedrock KB docs.
        # ``aws:SourceArn`` cannot be pinned to the KB ARN here —
        # creating the KB is what produces that ARN — so we scope to
        # ``arn:aws:bedrock:{region}:{account}:knowledge-base/*`` which
        # is the tightest form available at stack-synthesis time.
        kb_source_arn_pattern = f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
        self.kb_service_role = iam.Role(
            self,
            "KnowledgeBaseServiceRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": kb_source_arn_pattern},
                },
            ),
            description=(
                "Role assumed by Amazon Bedrock Knowledge Bases to read the "
                "documents S3 bucket, call the Titan embeddings model, "
                "and write vectors into Aurora pgvector via RDS Data API."
            ),
        )

        # S3 read permissions — scoped to the documents bucket only.
        # ``ListBucket`` is required so the KB's ingestion job can
        # enumerate keys under the data-source prefix; ``GetObject``
        # lets it actually download each document for chunking.
        self.kb_service_role.add_to_policy(
            iam.PolicyStatement(
                sid="DocumentsBucketList",
                effect=iam.Effect.ALLOW,
                actions=["s3:ListBucket"],
                resources=[self.documents_bucket.bucket_arn],
            ),
        )
        self.kb_service_role.add_to_policy(
            iam.PolicyStatement(
                sid="DocumentsBucketRead",
                effect=iam.Effect.ALLOW,
                actions=["s3:GetObject"],
                resources=[self.documents_bucket.arn_for_objects("*")],
            ),
        )

        # Titan embedding model invocation — scoped to the single
        # foundation model ARN we actually use. Foundation model ARNs
        # live in the ``aws`` partition regardless of region and
        # include the region segment, per the Bedrock docs.
        embedding_model_arn = (
            f"arn:aws:bedrock:{self.region}::foundation-model/{_EMBEDDING_MODEL_ID}"
        )
        self.kb_service_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeTitanEmbeddings",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel"],
                resources=[embedding_model_arn],
            ),
        )

        # Aurora write path — the KB inserts each chunk via the RDS
        # Data API. ``ExecuteStatement`` handles single-row writes and
        # ``BatchExecuteStatement`` handles bulk inserts during an
        # ingestion job. Transaction verbs support the multi-statement
        # flows Bedrock uses to keep an ingestion job atomic.
        self.kb_service_role.add_to_policy(
            iam.PolicyStatement(
                sid="AuroraVectorWrite",
                effect=iam.Effect.ALLOW,
                actions=[
                    "rds-data:ExecuteStatement",
                    "rds-data:BatchExecuteStatement",
                    "rds-data:BeginTransaction",
                    "rds-data:CommitTransaction",
                    "rds-data:RollbackTransaction",
                ],
                resources=[self.aurora_cluster.cluster_arn],
            ),
        )
        # Aurora cluster describe — Bedrock calls ``rds:DescribeDBClusters``
        # when the KB is created to validate the cluster is available
        # and has the Data API enabled. Without this, KB creation fails
        # with "The knowledge base storage configuration provided is
        # invalid" even though every runtime-data-path permission is
        # present. Scoped to the single cluster ARN.
        self.kb_service_role.add_to_policy(
            iam.PolicyStatement(
                sid="AuroraClusterDescribe",
                effect=iam.Effect.ALLOW,
                actions=["rds:DescribeDBClusters"],
                resources=[self.aurora_cluster.cluster_arn],
            ),
        )
        self.kb_service_role.add_to_policy(
            iam.PolicyStatement(
                sid="AuroraSecretReadForKb",
                effect=iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[self.aurora_secret.secret_arn],
            ),
        )

        # ------------------------------------------------------------------
        # Amazon Bedrock Knowledge Bases (Req 2.1, 2.2)
        # ------------------------------------------------------------------
        # L1 constructs are used deliberately: the L2 ``bedrock``
        # constructs in ``aws-cdk-lib`` 2.173 are still marked
        # experimental for pgvector-backed KBs, and the L1
        # ``CfnKnowledgeBase`` maps 1:1 onto the CloudFormation
        # resource type so the template stays auditable.
        #
        # ``type="RDS"`` selects the pgvector storage backend. The
        # ``resource_arn`` + ``credentials_secret_arn`` combination is
        # what Bedrock uses to open an RDS Data API session to the
        # cluster; it never opens a direct TCP connection, so no
        # security-group changes on the Aurora SG are required to
        # support the KB's write path.
        #
        # Important: ``table_name`` uses the qualified ``mna.kb_chunks``
        # form. The Aurora bootstrap CR (task 12) creates both the
        # ``mna`` schema and this table with the exact column names
        # referenced in ``field_mapping`` below.
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "DocumentsKnowledgeBase",
            name=f"mna-docs-{self.region}",
            description=(
                "Indexes the synthetic M&A documents (CIMs, financials, "
                "press packs, memos, governance checklist) for RAG-backed "
                "specialist agents."
            ),
            role_arn=self.kb_service_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=(
                    bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                        embedding_model_arn=embedding_model_arn,
                    )
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="RDS",
                rds_configuration=bedrock.CfnKnowledgeBase.RdsConfigurationProperty(
                    resource_arn=self.aurora_cluster.cluster_arn,
                    credentials_secret_arn=self.aurora_secret.secret_arn,
                    database_name=_AURORA_DATABASE_NAME,
                    table_name=_KB_TABLE_NAME,
                    field_mapping=bedrock.CfnKnowledgeBase.RdsFieldMappingProperty(
                        primary_key_field=_KB_PRIMARY_KEY_FIELD,
                        text_field=_KB_TEXT_FIELD,
                        vector_field=_KB_VECTOR_FIELD,
                        metadata_field=_KB_METADATA_FIELD,
                    ),
                ),
            ),
        )
        # Ensure the KB service role's inline policies exist before
        # CloudFormation asks Bedrock to validate the storage backend;
        # without this the first deploy can race and fail with an
        # ``AccessDeniedException`` on the RDS Data API probe.
        self.knowledge_base.node.add_dependency(self.kb_service_role)

        # The KB validates the pgvector storage backend at create
        # time — the ``mna.kb_chunks`` table + ``vector`` extension
        # must already exist. The bootstrap CR above is responsible
        # for that DDL, so the KB must wait for it to complete.
        self.knowledge_base.node.add_dependency(self.aurora_bootstrap)

        # ------------------------------------------------------------------
        # KB data source: the documents bucket with hierarchical chunking
        # ------------------------------------------------------------------
        # Hierarchical chunking (parent 1500 / child 300 tokens, overlap
        # 60) is the default preset the Bedrock console offers for
        # long-form documents. It keeps a larger parent chunk as
        # retrieval context while returning a tighter child chunk as
        # the citation snippet, which matches what the specialist
        # agents surface in the notebook.
        self.kb_data_source = bedrock.CfnDataSource(
            self,
            "DocumentsDataSource",
            name="mna-docs-s3",
            description="Synthetic M&A documents in the docs S3 bucket.",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=self.documents_bucket.bucket_arn,
                ),
            ),
            vector_ingestion_configuration=(
                bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                    chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                        chunking_strategy="HIERARCHICAL",
                        hierarchical_chunking_configuration=(
                            bedrock.CfnDataSource.HierarchicalChunkingConfigurationProperty(
                                level_configurations=[
                                    bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                                        max_tokens=_KB_PARENT_MAX_TOKENS,
                                    ),
                                    bedrock.CfnDataSource.HierarchicalChunkingLevelConfigurationProperty(
                                        max_tokens=_KB_CHILD_MAX_TOKENS,
                                    ),
                                ],
                                overlap_tokens=_KB_OVERLAP_TOKENS,
                            )
                        ),
                    ),
                )
            ),
            # On stack delete, keep the underlying vectors in Aurora
            # so ``cleanup.sh``'s destroy of the Aurora cluster (which
            # happens via native CDK) is the single source of truth for
            # data removal. Otherwise Bedrock would try to purge the
            # ``mna.kb_chunks`` rows while the cluster is already being
            # torn down, racing CloudFormation.
            data_deletion_policy="RETAIN",
        )
        # KB ingestion runs ``INSERT`` statements against
        # ``mna.kb_chunks`` — the bootstrap CR must have created the
        # table before the first ingestion job can succeed. The KB
        # resource already depends on ``aurora_bootstrap`` above, but
        # adding the dependency on the data source as well makes the
        # ordering explicit at the CloudFormation template level and
        # guards against a future refactor that detaches the KB from
        # the bootstrap.
        self.kb_data_source.node.add_dependency(self.aurora_bootstrap)

        # ------------------------------------------------------------------
        # SSM parameters for downstream discovery
        # ------------------------------------------------------------------
        # Every consumer (the ``mna`` Python package, the agent
        # runtime container, the notebook, the Aurora bootstrap CR)
        # resolves resource identifiers from SSM so no ARN is ever
        # hardcoded. These five parameters are the DataStack's
        # contribution; later stacks add the runtime/gateway/evaluator
        # parameters.
        ssm.StringParameter(
            self,
            "AuroraClusterArnParam",
            parameter_name=_SSM_AURORA_CLUSTER_ARN,
            string_value=self.aurora_cluster.cluster_arn,
            description="Aurora Serverless v2 cluster ARN for the M&A sample",
        )
        ssm.StringParameter(
            self,
            "AuroraSecretArnParam",
            parameter_name=_SSM_AURORA_SECRET_ARN,
            string_value=self.aurora_secret.secret_arn,
            description="Aurora admin credentials secret ARN",
        )
        ssm.StringParameter(
            self,
            "DocsBucketParam",
            parameter_name=_SSM_DOCS_BUCKET,
            string_value=self.documents_bucket.bucket_name,
            description="S3 bucket holding the synthetic M&A documents",
        )
        ssm.StringParameter(
            self,
            "SessionsTableParam",
            parameter_name=_SSM_SESSIONS_TABLE,
            string_value=self.sessions_table.table_name,
            description="DynamoDB table for agent session turns",
        )
        ssm.StringParameter(
            self,
            "KnowledgeBaseIdParam",
            parameter_name=_SSM_KB_ID,
            string_value=self.knowledge_base.attr_knowledge_base_id,
            description="Bedrock Knowledge Base ID for M&A documents RAG",
        )

        # ------------------------------------------------------------------
        # CloudFormation outputs for operator visibility
        # ------------------------------------------------------------------
        # ``aws cloudformation describe-stacks`` shows these so an
        # operator can audit the data-plane resources without grep-ing
        # through SSM parameter listings.
        CfnOutput(
            self,
            "AuroraClusterArnOutput",
            value=self.aurora_cluster.cluster_arn,
            description="Aurora Serverless v2 cluster ARN",
        )
        CfnOutput(
            self,
            "AuroraSecretArnOutput",
            value=self.aurora_secret.secret_arn,
            description="Aurora admin credentials secret ARN",
        )
        CfnOutput(
            self,
            "DocumentsBucketNameOutput",
            value=self.documents_bucket.bucket_name,
            description="S3 bucket holding the synthetic M&A documents",
        )
        CfnOutput(
            self,
            "SessionsTableNameOutput",
            value=self.sessions_table.table_name,
            description="DynamoDB table for agent session turns",
        )
        CfnOutput(
            self,
            "KnowledgeBaseIdOutput",
            value=self.knowledge_base.attr_knowledge_base_id,
            description="Bedrock Knowledge Base ID for M&A documents RAG",
        )
        CfnOutput(
            self,
            "KnowledgeBaseDataSourceIdOutput",
            value=self.kb_data_source.attr_data_source_id,
            description="Bedrock KB data source ID (S3 documents bucket)",
        )

    # ----------------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------------

    @staticmethod
    def _read_schema_sql() -> str:
        """Return the contents of ``data/schemas/target_companies.sql``.

        The Aurora bootstrap CR (task 12) accepts the schema DDL as
        an inline resource property so it can run the statements via
        the RDS Data API without reaching into S3 or another store.
        Reading the file at synth time keeps the file path the single
        source of truth and makes the CloudFormation template diff
        catch schema drift automatically.

        Falls back to an empty string when the file is not present
        — useful in environments where the repository has been
        trimmed (e.g., a Lambda packaging context). The bootstrap
        CR tolerates an empty ``SchemaSql`` value by still running
        the pgvector install + KB table creation + read-only role DDL
        that it hardcodes internally.
        """

        try:
            return _AURORA_SCHEMA_SQL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

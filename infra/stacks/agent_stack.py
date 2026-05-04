"""AgentStack - AgentCore Runtime, Memory, Guardrail, and build pipeline.

Task 11 populated this stack with the container build pipeline (ECR
repository, S3 source asset, CodeBuild project, and the build +
waiter Custom Resources). Task 13 layers on:

* a Bedrock Guardrail (harmful-content filters + financial-advice
  denial topic) via the native ``aws_bedrock.CfnGuardrail`` L1;
* an AgentCore Memory resource (native L1 if ``cdk-lib`` ships one,
  else a Custom Resource backed by ``lambda/agentcore_memory``);
* an AgentCore Runtime resource pointing at the ECR image the build
  pipeline produces (native L1 or Custom Resource backed by
  ``lambda/agentcore_runtime``);
* the agent runtime IAM role scoped with least-privilege permissions
  per the design's *IAM Summary* table;
* the SSM parameter ``/mna/runtime/arn`` that :mod:`mna.config`
  resolves.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Infrastructure as Code Design - AgentStack*, *Container
Build Pipeline*, *Custom Resources Inventory*, and *Security Design
- IAM Summary*.

Requirements implemented by this stack:

* **1.4** The agent runtime is hosted on Amazon Bedrock AgentCore
  Runtime (native or CR-managed).
* **2.3** / **2.4** AgentCore Memory is provisioned and its
  ``prior_deals`` namespace is seeded so the Strategic Fit agent has
  access to prior-deal memos.
* **4.1** A Bedrock Guardrail is attached to the runtime (via the
  ``MNA_GUARDRAIL_ID`` environment variable) and configured with
  harmful-content filters and a ``financial_advice`` denial topic.
* **11a.1–11a.9** Every Custom Resource this stack creates follows
  the shared CR safety contract from task 10.
* **14.1** The agent runtime IAM role follows least privilege — every
  statement is scoped to a specific resource ARN, documented inline,
  and surfaced in the README's IAM summary.

Public attributes consumed downstream:

* :attr:`guardrail` — the ``CfnGuardrail`` resource.
* :attr:`guardrail_id` / :attr:`guardrail_version` — token-valued
  strings downstream tooling can pass via environment variables.
* :attr:`agent_runtime_role` — the IAM role attached to the runtime.
* :attr:`memory_id` / :attr:`memory_arn` — Memory resource tokens.
* :attr:`runtime_id` / :attr:`runtime_arn` — Runtime resource tokens.
* :attr:`runtime_arn_parameter` — the SSM parameter publishing the
  Runtime ARN at ``/mna/runtime/arn``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aws_cdk import CfnOutput, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

from infra.constructs import BuildPipelineConstruct

if TYPE_CHECKING:
    from infra.stacks.data_stack import DataStack
    from infra.stacks.evaluator_stack import EvaluatorStack
    from infra.stacks.gateway_stack import GatewayStack


# --------------------------------------------------------------------------- #
# Native-or-CR probe
#
# The design calls for a "native first, CR fallback" strategy for the two
# AgentCore control-plane resources. ``cdk-lib`` 2.173 ships a native
# ``CfnRuntime`` but does *not* ship a native ``CfnMemory``; a future
# CDK release is expected to add both. The probe below checks what is
# actually available at synth time so the stack wires the right path
# automatically.
#
# A small caveat: even when a native resource exists, the task brief
# explicitly prefers the CR-based path ("Default path should be the
# CR-based approach since native resources are still emerging"). We
# honour that preference by only flipping to native when *both*
# resources are available, i.e. the CDK release ships a complete set.
# This keeps the two resources consistent — running one native and one
# CR would make cross-resource dependency ordering harder to audit.
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - exercised transitively by ``cdk synth``
    from aws_cdk import aws_bedrockagentcore as _bac
except ImportError:  # pragma: no cover
    _bac = None  # type: ignore[assignment]

HAS_AGENTCORE_RUNTIME_NATIVE: bool = _bac is not None and hasattr(_bac, "CfnRuntime")
HAS_AGENTCORE_MEMORY_NATIVE: bool = _bac is not None and hasattr(_bac, "CfnMemory")
# Global toggle — only use the native path when the CDK release is
# complete enough to give us both resources. Otherwise ship both as
# Custom Resources so the two are consistent.
USE_AGENTCORE_NATIVE: bool = (
    HAS_AGENTCORE_RUNTIME_NATIVE and HAS_AGENTCORE_MEMORY_NATIVE
)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# SSM parameter for the Runtime ARN. Kept in sync with
# ``src/mna/config.py::PARAM_RUNTIME_ARN`` so the shared Python
# package, the notebook, and the CLI all resolve the same key.
_SSM_RUNTIME_ARN = "/mna/runtime/arn"

# Deterministic resource names so IAM policies that reference the
# Runtime or Memory ARN can be expressed with stable ARNs at synth
# time. CloudFormation permits updating description / role / image
# without a replace cycle, so pinning the name never forces a
# destructive redeploy.
# AgentCore resource names. Memory and Runtime names must match
# ``[a-zA-Z][a-zA-Z0-9_]{0,47}`` — underscores allowed, hyphens
# forbidden. This is the opposite of the Gateway and Gateway-Target
# constraints (which forbid underscores, allow hyphens), so naming
# conventions have to stay per-resource-type.
_RUNTIME_NAME = "mna_supervisor"
_MEMORY_NAME = "mna_agent_memory"

# Guardrail constants. Harmful-content filters are enumerated
# explicitly (rather than a simple "enable everything" setting) so a
# reviewer can audit which filter categories are applied and at what
# strength. HIGH strength across the board matches the design's
# "concrete safety controls" requirement (Req 4.1).
_GUARDRAIL_NAME = "mna-agent-guardrail"
_GUARDRAIL_HARMFUL_CATEGORIES: tuple[str, ...] = (
    "VIOLENCE",
    "HATE",
    "SEXUAL",
    "INSULTS",
    "MISCONDUCT",
)
_GUARDRAIL_CATEGORY_STRENGTH = "HIGH"

# Denial topic that refuses to produce personalized financial advice.
# The examples cover the three most common ways a reader prompt could
# try to coax the system into prescriptive guidance — the Bedrock
# Guardrail service uses them as few-shot exemplars when evaluating a
# turn.
_GUARDRAIL_DENIAL_TOPIC_NAME = "financial_advice"
_GUARDRAIL_DENIAL_TOPIC_DESCRIPTION = (
    "Personalized financial, investment, or tax recommendations. The "
    "sample is for demonstration purposes only and must not produce "
    "actionable advice directed at a specific individual or account."
)
_GUARDRAIL_DENIAL_EXAMPLES: tuple[str, ...] = (
    "Should I personally buy this company's stock?",
    "How should I allocate my personal portfolio given this analysis?",
    "What specific investment should I make in my retirement account?",
)

# Bedrock foundation model ARNs the runtime is allowed to invoke.
# Pinned to the models the design calls out (Req 12.2):
#   * Claude Sonnet 4.5      — supervisor
#   * Claude Haiku 3.5       — specialists (cheaper for routing tasks)
#   * Titan Text Embeddings  — KB retrieval embedding round trips
# The actual model IDs are parameterized by Bedrock region — the IAM
# statements use region tokens so the resource ARNs are valid in every
# supported region without hardcoding one.
_FOUNDATION_MODEL_IDS: tuple[str, ...] = (
    "anthropic.claude-sonnet-4-5-20250929-v1:0",
    "anthropic.claude-haiku-4-5-20251001-v1:0",
    "amazon.titan-embed-text-v2:0",
)

#: US cross-region inference profiles we route through by default.
#: ``us.anthropic...`` profiles fan requests across US regions and
#: stay Active on accounts that haven't invoked a model in 30 days
#: (the direct model ids get flagged Legacy). Invoking through a
#: profile requires ``bedrock:InvokeModel`` permission on both the
#: profile ARN *and* every foundation model the profile routes to.
_INFERENCE_PROFILE_IDS: tuple[str, ...] = (
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)

# Lambda sizing for the two CRs. 512 MB gives the handlers comfortable
# CPU + network headroom for the synchronous create/update/describe
# call sequence. 15-minute timeout is the Lambda service maximum and
# exceeds the 14-minute cap the shared CR base enforces on polling.
_CR_LAMBDA_MEMORY_MB = 512
_CR_LAMBDA_TIMEOUT_MINUTES = 15


class AgentStack(Stack):
    """Container build pipeline + AgentCore Runtime, Memory, and Guardrail.

    The stack wires the build pipeline (task 11) together with the
    AgentCore resources (task 13) so a single ``cdk deploy`` produces
    a fully-working runtime. Every IAM statement attached to the agent
    runtime role is documented inline with the specific design
    requirement it satisfies.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        data_stack: DataStack | None = None,
        evaluator_stack: EvaluatorStack | None = None,
        gateway_stack: GatewayStack | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Retain references so the IAM wiring below can resolve ARNs
        # without reaching back into :mod:`infra.app`.
        self._data_stack = data_stack
        self._evaluator_stack = evaluator_stack
        self._gateway_stack = gateway_stack

        # ------------------------------------------------------------------
        # Container build pipeline (Task 11)
        # ------------------------------------------------------------------
        # Everything required to produce an ARM64 agent image in ECR:
        # the ECR repository + lifecycle policy, the S3 source bucket,
        # the CodeBuild project, and the two build Custom Resources.
        self.build_pipeline = BuildPipelineConstruct(self, "BuildPipeline")

        # Hoist the fields downstream wiring cares about onto the
        # stack.
        self.ecr_repository = self.build_pipeline.ecr_repository
        self.build_project = self.build_pipeline.build_project
        self.image_uri = self.build_pipeline.image_uri

        # ------------------------------------------------------------------
        # Bedrock Guardrail (Req 4.1)
        # ------------------------------------------------------------------
        # Harmful-content filters applied at HIGH strength across every
        # Bedrock filter category the service exposes today. A single
        # denial topic refuses to produce personalized financial advice,
        # enforcing the "no prescriptive guidance" posture the design
        # commits to in §Components - Supervisor Agent.
        #
        # The L1 ``CfnGuardrail`` is used deliberately: the L2 helper in
        # ``aws-cdk-lib`` 2.173 for Guardrails still drops several
        # properties during render, which the L1 preserves verbatim.
        self.guardrail = bedrock.CfnGuardrail(
            self,
            "AgentGuardrail",
            name=_GUARDRAIL_NAME,
            description=(
                "Harmful content filters + financial-advice denial "
                "topic for the M&A Due Diligence sample supervisor "
                "and specialist agents."
            ),
            blocked_input_messaging=(
                "This request cannot be processed. The M&A Due Diligence "
                "sample does not provide personalized financial advice "
                "or engage with content that violates its content "
                "policies."
            ),
            blocked_outputs_messaging=(
                "The generated response was blocked by a safety control. "
                "The M&A Due Diligence sample is for demonstration "
                "purposes only and cannot return personalized financial "
                "advice."
            ),
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type=category,
                        input_strength=_GUARDRAIL_CATEGORY_STRENGTH,
                        output_strength=_GUARDRAIL_CATEGORY_STRENGTH,
                    )
                    for category in _GUARDRAIL_HARMFUL_CATEGORIES
                ],
            ),
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name=_GUARDRAIL_DENIAL_TOPIC_NAME,
                        definition=_GUARDRAIL_DENIAL_TOPIC_DESCRIPTION,
                        examples=list(_GUARDRAIL_DENIAL_EXAMPLES),
                        type="DENY",
                    ),
                ],
            ),
        )

        # Published version marker (``CfnGuardrailVersion``) so the
        # runtime can reference a stable snapshot rather than the
        # mutable DRAFT. Without a version, every edit to the guardrail
        # would immediately change the guard behaviour at runtime — a
        # deployment best practice the Bedrock docs call out.
        self.guardrail_version = bedrock.CfnGuardrailVersion(
            self,
            "AgentGuardrailVersion",
            guardrail_identifier=self.guardrail.attr_guardrail_id,
            description=(
                "Immutable guardrail version referenced by the AgentCore "
                "runtime. Republishing the guardrail creates a new "
                "version; the runtime updates its reference via the "
                "MNA_GUARDRAIL_VERSION environment variable."
            ),
        )
        # Expose the ID / version as tokens so the runtime wiring below
        # can pass them verbatim via environment variables.
        self.guardrail_id: str = self.guardrail.attr_guardrail_id
        self.guardrail_arn: str = self.guardrail.attr_guardrail_arn
        self.guardrail_version_string: str = self.guardrail_version.attr_version

        # ------------------------------------------------------------------
        # AgentCore Memory (Req 2.3, 2.4)
        # ------------------------------------------------------------------
        # When ``USE_AGENTCORE_NATIVE`` is ``True`` the native L1
        # construct is used. When it is ``False`` — the default today
        # because ``cdk-lib`` 2.173 lacks ``CfnMemory`` — we ship the
        # Memory CR at ``lambda/agentcore_memory/handler.py``.
        #
        # Whichever path runs, this section sets two token-valued
        # attributes:
        #
        #   * ``self.memory_id``  — the Memory resource identifier.
        #   * ``self.memory_arn`` — the Memory resource ARN.
        #
        # Downstream IAM statements below reference the ARN so the
        # agent runtime can read/write memory records (Req 2.4).
        self.memory_id, self.memory_arn = self._provision_memory()

        # ------------------------------------------------------------------
        # Agent runtime IAM role (Req 1.4, 14.1)
        # ------------------------------------------------------------------
        # The role attached to the AgentCore Runtime. Every statement
        # below is scoped to a specific resource ARN and references the
        # design requirement it satisfies. The README's "IAM Summary"
        # table mirrors this block so a security reviewer can verify
        # the two are in sync.
        self.agent_runtime_role = iam.Role(
            self,
            "AgentRuntimeRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description=(
                "Role assumed by the Bedrock AgentCore Runtime hosting "
                "the supervisor and specialist agents. Least-privilege "
                "by design -- every statement is documented inline."
            ),
        )

        # 1. Bedrock model invocation (Req 1.4, 12.2).
        #
        #    ``bedrock:InvokeModel`` and ``InvokeModelWithResponseStream``
        #    scoped to the foundation model ARNs *and* the US
        #    cross-region inference profiles listed above. We route
        #    through inference profiles by default (``us.anthropic...``)
        #    because the direct model IDs have been flagged Legacy by
        #    Anthropic; invoking a profile requires permission on both
        #    the profile ARN and every underlying model the profile
        #    routes to, so we grant both.
        #
        #    Foundation-model ARNs use ``*`` for the region segment
        #    because cross-region inference profiles fan requests to
        #    whichever region has capacity (e.g. ``us-east-2`` even
        #    when the profile lives in ``us-east-1``). Pinning the
        #    region to ``self.region`` causes ``AccessDeniedException``
        #    whenever the profile routes to a different region.
        foundation_model_arns = [
            f"arn:aws:bedrock:*::foundation-model/{model_id}"
            for model_id in _FOUNDATION_MODEL_IDS
        ]
        inference_profile_arns = [
            (
                f"arn:aws:bedrock:{self.region}:"
                f"{self.account}:inference-profile/{profile_id}"
            )
            for profile_id in _INFERENCE_PROFILE_IDS
        ]
        self.agent_runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockInvokeFoundationModels",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=foundation_model_arns + inference_profile_arns,
            ),
        )

        # 2. Bedrock Guardrail (Req 4.1).
        #
        #    ``ApplyGuardrail`` is the action AgentCore uses to run the
        #    configured guardrail against every turn. Scope tied to the
        #    specific guardrail ARN produced above.
        self.agent_runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockApplyGuardrail",
                effect=iam.Effect.ALLOW,
                actions=["bedrock:ApplyGuardrail"],
                resources=[self.guardrail_arn],
            ),
        )

        # 3. Knowledge Base retrieval (Req 2.1, 2.2).
        #
        #    ``bedrock-agent-runtime:Retrieve`` scoped to the KB ARN
        #    published by :class:`DataStack`. When ``data_stack`` is
        #    unavailable (e.g. the stack is synthesized in isolation
        #    for unit testing) we skip this statement entirely — the
        #    agent will surface a clean "KB not configured" error at
        #    runtime rather than a cryptic IAM denial.
        if data_stack is not None:
            kb_arn = (
                f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/"
                f"{data_stack.knowledge_base.attr_knowledge_base_id}"
            )
            self.agent_runtime_role.add_to_policy(
                iam.PolicyStatement(
                    sid="KnowledgeBaseRetrieve",
                    effect=iam.Effect.ALLOW,
                    # IAM evaluates KB retrieval under the ``bedrock:``
                    # prefix, not ``bedrock-agent-runtime:`` (the boto3
                    # client name). Same pattern as AgentCore control-
                    # plane actions using ``bedrock-agentcore:`` instead
                    # of ``bedrock-agentcore-control:``.
                    actions=["bedrock:Retrieve"],
                    resources=[kb_arn],
                ),
            )

            # 4. Aurora read-only via RDS Data API (Req 2a.5, 14.7).
            #
            #    :class:`DataStack` pre-computes a managed policy scoped
            #    to the cluster ARN and the admin secret. Attaching it
            #    here keeps the statements co-located with the rest of
            #    the read-only DB contract (DB role, SELECT-only SQL
            #    validation in the text-to-SQL tool).
            self.agent_runtime_role.add_managed_policy(
                data_stack.aurora_read_only_policy,
            )

            # 5. DynamoDB sessions table (Req 2.6).
            #
            #    The agent writes every turn (prompt + response +
            #    citations + evaluator result) to the sessions table.
            #    Scope to GetItem/PutItem/UpdateItem/Query — no delete
            #    permission because session TTL handles lifecycle and
            #    the agents have no reason to hard-delete rows.
            self.agent_runtime_role.add_to_policy(
                iam.PolicyStatement(
                    sid="SessionsTableReadWrite",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:Query",
                    ],
                    resources=[data_stack.sessions_table.table_arn],
                ),
            )

        # 6. AgentCore Memory read/write (Req 2.3, 2.4).
        #
        #    The Strategic Fit agent reads from ``prior_deals`` and
        #    every agent writes session-turn context. The ARN token
        #    resolves after the Memory CR (or native resource) runs; we
        #    pass the token directly so CloudFormation records the
        #    dependency automatically.
        self.agent_runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemoryReadWrite",
                effect=iam.Effect.ALLOW,
                actions=[
                    # Control-plane description is needed for health
                    # checks at cold start.
                    "bedrock-agentcore:GetMemory",
                    # Data-plane memory operations — the surface used
                    # by ``mna.tools.memory``.
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:CreateMemoryRecord",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                ],
                resources=[self.memory_arn],
            ),
        )

        # 7. AgentCore Gateway invocation (Req 3.1, 3.3).
        #
        #    The Financial Analysis agent invokes the market-data tool
        #    through the Gateway's MCP endpoint. We scope the action to
        #    the Gateway ARN resolved from the SSM parameter published
        #    by :class:`GatewayStack`. The SSM value is a placeholder
        #    until task 16 overwrites it, so we also accept any ARN
        #    under the account+region's Gateway namespace in case the
        #    Gateway is created natively (native L1) and surfaces a
        #    different ARN than the placeholder.
        if gateway_stack is not None:
            gateway_arn_wildcard = (
                f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*"
            )
            self.agent_runtime_role.add_to_policy(
                iam.PolicyStatement(
                    sid="AgentCoreGatewayInvoke",
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "bedrock-agentcore:InvokeGateway",
                        "bedrock-agentcore:GetGateway",
                    ],
                    # Wildcarding the Gateway ID is unavoidable at synth
                    # time — the Gateway ARN is not known until task 16
                    # resolves. Account+region scoping keeps the blast
                    # radius tight.
                    resources=[gateway_arn_wildcard],
                ),
            )

        # 8. Citation-check evaluator invocation (Req 4.2, 4.3).
        #
        #    The Compliance Validation agent invokes the evaluator via
        #    its ``citation_check`` tool, which is a direct Lambda
        #    invoke scoped to the evaluator's function ARN.
        if evaluator_stack is not None:
            self.agent_runtime_role.add_to_policy(
                iam.PolicyStatement(
                    sid="EvaluatorInvoke",
                    effect=iam.Effect.ALLOW,
                    actions=["lambda:InvokeFunction"],
                    resources=[evaluator_stack.evaluator_function.function_arn],
                ),
            )

        # 9. CloudWatch Logs for the runtime's own log stream (Req 9.1).
        #
        #    AgentCore Runtime writes per-invocation logs into a
        #    service-managed log group under ``/aws/bedrock-agentcore/
        #    runtimes/<runtime_name>-<suffix>-DEFAULT``. The runtime's
        #    execution role (this one) is what the container
        #    effectively runs as, so it needs to be able to:
        #
        #    * ``CreateLogGroup`` on first container start — AgentCore
        #      does *not* pre-create the group for us; the first log
        #      call from the container creates it.
        #    * ``CreateLogStream`` / ``PutLogEvents`` for ongoing logging.
        #    * ``DescribeLogStreams`` because some logging libraries
        #      probe stream existence before writing.
        #
        #    Omitting ``CreateLogGroup`` causes the container to run
        #    but log nothing — the HTTP surface still responds, but
        #    every handler failure is invisible and debugging becomes
        #    a black box.
        self.agent_runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreRuntimeLogs",
                effect=iam.Effect.ALLOW,
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*",
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:/aws/bedrock-agentcore/*:*",
                ],
            ),
        )

        # 10. ECR image pull (Req 1.4).
        #
        #    AgentCore Runtime fetches the container image from ECR on
        #    startup. The role needs pull-layer permissions scoped to
        #    the repository created by the build pipeline.
        self.agent_runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrTokenForImagePull",
                effect=iam.Effect.ALLOW,
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            ),
        )
        self.agent_runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPullAgentImage",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[self.ecr_repository.repository_arn],
            ),
        )

        # 11. SSM parameter read (for dynamic config lookup).
        #
        #    :mod:`mna.config` resolves every ``/mna/*`` parameter at
        #    runtime startup. Scope to the ``/mna/*`` prefix so the
        #    runtime cannot peek at unrelated parameters.
        self.agent_runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="SsmReadMnaParameters",
                effect=iam.Effect.ALLOW,
                actions=[
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:GetParametersByPath",
                ],
                resources=[
                    f"arn:aws:ssm:{self.region}:{self.account}:parameter/mna/*",
                ],
            ),
        )

        # ------------------------------------------------------------------
        # AgentCore Runtime (Req 1.4)
        # ------------------------------------------------------------------
        # Environment variables surfaced to the runtime container.
        # Every value is a token so CloudFormation records the
        # dependency between the runtime and its upstream resources
        # automatically.
        runtime_env_vars: dict[str, str] = {
            "MNA_GUARDRAIL_ID": self.guardrail_id,
            "MNA_GUARDRAIL_VERSION": self.guardrail_version_string,
            "MNA_MEMORY_ID": self.memory_id,
        }
        if data_stack is not None:
            runtime_env_vars["MNA_KB_ID"] = data_stack.knowledge_base.attr_knowledge_base_id
            runtime_env_vars["MNA_AURORA_CLUSTER_ARN"] = data_stack.aurora_cluster.cluster_arn
            runtime_env_vars["MNA_AURORA_SECRET_ARN"] = data_stack.aurora_secret.secret_arn
            runtime_env_vars["MNA_SESSIONS_TABLE"] = data_stack.sessions_table.table_name
        if gateway_stack is not None:
            runtime_env_vars["MNA_GATEWAY_ARN"] = gateway_stack.gateway_arn_parameter.string_value
        if evaluator_stack is not None:
            runtime_env_vars["MNA_EVALUATOR_ARN"] = (
                evaluator_stack.evaluator_function.function_arn
            )

        self.runtime_id, self.runtime_arn = self._provision_runtime(
            image_uri=self.image_uri,
            role_arn=self.agent_runtime_role.role_arn,
            env_vars=runtime_env_vars,
        )

        # ------------------------------------------------------------------
        # CloudWatch log delivery for the runtime (Req 9.1)
        # ------------------------------------------------------------------
        # AgentCore Runtime does not auto-ship its container's stdout
        # to CloudWatch -- you have to attach a Vended-Logs delivery
        # pipeline (DeliverySource -> Delivery -> DeliveryDestination).
        # Without this, the runtime still responds to invocations but
        # every log line inside the container is silently discarded.
        # Three L1 resources wire it together:
        #
        # 1. ``CfnDeliverySource`` says "these are the logs I want to
        #    ship" (identified by the runtime ARN and the log type
        #    ``APPLICATION_LOGS``).
        # 2. ``CfnDeliveryDestination`` points at the CloudWatch log
        #    group that receives the stream.
        # 3. ``CfnDelivery`` connects source to destination.
        #
        # The log group follows AgentCore's service convention
        # (``/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT``)
        # so the ``aws logs tail`` commands in the README resolve
        # without the reader having to look up a CDK-generated name.
        runtime_log_group_name = (
            f"/aws/bedrock-agentcore/runtimes/{_RUNTIME_NAME}-APPLICATION"
        )
        self.runtime_log_group = logs.LogGroup(
            self,
            "RuntimeLogGroup",
            log_group_name=runtime_log_group_name,
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        runtime_delivery_source = logs.CfnDeliverySource(
            self,
            "RuntimeDeliverySource",
            name=f"mna-runtime-{_RUNTIME_NAME}",
            resource_arn=self.runtime_arn,
            log_type="APPLICATION_LOGS",
        )
        runtime_delivery_destination = logs.CfnDeliveryDestination(
            self,
            "RuntimeDeliveryDestination",
            name=f"mna-runtime-{_RUNTIME_NAME}-dest",
            destination_resource_arn=self.runtime_log_group.log_group_arn,
        )
        runtime_delivery = logs.CfnDelivery(
            self,
            "RuntimeDelivery",
            delivery_source_name=runtime_delivery_source.name,
            delivery_destination_arn=runtime_delivery_destination.attr_arn,
        )
        runtime_delivery.add_dependency(runtime_delivery_source)
        runtime_delivery.add_dependency(runtime_delivery_destination)

        # ------------------------------------------------------------------
        # SSM parameter ``/mna/runtime/arn`` (Req 1.4, 7.4)
        # ------------------------------------------------------------------
        # :mod:`mna.config.load_config` resolves this parameter to give
        # the notebook, the CLI, and the smoke test the runtime ARN.
        self.runtime_arn_parameter = ssm.StringParameter(
            self,
            "RuntimeArnParameter",
            parameter_name=_SSM_RUNTIME_ARN,
            string_value=self.runtime_arn,
            description=(
                "ARN of the AgentCore Runtime hosting the M&A "
                "supervisor + specialist agents. Consumed by "
                "mna.config.load_config() and the notebook's "
                "environment-validation cell."
            ),
        )

        # ------------------------------------------------------------------
        # CloudFormation outputs for operator visibility
        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "AgentRuntimeArnOutput",
            value=self.runtime_arn,
            description="ARN of the AgentCore Runtime",
        )
        CfnOutput(
            self,
            "AgentRuntimeIdOutput",
            value=self.runtime_id,
            description="ID of the AgentCore Runtime",
        )
        CfnOutput(
            self,
            "AgentMemoryIdOutput",
            value=self.memory_id,
            description="ID of the AgentCore Memory",
        )
        CfnOutput(
            self,
            "AgentGuardrailIdOutput",
            value=self.guardrail_id,
            description="ID of the Bedrock Guardrail applied to the supervisor",
        )
        CfnOutput(
            self,
            "AgentRuntimeRoleArnOutput",
            value=self.agent_runtime_role.role_arn,
            description="ARN of the IAM role assumed by the AgentCore Runtime",
        )

    # ----------------------------------------------------------------------
    # Memory provisioning
    # ----------------------------------------------------------------------

    def _provision_memory(self) -> tuple[str, str]:
        """Provision the AgentCore Memory resource.

        Uses the native L1 when ``USE_AGENTCORE_NATIVE`` is true;
        otherwise deploys the Memory Custom Resource backed by
        ``lambda/agentcore_memory/handler.py``. Returns the
        ``(memory_id, memory_arn)`` tuple used by downstream IAM
        wiring and environment-variable injection.
        """

        if USE_AGENTCORE_NATIVE:  # pragma: no cover - requires a newer cdk-lib
            return self._provision_memory_native()
        return self._provision_memory_cr()

    def _provision_memory_native(self) -> tuple[str, str]:  # pragma: no cover
        """Native ``AWS::BedrockAgentCore::Memory`` path.

        Reached only when the installed ``aws-cdk-lib`` ships the
        ``CfnMemory`` L1 — at the time of writing this branch is a
        placeholder for the future CDK release. When it fires, the
        implementation builds the native resource with the same
        ``EventExpiryDuration`` and default namespace set the CR
        seeds, and returns the same ``(id, arn)`` tuple contract.
        """

        assert _bac is not None  # noqa: S101 - guarded by USE_AGENTCORE_NATIVE
        cfn_memory_cls = _bac.CfnMemory  # type: ignore[attr-defined]
        memory = cfn_memory_cls(
            self,
            "AgentMemory",
            name=_MEMORY_NAME,
            description="AgentCore Memory for the M&A Due Diligence sample.",
            event_expiry_duration=30,
        )
        # The seeding path in the CR relies on ``CreateEvent`` after
        # the resource reaches ACTIVE. The native path cannot run
        # arbitrary Lambda code, so for the native branch we rely on
        # the ``data/generate.py memory`` subcommand (task 30) to
        # populate ``prior_deals`` post-deploy. Same end-state,
        # different timing.
        return memory.attr_id, memory.attr_arn

    def _provision_memory_cr(self) -> tuple[str, str]:
        """Custom Resource path for AgentCore Memory.

        Deploys the Memory Lambda handler plus its provider framework
        and returns the ``(id, arn)`` tuple from the CR's response.
        """

        memory_function = self._build_cr_function(
            construct_id="AgentMemoryFunction",
            handler_module="agentcore_memory",
            description=(
                "Custom Resource Lambda that manages the AgentCore "
                "Memory resource lifecycle and seeds the default "
                "namespaces (prior_deals, session_seed)."
            ),
        )

        # Control-plane permissions for the CR Lambda. Scope to the
        # specific actions the handler issues — no wildcard on
        # ``bedrock-agentcore:*``. IAM evaluates the AgentCore control
        # plane under the ``bedrock-agentcore:`` prefix even though
        # the boto3 client is named ``bedrock-agentcore-control``.
        memory_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemoryControl",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:CreateMemory",
                    "bedrock-agentcore:UpdateMemory",
                    "bedrock-agentcore:DeleteMemory",
                    "bedrock-agentcore:GetMemory",
                    "bedrock-agentcore:ListMemories",
                ],
                resources=["*"],
            ),
        )
        # Data-plane ``CreateEvent`` is how the CR seeds each namespace.
        memory_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreMemorySeedEvents",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:CreateEvent",
                ],
                resources=["*"],
            ),
        )

        provider = cr.Provider(
            self,
            "AgentMemoryProvider",
            on_event_handler=memory_function,
        )
        memory_cr = CustomResource(
            self,
            "AgentMemory",
            service_token=provider.service_token,
            properties={
                "MemoryName": _MEMORY_NAME,
                "Description": "AgentCore Memory for the M&A Due Diligence sample.",
                "EventExpiryDays": 30,
                # ``session_seed`` plus ``prior_deals`` are baked in as
                # defaults inside the handler. Callers can extend the
                # list via ``Namespaces`` when additional workflows
                # need dedicated namespaces.
                "Namespaces": [],
            },
        )
        return (
            memory_cr.get_att_string("MemoryId"),
            memory_cr.get_att_string("MemoryArn"),
        )

    # ----------------------------------------------------------------------
    # Runtime provisioning
    # ----------------------------------------------------------------------

    def _provision_runtime(
        self,
        *,
        image_uri: str,
        role_arn: str,
        env_vars: dict[str, str],
    ) -> tuple[str, str]:
        """Provision the AgentCore Runtime resource.

        Uses the native L1 when ``USE_AGENTCORE_NATIVE`` is true;
        otherwise deploys the Runtime Custom Resource backed by
        ``lambda/agentcore_runtime/handler.py``.
        """

        if USE_AGENTCORE_NATIVE:  # pragma: no cover - requires a newer cdk-lib
            return self._provision_runtime_native(
                image_uri=image_uri,
                role_arn=role_arn,
                env_vars=env_vars,
            )
        return self._provision_runtime_cr(
            image_uri=image_uri,
            role_arn=role_arn,
            env_vars=env_vars,
        )

    def _provision_runtime_native(  # pragma: no cover - requires newer cdk-lib
        self,
        *,
        image_uri: str,
        role_arn: str,
        env_vars: dict[str, str],
    ) -> tuple[str, str]:
        """Native ``AWS::BedrockAgentCore::Runtime`` path."""

        assert _bac is not None  # noqa: S101 - guarded by USE_AGENTCORE_NATIVE
        runtime = _bac.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name=_RUNTIME_NAME,
            agent_runtime_artifact={
                "containerConfiguration": {"containerUri": image_uri},
            },
            network_configuration={"networkMode": "PUBLIC"},
            role_arn=role_arn,
            environment_variables=env_vars,
            description="AgentCore Runtime for the M&A Due Diligence sample.",
        )
        # Runtime cannot be created until the image actually exists in
        # ECR — the build waiter CR owns that ordering contract.
        runtime.node.add_dependency(self.build_pipeline.build_waiter)
        return runtime.attr_agent_runtime_id, runtime.attr_agent_runtime_arn

    def _provision_runtime_cr(
        self,
        *,
        image_uri: str,
        role_arn: str,
        env_vars: dict[str, str],
    ) -> tuple[str, str]:
        """Custom Resource path for AgentCore Runtime."""

        runtime_function = self._build_cr_function(
            construct_id="AgentRuntimeFunction",
            handler_module="agentcore_runtime",
            description=(
                "Custom Resource Lambda that manages the AgentCore "
                "Runtime resource lifecycle (create/update/delete)."
            ),
        )

        # Control-plane permissions for the CR Lambda. The handler
        # needs to create/update/describe/delete the runtime; it also
        # needs ``iam:PassRole`` to hand the agent runtime role to the
        # AgentCore service.
        runtime_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreRuntimeControl",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:CreateAgentRuntime",
                    "bedrock-agentcore:UpdateAgentRuntime",
                    "bedrock-agentcore:DeleteAgentRuntime",
                    "bedrock-agentcore:GetAgentRuntime",
                    "bedrock-agentcore:ListAgentRuntimes",
                    # ``CreateAgentRuntime`` provisions an
                    # ``AgentRuntimeEndpoint`` under the covers. The
                    # service requires the caller to hold the
                    # endpoint-level actions too, otherwise runtime
                    # creation fails with AccessDenied on
                    # ``CreateAgentRuntimeEndpoint``.
                    "bedrock-agentcore:CreateAgentRuntimeEndpoint",
                    "bedrock-agentcore:UpdateAgentRuntimeEndpoint",
                    "bedrock-agentcore:DeleteAgentRuntimeEndpoint",
                    "bedrock-agentcore:GetAgentRuntimeEndpoint",
                    "bedrock-agentcore:ListAgentRuntimeEndpoints",
                ],
                resources=["*"],
            ),
        )
        # Workload-identity permissions. AgentCore provisions an
        # associated workload identity when creating a runtime; the
        # caller needs permission to manage those entries. Without
        # this the runtime provisioning fails with "Failed to create
        # runtime dependencies" and a terminal FAILED status. Same
        # pattern as the Gateway CR (see gateway_stack.py).
        runtime_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreRuntimeWorkloadIdentity",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:CreateWorkloadIdentity",
                    "bedrock-agentcore:GetWorkloadIdentity",
                    "bedrock-agentcore:UpdateWorkloadIdentity",
                    "bedrock-agentcore:DeleteWorkloadIdentity",
                    "bedrock-agentcore:ListWorkloadIdentities",
                ],
                resources=["*"],
            ),
        )
        runtime_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassAgentRuntimeRole",
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.agent_runtime_role.role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
                    },
                },
            ),
        )

        provider = cr.Provider(
            self,
            "AgentRuntimeProvider",
            on_event_handler=runtime_function,
        )
        runtime_cr = CustomResource(
            self,
            "AgentRuntime",
            service_token=provider.service_token,
            properties={
                "RuntimeName": _RUNTIME_NAME,
                "ImageUri": image_uri,
                "RoleArn": role_arn,
                "Description": "AgentCore Runtime for the M&A Due Diligence sample.",
                "NetworkMode": "PUBLIC",
                "EnvironmentVariables": env_vars,
            },
        )
        # The Runtime CR depends on the build waiter so CloudFormation
        # does not try to create the runtime before the ECR image
        # exists (task 13 notes, §Custom Resource Safety Requirements
        # rule 6).
        runtime_cr.node.add_dependency(self.build_pipeline.build_waiter)
        return (
            runtime_cr.get_att_string("AgentRuntimeId"),
            runtime_cr.get_att_string("AgentRuntimeArn"),
        )

    # ----------------------------------------------------------------------
    # CR Lambda helper
    # ----------------------------------------------------------------------

    def _build_cr_function(
        self,
        *,
        construct_id: str,
        handler_module: str,
        description: str,
    ) -> lambda_.Function:
        """Build a CR Lambda that bundles the shared ``_cr_common`` base.

        Mirrors the packaging pattern the build pipeline uses — the
        Lambda asset points at the whole ``lambda/`` directory with
        unrelated handlers excluded so the shared
        ``_cr_common/send_response.py`` ships alongside the handler
        without requiring a Lambda layer (and therefore without
        requiring Docker on the reader's machine — Req NFR-RT-4).
        """

        import pathlib

        repo_root = pathlib.Path(__file__).resolve().parents[2]
        lambda_root = str((repo_root / "lambda").resolve())

        log_group = logs.LogGroup(
            self,
            f"{construct_id}LogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Exclude every sibling handler so this function's asset hash
        # does not change when unrelated Lambdas are edited.
        sibling_dirs = {
            "aurora_bootstrap",
            "build_trigger",
            "build_waiter",
            "citation_check",
            "market_data",
            "agentcore_memory",
            "agentcore_runtime",
            "agentcore_gateway",
        }
        sibling_dirs.discard(handler_module)
        exclude = [f"{d}/**" for d in sibling_dirs] + [
            "**/__pycache__/**",
            "*.pyc",
            ".gitkeep",
        ]

        return lambda_.Function(
            self,
            construct_id,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler=f"{handler_module}.handler.handler",
            code=lambda_.Code.from_asset(lambda_root, exclude=exclude),
            memory_size=_CR_LAMBDA_MEMORY_MB,
            timeout=Duration.minutes(_CR_LAMBDA_TIMEOUT_MINUTES),
            log_group=log_group,
            description=description,
            # ``_vendor`` holds pinned boto3/botocore (see
            # ``lambda/requirements.txt``); prepend it so our copy
            # wins over the runtime-bundled SDK.
            environment={"PYTHONPATH": "/var/task/_vendor"},
        )

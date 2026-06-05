"""GatewayStack - AgentCore Gateway + market-data AWS Lambda function.

This stack owns the single external tool the sample exposes via
Amazon Bedrock AgentCore Gateway, wired to an AWS Lambda function that returns
deterministic synthetic comparable-company multiples. The Financial
Analysis agent (task 23) consumes the tool through the
:mod:`mna.tools.market_data` wrapper (task 20) which in turn
invokes the Gateway MCP endpoint.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Infrastructure as Code Design - GatewayStack*,
*Gateway Targets: Lambda*, and *Custom Resources Inventory* (item 5).

Requirements implemented by this stack:

- **3.1** Exposes at least one external tool through AgentCore
  Gateway. The Gateway itself is provisioned here — natively if the
  installed ``aws-cdk-lib`` exposes ``AWS::BedrockAgentCore::Gateway``
  / ``GatewayTarget`` L1s, otherwise via the Custom Resource at
  ``lambda/agentcore_gateway/handler.py`` (task 16).
- **3.2** The external tool is backed by an AWS Lambda function
  returning deterministic synthetic data. The market-data Lambda
  created here satisfies this requirement.
- **11a.1–11a.9** When the CR path is used, the handler follows the
  shared CR safety contract from task 10 (no top-level boto3,
  guaranteed response, delete idempotency, stable physical ID, sub-4
  KB response data).
- **14.1** The Gateway's service role is scoped to invoke only the
  market-data Lambda this stack creates.

Public attributes:

- :attr:`market_data_function` — the Python 3.11 Lambda backing the
  Gateway's MCP target.
- :attr:`market_data_log_group` — the CloudWatch log group with
  7-day retention associated with the Lambda.
- :attr:`gateway_service_role` — the IAM role assumed by AgentCore
  Gateway to invoke the market-data Lambda. Only populated when the
  Gateway is actually provisioned (native or CR); ``None`` otherwise.
- :attr:`gateway_id` / :attr:`gateway_arn` — the Gateway's identifier
  and ARN. Both are CloudFormation tokens when the Gateway is
  provisioned. When no Gateway is provisioned (pure unit testing of
  the stack in isolation), both attributes are ``None``.
- :attr:`gateway_arn_parameter` — the SSM parameter publishing the
  Gateway ARN at ``/mna/gateway/arn``. Value is a CloudFormation
  token pointing at the Gateway ARN when the Gateway is provisioned,
  or a placeholder string when it is not (useful for partial deploys
  and unit tests).
"""

from __future__ import annotations

import pathlib

from aws_cdk import CfnOutput, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from aws_cdk import custom_resources as cr
from constructs import Construct

# --------------------------------------------------------------------------- #
# Native-or-CR probe (Req 3.1, 11a.1–11a.9)
#
# The design calls for a "native first, CR fallback" strategy for the
# AgentCore Gateway control-plane resource. ``cdk-lib`` 2.173 does not
# ship a native ``CfnGateway`` today; a future release may. The probe
# below checks what is actually available at synth time so the stack
# wires the right path automatically, matching the pattern used by
# :class:`infra.stacks.agent_stack.AgentStack`.
# --------------------------------------------------------------------------- #

try:  # pragma: no cover - exercised transitively by ``cdk synth``
    from aws_cdk import aws_bedrockagentcore as _bac
except ImportError:  # pragma: no cover
    _bac = None  # type: ignore[assignment]

HAS_AGENTCORE_GATEWAY_NATIVE: bool = _bac is not None and hasattr(_bac, "CfnGateway")
HAS_AGENTCORE_GATEWAY_TARGET_NATIVE: bool = _bac is not None and hasattr(
    _bac, "CfnGatewayTarget"
)
# Only use the native path when the CDK release is complete enough to
# give us both resources. Otherwise fall back to the CR path so the
# two halves of the Gateway + target pair remain consistent.
USE_GATEWAY_NATIVE: bool = (
    HAS_AGENTCORE_GATEWAY_NATIVE and HAS_AGENTCORE_GATEWAY_TARGET_NATIVE
)


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# SSM parameter name. Kept in sync with
# ``src/mna/config.py::PARAM_GATEWAY_ARN`` so the shared Python
# package and the notebook resolve the same key without hardcoding a
# string in two places.
_SSM_GATEWAY_ARN = "/mna/gateway/arn"

# Placeholder SSM value. Used only when the Gateway is intentionally
# not provisioned (future-proofing hook — no code path hits this
# branch today). :mod:`mna.config` will still load successfully so
# the notebook's environment-validation cell produces a precise error
# message.
_SSM_GATEWAY_ARN_PLACEHOLDER = "pending-custom-resource"

# Lambda sizing for the market-data Lambda. 256 MB is sufficient for
# the deterministic synthetic generator — the Lambda does no I/O,
# holds no state, and its P99 runtime is dominated by JSON encoding.
_LAMBDA_MEMORY_SIZE_MB = 256
_LAMBDA_TIMEOUT_SECONDS = 10

# Lambda asset directory for the market-data handler.
#
# Resolved from ``__file__`` rather than a CWD-relative string so the
# asset path works regardless of where ``cdk`` is invoked. The other
# stacks use the same ``parents[2]`` pattern; kept consistent here.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LAMBDA_ASSET_DIR = str(_REPO_ROOT / "lambda" / "market_data")
_LAMBDA_HANDLER = "handler.handler"

# Fixed function name so the Gateway's IAM role policy can be scoped
# to a predictable ARN at synth time (avoids the circular dependency
# of "policy → function ARN → IAM role → function").
_FUNCTION_NAME = "mna-market-data"

# Gateway + target naming. Deterministic names so downstream tooling
# and the MCP tool-invocation path in :mod:`mna.tools.market_data`
# MCP target name. ``AgentCore`` rejects underscores in target
# names (regex ``([0-9a-zA-Z][-]?){1,100}``), so the on-AWS name
# uses a hyphen. Every Python-side identifier that refers to the
# tool module (``lambda/market_data/``, ``mna.tools.market_data``)
# keeps the underscore because Python identifiers are the opposite
# constraint — dashes are forbidden in module names.
_GATEWAY_NAME = "mna-gateway"
_GATEWAY_TARGET_NAME = "market-data"
_GATEWAY_PROTOCOL_TYPE = "MCP"

# Lambda sizing for the Gateway CR itself. 512 MB gives the handler
# comfortable CPU + network headroom for the synchronous
# create/update/list/delete call sequence. 15-minute timeout is the
# Lambda service maximum.
_CR_LAMBDA_MEMORY_MB = 512
_CR_LAMBDA_TIMEOUT_MINUTES = 15


class GatewayStack(Stack):
    """Market-data Lambda + AgentCore Gateway provisioning.

    Creates the Lambda target unconditionally, then wires the Gateway
    itself via the native L1 (when available) or a Custom Resource
    backed by ``lambda/agentcore_gateway/handler.py`` (task 16). The
    Gateway's ARN is published at ``/mna/gateway/arn`` so the agent
    runtime can resolve it at cold-start time without a direct stack
    dependency on this one.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # CloudWatch log group (Req 3.2, Req 13.1)
        # ------------------------------------------------------------------
        # Creating the log group explicitly (instead of relying on
        # Lambda's implicit "first invocation creates the log group"
        # behavior) lets us:
        #
        # 1. Pin the retention window to 7 days up-front. Lambda's
        #    implicit log group is created with "never expire"
        #    retention, which would violate Req 13.1 by accumulating
        #    logs indefinitely.
        # 2. Guarantee the log group is destroyed on ``cdk destroy``
        #    (Req 8.4). The implicit log group survives stack deletion
        #    and shows up as an orphan in the verification step.
        # 3. Name the group deterministically so the README's
        #    troubleshooting section can point readers at an exact
        #    path without relying on a UUID suffix.
        self.market_data_log_group = logs.LogGroup(
            self,
            "MarketDataLogGroup",
            log_group_name=f"/aws/lambda/{_FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Market-data Lambda (Req 3.2)
        # ------------------------------------------------------------------
        # Python 3.13 matches the CR Lambdas (Req 11.1 floor is 3.11).
        # 3.13 ships a newer Amazon Linux 2023 base plus a newer
        # boto3 than the deprecated 3.11 runtime.
        self.market_data_function = lambda_.Function(
            self,
            "MarketDataFunction",
            function_name=_FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler=_LAMBDA_HANDLER,
            code=lambda_.Code.from_asset(_LAMBDA_ASSET_DIR),
            memory_size=_LAMBDA_MEMORY_SIZE_MB,
            timeout=Duration.seconds(_LAMBDA_TIMEOUT_SECONDS),
            log_group=self.market_data_log_group,
            description=(
                "Market-data Gateway tool for the M&A Due Diligence "
                "sample. Returns deterministic synthetic comparable "
                "multiples (EV/EBITDA, EV/Revenue) for a given "
                "industry code and deal-size band."
            ),
        )

        # ------------------------------------------------------------------
        # Gateway service role (Req 3.1, 14.1)
        # ------------------------------------------------------------------
        # The AgentCore Gateway service assumes this role to invoke
        # the Lambda target. Scope is narrow: only
        # ``lambda:InvokeFunction`` on the market-data function ARN.
        # CloudWatch Logs permissions are not required here because
        # the Gateway itself writes invocation traces to its own
        # service-managed log group — the log group for the Lambda function is
        # written by the Lambda runtime under its execution role
        # (CDK's default role), not this Gateway role.
        self.gateway_service_role = iam.Role(
            self,
            "GatewayServiceRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description=(
                "Role assumed by AgentCore Gateway to invoke "
                "the M&A Due Diligence sample's market-data Lambda "
                "target. Least-privilege by design."
            ),
        )
        self.gateway_service_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeMarketDataLambda",
                effect=iam.Effect.ALLOW,
                actions=["lambda:InvokeFunction"],
                resources=[self.market_data_function.function_arn],
            ),
        )

        # ------------------------------------------------------------------
        # AgentCore Gateway + Target (Req 3.1)
        # ------------------------------------------------------------------
        # Whichever path runs (native L1 or CR fallback), this section
        # assigns three token-valued attributes:
        #
        #   * ``self.gateway_id``  — the Gateway resource identifier.
        #   * ``self.gateway_arn`` — the Gateway resource ARN.
        #   * ``self.target_id``   — the MCP target identifier.
        #
        # The SSM parameter created below then publishes the ARN for
        # downstream discovery.
        self.gateway_id: str | None
        self.gateway_arn: str | None
        self.target_id: str | None
        self.gateway_id, self.gateway_arn, self.target_id = self._provision_gateway()

        # ------------------------------------------------------------------
        # SSM parameter ``/mna/gateway/arn`` (Req 3.1)
        # ------------------------------------------------------------------
        # When the Gateway is provisioned, the parameter value is a
        # CloudFormation token pointing at the Gateway ARN — the token
        # resolves at deploy time so :mod:`mna.config.load_config`
        # always sees the real ARN. When provisioning is skipped (a
        # hypothetical future hook; not exercised in the current code
        # path), the placeholder string keeps the parameter
        # resolvable for partial-deploy debugging.
        ssm_value = self.gateway_arn or _SSM_GATEWAY_ARN_PLACEHOLDER
        self.gateway_arn_parameter = ssm.StringParameter(
            self,
            "GatewayArnParameter",
            parameter_name=_SSM_GATEWAY_ARN,
            string_value=ssm_value,
            description=(
                "ARN of the AgentCore Gateway fronting the market-data "
                "Lambda. Populated by GatewayStack either directly "
                "from the native resource or via the CR fallback at "
                "lambda/agentcore_gateway/handler.py. Consumed by the "
                "Financial Analysis agent and the mna Python package "
                "via mna.config.load_config()."
            ),
        )

        # ------------------------------------------------------------------
        # CloudFormation outputs for operator visibility
        # ------------------------------------------------------------------
        if self.gateway_arn is not None:
            CfnOutput(
                self,
                "GatewayArnOutput",
                value=self.gateway_arn,
                description="ARN of the AgentCore Gateway",
            )
        if self.gateway_id is not None:
            CfnOutput(
                self,
                "GatewayIdOutput",
                value=self.gateway_id,
                description="ID of the AgentCore Gateway",
            )
        if self.target_id is not None:
            CfnOutput(
                self,
                "GatewayTargetIdOutput",
                value=self.target_id,
                description="ID of the MCP target registered on the AgentCore Gateway",
            )

    # ----------------------------------------------------------------------
    # Gateway provisioning
    # ----------------------------------------------------------------------

    def _provision_gateway(self) -> tuple[str, str, str]:
        """Provision the Gateway + target and return ``(id, arn, target_id)``.

        Uses the native L1 pair when ``USE_GATEWAY_NATIVE`` is true;
        otherwise deploys the Gateway Custom Resource backed by
        ``lambda/agentcore_gateway/handler.py``.
        """

        if USE_GATEWAY_NATIVE:  # pragma: no cover - requires a newer cdk-lib
            return self._provision_gateway_native()
        return self._provision_gateway_cr()

    def _provision_gateway_native(self) -> tuple[str, str, str]:  # pragma: no cover
        """Native ``AWS::BedrockAgentCore::Gateway`` + ``::GatewayTarget`` path.

        Reached only when the installed ``aws-cdk-lib`` ships both
        ``CfnGateway`` and ``CfnGatewayTarget`` L1s — at the time of
        writing this branch is a placeholder for the future CDK
        release. When it fires, the implementation builds the native
        resources and returns the same ``(id, arn, target_id)`` tuple
        contract.
        """

        assert _bac is not None  # noqa: S101 - guarded by USE_GATEWAY_NATIVE
        cfn_gateway_cls = _bac.CfnGateway  # type: ignore[attr-defined]
        cfn_target_cls = _bac.CfnGatewayTarget  # type: ignore[attr-defined]

        gateway = cfn_gateway_cls(
            self,
            "AgentGateway",
            name=_GATEWAY_NAME,
            protocol_type=_GATEWAY_PROTOCOL_TYPE,
            role_arn=self.gateway_service_role.role_arn,
            description="AgentCore Gateway for the M&A Due Diligence sample.",
        )
        target = cfn_target_cls(
            self,
            "AgentGatewayTarget",
            gateway_identifier=gateway.attr_gateway_id,
            name=_GATEWAY_TARGET_NAME,
            target_configuration={
                "mcp": {
                    "lambda": {
                        "lambdaArn": self.market_data_function.function_arn,
                    },
                },
            },
        )
        target.add_dependency(gateway)
        return gateway.attr_gateway_id, gateway.attr_gateway_arn, target.attr_target_id

    def _provision_gateway_cr(self) -> tuple[str, str, str]:
        """Custom Resource path for AgentCore Gateway + target.

        Deploys the Gateway Lambda handler plus its provider framework
        and returns the ``(id, arn, target_id)`` tuple from the CR's
        response. The handler at ``lambda/agentcore_gateway/handler.py``
        owns the full lifecycle: Create builds the Gateway and target
        together, Update reapplies both in place, Delete removes the
        target first and then the Gateway with idempotent
        not-found handling.
        """

        gateway_function = self._build_cr_function(
            construct_id="AgentGatewayFunction",
            handler_module="agentcore_gateway",
            description=(
                "Custom Resource Lambda that manages the AgentCore "
                "Gateway + MCP target lifecycle (create/update/delete)."
            ),
        )

        # Control-plane permissions for the CR Lambda. Scope to the
        # specific actions the handler issues — no wildcard on
        # ``bedrock-agentcore:*``. IAM evaluates the AgentCore control
        # plane under the ``bedrock-agentcore:`` prefix even though
        # the boto3 client is named ``bedrock-agentcore-control``.
        # ``Resource="*"`` is used because several of these actions
        # (Create/List) do not support resource-level permissions per
        # the AWS IAM service authorization reference. Where a resource
        # ARN does apply (Get/Update/Delete on a specific gateway), the
        # account+region scope below limits exposure to this stack.
        gateway_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreGatewayControl",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:CreateGateway",
                    "bedrock-agentcore:UpdateGateway",
                    "bedrock-agentcore:DeleteGateway",
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:ListGateways",
                    "bedrock-agentcore:CreateGatewayTarget",
                    "bedrock-agentcore:UpdateGatewayTarget",
                    "bedrock-agentcore:DeleteGatewayTarget",
                    "bedrock-agentcore:GetGatewayTarget",
                    "bedrock-agentcore:ListGatewayTargets",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*/target/*",
                ],
            ),
        )
        # Workload-identity permissions. When AgentCore provisions a
        # gateway it creates an associated workload identity in the
        # account's default directory, so the caller (this CR Lambda)
        # needs permission to create and manage those. Without this
        # the gateway provisioning fails asynchronously with
        # "Failed to create gateway dependencies" and a terminal
        # FAILED status — surfaced by the ``_wait_for_gateway_ready``
        # helper in the handler. Scoped to account+region to limit
        # blast radius.
        gateway_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="AgentCoreWorkloadIdentity",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:CreateWorkloadIdentity",
                    "bedrock-agentcore:GetWorkloadIdentity",
                    "bedrock-agentcore:UpdateWorkloadIdentity",
                    "bedrock-agentcore:DeleteWorkloadIdentity",
                    "bedrock-agentcore:ListWorkloadIdentities",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:workload-identity/*",
                ],
            ),
        )
        # ``iam:PassRole`` is required so the CR Lambda can hand the
        # Gateway service role to AgentCore at ``CreateGateway`` time.
        gateway_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="PassGatewayServiceRole",
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.gateway_service_role.role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "bedrock-agentcore.amazonaws.com",
                    },
                },
            ),
        )
        # Security exception: ``sts:GetCallerIdentity`` is an account-level
        # action with no resource-level condition keys per AWS IAM documentation; it
        # must use ``resources=["*"]``. Backs the ARN-synthesis fallback
        # in the handler: when the AgentCore ``CreateGateway`` response
        # omits ``gatewayArn`` (observed in some early GA releases), the
        # handler constructs the ARN from partition/region/account + id
        # so CloudFormation's ``Fn::GetAtt GatewayArn`` always resolves.
        gateway_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="StsGetCallerIdentity",
                effect=iam.Effect.ALLOW,
                actions=["sts:GetCallerIdentity"],
                resources=["*"],
            ),
        )

        provider = cr.Provider(
            self,
            "AgentGatewayProvider",
            on_event_handler=gateway_function,
        )
        gateway_cr = CustomResource(
            self,
            "AgentGateway",
            service_token=provider.service_token,
            properties={
                "GatewayName": _GATEWAY_NAME,
                "Description": "AgentCore Gateway for the M&A Due Diligence sample.",
                "ProtocolType": _GATEWAY_PROTOCOL_TYPE,
                "RoleArn": self.gateway_service_role.role_arn,
                "TargetName": _GATEWAY_TARGET_NAME,
                "TargetLambdaArn": self.market_data_function.function_arn,
                # MCP tool schema for the single Lambda target. The
                # AgentCore Gateway API requires an inline (or S3)
                # tool schema on Lambda targets so callers know the
                # tool name and argument shape before invocation.
                # Matches the market-data handler's contract:
                # ``(industry_code, deal_size_band)`` → synthetic
                # comparables + summary stats (see
                # ``lambda/market_data/handler.py`` docstring).
                "ToolSchema": {
                    "inlinePayload": [
                        {
                            "name": "get_comparable_multiples",
                            "description": (
                                "Return synthetic comparable-company "
                                "multiples (EV/EBITDA, EV/Revenue) plus "
                                "summary stats for a given industry "
                                "segment and deal-size band. Always "
                                "labeled as synthetic; never represents "
                                "real market data."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "industry_code": {
                                        "type": "string",
                                        "description": (
                                            "Industry segment slug "
                                            "(e.g. 'transportation', "
                                            "'logistics')."
                                        ),
                                    },
                                    "deal_size_band": {
                                        "type": "string",
                                        "description": (
                                            "Deal-size bucket label "
                                            "(e.g. '100M-500M')."
                                        ),
                                    },
                                },
                                "required": [
                                    "industry_code",
                                    "deal_size_band",
                                ],
                            },
                        },
                    ],
                },
                # Credential provider configuration the Gateway uses
                # to authenticate when invoking the Lambda target.
                # ``GATEWAY_IAM_ROLE`` tells AgentCore to use the
                # Gateway's attached service role (``gateway_service_role``
                # above, which already holds ``lambda:InvokeFunction``
                # on the market-data Lambda). No JWT, OAuth, or API
                # key infrastructure is required — aligned with the
                # sample's "Identity out of scope" stance.
                "CredentialProviderConfigurations": [
                    {"credentialProviderType": "GATEWAY_IAM_ROLE"},
                ],
            },
        )
        return (
            gateway_cr.get_att_string("GatewayId"),
            gateway_cr.get_att_string("GatewayArn"),
            gateway_cr.get_att_string("TargetId"),
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

        Mirrors the packaging pattern in :class:`AgentStack` — the
        Lambda asset points at the whole ``lambda/`` directory with
        unrelated handlers excluded so the shared
        ``_cr_common/send_response.py`` ships alongside the handler
        without requiring a Lambda layer (and therefore without
        requiring Docker on the reader's machine — Req NFR-RT-4).
        """

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

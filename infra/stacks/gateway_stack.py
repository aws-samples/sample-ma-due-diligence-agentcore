"""GatewayStack - AgentCore Gateway + market-data AWS Lambda function.

This stack owns the single external tool the sample exposes via
Amazon Bedrock AgentCore Gateway, wired to an AWS Lambda function that returns
deterministic synthetic comparable-company multiples. The Financial
Analysis agent consumes the tool through the
:mod:`mna.tools.market_data` wrapper which in turn invokes the
Gateway MCP endpoint.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Infrastructure as Code Design - GatewayStack* and
*Gateway Targets: Lambda*.

Requirements implemented by this stack:

- **3.1** Exposes at least one external tool through AgentCore
  Gateway. Provisioned using the stable
  ``aws_bedrockagentcore.Gateway`` L2 construct.
- **3.2** The external tool is backed by an AWS Lambda function
  returning deterministic synthetic data.
- **14.1** The Gateway's service role is auto-created by the L2
  construct and scoped to invoke only the market-data Lambda.

Public attributes:

- :attr:`market_data_function` — the Python 3.13 Lambda backing the
  Gateway's MCP target.
- :attr:`market_data_log_group` — the CloudWatch log group with
  7-day retention associated with the Lambda.
- :attr:`gateway` — the ``aws_bedrockagentcore.Gateway`` L2 construct.
- :attr:`gateway_id` / :attr:`gateway_arn` — the Gateway's identifier
  and ARN (CloudFormation tokens).
- :attr:`gateway_arn_parameter` — the SSM parameter publishing the
  Gateway ARN at ``/mna/gateway/arn``.
"""

from __future__ import annotations

import pathlib

from aws_cdk import CfnOutput, CfnResource, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from aws_cdk import aws_bedrockagentcore as bac
from constructs import Construct


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_SSM_GATEWAY_ARN = "/mna/gateway/arn"

_LAMBDA_MEMORY_SIZE_MB = 256
_LAMBDA_TIMEOUT_SECONDS = 10

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LAMBDA_ASSET_DIR = str(_REPO_ROOT / "lambda" / "market_data")
_LAMBDA_HANDLER = "handler.handler"

_FUNCTION_NAME = "mna-market-data"

_GATEWAY_NAME = "mna-gateway"
_GATEWAY_TARGET_NAME = "market-data"


class GatewayStack(Stack):
    """Market-data Lambda + AgentCore Gateway provisioning (L2 constructs).

    Creates the Lambda target unconditionally, then provisions the
    Gateway and its MCP Lambda target using the stable
    ``aws_bedrockagentcore.Gateway`` and
    ``aws_bedrockagentcore.GatewayTarget`` L2 constructs. The
    Gateway's service role is auto-created by the L2 with
    least-privilege ``lambda:InvokeFunction`` on the market-data
    function. The Gateway ARN is published at ``/mna/gateway/arn``
    so the agent runtime can resolve it at cold-start without a
    direct stack dependency.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # CloudWatch log group (Req 3.2, Req 13.1)
        # ------------------------------------------------------------------
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
        # AgentCore Gateway (Req 3.1) — L2 construct
        # ------------------------------------------------------------------
        # The L2 ``Gateway`` construct auto-creates the service role
        # (least-privilege ``lambda:InvokeFunction`` on the target
        # function) and registers the MCP Lambda target via
        # ``add_lambda_target()``.  The tool schema is supplied
        # inline via ``ToolSchema.from_inline()``.
        self.gateway = bac.Gateway(
            self,
            "AgentGateway",
            gateway_name=_GATEWAY_NAME,
            description="AgentCore Gateway for the M&A Due Diligence sample.",
            authorizer_configuration=bac.IamAuthorizer(),
        )

        # Register the market-data Lambda as an MCP target with an
        # inline tool schema so AgentCore knows the tool name and
        # argument shape before invocation.
        self.market_data_target = self.gateway.add_lambda_target(
            "MarketDataTarget",
            lambda_function=self.market_data_function,
            gateway_target_name=_GATEWAY_TARGET_NAME,
            tool_schema=bac.ToolSchema.from_inline(
                schema=[
                    bac.ToolDefinition(
                        name="get_comparable_multiples",
                        description=(
                            "Return synthetic comparable-company "
                            "multiples (EV/EBITDA, EV/Revenue) plus "
                            "summary stats for a given industry "
                            "segment and deal-size band. Always "
                            "labeled as synthetic; never represents "
                            "real market data."
                        ),
                        input_schema=bac.SchemaDefinition(
                            type=bac.SchemaDefinitionType.OBJECT,
                            properties={
                                "industry_code": bac.SchemaDefinition(
                                    type=bac.SchemaDefinitionType.STRING,
                                    description=(
                                        "Industry segment slug "
                                        "(e.g. 'transportation', "
                                        "'logistics')."
                                    ),
                                ),
                                "deal_size_band": bac.SchemaDefinition(
                                    type=bac.SchemaDefinitionType.STRING,
                                    description=(
                                        "Deal-size bucket label "
                                        "(e.g. '100M-500M')."
                                    ),
                                ),
                            },
                            required=["industry_code", "deal_size_band"],
                        ),
                    ),
                ],
            ),
        )

        # Expose the gateway ID and ARN as token-valued attributes so
        # downstream stacks and SSM wiring can consume them directly.
        self.gateway_id: str = self.gateway.gateway_id
        self.gateway_arn: str = self.gateway.gateway_arn

        # ------------------------------------------------------------------
        # Cedar Policy Engine + Policy (L1 constructs)
        #
        # Provides deterministic authorization control over the
        # market-data tool. Only allows queries for transportation,
        # logistics, and trucking industries — anything else is denied
        # by default (Cedar default-deny model).
        # ------------------------------------------------------------------

        # Policy Engine
        self.cfn_policy_engine = bac.CfnPolicyEngine(
            self,
            "PolicyEngine",
            name="mna_policy_engine",
            description=(
                "Cedar policy engine for the M&A Due Diligence sample. "
                "Restricts market-data tool queries to transportation-"
                "related industries only."
            ),
        )
        policy_engine_arn = self.cfn_policy_engine.attr_policy_engine_arn

        # Cedar Policy — references the Gateway ARN in the resource scope.
        # The policy is maintained in a separate .cedar file for readability
        # and version control. The {{GATEWAY_ARN}} placeholder is replaced
        # at synth time with the actual Gateway ARN token.
        cfn_gateway = self.gateway.node.default_child
        _CEDAR_POLICY_PATH = pathlib.Path(__file__).resolve().parent.parent / "policies" / "market_data_gateway.cedar"
        cedar_template = _CEDAR_POLICY_PATH.read_text(encoding="utf-8")

        # Strip comments (lines starting with //) since they are not valid
        # in the Cedar statement submitted to the API.
        cedar_body = "\n".join(
            line for line in cedar_template.splitlines()
            if not line.strip().startswith("//")
        ).strip()

        # Replace the placeholder with the actual Gateway ARN (CFN token).
        # Fn.join is needed because the ARN is a CloudFormation token at
        # synth time, not a plain string.
        parts = cedar_body.split("{{GATEWAY_ARN}}")
        cedar_statement = Fn.join("", [
            parts[0],
            cfn_gateway.attr_gateway_arn,
            parts[1],
        ])

        self.cfn_policy = bac.CfnPolicy(
            self,
            "TransportationPolicy",
            name="allow_transportation_only",
            policy_engine_id=self.cfn_policy_engine.attr_policy_engine_id,
            definition=bac.CfnPolicy.PolicyDefinitionProperty(
                cedar=bac.CfnPolicy.CedarPolicyProperty(
                    statement=cedar_statement,
                ),
            ),
            description=(
                "Only allow market data queries for transportation, "
                "logistics, and trucking industries."
            ),
            validation_mode="IGNORE_ALL_FINDINGS",
        )
        self.cfn_policy.add_dependency(self.cfn_policy_engine)
        # Policy must wait for the Gateway Target to be fully registered
        # so the Cedar schema includes the tool action name.
        target_cfn = self.market_data_target.node.default_child
        if target_cfn:
            self.cfn_policy.add_dependency(target_cfn)

        # Grant the Gateway service role permission to evaluate policies
        self.gateway.role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="PolicyEngineEvaluate",
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock-agentcore:GetPolicyEngine",
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                    "bedrock-agentcore:ListPolicies",
                    "bedrock-agentcore:GetPolicy",
                ],
                resources=[
                    policy_engine_arn,
                    self.gateway_arn,
                ],
            )
        )

        # ------------------------------------------------------------------
        # Custom Resource: Attach policy engine to gateway (with retry)
        #
        # This runs AFTER all other resources are created. The Lambda
        # retries update_gateway until IAM permission propagation
        # completes — this is deterministic (only succeeds when the
        # API confirms the association works).
        # ------------------------------------------------------------------
        policy_attach_fn = lambda_.Function(
            self,
            "PolicyAttachFunction",
            function_name="mna-policy-attach",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="handler.handler",
            code=lambda_.Code.from_asset(str(_REPO_ROOT / "lambda" / "policy_attach")),
            timeout=Duration.seconds(90),
            memory_size=128,
            description="Custom Resource: attach policy engine to gateway with IAM retry",
        )

        # The CR Lambda needs permission to update the gateway
        policy_attach_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock-agentcore:UpdateGateway", "bedrock-agentcore:GetGateway"],
                resources=[self.gateway_arn],
            )
        )
        policy_attach_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[self.gateway.role.role_arn],
            )
        )

        # Custom Resource that triggers the attachment
        cr = CfnResource(
            self,
            "PolicyAttachCR",
            type="AWS::CloudFormation::CustomResource",
            properties={
                "ServiceToken": policy_attach_fn.function_arn,
                "GATEWAY_ID": self.gateway_id,
                "GATEWAY_NAME": _GATEWAY_NAME,
                "GATEWAY_ROLE_ARN": self.gateway.role.role_arn,
                "POLICY_ENGINE_ARN": policy_engine_arn,
            },
        )
        # Explicit dependencies: CR runs last
        cr.add_dependency(self.cfn_policy_engine)
        cr.add_dependency(self.cfn_policy)
        cr.add_dependency(cfn_gateway)

        # ------------------------------------------------------------------
        # SSM parameter ``/mna/gateway/arn`` (Req 3.1)
        # ------------------------------------------------------------------
        self.gateway_arn_parameter = ssm.StringParameter(
            self,
            "GatewayArnParameter",
            parameter_name=_SSM_GATEWAY_ARN,
            string_value=self.gateway_arn,
            description=(
                "ARN of the AgentCore Gateway fronting the market-data "
                "Lambda. Consumed by the Financial Analysis agent and "
                "the mna Python package via mna.config.load_config()."
            ),
        )

        # ------------------------------------------------------------------
        # CloudFormation outputs for operator visibility
        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "GatewayArnOutput",
            value=self.gateway_arn,
            description="ARN of the AgentCore Gateway",
        )
        CfnOutput(
            self,
            "GatewayIdOutput",
            value=self.gateway_id,
            description="ID of the AgentCore Gateway",
        )

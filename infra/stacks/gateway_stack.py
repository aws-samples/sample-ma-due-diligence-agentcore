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

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
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
        )

        # Register the market-data Lambda as an MCP target with an
        # inline tool schema so AgentCore knows the tool name and
        # argument shape before invocation.
        self.gateway.add_lambda_target(
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

"""EvaluatorStack - Citation-check AWS Lambda evaluator.

This stack owns the custom evaluator described in the design document
(section *Infrastructure as Code Design - EvaluatorStack* and
*Evaluator: Citation Check*). A single Python 3.11 AWS Lambda function validates
that every factual claim in an agent response is backed by at least
one citation, returning a pass/fail result with per-claim detail. The
result is consumed by the Compliance Validation agent (task 25) and
persisted alongside the agent response in DynamoDB (``mna-sessions``)
so a reader can audit the evaluator's verdict after the fact.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Components and Interfaces → Evaluator: Citation Check* and
*Infrastructure as Code Design → EvaluatorStack*.

Requirements implemented by this stack:

- **4.2** Provides a custom evaluator that validates every factual
  claim in an agent response has at least one supporting citation. The
  Lambda is the canonical implementation; :mod:`mna.evaluators.citation_check`
  is a local mirror for fast testing only (see design §Components →
  Evaluator: Citation Check).
- **4.3** The evaluator produces a pass/fail result with per-claim
  detail. Task 14 (Phase 3) ships the real handler body; this stack
  wires the Lambda so the Compliance Validation agent (task 25) and
  the notebook/CLI surfaces (tasks 33, 34) can invoke it via the ARN
  published at ``/mna/evaluator/arn``.
- **4.4** Evaluator results are stored alongside the agent response
  for auditability. This stack does not itself write to DynamoDB —
  persistence is the agent runtime's responsibility (the
  ``mna-sessions`` table lives in :class:`DataStack`) — but by
  publishing a stable ARN here we give the runtime a single,
  discoverable invocation target so every evaluation is captured on
  the same session-turn record.

Public attributes:

- :attr:`evaluator_function` — the Python 3.11 Lambda. The
  :class:`AgentStack` grants the agent runtime role permission to
  invoke it as part of task 13.
- :attr:`log_group` — the CloudWatch log group with 7-day retention
  associated with the Lambda. Exposed so downstream stacks (or smoke
  tests) can attach subscription filters or metric filters without
  guessing the log group name.
- :attr:`evaluator_arn_parameter` — the SSM parameter publishing the
  Lambda ARN at ``/mna/evaluator/arn``.
"""

from __future__ import annotations

import pathlib

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

# SSM parameter name. Kept in sync with
# ``src/mna/config.py::PARAM_EVALUATOR_ARN`` so the shared Python
# package and the notebook resolve the same key without hardcoding a
# string in two places.
_SSM_EVALUATOR_ARN = "/mna/evaluator/arn"

# Lambda sizing. 512 MB is the point on the Lambda memory curve where
# the CPU allocation is large enough to tokenize a few-thousand-word
# response in well under a second while still fitting inside the
# sample's $5 cost budget (Req 13.1). The 30-second timeout leaves
# plenty of headroom over the Lambda's P99 runtime and prevents a
# malformed request from ever blocking the Compliance Validation
# agent for longer than a single turn.
_LAMBDA_MEMORY_SIZE_MB = 512
_LAMBDA_TIMEOUT_SECONDS = 30

# Lambda asset directory. The placeholder ``handler.py`` committed
# alongside this stack satisfies ``Code.from_asset`` so ``cdk synth``
# succeeds today; task 14 (Phase 3) replaces the body with the real
# citation-check implementation without touching this stack.
#
# Resolved from ``__file__`` rather than a CWD-relative string so the
# asset path works regardless of where ``cdk`` is invoked. The other
# stacks (``DataStack``, ``AgentStack``, ``BuildPipelineConstruct``)
# use the same ``parents[2]`` pattern; kept consistent here.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LAMBDA_ASSET_DIR = str(_REPO_ROOT / "lambda" / "citation_check")
_LAMBDA_HANDLER = "handler.handler"

# Fixed function name so the IAM policy AgentStack attaches to the
# agent runtime role can be scoped to a predictable ARN at synth time
# (avoids the circular dependency of "policy → function ARN → IAM
# role → function" that would otherwise require a Custom Resource).
_FUNCTION_NAME = "mna-citation-check"


class EvaluatorStack(Stack):
    """Citation-check Lambda evaluator plus its log group and SSM entry.

    The stack is intentionally thin — one Lambda, one log group, one
    SSM parameter — because the heavy lifting (claim extraction,
    citation matching, scoring) lives inside the Lambda handler. This
    keeps the CloudFormation template auditable and makes teardown
    cheap (Req 8.4).
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # CloudWatch log group (Req 4.4, Req 13.1)
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
        #
        # The ``/aws/lambda/<function-name>`` prefix is what the Lambda
        # runtime itself writes to, so binding this log group to the
        # Function (via ``log_group=`` below) captures every invocation
        # log line without a separate CloudWatch Logs configuration.
        self.log_group = logs.LogGroup(
            self,
            "CitationCheckLogGroup",
            log_group_name=f"/aws/lambda/{_FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_WEEK,
            # Sample is ephemeral; teardown must remove the group
            # cleanly (Req 8.4). Dropping the logs on destroy is
            # acceptable for a sample — readers running into a real
            # issue can re-deploy and reproduce.
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Citation-check Lambda (Req 4.2, 4.3)
        # ------------------------------------------------------------------
        # Python 3.11 matches the rest of the sample (Req 11.1) so a
        # reader debugging the evaluator can reuse the same local
        # Python install they use for the notebook and CLI. The
        # handler lives in ``lambda/citation_check/handler.py``;
        # ``Code.from_asset`` hashes the directory contents so each
        # change to the handler produces a new Lambda version on
        # ``cdk deploy`` (Req 4.3 requires a reproducible pass/fail
        # result, not a stable ARN, so versioning is fine).
        #
        # We pass ``log_group=`` rather than ``log_retention=`` so the
        # Lambda writes to the explicit group created above — the
        # latter would implicitly create a *second* log group via a
        # CDK-managed Custom Resource, which defeats the Req 8.4
        # "cleanup must not leave orphans" goal and clutters the
        # CloudFormation template with an extra helper Lambda.
        self.evaluator_function = lambda_.Function(
            self,
            "CitationCheckFunction",
            function_name=_FUNCTION_NAME,
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler=_LAMBDA_HANDLER,
            code=lambda_.Code.from_asset(_LAMBDA_ASSET_DIR),
            memory_size=_LAMBDA_MEMORY_SIZE_MB,
            timeout=Duration.seconds(_LAMBDA_TIMEOUT_SECONDS),
            # Explicit log group binding — see note above.
            log_group=self.log_group,
            description=(
                "Citation-check evaluator for the M&A Due Diligence "
                "sample. Validates every factual claim in an agent "
                "response has at least one supporting citation and "
                "returns a pass/fail result with per-claim detail."
            ),
        )

        # ------------------------------------------------------------------
        # SSM parameter ``/mna/evaluator/arn`` (Req 4.3, 4.4)
        # ------------------------------------------------------------------
        # Published so:
        #
        # - :class:`AgentStack` (task 13) can resolve the Lambda ARN
        #   when building the agent runtime's invoke-evaluator IAM
        #   statement.
        # - :mod:`mna.config.load_config` resolves the ARN for the
        #   notebook and CLI without a CloudFormation export lookup.
        # - :mod:`mna.agents.compliance_validation` (task 25) passes
        #   the ARN to its ``citation_check`` tool at bootstrap time.
        #
        # Storing the ARN in Parameter Store (rather than a
        # CloudFormation export) decouples this stack from the
        # AgentStack's synthesis order — readers running
        # ``cdk deploy MnaEvaluatorStack`` in isolation still get a
        # resolvable ARN for local testing.
        self.evaluator_arn_parameter = ssm.StringParameter(
            self,
            "EvaluatorArnParameter",
            parameter_name=_SSM_EVALUATOR_ARN,
            string_value=self.evaluator_function.function_arn,
            description=(
                "ARN of the citation-check evaluator Lambda. Consumed "
                "by the Compliance Validation agent and the mna "
                "Python package via mna.config.load_config()."
            ),
        )

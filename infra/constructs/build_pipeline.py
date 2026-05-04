"""``BuildPipelineConstruct`` — ECR, CodeBuild, and the build CRs.

This construct owns every AWS resource required to turn the agent
source tree into an ARM64 Linux container image that AgentCore
Runtime (task 13) can consume:

1. **ECR repository** — stores the built image and keeps a sliding
   window of the last three tagged revisions (lifecycle policy).
2. **S3 source bucket** — CDK uploads ``src/mna/`` +
   ``requirements.txt`` + ``infra/agent_image/Dockerfile`` as a
   versioned zipped asset each synth. CodeBuild pulls the source
   from this bucket.
3. **CodeBuild project** — managed ARM64 Linux build environment
   (``aws/codebuild/amazonlinux2-aarch64-standard:3.0``), runs
   ``docker build`` and ``docker push``, tags the resulting image
   with the asset's content hash *and* ``latest``.
4. **Build trigger Custom Resource** — starts CodeBuild via
   ``lambda/build_trigger/handler.py`` on every stack deploy. The CR
   receives the asset hash as a resource property so CloudFormation
   only replaces the CR (and re-triggers the build) when the source
   actually changes.
5. **Build waiter Custom Resource** — polls CodeBuild via
   ``lambda/build_waiter/handler.py`` every 30 seconds (14-minute
   cap) and returns the final image URI back to CloudFormation so
   AgentStack can wire it into downstream resources.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Container Build Pipeline* and *Custom Resources Inventory*.

Requirements satisfied:

* **NFR-RT-4** — reader never runs ``docker build`` locally; all
  container work lives in AWS CodeBuild.
* **NFR-RT-5** — CodeBuild environment is ``aws/codebuild/
  amazonlinux2-aarch64-standard:3.0``, a managed ARM64 image.
* **NFR-RT-6** — the reader's Windows 11 machine with just AWS CLI v2
  + Python + Node + PowerShell can deploy this pipeline without
  installing Docker (because Docker runs inside CodeBuild).
* **11a.1–11a.9** — the two CR Lambdas follow the shared CR base
  module (task 10); unit tests in ``tests/unit/test_build_trigger.py``
  and ``tests/unit/test_build_waiter.py`` cover the full CR safety
  matrix.

Public attributes exposed to :class:`AgentStack`:

* :attr:`ecr_repository` — the ECR repository for the agent image.
* :attr:`build_project` — the CodeBuild project.
* :attr:`source_asset` — the S3 asset wrapping the agent source.
* :attr:`image_tag` — the content-addressed tag (CDK asset hash).
* :attr:`image_uri` — ``<repo_uri>:<image_tag>`` token; resolved
  after the build waiter completes.
* :attr:`build_trigger` / :attr:`build_waiter` — the two CR
  constructs, primarily surfaced so higher-level stacks can add
  ``add_dependency()`` edges if they need to gate on the build.
"""

from __future__ import annotations

import pathlib

from aws_cdk import CustomResource, Duration, RemovalPolicy
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import custom_resources as cr
from constructs import Construct

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# ECR repository name. Hardcoded (rather than CDK-generated) so that
# downstream IAM policies (AgentStack task 13) can reference a stable
# resource ARN without introducing a synthesis-order cycle.
_ECR_REPOSITORY_NAME = "mna-agent"

# Keep the last three image revisions in ECR (task 11a.1). Older
# images are cleaned up by the lifecycle policy so storage cost stays
# bounded across many redeploys.
_ECR_IMAGE_RETENTION_COUNT = 3

# CodeBuild project name. Predictable so the README's troubleshooting
# section can reference a concrete CloudWatch log group path.
_CODEBUILD_PROJECT_NAME = "mna-agent-builder"

# Build timeouts. CodeBuild itself has a 15 minute cap imposed by the
# build waiter CR; this matches so a genuinely runaway build fails
# inside CodeBuild first (surfacing a useful CodeBuild error message)
# rather than only in the waiter.
_CODEBUILD_TIMEOUT_MINUTES = 14

# CR Lambda configuration. 1024 MB provides enough CPU headroom to
# keep the build-trigger Lambda's cold start under a second (important
# when CloudFormation retries on stack-update) and gives the waiter
# Lambda plenty of margin for its 30 s poll sleeps without paying for
# unused memory.
_CR_LAMBDA_MEMORY_MB = 512
# The trigger Lambda finishes in seconds (a single StartBuild call)
# so a short timeout is plenty.
_BUILD_TRIGGER_LAMBDA_TIMEOUT_SECONDS = 60
# The waiter Lambda needs the full 14-minute polling window plus a
# minute of headroom for the final API call and response upload, so we
# pin its timeout to the Lambda service maximum of 15 minutes.
_BUILD_WAITER_LAMBDA_TIMEOUT_MINUTES = 15

# Source-asset staging paths. The ``s3_assets.Asset`` staging directory
# contains ``requirements.txt``, ``src/mna/``, and
# ``infra/agent_image/Dockerfile`` all rooted at the repository root
# so the buildspec can reference them with stable relative paths.
# The asset is sourced from the repository root to guarantee the
# Dockerfile's ``COPY`` statements resolve.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SOURCE_ASSET_PATH = _REPO_ROOT  # the entire repo; CodeBuild uses only what the buildspec needs


class BuildPipelineConstruct(Construct):
    """CodeBuild-driven container image pipeline for the agent runtime."""

    def __init__(self, scope: Construct, construct_id: str) -> None:
        super().__init__(scope, construct_id)

        # ------------------------------------------------------------------
        # ECR repository (Req 11a.1 — "lifecycle keeps 3 images")
        # ------------------------------------------------------------------
        # ``image_scan_on_push=True`` is a zero-cost enhancement that
        # surfaces Inspector findings for each pushed image; helpful
        # for a security-conscious reader evaluating the sample. The
        # MUTABLE tag policy lets the CodeBuild project retag
        # ``latest`` on each successful build.
        self.ecr_repository = ecr.Repository(
            self,
            "AgentImageRepository",
            repository_name=_ECR_REPOSITORY_NAME,
            image_scan_on_push=True,
            image_tag_mutability=ecr.TagMutability.MUTABLE,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description=(
                        f"Keep only the last {_ECR_IMAGE_RETENTION_COUNT} "
                        "images to bound ECR storage cost across redeploys."
                    ),
                    max_image_count=_ECR_IMAGE_RETENTION_COUNT,
                    rule_priority=1,
                ),
            ],
            # Sample is ephemeral; teardown must remove the repo and
            # its images cleanly (Req 8.4). ``empty_on_delete=True``
            # is the ECR-native way to drop images on destroy without
            # requiring a CDK Custom Resource.
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # ------------------------------------------------------------------
        # S3 source asset (Req 11a.1 — "S3 source bucket")
        # ------------------------------------------------------------------
        # ``s3_assets.Asset`` wraps the repository root into a zipped
        # upload keyed by content hash. The hash becomes our image tag
        # and also identifies "did the source change?" for the trigger
        # CR below. CDK transparently re-uploads the asset only when
        # the hash changes, so redeploys without a code change are
        # cheap.
        #
        # Excluding transient build artefacts keeps the asset small,
        # the upload fast, and the Dockerfile's COPY statements
        # deterministic. Anything the buildspec actually needs
        # (``requirements.txt``, ``src/mna/``, ``infra/agent_image``)
        # is explicitly included by not being in the exclude list.
        self.source_asset = s3_assets.Asset(
            self,
            "AgentSourceAsset",
            path=str(_SOURCE_ASSET_PATH),
            exclude=[
                ".venv",
                "cdk.out",
                "infra/cdk.out",
                "__pycache__",
                "**/__pycache__/**",
                "*.pyc",
                ".pytest_cache",
                ".ruff_cache",
                ".git",
                ".gitignore",
                "tests",
                "notebooks",
                "data/*.pdf",
                "ML-*.pdf",
                "*.log",
                "node_modules",
                "build",
                "dist",
                "src/mna.egg-info",
            ],
        )
        # The content-hash-based image tag. CDK exposes this as a
        # synth-time token so we can pass it into the CodeBuild
        # environment and into the build trigger CR's resource
        # properties; CloudFormation then diffs on the token to decide
        # whether a stack update should re-run the build.
        self.image_tag = self.source_asset.asset_hash

        # ------------------------------------------------------------------
        # CodeBuild project (Req NFR-RT-5 — managed ARM64 Linux)
        # ------------------------------------------------------------------
        # Separate log group so the retention window (7 days) and the
        # ``cdk destroy`` cleanup behavior are explicit rather than
        # relying on CodeBuild's defaults.
        build_log_group = logs.LogGroup(
            self,
            "BuildProjectLogGroup",
            log_group_name=f"/aws/codebuild/{_CODEBUILD_PROJECT_NAME}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Build spec defined inline so the CDK template is the single
        # source of truth for the build commands (no second file to
        # keep in sync). The spec:
        #
        # 1. Logs into ECR using the CodeBuild role's credentials.
        # 2. Builds the Docker image against the source asset,
        #    tagging with both the content hash and ``latest``.
        # 3. Pushes both tags so the AgentCore Runtime can pin the
        #    hash while ``latest`` always resolves to the newest
        #    build (useful for manual ad-hoc invocations).
        buildspec = codebuild.BuildSpec.from_object(
            {
                "version": "0.2",
                "env": {
                    "variables": {
                        # Defaults; overridden per-build by the trigger CR
                        # to ensure the env vars match the asset hash
                        # that triggered *this* build.
                        "ECR_REPOSITORY_URI": self.ecr_repository.repository_uri,
                        "IMAGE_TAG": self.image_tag,
                        "SOURCE_VERSION": self.image_tag,
                    },
                },
                "phases": {
                    "pre_build": {
                        "commands": [
                            "echo Logging in to Amazon ECR...",
                            (
                                "aws ecr get-login-password --region $AWS_REGION "
                                "| docker login --username AWS --password-stdin "
                                "$ECR_REPOSITORY_URI"
                            ),
                        ],
                    },
                    "build": {
                        "commands": [
                            "echo Build started on `date`",
                            "echo Building the Docker image from infra/agent_image/Dockerfile",
                            (
                                "docker build "
                                "-f infra/agent_image/Dockerfile "
                                "-t $ECR_REPOSITORY_URI:$IMAGE_TAG "
                                "-t $ECR_REPOSITORY_URI:latest "
                                "."
                            ),
                        ],
                    },
                    "post_build": {
                        "commands": [
                            "echo Build completed on `date`",
                            "echo Pushing the Docker image...",
                            "docker push $ECR_REPOSITORY_URI:$IMAGE_TAG",
                            "docker push $ECR_REPOSITORY_URI:latest",
                        ],
                    },
                },
            }
        )

        self.build_project = codebuild.Project(
            self,
            "AgentBuildProject",
            project_name=_CODEBUILD_PROJECT_NAME,
            description=(
                "Builds the ARM64 agent runtime container image for the "
                "M&A Due Diligence sample. Triggered by the build_trigger "
                "Custom Resource on source-asset hash change."
            ),
            source=codebuild.Source.s3(
                bucket=self.source_asset.bucket,
                path=self.source_asset.s3_object_key,
            ),
            environment=codebuild.BuildEnvironment(
                # Managed ARM64 Linux build image. Matches
                # ``aws/codebuild/amazonlinux2-aarch64-standard:3.0``
                # (Req NFR-RT-5).
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL,
                # Privileged mode is required for ``docker build`` —
                # CodeBuild's documentation calls this out explicitly
                # for image-building projects.
                privileged=True,
            ),
            build_spec=buildspec,
            timeout=Duration.minutes(_CODEBUILD_TIMEOUT_MINUTES),
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=build_log_group,
                    enabled=True,
                ),
            ),
        )

        # Grant the build project permission to push images into the
        # ECR repository. ``grant_pull_push`` covers both the
        # ``ecr:GetAuthorizationToken`` (via a dedicated statement the
        # helper appends) and the per-repo actions
        # (``BatchCheckLayerAvailability``, ``InitiateLayerUpload``,
        # ``UploadLayerPart``, ``CompleteLayerUpload``, ``PutImage``).
        self.ecr_repository.grant_pull_push(self.build_project)

        # Explicit ``ecr:GetAuthorizationToken`` on ``*`` — required by
        # the ECR login flow and not always added by ``grant_pull_push``
        # in older CDK versions. Scoped to the action only; the
        # repository-level statements above still bound the write
        # surface.
        self.build_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="EcrAuthToken",
                effect=iam.Effect.ALLOW,
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            ),
        )

        # Source asset access: CodeBuild's role already receives S3
        # read permissions for the exact asset object when we pass the
        # asset to ``codebuild.Source.s3``, but we also explicitly
        # grant bucket-level list to simplify error messages when the
        # asset moves across deploys.
        self.source_asset.grant_read(self.build_project)

        # ------------------------------------------------------------------
        # Build trigger CR (Req 11a.1, 11a.4–11a.9)
        # ------------------------------------------------------------------
        # Both CR Lambdas share the project-wide ``_cr_common/``
        # package for response handling (rule 8). Rather than deploy
        # ``_cr_common`` as a Lambda layer (which would require Docker
        # bundling — forbidden by Req NFR-RT-4) we point each Lambda
        # at the full ``lambda/`` directory and use the handler-relative
        # path. At runtime the package structure is:
        #
        #     /var/task/_cr_common/send_response.py
        #     /var/task/build_trigger/handler.py
        #     /var/task/build_waiter/handler.py
        #
        # The handlers' ``_load_cr_common`` fallback searches this
        # layout (``here.parent / "_cr_common"``) and finds the shared
        # module without a layer.
        #
        # ``exclude`` trims the unrelated handlers from each function's
        # asset so the bundle stays small and unrelated handler
        # changes do not retrigger deploys.
        lambda_root = str((_REPO_ROOT / "lambda").resolve())

        build_trigger_log_group = logs.LogGroup(
            self,
            "BuildTriggerLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.build_trigger_function = lambda_.Function(
            self,
            "BuildTriggerFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="build_trigger.handler.handler",
            code=lambda_.Code.from_asset(
                lambda_root,
                exclude=[
                    "build_waiter/**",
                    "citation_check/**",
                    "market_data/**",
                    "agentcore_gateway/**",
                    "**/__pycache__/**",
                    "*.pyc",
                    ".gitkeep",
                ],
            ),
            memory_size=_CR_LAMBDA_MEMORY_MB,
            timeout=Duration.seconds(_BUILD_TRIGGER_LAMBDA_TIMEOUT_SECONDS),
            log_group=build_trigger_log_group,
            description=(
                "Custom Resource Lambda that starts the agent-image "
                "CodeBuild project when the source asset hash changes."
            ),
            # ``_vendor`` holds pinned boto3/botocore (see
            # ``lambda/requirements.txt``); prepend it so our copy
            # wins over the runtime-bundled SDK.
            environment={"PYTHONPATH": "/var/task/_vendor"},
        )

        self.build_trigger_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="StartAgentBuild",
                effect=iam.Effect.ALLOW,
                actions=["codebuild:StartBuild"],
                resources=[self.build_project.project_arn],
            ),
        )

        trigger_provider = cr.Provider(
            self,
            "BuildTriggerProvider",
            on_event_handler=self.build_trigger_function,
        )

        self.build_trigger = CustomResource(
            self,
            "BuildTrigger",
            service_token=trigger_provider.service_token,
            properties={
                "ProjectName": self.build_project.project_name,
                "EcrRepositoryUri": self.ecr_repository.repository_uri,
                "ImageTag": self.image_tag,
                # Passing the asset hash as the SourceVersion means
                # CloudFormation diffs on it for stack updates — no
                # change, no CR run, no CodeBuild execution.
                "SourceVersion": self.image_tag,
            },
        )
        self.build_trigger.node.add_dependency(self.build_project)
        self.build_trigger.node.add_dependency(self.ecr_repository)
        self.build_trigger.node.add_dependency(self.source_asset)

        # ``BuildId`` is surfaced by the trigger CR's response. We use
        # ``get_att_string`` so the token flows through the waiter CR's
        # resource properties, ensuring CloudFormation sequences trigger
        # → waiter deterministically.
        build_id_token = self.build_trigger.get_att_string("BuildId")

        # ------------------------------------------------------------------
        # Build waiter CR (Req 11a.1, 11a.5 — 14-minute cap)
        # ------------------------------------------------------------------
        build_waiter_log_group = logs.LogGroup(
            self,
            "BuildWaiterLogGroup",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.build_waiter_function = lambda_.Function(
            self,
            "BuildWaiterFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="build_waiter.handler.handler",
            code=lambda_.Code.from_asset(
                lambda_root,
                exclude=[
                    "build_trigger/**",
                    "citation_check/**",
                    "market_data/**",
                    "agentcore_gateway/**",
                    "**/__pycache__/**",
                    "*.pyc",
                    ".gitkeep",
                ],
            ),
            memory_size=_CR_LAMBDA_MEMORY_MB,
            timeout=Duration.minutes(_BUILD_WAITER_LAMBDA_TIMEOUT_MINUTES),
            log_group=build_waiter_log_group,
            description=(
                "Custom Resource Lambda that polls CodeBuild until the "
                "agent image build completes (14-minute cap)."
            ),
            # ``_vendor`` holds pinned boto3/botocore (see
            # ``lambda/requirements.txt``); prepend it so our copy
            # wins over the runtime-bundled SDK.
            environment={"PYTHONPATH": "/var/task/_vendor"},
        )

        self.build_waiter_function.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadBuildStatus",
                effect=iam.Effect.ALLOW,
                actions=["codebuild:BatchGetBuilds"],
                resources=[self.build_project.project_arn],
            ),
        )

        waiter_provider = cr.Provider(
            self,
            "BuildWaiterProvider",
            on_event_handler=self.build_waiter_function,
        )

        self.build_waiter = CustomResource(
            self,
            "BuildWaiter",
            service_token=waiter_provider.service_token,
            properties={
                "BuildId": build_id_token,
                "EcrRepositoryUri": self.ecr_repository.repository_uri,
                "ImageTag": self.image_tag,
            },
        )
        self.build_waiter.node.add_dependency(self.build_trigger)

        # ``ImageUri`` is resolved only after the waiter succeeds.
        # Downstream stacks (AgentStack task 13) wire this token into
        # the AgentCore Runtime's image reference so CloudFormation
        # never points the runtime at an ECR tag that has not been
        # pushed yet.
        self.image_uri = self.build_waiter.get_att_string("ImageUri")

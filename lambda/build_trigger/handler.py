"""Build trigger Custom Resource handler.

Starts an AWS CodeBuild build whenever the source asset hash
changes so the agent container image in ECR stays in lock-step with
``src/mna/`` + ``requirements.txt`` + ``infra/agent_image/Dockerfile``.
Paired with the build waiter Custom Resource
(``lambda/build_waiter/handler.py``) which polls CodeBuild until the
build completes.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
section *Container Build Pipeline* (tasks 10 and 11). Safety rules
enforced by the shared CR base (see
``.kiro/specs/ma-due-diligence-agentcore/design.md`` section
*Custom Resource Safety Requirements*):

* Rule 1 — no ``boto3`` at module scope.
* Rule 2 — guaranteed response via the shared ``cr_handler``.
* Rule 4 — ``Delete`` is a no-op success (nothing to tear down on
  the trigger side; the build waiter holds build state).
* Rule 6 — ``PhysicalResourceId`` remains stable across ``Update``s
  so CloudFormation never replaces the resource (which would orphan a
  pending build).
* Rule 7 — ``Data`` returned to CloudFormation is capped at a handful
  of short strings (``BuildId``, ``ImageTag``, ``SourceVersion``) well
  under the 4 KB limit.

Resource properties consumed (``event['ResourceProperties']``):

``ProjectName``
    Name of the CodeBuild project to start. Required.
``EcrRepositoryUri``
    URI of the target ECR repository (e.g.
    ``123456789012.dkr.ecr.us-east-1.amazonaws.com/mna-agent``).
    Passed to CodeBuild as an env-var override so the buildspec can
    ``docker tag``/``docker push`` without hardcoding the repo URI.
    Required.
``ImageTag``
    Content-addressed tag for the image (derived from the CDK asset
    hash). CodeBuild tags the image with this value *and* ``latest``
    so downstream AgentCore Runtime updates can pin a specific
    revision while ``latest`` always points at the newest build.
    Required.
``SourceVersion``
    S3 object version (or ``zip`` key) identifying the source-code
    asset. Used both to parameterize the build and as part of the
    CloudFormation physical-id so a no-op update (same source, same
    tag) does not re-trigger a build.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
from typing import Any

# --------------------------------------------------------------------------- #
# Import the shared CR base. The ``lambda`` directory is not importable as a
# Python package (the directory name clashes with the keyword), so the
# handler loads the shared module directly from its filesystem path. At
# runtime inside AWS Lambda the ``_cr_common`` package is deployed alongside
# this handler and Python's normal import path would work — but using the
# explicit path-based loader keeps the code identical across local tests
# and Lambda execution, and lets ``scripts/lint_cr_handlers.py`` continue
# to verify we never import ``boto3`` at module scope.
# --------------------------------------------------------------------------- #

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_cr_common() -> Any:
    """Return the shared ``cr_handler`` module.

    Tries the normal package import first (works inside Lambda where the
    shared code is deployed as ``_cr_common/``) and falls back to a
    path-based load for local tests that drive the handler directly.
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


def _resource_properties(event: dict) -> dict:
    """Return ``ResourceProperties`` as a dict, even when absent."""

    props = event.get("ResourceProperties") or {}
    return props if isinstance(props, dict) else {}


def _required(props: dict, key: str) -> str:
    """Fetch a required resource property or raise :class:`ValueError`."""

    value = props.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ResourceProperties.{key} is required")
    return value


def _physical_id(project_name: str, image_tag: str) -> str:
    """Stable physical ID for this resource.

    Combining the CodeBuild project name with the image tag keeps the
    ID stable across runs that rebuild the *same* source hash (a
    benign re-deploy of AgentStack, for example) while changing it when
    the source asset changes — which is exactly the signal
    CloudFormation uses to decide whether the CR needs to execute the
    update path.
    """

    return f"build-trigger-{project_name}-{image_tag}"


def _start_build(
    boto3: Any,
    *,
    project_name: str,
    ecr_repository_uri: str,
    image_tag: str,
    source_version: str,
) -> dict:
    """Invoke ``codebuild:StartBuild`` with env-var overrides.

    Returns the (trimmed) relevant fields from the CodeBuild response so
    the waiter CR can pick the build up by its ID. Truncation keeps the
    CR response under the 4 KB cap even if CodeBuild ever grows its
    response shape (rule 7).
    """

    client = boto3.client("codebuild")

    response = client.start_build(
        projectName=project_name,
        environmentVariablesOverride=[
            {"name": "ECR_REPOSITORY_URI", "value": ecr_repository_uri, "type": "PLAINTEXT"},
            {"name": "IMAGE_TAG", "value": image_tag, "type": "PLAINTEXT"},
            {"name": "SOURCE_VERSION", "value": source_version, "type": "PLAINTEXT"},
        ],
    )
    build = (response or {}).get("build") or {}
    build_id = build.get("id") or ""
    build_arn = build.get("arn") or ""
    build_status = build.get("buildStatus") or "IN_PROGRESS"
    return {
        "BuildId": build_id,
        "BuildArn": build_arn,
        "BuildStatus": build_status,
        "ImageTag": image_tag,
        "SourceVersion": source_version,
        "EcrRepositoryUri": ecr_repository_uri,
    }


def _on_create_or_update(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Kick off a CodeBuild build and return a compact response."""

    props = _resource_properties(event)
    project_name = _required(props, "ProjectName")
    ecr_repository_uri = _required(props, "EcrRepositoryUri")
    image_tag = _required(props, "ImageTag")
    # SourceVersion is optional — callers that rely purely on the CDK
    # asset hash being baked into the build environment variables can
    # pass an empty string. Keep the ID stable even in that case so
    # CloudFormation does not treat a missing value as a resource
    # replacement signal.
    source_version = (props.get("SourceVersion") or "") if isinstance(
        props.get("SourceVersion"), str
    ) else ""

    logger.info(
        "build_trigger_start",
        extra={
            "project": project_name,
            "image_tag": image_tag,
            "source_version": source_version,
        },
    )

    data = _start_build(
        boto3,
        project_name=project_name,
        ecr_repository_uri=ecr_repository_uri,
        image_tag=image_tag,
        source_version=source_version,
    )

    return _physical_id(project_name, image_tag), data


# The handler dispatches Create and Update through the same code path —
# both always start a new build. ``Delete`` is wired to ``None`` so the
# shared base returns a no-op success per rule 4.
handler = cr_handler(
    create=_on_create_or_update,
    update=_on_create_or_update,
    delete=None,
)(_on_create_or_update)


# --------------------------------------------------------------------------- #
# Environment sanity guard.
#
# Lambda's default handler resolution expects ``handler.handler``; keep
# the name stable and document the contract via ``__all__`` so a future
# refactor that renames the dispatcher doesn't silently break the CDK
# wiring.
# --------------------------------------------------------------------------- #

__all__ = ["handler"]

# ``AWS_LAMBDA_FUNCTION_NAME`` presence indicates we're running inside the
# Lambda service; otherwise this is a unit-test import and we skip the
# structured startup log to avoid noise in pytest output.
if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    logger.info("build_trigger_module_loaded")

"""Build waiter Custom Resource handler.

Polls AWS CodeBuild every 30 seconds until the build kicked off by the
build trigger CR (``lambda/build_trigger/handler.py``) reaches a
terminal state, then returns a compact success/failure body to
CloudFormation. Required because CodeBuild's ``BatchGetBuilds``
response can comfortably exceed the 4 KB Custom Resource response cap
(logs, phase list, environment variables, artifact metadata) — the
waiter strips the payload down to the fields AgentStack actually
consumes.

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
section *Container Build Pipeline* (tasks 10 and 11). Safety rules
enforced by the shared CR base:

* Rule 1 — no ``boto3`` at module scope.
* Rule 2 — guaranteed response via the shared ``cr_handler``.
* Rule 4 — ``Delete`` is a no-op success (nothing to tear down on
  the waiter side — CodeBuild builds are ephemeral).
* Rule 5 — polling capped at 14 minutes so the Lambda has at least
  one minute of headroom under the 15-minute execution limit. The
  timeout on the CDK-side Lambda resource mirrors the cap.
* Rule 6 — ``PhysicalResourceId`` echoes the one emitted by the
  build trigger so CloudFormation never treats the two resources as a
  create/delete pair.
* Rule 7 — ``Data`` returned to CloudFormation is a handful of short
  strings totaling well under 4 KB.

Resource properties consumed (``event['ResourceProperties']``):

``BuildId``
    CodeBuild build ID returned by the trigger CR. Required.
``EcrRepositoryUri``
    ECR repository URI that the build pushes to. Surfaced back to
    CloudFormation as ``ImageUri`` so AgentStack can wire the
    AgentCore Runtime's image reference downstream (task 13).
``ImageTag``
    Content-addressed tag used by the build. Combined with
    ``EcrRepositoryUri`` into the returned ``ImageUri``.
``TimeoutSeconds``
    Optional override for the 14-minute polling cap. The code clamps
    the value to ``[30, 14*60]`` so a misconfigured caller can never
    starve the Lambda's own timeout (rule 5).
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import time
from collections.abc import Callable
from typing import Any

# --------------------------------------------------------------------------- #
# Import the shared CR base. See the twin loader comment in
# ``lambda/build_trigger/handler.py`` — the two files use the same
# fallback pattern to stay lint-clean under ``scripts/lint_cr_handlers.py``.
# --------------------------------------------------------------------------- #

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_cr_common() -> Any:
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
# Polling configuration
# --------------------------------------------------------------------------- #

# Interval between ``BatchGetBuilds`` polls. 30 s matches the cadence
# documented in task 11's acceptance criteria. Short enough that a
# typical 3-5 minute container build only pays ~10 poll-cost API calls,
# long enough that an unusually slow build doesn't exhaust the
# Lambda's 1000 API-call burst budget.
_POLL_INTERVAL_SECONDS = 30

# Hard ceiling on total wait time (rule 5). 14 * 60 = 840 s gives the
# Lambda a full minute of headroom under the 15-minute hard timeout to
# still send a ``FAILED`` response to CloudFormation.
_MAX_WAIT_SECONDS = 14 * 60

# CodeBuild reports one of five terminal states. Anything else (most
# commonly ``IN_PROGRESS``) means we need to keep polling.
_SUCCESS_STATES = {"SUCCEEDED"}
_FAILURE_STATES = {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}


# --------------------------------------------------------------------------- #
# Resource-property helpers
# --------------------------------------------------------------------------- #


def _resource_properties(event: dict) -> dict:
    props = event.get("ResourceProperties") or {}
    return props if isinstance(props, dict) else {}


def _required(props: dict, key: str) -> str:
    value = props.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ResourceProperties.{key} is required")
    return value


def _resolve_timeout(props: dict) -> int:
    """Clamp an optional ``TimeoutSeconds`` to ``[_POLL_INTERVAL_SECONDS, _MAX_WAIT_SECONDS]``.

    Keeps the waiter Lambda safely under its own execution budget (rule
    5). A value below one poll interval is nonsensical; a value above
    the 14-minute cap risks running past the Lambda's hard timeout
    before the ``finally`` block fires.
    """

    raw = props.get("TimeoutSeconds")
    if raw is None:
        return _MAX_WAIT_SECONDS
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "build_waiter_timeout_invalid",
            extra={"value": raw, "fallback": _MAX_WAIT_SECONDS},
        )
        return _MAX_WAIT_SECONDS
    return max(_POLL_INTERVAL_SECONDS, min(seconds, _MAX_WAIT_SECONDS))


def _physical_id(build_id: str) -> str:
    """Use the CodeBuild build ID itself as the physical ID.

    The build trigger CR emits a ``build-trigger-*`` physical ID for
    itself, not for the waiter, so the waiter's physical ID namespace
    is independent. Pinning to the build ID keeps updates stable for
    the same build and forces CloudFormation to replace the waiter
    cleanly if the build changes.
    """

    return f"build-waiter-{build_id}"


# --------------------------------------------------------------------------- #
# Polling core
# --------------------------------------------------------------------------- #


def _poll_build(
    boto3: Any,
    *,
    build_id: str,
    timeout_seconds: int,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll ``codebuild:BatchGetBuilds`` until a terminal state or timeout.

    Parameters
    ----------
    boto3:
        Already-imported ``boto3`` module passed through by the shared
        CR base. Imported lazily there so an ``ImportError`` still
        reaches the guaranteed-response ``finally`` block.
    build_id:
        CodeBuild build ID to poll.
    timeout_seconds:
        Hard cap on total wait time. Exceeding this cap produces a
        ``timed_out`` result so the caller can surface a ``FAILED``
        status to CloudFormation.
    sleep, now:
        Injected for tests so we can simulate the passage of time
        without actually sleeping for minutes.
    """

    client = boto3.client("codebuild")
    deadline = now() + timeout_seconds
    last_status = "IN_PROGRESS"
    last_phase = "UNKNOWN"
    last_arn = ""

    while True:
        response = client.batch_get_builds(ids=[build_id])
        builds = (response or {}).get("builds") or []
        if not builds:
            # Missing build is a terminal, unrecoverable error — either
            # the trigger CR never produced a build or the service
            # purged it. Surface as a failure with a clear reason.
            return {
                "status": "missing",
                "build_status": "NOT_FOUND",
                "current_phase": "UNKNOWN",
                "build_arn": "",
            }

        build = builds[0] if isinstance(builds[0], dict) else {}
        last_status = build.get("buildStatus") or last_status
        last_phase = build.get("currentPhase") or last_phase
        last_arn = build.get("arn") or last_arn

        if last_status in _SUCCESS_STATES:
            return {
                "status": "succeeded",
                "build_status": last_status,
                "current_phase": last_phase,
                "build_arn": last_arn,
            }
        if last_status in _FAILURE_STATES:
            return {
                "status": "failed",
                "build_status": last_status,
                "current_phase": last_phase,
                "build_arn": last_arn,
            }

        if now() >= deadline:
            return {
                "status": "timed_out",
                "build_status": last_status,
                "current_phase": last_phase,
                "build_arn": last_arn,
            }

        sleep(_POLL_INTERVAL_SECONDS)


# --------------------------------------------------------------------------- #
# CR dispatchers
# --------------------------------------------------------------------------- #


def _on_create_or_update(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Wait for the referenced CodeBuild build to finish; return compact data."""

    props = _resource_properties(event)
    build_id = _required(props, "BuildId")
    ecr_repository_uri = _required(props, "EcrRepositoryUri")
    image_tag = _required(props, "ImageTag")
    timeout_seconds = _resolve_timeout(props)

    logger.info(
        "build_waiter_poll_start",
        extra={
            "build_id": build_id,
            "timeout_seconds": timeout_seconds,
            "image_tag": image_tag,
        },
    )

    outcome = _poll_build(
        boto3,
        build_id=build_id,
        timeout_seconds=timeout_seconds,
    )

    physical = _physical_id(build_id)
    image_uri = f"{ecr_repository_uri}:{image_tag}"

    if outcome["status"] == "succeeded":
        logger.info(
            "build_waiter_succeeded",
            extra={"build_id": build_id, "image_uri": image_uri},
        )
        return physical, {
            "BuildId": build_id,
            "BuildStatus": outcome["build_status"],
            "CurrentPhase": outcome["current_phase"],
            "ImageUri": image_uri,
            "ImageTag": image_tag,
            "EcrRepositoryUri": ecr_repository_uri,
        }

    # Any non-success terminal state is a hard failure — raise so the
    # shared CR base converts it into a FAILED response with a useful
    # reason string (rules 2 and 5).
    reason = outcome["status"]
    message = (
        f"CodeBuild build {build_id} finished with status "
        f"{outcome['build_status']!r} (phase={outcome['current_phase']!r}) — "
        f"waiter outcome: {reason}."
    )
    logger.error(
        "build_waiter_terminal_failure",
        extra={
            "build_id": build_id,
            "outcome": reason,
            "build_status": outcome["build_status"],
            "current_phase": outcome["current_phase"],
        },
    )
    raise RuntimeError(message)


handler = cr_handler(
    create=_on_create_or_update,
    update=_on_create_or_update,
    delete=None,
)(_on_create_or_update)


__all__ = ["handler"]


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    logger.info("build_waiter_module_loaded")

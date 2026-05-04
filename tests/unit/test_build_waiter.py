"""Unit tests for ``lambda/build_waiter/handler.py``.

Covers the six scenarios called out in task 11's acceptance criteria:

* simulated ``ImportError`` (rule 1 — handler still responds FAILED)
* successful ``Create`` (SUCCEEDED → SUCCESS with ``ImageUri`` token)
* CodeBuild failure status (FAILED → FAILED response with reason)
* polling timeout (mocked time source → FAILED within the 14 min cap)
* ``Delete`` of a missing resource (no polling, SUCCESS)
* invalid resource properties (fail-fast BEFORE any poll)

Tests inject ``sleep`` / ``now`` into :func:`_poll_build` via the
module's private callable so wall-clock behavior is fully controllable
and the tests never actually sleep for seconds.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any
from unittest import mock

import pytest

# --------------------------------------------------------------------------- #
# Module loading — matches ``test_build_trigger.py``.
# --------------------------------------------------------------------------- #

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CR_COMMON_PATH = _REPO_ROOT / "lambda" / "_cr_common" / "send_response.py"
_BUILD_WAITER_HANDLER_PATH = _REPO_ROOT / "lambda" / "build_waiter" / "handler.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def send_response_module():
    module = _load_module("cr_common_send_response_bw", _CR_COMMON_PATH)
    sys.modules["_cr_common_send_response"] = module
    yield module
    sys.modules.pop("cr_common_send_response_bw", None)
    sys.modules.pop("_cr_common_send_response", None)


@pytest.fixture
def build_waiter_module(send_response_module):  # noqa: ARG001 - dependency load order
    module = _load_module("build_waiter_handler", _BUILD_WAITER_HANDLER_PATH)
    yield module
    sys.modules.pop("build_waiter_handler", None)


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #


class FakeContext:
    def __init__(self) -> None:
        self.log_stream_name = "2024/01/01/[$LATEST]build-waiter"


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


class _FakeCodeBuildClient:
    """Iterates through a scripted sequence of ``batch_get_builds`` responses."""

    def __init__(self, responses: list[dict] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    def batch_get_builds(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if not self._responses:
            # Default to a still-in-progress response so tests that
            # rely on timeout behaviour can poll indefinitely.
            return {
                "builds": [
                    {
                        "id": kwargs["ids"][0],
                        "arn": f"arn:aws:codebuild:::build/{kwargs['ids'][0]}",
                        "buildStatus": "IN_PROGRESS",
                        "currentPhase": "BUILD",
                    },
                ],
            }
        return self._responses.pop(0)


def _fake_boto3_module(client: _FakeCodeBuildClient):
    module = mock.MagicMock()
    module.client.return_value = client
    return module


def _event(
    request_type: str = "Create",
    *,
    physical_id: str = "",
    properties: dict | None = None,
) -> dict:
    event: dict[str, Any] = {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource-response.example.com/presigned",
        "StackId": "arn:aws:cloudformation:us-east-1:111111111111:stack/test/guid",
        "RequestId": "req-guid",
        "LogicalResourceId": "BuildWaiter",
        "ResourceProperties": properties or {
            "BuildId": "mna-agent-builder:build-1",
            "EcrRepositoryUri": "111111111111.dkr.ecr.us-east-1.amazonaws.com/mna-agent",
            "ImageTag": "abc123def456",
        },
    }
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    return event


def _captured_body(mock_urlopen: mock.MagicMock) -> dict:
    assert mock_urlopen.called, "expected a CloudFormation response upload"
    args, _kwargs = mock_urlopen.call_args
    request = args[0]
    return json.loads(request.data.decode("utf-8"))


class _FakeClock:
    """Deterministic ``time.monotonic`` stand-in that advances on every call."""

    def __init__(self, *, step: float = 30.0) -> None:
        self._value = 0.0
        self._step = step
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._value

    def sleep(self, seconds: float) -> None:
        # Advance the clock by the requested sleep plus one "loop
        # body" step so a test using a 30 s poll interval reaches the
        # 14-minute cap in a predictable number of iterations.
        self.sleeps.append(seconds)
        self._value += seconds


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestImportErrorPath:
    def test_import_error_sends_failed(self, build_waiter_module, lambda_context) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "boto3":
                raise ImportError("simulated boto3 missing from deployment")
            return real_import(name, globals, locals, fromlist, level)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch("builtins.__import__", side_effect=fake_import),
        ):
            build_waiter_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImportError" in body["Reason"]


class TestSuccessPath:
    def test_succeeded_returns_image_uri(self, build_waiter_module, lambda_context) -> None:
        fake_client = _FakeCodeBuildClient(
            responses=[
                {
                    "builds": [
                        {
                            "id": "mna-agent-builder:build-1",
                            "arn": "arn:aws:codebuild:::build/mna-agent-builder:build-1",
                            "buildStatus": "SUCCEEDED",
                            "currentPhase": "COMPLETED",
                        },
                    ],
                },
            ],
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_waiter_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["Data"]["BuildStatus"] == "SUCCEEDED"
        assert body["Data"]["ImageUri"] == (
            "111111111111.dkr.ecr.us-east-1.amazonaws.com/mna-agent:abc123def456"
        )
        assert body["Data"]["ImageTag"] == "abc123def456"
        assert len(fake_client.calls) == 1

    def test_poll_returns_success_after_in_progress(
        self, build_waiter_module, lambda_context
    ) -> None:
        """Polling continues through IN_PROGRESS responses until SUCCEEDED."""

        clock = _FakeClock()
        fake_client = _FakeCodeBuildClient(
            responses=[
                {
                    "builds": [
                        {
                            "id": "b",
                            "buildStatus": "IN_PROGRESS",
                            "currentPhase": "BUILD",
                            "arn": "arn:b",
                        },
                    ],
                },
                {
                    "builds": [
                        {
                            "id": "b",
                            "buildStatus": "IN_PROGRESS",
                            "currentPhase": "UPLOAD_ARTIFACTS",
                            "arn": "arn:b",
                        },
                    ],
                },
                {
                    "builds": [
                        {
                            "id": "b",
                            "buildStatus": "SUCCEEDED",
                            "currentPhase": "COMPLETED",
                            "arn": "arn:b",
                        },
                    ],
                },
            ],
        )

        # Call the poller directly — the full handler's flow is
        # exercised above, so this test zeros in on the loop's state
        # machine and the sleep cadence.
        result = build_waiter_module._poll_build(
            _fake_boto3_module(fake_client),
            build_id="b",
            timeout_seconds=14 * 60,
            sleep=clock.sleep,
            now=clock.now,
        )

        assert result["status"] == "succeeded"
        assert result["build_status"] == "SUCCEEDED"
        # Two 30-second sleeps between the three polls.
        assert clock.sleeps == [30, 30]


class TestFailurePath:
    def test_failed_build_status_becomes_failed_response(
        self, build_waiter_module, lambda_context
    ) -> None:
        fake_client = _FakeCodeBuildClient(
            responses=[
                {
                    "builds": [
                        {
                            "id": "mna-agent-builder:build-1",
                            "buildStatus": "FAILED",
                            "currentPhase": "BUILD",
                            "arn": "arn:aws:codebuild:::build/mna-agent-builder:build-1",
                        },
                    ],
                },
            ],
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_waiter_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "FAILED" in body["Reason"]
        assert "RuntimeError" in body["Reason"]

    def test_stopped_build_status_is_treated_as_failure(self, build_waiter_module) -> None:
        clock = _FakeClock()
        fake_client = _FakeCodeBuildClient(
            responses=[
                {
                    "builds": [
                        {
                            "id": "b",
                            "buildStatus": "STOPPED",
                            "currentPhase": "BUILD",
                            "arn": "arn:b",
                        },
                    ],
                },
            ],
        )

        result = build_waiter_module._poll_build(
            _fake_boto3_module(fake_client),
            build_id="b",
            timeout_seconds=14 * 60,
            sleep=clock.sleep,
            now=clock.now,
        )
        assert result["status"] == "failed"
        assert result["build_status"] == "STOPPED"


class TestTimeoutPath:
    def test_timeout_surfaces_as_failed_response(
        self, build_waiter_module, lambda_context
    ) -> None:
        # Fake client always returns IN_PROGRESS. Combined with a
        # short timeout and a deterministic clock, the poll loop
        # exits through the timeout branch.
        clock = _FakeClock()
        fake_client = _FakeCodeBuildClient()  # default IN_PROGRESS responses
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
            # Patch the module's ``_poll_build`` time injection by
            # calling through the event with a short custom cap.
            mock.patch.object(build_waiter_module.time, "sleep", clock.sleep),
            mock.patch.object(build_waiter_module.time, "monotonic", clock.now),
        ):
            build_waiter_module.handler(
                _event(
                    "Create",
                    properties={
                        "BuildId": "mna-agent-builder:build-1",
                        "EcrRepositoryUri": (
                            "111111111111.dkr.ecr.us-east-1.amazonaws.com/mna-agent"
                        ),
                        "ImageTag": "abc123def456",
                        # Force a quick timeout — the code clamps to
                        # _POLL_INTERVAL_SECONDS (30 s) as the floor.
                        "TimeoutSeconds": 30,
                    },
                ),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "timed_out" in body["Reason"] or "TIMED_OUT" in body["Reason"] or (
            "IN_PROGRESS" in body["Reason"]
        )
        # The waiter should have polled at least twice before giving up.
        assert len(fake_client.calls) >= 2

    def test_timeout_clamped_to_safe_range(self, build_waiter_module) -> None:
        # The helper clamps a huge timeout down to the 14 min cap and
        # a tiny timeout up to one poll interval. Both branches keep
        # the Lambda within its execution window.
        assert build_waiter_module._resolve_timeout({"TimeoutSeconds": 10**9}) == 14 * 60
        assert build_waiter_module._resolve_timeout({"TimeoutSeconds": 1}) == 30
        assert build_waiter_module._resolve_timeout({}) == 14 * 60
        assert build_waiter_module._resolve_timeout({"TimeoutSeconds": "not-a-number"}) == 14 * 60


class TestMissingBuildPath:
    def test_missing_build_surfaces_as_failed(
        self, build_waiter_module, lambda_context
    ) -> None:
        fake_client = _FakeCodeBuildClient(responses=[{"builds": []}])
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_waiter_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "NOT_FOUND" in body["Reason"] or "missing" in body["Reason"].lower()


class TestDeleteOfMissing:
    def test_delete_is_a_no_op_success(self, build_waiter_module, lambda_context) -> None:
        fake_client = _FakeCodeBuildClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_waiter_module.handler(
                _event("Delete", physical_id="build-waiter-mna-agent-builder:build-1"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # No poll should have happened on a delete.
        assert fake_client.calls == []

    def test_invalid_properties_fail_fast(
        self, build_waiter_module, lambda_context
    ) -> None:
        fake_client = _FakeCodeBuildClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        event = _event("Create", properties={"BuildId": "only-build-id"})

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_waiter_module.handler(event, lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "EcrRepositoryUri" in body["Reason"]
        assert fake_client.calls == []

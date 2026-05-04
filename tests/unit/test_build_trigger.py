"""Unit tests for ``lambda/build_trigger/handler.py``.

Covers the five CR safety scenarios called out in task 11's
acceptance criteria:

* simulated ``ImportError`` (rule 1 — handler still responds FAILED)
* successful ``Create`` (StartBuild invoked, SUCCESS returned)
* successful ``Update`` (stable physical ID across source changes)
* CodeBuild StartBuild failure (FAILED response, reason truncated)
* ``Delete`` of a missing resource (no StartBuild call, SUCCESS)

Tests run with no AWS calls: ``boto3`` is replaced with a fake client
that records invocations, and ``urllib.request.urlopen`` is patched so
we can assert on the body CloudFormation would receive.
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
# Module loading
#
# The repo's ``lambda/`` directory cannot be imported as a package because
# ``lambda`` is a reserved word. We load ``send_response`` and each handler
# by file path and register them under benign names in :data:`sys.modules`
# so ``@cr_handler`` decorations and package-relative imports resolve.
# --------------------------------------------------------------------------- #

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CR_COMMON_PATH = _REPO_ROOT / "lambda" / "_cr_common" / "send_response.py"
_BUILD_TRIGGER_HANDLER_PATH = _REPO_ROOT / "lambda" / "build_trigger" / "handler.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def send_response_module():
    module = _load_module("cr_common_send_response_bt", _CR_COMMON_PATH)
    # Expose under the name the handler's fallback loader expects.
    sys.modules["_cr_common_send_response"] = module
    yield module
    sys.modules.pop("cr_common_send_response_bt", None)
    sys.modules.pop("_cr_common_send_response", None)


@pytest.fixture
def build_trigger_module(send_response_module):  # noqa: ARG001 - dependency load order
    """Load the handler fresh per-test so decorator state does not leak."""

    module = _load_module("build_trigger_handler", _BUILD_TRIGGER_HANDLER_PATH)
    yield module
    sys.modules.pop("build_trigger_handler", None)


# --------------------------------------------------------------------------- #
# Fakes and fixtures
# --------------------------------------------------------------------------- #


class FakeContext:
    def __init__(self) -> None:
        self.log_stream_name = "2024/01/01/[$LATEST]build-trigger"


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


class _FakeCodeBuildClient:
    """Records calls and returns canned ``start_build`` responses."""

    def __init__(self, *, response: dict | None = None, exc: Exception | None = None) -> None:
        self._response = response
        self._exc = exc
        self.calls: list[dict] = []

    def start_build(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response or {
            "build": {
                "id": "mna-agent-builder:build-1",
                "arn": "arn:aws:codebuild:us-east-1:111111111111:build/mna-agent-builder:build-1",
                "buildStatus": "IN_PROGRESS",
            },
        }


def _fake_boto3_module(codebuild_client: _FakeCodeBuildClient):
    """Return an object exposing ``boto3.client`` that hands back ``codebuild_client``."""

    module = mock.MagicMock()
    module.client.return_value = codebuild_client
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
        "LogicalResourceId": "BuildTrigger",
        "ResourceProperties": properties or {
            "ProjectName": "mna-agent-builder",
            "EcrRepositoryUri": "111111111111.dkr.ecr.us-east-1.amazonaws.com/mna-agent",
            "ImageTag": "abc123def456",
            "SourceVersion": "abc123def456",
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


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


class TestImportErrorPath:
    """Rule 1 — ImportError on ``boto3`` still produces a FAILED response."""

    def test_import_error_sends_failed(self, build_trigger_module, lambda_context) -> None:
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
            build_trigger_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImportError" in body["Reason"]
        assert body["PhysicalResourceId"]


class TestCreateSuccess:
    def test_create_starts_codebuild_and_returns_compact_data(
        self, build_trigger_module, lambda_context
    ) -> None:
        fake_client = _FakeCodeBuildClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_trigger_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["Data"]["BuildId"] == "mna-agent-builder:build-1"
        assert body["Data"]["ImageTag"] == "abc123def456"
        assert (
            body["Data"]["EcrRepositoryUri"]
            == "111111111111.dkr.ecr.us-east-1.amazonaws.com/mna-agent"
        )

        # StartBuild got the project name and the env-var overrides.
        assert len(fake_client.calls) == 1
        call = fake_client.calls[0]
        assert call["projectName"] == "mna-agent-builder"
        env_overrides = {
            entry["name"]: entry["value"] for entry in call["environmentVariablesOverride"]
        }
        assert env_overrides["ECR_REPOSITORY_URI"] == (
            "111111111111.dkr.ecr.us-east-1.amazonaws.com/mna-agent"
        )
        assert env_overrides["IMAGE_TAG"] == "abc123def456"
        assert env_overrides["SOURCE_VERSION"] == "abc123def456"

    def test_physical_id_is_stable_across_update_with_same_tag(
        self, build_trigger_module, lambda_context
    ) -> None:
        fake_client = _FakeCodeBuildClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_trigger_module.handler(_event("Create"), lambda_context)
            first_id = _captured_body(mock_urlopen)["PhysicalResourceId"]

            mock_urlopen.reset_mock()
            build_trigger_module.handler(
                _event("Update", physical_id=first_id),
                lambda_context,
            )
            second_id = _captured_body(mock_urlopen)["PhysicalResourceId"]

        assert first_id == second_id
        assert "mna-agent-builder" in first_id
        assert "abc123def456" in first_id


class TestCodeBuildStartFailure:
    def test_codebuild_exception_produces_failed_response(
        self, build_trigger_module, lambda_context
    ) -> None:
        class CodeBuildClientError(Exception):
            pass

        fake_client = _FakeCodeBuildClient(
            exc=CodeBuildClientError("AccessDeniedException: not authorized"),
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_trigger_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "CodeBuildClientError" in body["Reason"]
        assert "not authorized" in body["Reason"]

    def test_missing_resource_properties_fail_fast(
        self, build_trigger_module, lambda_context
    ) -> None:
        fake_client = _FakeCodeBuildClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        event = _event("Create", properties={"ProjectName": "only-name"})

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_trigger_module.handler(event, lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "EcrRepositoryUri" in body["Reason"]
        # StartBuild should never have been called.
        assert fake_client.calls == []


class TestDeleteOfMissing:
    def test_delete_is_a_no_op_success(self, build_trigger_module, lambda_context) -> None:
        fake_client = _FakeCodeBuildClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            build_trigger_module.handler(
                _event("Delete", physical_id="build-trigger-mna-agent-builder-abc123def456"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # Delete must not invoke StartBuild.
        assert fake_client.calls == []
        assert body["PhysicalResourceId"] == "build-trigger-mna-agent-builder-abc123def456"

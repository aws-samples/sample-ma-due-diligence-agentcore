"""Unit tests for ``lambda/agentcore_runtime/handler.py``.

Covers the CR safety scenarios from task 13's acceptance criteria:

* simulated ``ImportError`` (rule 1 — handler still responds FAILED)
* successful ``Create`` with polling until READY
* ``Update`` with a new image URI (idempotent, stable physical ID)
* ``Delete`` of a missing resource (treated as success)
* missing required resource property → FAILED

The tests never touch AWS: ``boto3`` is replaced with a fake client
that records every control-plane call, and ``urllib.request.urlopen``
is patched so we can inspect the body CloudFormation would receive.
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
# --------------------------------------------------------------------------- #

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CR_COMMON_PATH = _REPO_ROOT / "lambda" / "_cr_common" / "send_response.py"
_HANDLER_PATH = _REPO_ROOT / "lambda" / "agentcore_runtime" / "handler.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def send_response_module():
    module = _load_module("cr_common_send_response_acr", _CR_COMMON_PATH)
    sys.modules["_cr_common_send_response"] = module
    yield module
    sys.modules.pop("cr_common_send_response_acr", None)
    sys.modules.pop("_cr_common_send_response", None)


@pytest.fixture
def runtime_module(send_response_module):  # noqa: ARG001 - load ordering
    module = _load_module("agentcore_runtime_handler", _HANDLER_PATH)
    # Neutralize polling delay so tests run instantly.
    module._POLL_INTERVAL_SECONDS = 0
    yield module
    sys.modules.pop("agentcore_runtime_handler", None)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeContext:
    def __init__(self) -> None:
        self.log_stream_name = "2024/01/01/[$LATEST]agentcore-runtime"


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


class _FakeAgentCoreClient:
    """Records calls and returns canned responses for the Runtime API."""

    def __init__(
        self,
        *,
        create_response: dict | None = None,
        describe_status: str = "READY",
        create_exc: Exception | None = None,
        update_exc: Exception | None = None,
        delete_exc: Exception | None = None,
    ) -> None:
        self.create_calls: list[dict] = []
        self.update_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.delete_calls: list[dict] = []

        self._create_response = create_response or {
            "agentRuntime": {
                "agentRuntimeId": "rt-xyz789",
                "agentRuntimeArn": (
                    "arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                    "runtime/rt-xyz789"
                ),
                "status": "CREATING",
            },
        }
        self._describe_status = describe_status
        self._create_exc = create_exc
        self._update_exc = update_exc
        self._delete_exc = delete_exc

    def create_agent_runtime(self, **kwargs: Any) -> dict:
        self.create_calls.append(kwargs)
        if self._create_exc is not None:
            raise self._create_exc
        return self._create_response

    def update_agent_runtime(self, **kwargs: Any) -> dict:
        self.update_calls.append(kwargs)
        if self._update_exc is not None:
            raise self._update_exc
        return {
            "agentRuntime": {
                "agentRuntimeId": kwargs["agentRuntimeId"],
                "status": "UPDATING",
            },
        }

    def get_agent_runtime(self, **kwargs: Any) -> dict:
        self.get_calls.append(kwargs)
        return {
            "agentRuntime": {
                "agentRuntimeId": kwargs["agentRuntimeId"],
                "agentRuntimeArn": (
                    f"arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                    f"runtime/{kwargs['agentRuntimeId']}"
                ),
                "status": self._describe_status,
            },
        }

    def delete_agent_runtime(self, **kwargs: Any) -> dict:
        self.delete_calls.append(kwargs)
        if self._delete_exc is not None:
            raise self._delete_exc
        return {}


def _fake_boto3_module(client: _FakeAgentCoreClient):
    module = mock.MagicMock()
    module.client.return_value = client
    return module


def _default_properties(**overrides: Any) -> dict:
    props: dict[str, Any] = {
        "RuntimeName": "mna_supervisor",
        "ImageUri": (
            "111122223333.dkr.ecr.us-east-1.amazonaws.com/mna-agent:abc123"
        ),
        "RoleArn": "arn:aws:iam::111122223333:role/AgentRuntimeRole",
        "Description": "test runtime",
        "NetworkMode": "PUBLIC",
        "EnvironmentVariables": {"MNA_GUARDRAIL_ID": "gr-abc", "MNA_MEMORY_ID": "mem-abc"},
    }
    props.update(overrides)
    return props


def _event(
    request_type: str = "Create",
    *,
    physical_id: str = "",
    properties: dict | None = None,
) -> dict:
    event: dict[str, Any] = {
        "RequestType": request_type,
        "ResponseURL": "https://cfn-cr-responses.example.com/presigned",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/test/guid",
        "RequestId": "req-guid",
        "LogicalResourceId": "AgentRuntime",
        "ResourceProperties": properties if properties is not None else _default_properties(),
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
    def test_import_error_sends_failed(self, runtime_module, lambda_context) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "boto3":
                raise ImportError("simulated boto3 missing")
            return real_import(name, globals, locals, fromlist, level)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch("builtins.__import__", side_effect=fake_import),
        ):
            runtime_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImportError" in body["Reason"]
        assert body["PhysicalResourceId"]


class TestCreateSuccess:
    def test_create_calls_create_agent_runtime_and_waits_for_ready(
        self, runtime_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            runtime_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]
        assert body["Data"]["AgentRuntimeId"] == "rt-xyz789"
        assert body["Data"]["AgentRuntimeArn"].endswith("runtime/rt-xyz789")
        assert body["Data"]["ImageUri"].endswith(":abc123")
        assert body["Data"]["Status"] == "READY"

        # CreateAgentRuntime should have received the image, role,
        # network, and env vars from the caller.
        assert len(fake_client.create_calls) == 1
        call = fake_client.create_calls[0]
        assert call["agentRuntimeName"] == "mna_supervisor"
        assert (
            call["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
            == "111122223333.dkr.ecr.us-east-1.amazonaws.com/mna-agent:abc123"
        )
        assert call["roleArn"] == "arn:aws:iam::111122223333:role/AgentRuntimeRole"
        assert call["networkConfiguration"]["networkMode"] == "PUBLIC"
        assert call["environmentVariables"]["MNA_GUARDRAIL_ID"] == "gr-abc"

        # At least one GetAgentRuntime poll should have run to
        # confirm the READY state before returning SUCCESS.
        assert len(fake_client.get_calls) >= 1
        assert fake_client.get_calls[0]["agentRuntimeId"] == "rt-xyz789"

        # Physical ID == runtime ID (rule 6).
        assert body["PhysicalResourceId"] == "rt-xyz789"


class TestUpdateWithImageChange:
    def test_update_with_new_image_uri_is_applied_in_place(
        self, runtime_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            runtime_module.handler(_event("Create"), lambda_context)
            first_body = _captured_body(mock_urlopen)
            first_id = first_body["PhysicalResourceId"]
            mock_urlopen.reset_mock()

            new_image = (
                "111122223333.dkr.ecr.us-east-1.amazonaws.com/mna-agent:def456"
            )
            updated_props = _default_properties(ImageUri=new_image)
            runtime_module.handler(
                _event("Update", physical_id=first_id, properties=updated_props),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]
        # Physical ID stays the same — no replace cycle.
        assert body["PhysicalResourceId"] == first_id
        # UpdateAgentRuntime was called with the new image URI.
        assert len(fake_client.update_calls) == 1
        upd = fake_client.update_calls[0]
        assert (
            upd["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
            == new_image
        )
        assert upd["agentRuntimeId"] == first_id
        # Data echoes the new image URI so the build waiter →
        # update → output chain is audit-friendly.
        assert body["Data"]["ImageUri"] == new_image

    def test_update_with_new_role_arn_is_applied(
        self, runtime_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            runtime_module.handler(_event("Create"), lambda_context)
            first_id = _captured_body(mock_urlopen)["PhysicalResourceId"]
            mock_urlopen.reset_mock()

            new_role = "arn:aws:iam::111122223333:role/NewAgentRuntimeRole"
            updated = _default_properties(RoleArn=new_role)
            runtime_module.handler(
                _event("Update", physical_id=first_id, properties=updated),
                lambda_context,
            )

        upd = fake_client.update_calls[0]
        assert upd["roleArn"] == new_role


class TestDeleteOfMissing:
    def test_delete_of_missing_treated_as_success(
        self, runtime_module, lambda_context
    ) -> None:
        class NotFoundException(Exception):
            pass

        fake_client = _FakeAgentCoreClient(
            delete_exc=NotFoundException("runtime gone"),
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            runtime_module.handler(
                _event("Delete", physical_id="rt-already-gone"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "rt-already-gone"
        # The handler still attempted the delete; the shared base's
        # ``is_missing_error`` classifier converts the raised
        # ``NotFoundException`` into a SUCCESS.
        assert len(fake_client.delete_calls) == 1

    def test_delete_with_no_physical_id_is_success(
        self, runtime_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            runtime_module.handler(
                _event("Delete", physical_id=""),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # No delete API call should be issued when nothing was created.
        assert fake_client.delete_calls == []


class TestMissingProperties:
    def test_missing_image_uri_fails_fast(
        self, runtime_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties()
        props.pop("ImageUri")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            runtime_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImageUri" in body["Reason"]
        assert fake_client.create_calls == []

    def test_missing_role_arn_fails_fast(
        self, runtime_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties()
        props.pop("RoleArn")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            runtime_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "RoleArn" in body["Reason"]
        assert fake_client.create_calls == []

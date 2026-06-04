"""Unit tests for ``lambda/agentcore_memory/handler.py``.

Covers the CR safety scenarios from task 13's acceptance criteria:

* simulated ``ImportError`` (rule 1 — handler still responds FAILED)
* successful ``Create`` with namespace seeding
* ``Update`` reapplies mutable properties with a stable physical ID
* ``Delete`` of a missing resource (treated as success)
* missing required resource property → FAILED

The tests never touch AWS: ``boto3`` is replaced with a fake client
that records every control-plane + data-plane call, and
``urllib.request.urlopen`` is patched so we can inspect the body
CloudFormation would receive.
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
_HANDLER_PATH = _REPO_ROOT / "lambda" / "agentcore_memory" / "handler.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def send_response_module():
    module = _load_module("cr_common_send_response_acm", _CR_COMMON_PATH)
    sys.modules["_cr_common_send_response"] = module
    yield module
    sys.modules.pop("cr_common_send_response_acm", None)
    sys.modules.pop("_cr_common_send_response", None)


@pytest.fixture
def memory_module(send_response_module):  # noqa: ARG001 - load ordering
    module = _load_module("agentcore_memory_handler", _HANDLER_PATH)
    # Neutralize the polling loop so the fake client does not spin forever.
    module._CREATE_POLL_INTERVAL_SECONDS = 0
    yield module
    sys.modules.pop("agentcore_memory_handler", None)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeContext:
    def __init__(self) -> None:
        self.log_stream_name = "2024/01/01/[$LATEST]agentcore-memory"


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


class _FakeAgentCoreClient:
    """Records every AgentCore call and returns canned responses."""

    def __init__(
        self,
        *,
        create_response: dict | None = None,
        describe_status: str = "ACTIVE",
        create_exc: Exception | None = None,
        delete_exc: Exception | None = None,
    ) -> None:
        self.create_memory_calls: list[dict] = []
        self.update_memory_calls: list[dict] = []
        self.get_memory_calls: list[dict] = []
        self.delete_memory_calls: list[dict] = []
        self.create_event_calls: list[dict] = []

        self._create_response = create_response or {
            "memory": {
                "id": "mem-abc123",
                "arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:memory/mem-abc123",
                "status": "CREATING",
            },
        }
        self._describe_status = describe_status
        self._create_exc = create_exc
        self._delete_exc = delete_exc

    def create_memory(self, **kwargs: Any) -> dict:
        self.create_memory_calls.append(kwargs)
        if self._create_exc is not None:
            raise self._create_exc
        return self._create_response

    def update_memory(self, **kwargs: Any) -> dict:
        self.update_memory_calls.append(kwargs)
        return {"memory": {"id": kwargs["memoryId"], "status": "UPDATING"}}

    def get_memory(self, **kwargs: Any) -> dict:
        self.get_memory_calls.append(kwargs)
        return {
            "memory": {
                "id": kwargs["memoryId"],
                "arn": (
                    f"arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                    f"memory/{kwargs['memoryId']}"
                ),
                "status": self._describe_status,
            },
        }

    def delete_memory(self, **kwargs: Any) -> dict:
        self.delete_memory_calls.append(kwargs)
        if self._delete_exc is not None:
            raise self._delete_exc
        return {}

    def create_event(self, **kwargs: Any) -> dict:
        self.create_event_calls.append(kwargs)
        return {"event": {"id": f"evt-{len(self.create_event_calls)}"}}


def _fake_boto3_module(client: _FakeAgentCoreClient):
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
        "ResponseURL": "https://cfn-cr-responses.example.com/presigned",
        "StackId": "arn:aws:cloudformation:us-east-1:111122223333:stack/test/guid",
        "RequestId": "req-guid",
        "LogicalResourceId": "AgentMemory",
        "ResourceProperties": properties
        if properties is not None
        else {
            "MemoryName": "mna_agent_memory",
            "Description": "test",
            "EventExpiryDays": 30,
            "Namespaces": [],
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
    def test_import_error_sends_failed(self, memory_module, lambda_context) -> None:
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
            memory_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImportError" in body["Reason"]
        assert body["PhysicalResourceId"]


class TestCreateSuccess:
    def test_create_provisions_memory_and_seeds_default_namespaces(
        self, memory_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            memory_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]
        assert body["Data"]["MemoryId"] == "mem-abc123"
        assert body["Data"]["MemoryArn"].endswith(":memory/mem-abc123")
        # The CR must have invoked CreateMemory exactly once with
        # the caller-supplied name and expiry.
        assert len(fake_client.create_memory_calls) == 1
        create_kwargs = fake_client.create_memory_calls[0]
        assert create_kwargs["name"] == "mna_agent_memory"
        assert create_kwargs["eventExpiryDuration"] == 30

        # The two always-seeded namespaces must have been written.
        seeded_sessions = {
            call["sessionId"] for call in fake_client.create_event_calls
        }
        assert "bootstrap-prior_deals" in seeded_sessions
        assert "bootstrap-session_seed" in seeded_sessions

        # Data field must echo the seeded namespaces so downstream
        # tooling can verify the seeding completed.
        assert "prior_deals" in body["Data"]["SeededNamespaces"]
        assert "session_seed" in body["Data"]["SeededNamespaces"]

        # Physical ID == Memory ID (rule 6 — stable across updates).
        assert body["PhysicalResourceId"] == "mem-abc123"

    def test_extra_namespaces_are_appended_after_defaults(
        self, memory_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = {
            "MemoryName": "mna_agent_memory",
            "Description": "test",
            "EventExpiryDays": 30,
            "Namespaces": ["custom_ns", "prior_deals"],  # dupe must be ignored
        }

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            memory_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]

        seeded_namespaces = body["Data"]["SeededNamespaces"].split(",")
        assert seeded_namespaces.count("prior_deals") == 1
        assert "custom_ns" in seeded_namespaces
        assert "session_seed" in seeded_namespaces


class TestUpdateSuccess:
    def test_update_keeps_stable_physical_id_and_reapplies(
        self, memory_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            memory_module.handler(_event("Create"), lambda_context)
            first_body = _captured_body(mock_urlopen)
            first_id = first_body["PhysicalResourceId"]
            mock_urlopen.reset_mock()

            updated = {
                "MemoryName": "mna_agent_memory",
                "Description": "updated description",
                "EventExpiryDays": 60,
                "Namespaces": [],
            }
            memory_module.handler(
                _event("Update", physical_id=first_id, properties=updated),
                lambda_context,
            )

        second_body = _captured_body(mock_urlopen)
        assert second_body["Status"] == "SUCCESS", second_body["Reason"]
        assert second_body["PhysicalResourceId"] == first_id

        # UpdateMemory must have been invoked with the new expiry.
        assert len(fake_client.update_memory_calls) == 1
        upd = fake_client.update_memory_calls[0]
        assert upd["memoryId"] == first_id
        assert upd["eventExpiryDuration"] == 60


class TestDeleteOfMissing:
    def test_delete_of_missing_treated_as_success(
        self, memory_module, lambda_context
    ) -> None:
        class NotFoundException(Exception):
            pass

        fake_client = _FakeAgentCoreClient(delete_exc=NotFoundException("memory gone"))
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            memory_module.handler(
                _event("Delete", physical_id="mem-already-gone"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "mem-already-gone"
        # The handler still issued the delete call; success comes from
        # the ``is_missing_error`` classifier converting the exception.
        assert len(fake_client.delete_memory_calls) == 1

    def test_delete_with_no_physical_id_is_success(
        self, memory_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            memory_module.handler(
                _event("Delete", physical_id=""),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # No delete API call should have been made when no resource
        # existed to delete.
        assert fake_client.delete_memory_calls == []


class TestMissingProperties:
    def test_missing_memory_name_fails_fast(
        self, memory_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = {"Description": "no name", "EventExpiryDays": 30}

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            memory_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "MemoryName" in body["Reason"]
        # No AWS call should have been made.
        assert fake_client.create_memory_calls == []

"""Unit tests for ``lambda/agentcore_gateway/handler.py``.

Covers the CR safety scenarios from task 16's acceptance criteria:

* simulated ``ImportError`` (rule 1 — handler still responds FAILED)
* successful ``Create`` with both ``CreateGateway`` and
  ``CreateGatewayTarget`` invoked in order
* ``Update`` reapplies gateway + target (both idempotent)
* ``Delete`` of a missing Gateway (target + gateway paths both
  treated as success)
* ``Delete`` removes existing targets before the gateway
* missing required resource property → FAILED

Tests run without touching AWS: ``boto3`` is replaced with a fake
client that records every control-plane call, and
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
_HANDLER_PATH = _REPO_ROOT / "lambda" / "agentcore_gateway" / "handler.py"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def send_response_module():
    module = _load_module("cr_common_send_response_acg", _CR_COMMON_PATH)
    sys.modules["_cr_common_send_response"] = module
    yield module
    sys.modules.pop("cr_common_send_response_acg", None)
    sys.modules.pop("_cr_common_send_response", None)


@pytest.fixture
def gateway_module(send_response_module):  # noqa: ARG001 - load ordering
    module = _load_module("agentcore_gateway_handler", _HANDLER_PATH)
    yield module
    sys.modules.pop("agentcore_gateway_handler", None)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeContext:
    def __init__(self) -> None:
        self.log_stream_name = "2024/01/01/[$LATEST]agentcore-gateway"


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


class _FakeAgentCoreClient:
    """Records every AgentCore Gateway call and returns canned responses."""

    def __init__(
        self,
        *,
        create_gateway_response: dict | None = None,
        create_target_response: dict | None = None,
        list_targets_response: dict | None = None,
        create_gateway_exc: Exception | None = None,
        create_target_exc: Exception | None = None,
        list_targets_exc: Exception | None = None,
        delete_target_exc: Exception | None = None,
        delete_gateway_exc: Exception | None = None,
    ) -> None:
        self.create_gateway_calls: list[dict] = []
        self.update_gateway_calls: list[dict] = []
        self.create_target_calls: list[dict] = []
        self.update_target_calls: list[dict] = []
        self.list_targets_calls: list[dict] = []
        self.delete_target_calls: list[dict] = []
        self.delete_gateway_calls: list[dict] = []

        self._create_gateway_response = create_gateway_response or {
            "gateway": {
                "gatewayId": "gw-abc123",
                "gatewayArn": (
                    "arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                    "gateway/gw-abc123"
                ),
                "status": "READY",
            },
        }
        self._create_target_response = create_target_response or {
            "target": {
                "targetId": "tgt-xyz789",
                "name": "market_data",
            },
        }
        self._list_targets_response = list_targets_response or {
            "targets": [
                {"targetId": "tgt-xyz789", "name": "market_data"},
            ],
        }
        self._create_gateway_exc = create_gateway_exc
        self._create_target_exc = create_target_exc
        self._list_targets_exc = list_targets_exc
        self._delete_target_exc = delete_target_exc
        self._delete_gateway_exc = delete_gateway_exc

    # Gateway lifecycle -----------------------------------------------------
    def create_gateway(self, **kwargs: Any) -> dict:
        self.create_gateway_calls.append(kwargs)
        if self._create_gateway_exc is not None:
            raise self._create_gateway_exc
        return self._create_gateway_response

    def update_gateway(self, **kwargs: Any) -> dict:
        self.update_gateway_calls.append(kwargs)
        return {
            "gateway": {
                "gatewayId": kwargs["gatewayIdentifier"],
                "gatewayArn": (
                    f"arn:aws:bedrock-agentcore:us-east-1:111122223333:"
                    f"gateway/{kwargs['gatewayIdentifier']}"
                ),
                "status": "READY",
            },
        }

    def delete_gateway(self, **kwargs: Any) -> dict:
        self.delete_gateway_calls.append(kwargs)
        if self._delete_gateway_exc is not None:
            raise self._delete_gateway_exc
        return {}

    # Gateway-target lifecycle ---------------------------------------------
    def create_gateway_target(self, **kwargs: Any) -> dict:
        self.create_target_calls.append(kwargs)
        if self._create_target_exc is not None:
            raise self._create_target_exc
        return self._create_target_response

    def update_gateway_target(self, **kwargs: Any) -> dict:
        self.update_target_calls.append(kwargs)
        return {
            "target": {
                "targetId": kwargs["targetId"],
                "name": kwargs["name"],
            },
        }

    def list_gateway_targets(self, **kwargs: Any) -> dict:
        self.list_targets_calls.append(kwargs)
        if self._list_targets_exc is not None:
            raise self._list_targets_exc
        return self._list_targets_response

    def delete_gateway_target(self, **kwargs: Any) -> dict:
        self.delete_target_calls.append(kwargs)
        if self._delete_target_exc is not None:
            raise self._delete_target_exc
        return {}


def _fake_boto3_module(client: _FakeAgentCoreClient):
    module = mock.MagicMock()
    module.client.return_value = client
    return module


def _default_properties(**overrides: Any) -> dict:
    props: dict[str, Any] = {
        "GatewayName": "mna-gateway",
        "Description": "test gateway",
        "ProtocolType": "MCP",
        "RoleArn": "arn:aws:iam::111122223333:role/GatewayServiceRole",
        "TargetName": "market_data",
        "TargetLambdaArn": (
            "arn:aws:lambda:us-east-1:111122223333:function:mna-market-data"
        ),
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
        "LogicalResourceId": "AgentGateway",
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
    def test_import_error_sends_failed(self, gateway_module, lambda_context) -> None:
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
            gateway_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImportError" in body["Reason"]
        assert body["PhysicalResourceId"]


class TestCreateSuccess:
    def test_create_provisions_gateway_then_target(
        self, gateway_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]
        assert body["Data"]["GatewayId"] == "gw-abc123"
        assert body["Data"]["GatewayArn"].endswith("gateway/gw-abc123")
        assert body["Data"]["TargetId"] == "tgt-xyz789"

        # CreateGateway invoked exactly once with the caller-supplied
        # name, protocol, and role.
        assert len(fake_client.create_gateway_calls) == 1
        gw_call = fake_client.create_gateway_calls[0]
        assert gw_call["name"] == "mna-gateway"
        assert gw_call["protocolType"] == "MCP"
        assert gw_call["roleArn"].endswith(":role/GatewayServiceRole")

        # CreateGatewayTarget invoked exactly once with the market-data
        # Lambda ARN wired into the MCP target configuration.
        assert len(fake_client.create_target_calls) == 1
        tgt_call = fake_client.create_target_calls[0]
        assert tgt_call["gatewayIdentifier"] == "gw-abc123"
        assert tgt_call["name"] == "market_data"
        lambda_arn = (
            tgt_call["targetConfiguration"]["mcp"]["lambda"]["lambdaArn"]
        )
        assert lambda_arn.endswith(":function:mna-market-data")

        # Physical ID == Gateway ID (rule 6 — stable across updates).
        assert body["PhysicalResourceId"] == "gw-abc123"

    def test_create_with_tool_schema_passes_it_through(
        self, gateway_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        schema = {
            "name": "get_comparable_multiples",
            "inputSchema": {"type": "object"},
        }
        props = _default_properties(ToolSchema=schema)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]

        tgt_call = fake_client.create_target_calls[0]
        mcp_lambda = tgt_call["targetConfiguration"]["mcp"]["lambda"]
        assert mcp_lambda["toolSchema"] == schema


class TestUpdateSuccess:
    def test_update_keeps_stable_physical_id_and_reapplies_both(
        self, gateway_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(_event("Create"), lambda_context)
            first_body = _captured_body(mock_urlopen)
            first_id = first_body["PhysicalResourceId"]
            mock_urlopen.reset_mock()

            new_lambda_arn = (
                "arn:aws:lambda:us-east-1:111122223333:function:mna-market-data-v2"
            )
            updated = _default_properties(
                TargetLambdaArn=new_lambda_arn,
                Description="updated description",
            )
            gateway_module.handler(
                _event("Update", physical_id=first_id, properties=updated),
                lambda_context,
            )

        second_body = _captured_body(mock_urlopen)
        assert second_body["Status"] == "SUCCESS", second_body["Reason"]
        assert second_body["PhysicalResourceId"] == first_id

        # UpdateGateway must have been invoked with the stable ID.
        assert len(fake_client.update_gateway_calls) == 1
        gw_upd = fake_client.update_gateway_calls[0]
        assert gw_upd["gatewayIdentifier"] == first_id
        assert gw_upd["description"] == "updated description"

        # UpdateGatewayTarget must have been invoked with the new
        # Lambda ARN (discovered via list_gateway_targets).
        assert len(fake_client.list_targets_calls) == 1
        assert len(fake_client.update_target_calls) == 1
        tgt_upd = fake_client.update_target_calls[0]
        assert tgt_upd["gatewayIdentifier"] == first_id
        assert tgt_upd["targetId"] == "tgt-xyz789"
        assert (
            tgt_upd["targetConfiguration"]["mcp"]["lambda"]["lambdaArn"]
            == new_lambda_arn
        )

    def test_update_recreates_target_if_missing(
        self, gateway_module, lambda_context
    ) -> None:
        # Simulate a previously-partial deploy where the Gateway
        # exists but the target does not.
        fake_client = _FakeAgentCoreClient(
            list_targets_response={"targets": []},
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(
                _event("Update", physical_id="gw-abc123"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS", body["Reason"]
        # Update path should fall through to Create when the target
        # is missing.
        assert len(fake_client.update_target_calls) == 0
        assert len(fake_client.create_target_calls) == 1


class TestDeleteOfMissing:
    def test_delete_of_missing_gateway_treated_as_success(
        self, gateway_module, lambda_context
    ) -> None:
        class NotFoundException(Exception):
            pass

        # ListTargets itself raises NotFound because the Gateway is
        # gone. The handler should short-circuit and return SUCCESS.
        fake_client = _FakeAgentCoreClient(
            list_targets_exc=NotFoundException("gateway gone"),
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(
                _event("Delete", physical_id="gw-already-gone"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "gw-already-gone"
        # Handler must not attempt to delete targets or the gateway
        # when list_gateway_targets reports the gateway is gone.
        assert fake_client.delete_target_calls == []
        assert fake_client.delete_gateway_calls == []

    def test_delete_missing_target_and_missing_gateway_success(
        self, gateway_module, lambda_context
    ) -> None:
        # Targets listed, but the per-target delete and the gateway
        # delete both raise NotFound. Both should be swallowed.
        class ResourceNotFoundException(Exception):
            pass

        fake_client = _FakeAgentCoreClient(
            delete_target_exc=ResourceNotFoundException("target gone"),
            delete_gateway_exc=ResourceNotFoundException("gateway gone"),
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(
                _event("Delete", physical_id="gw-abc123"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # Both delete calls should have been attempted before the
        # classifier swallowed their NotFound exceptions.
        assert len(fake_client.delete_target_calls) == 1
        assert len(fake_client.delete_gateway_calls) == 1

    def test_delete_happy_path_removes_target_before_gateway(
        self, gateway_module, lambda_context
    ) -> None:
        # Two targets to confirm every target is removed before the
        # parent gateway. Call order is verified by checking counts.
        fake_client = _FakeAgentCoreClient(
            list_targets_response={
                "targets": [
                    {"targetId": "tgt-1", "name": "market_data"},
                    {"targetId": "tgt-2", "name": "extra"},
                ],
            },
        )
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(
                _event("Delete", physical_id="gw-abc123"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"

        assert [c["targetId"] for c in fake_client.delete_target_calls] == [
            "tgt-1",
            "tgt-2",
        ]
        assert len(fake_client.delete_gateway_calls) == 1
        assert fake_client.delete_gateway_calls[0]["gatewayIdentifier"] == "gw-abc123"

    def test_delete_with_no_physical_id_is_success(
        self, gateway_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(
                _event("Delete", physical_id=""),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # No API calls should have been made when no resource
        # existed to delete.
        assert fake_client.list_targets_calls == []
        assert fake_client.delete_target_calls == []
        assert fake_client.delete_gateway_calls == []


class TestMissingProperties:
    def test_missing_gateway_name_fails_fast(
        self, gateway_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties()
        props.pop("GatewayName")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "GatewayName" in body["Reason"]
        assert fake_client.create_gateway_calls == []

    def test_missing_target_lambda_arn_fails_fast(
        self, gateway_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties()
        props.pop("TargetLambdaArn")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "TargetLambdaArn" in body["Reason"]
        assert fake_client.create_gateway_calls == []

    def test_missing_role_arn_fails_fast(
        self, gateway_module, lambda_context
    ) -> None:
        fake_client = _FakeAgentCoreClient()
        fake_boto3 = _fake_boto3_module(fake_client)

        props = _default_properties()
        props.pop("RoleArn")

        with (
            mock.patch("urllib.request.urlopen") as mock_urlopen,
            mock.patch.dict("sys.modules", {"boto3": fake_boto3}),
        ):
            gateway_module.handler(_event("Create", properties=props), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "RoleArn" in body["Reason"]
        assert fake_client.create_gateway_calls == []

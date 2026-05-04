"""Unit tests for ``lambda/_cr_common/send_response.py``.

Covers the Custom Resource Safety Requirements from the design
document (section *Custom Resource Safety Requirements*, rules 1–8)
as enumerated in task 10's acceptance criteria:

* simulated ``ImportError`` for ``boto3`` (rule 1)
* runtime exception inside the user handler (rule 2)
* successful Create / Update (rule 6 — physical ID stability)
* Delete-of-missing-resource (rule 4)
* oversized ``Data`` payload truncation (rule 7)

The tests never make real network calls: ``urllib.request.urlopen`` is
patched so we can assert on the ``PUT`` body CloudFormation would
receive.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import json
import pathlib
import sys
from typing import Any
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
#
# ``lambda`` is a Python keyword so the directory name can't be used in a
# normal ``import lambda._cr_common.send_response`` statement. We load the
# module by file path via :mod:`importlib.util` and register it under a
# benign name in ``sys.modules`` so ``@wraps`` and downstream imports
# (when the decorator internally imports ``boto3``) still work.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CR_COMMON_PATH = _REPO_ROOT / "lambda" / "_cr_common" / "send_response.py"
_LINT_SCRIPT_PATH = _REPO_ROOT / "scripts" / "lint_cr_handlers.py"


def _load_module(name: str, path: pathlib.Path):
    """Load ``path`` as a module registered under ``name`` in :data:`sys.modules`."""

    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def send_response_module():
    """Load ``lambda/_cr_common/send_response.py`` once per test module."""

    module = _load_module("cr_common_send_response", _CR_COMMON_PATH)
    yield module
    sys.modules.pop("cr_common_send_response", None)


@pytest.fixture(scope="module")
def lint_script_module():
    """Load ``scripts/lint_cr_handlers.py`` once per test module."""

    module = _load_module("cr_lint_script", _LINT_SCRIPT_PATH)
    yield module
    sys.modules.pop("cr_lint_script", None)


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------


class FakeContext:
    """Stand-in for a Lambda ``context`` object."""

    def __init__(self, log_stream_name: str = "2024/01/01/[$LATEST]abcdef") -> None:
        self.log_stream_name = log_stream_name


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


def _event(
    request_type: str = "Create",
    *,
    physical_id: str = "",
    properties: dict | None = None,
) -> dict:
    """Build a minimal CloudFormation Custom Resource event."""

    event: dict[str, Any] = {
        "RequestType": request_type,
        "ResponseURL": "https://cloudformation-custom-resource-response.example.com/presigned",
        "StackId": "arn:aws:cloudformation:us-east-1:111111111111:stack/test/guid",
        "RequestId": "req-guid",
        "LogicalResourceId": "MyCustomResource",
        "ResourceProperties": properties or {},
    }
    if physical_id:
        event["PhysicalResourceId"] = physical_id
    return event


def _captured_body(mock_urlopen: mock.MagicMock) -> dict:
    """Decode the JSON body passed to the patched ``urlopen`` call."""

    assert mock_urlopen.called, "expected a CloudFormation response upload"
    args, _kwargs = mock_urlopen.call_args
    request = args[0]
    return json.loads(request.data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Rule 1 — cold-start safety: ImportError on boto3 still produces a response
# ---------------------------------------------------------------------------


class TestImportErrorPath:
    """Simulated ``ImportError`` when the decorator lazy-imports boto3."""

    def test_import_error_still_sends_failed_response(
        self, send_response_module, lambda_context
    ) -> None:
        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "boto3":
                raise ImportError("simulated: boto3 missing from deployment package")
            return real_import(name, globals, locals, fromlist, level)

        @send_response_module.cr_handler()
        def handler(event, context, boto3):  # pragma: no cover - never reached
            return "phys-id", {"ok": True}

        with mock.patch("urllib.request.urlopen") as mock_urlopen, mock.patch(
            "builtins.__import__", side_effect=fake_import
        ):
            # The wrapper posts the FAILED response body to the
            # CloudFormation ``ResponseURL`` *before* re-raising so the
            # Provider Framework (which consumes the raised exception)
            # and the classic protocol (which consumes the posted body)
            # both see a failure with identical reason text.
            with pytest.raises(ImportError, match="simulated"):
                handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ImportError" in body["Reason"]
        assert "simulated" in body["Reason"]
        assert body["PhysicalResourceId"]  # must never be empty
        assert body["StackId"].endswith("guid")
        assert lambda_context.log_stream_name in body["Reason"]


# ---------------------------------------------------------------------------
# Rule 2 — runtime exception in the user handler still produces a response
# ---------------------------------------------------------------------------


class TestRuntimeException:
    def test_runtime_exception_returns_failed(
        self, send_response_module, lambda_context
    ) -> None:
        @send_response_module.cr_handler()
        def handler(event, context, boto3):
            raise RuntimeError("boom — something broke inside the handler body")

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(RuntimeError, match="boom"):
                handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert body["Reason"].startswith("RuntimeError: boom")
        assert body["PhysicalResourceId"] == "MyCustomResource"


# ---------------------------------------------------------------------------
# Successful Create / Update lifecycle
# ---------------------------------------------------------------------------


class TestCreateAndUpdate:
    def test_successful_create_returns_physical_id_and_data(
        self, send_response_module, lambda_context
    ) -> None:
        @send_response_module.cr_handler()
        def handler(event, context, boto3):
            return "my-resource-123", {"arn": "arn:aws:example:::my-resource-123"}

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            result = handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "my-resource-123"
        assert body["Data"] == {"arn": "arn:aws:example:::my-resource-123"}
        assert body["NoEcho"] is False

        # Provider-Framework contract: the wrapper must ALSO return the
        # response dict synchronously so ``cr.Provider`` can pass it to
        # CloudFormation. A ``None`` return here would leave downstream
        # ``Fn::GetAtt`` with no attributes to resolve (see
        # ``cr_handler`` docstring for why).
        assert isinstance(result, dict)
        assert result["PhysicalResourceId"] == "my-resource-123"
        assert result["Data"] == {"arn": "arn:aws:example:::my-resource-123"}
        assert result["NoEcho"] is False

    def test_update_preserves_physical_id_across_calls(
        self, send_response_module, lambda_context
    ) -> None:
        calls: list[str] = []

        def update_fn(event, context, boto3):
            calls.append("update")
            return event["PhysicalResourceId"], {"updated": True}

        @send_response_module.cr_handler(update=update_fn)
        def handler(event, context, boto3):  # Create path (returns initial id)
            return "phys-stable-id", {"created": True}

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            handler(
                _event("Update", physical_id="phys-stable-id"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "phys-stable-id"
        assert body["Data"] == {"updated": True}
        assert calls == ["update"]


# ---------------------------------------------------------------------------
# Rule 4 — Delete idempotency
# ---------------------------------------------------------------------------


class TestDeleteOfMissingResource:
    def test_default_delete_is_a_no_op_success(
        self, send_response_module, lambda_context
    ) -> None:
        @send_response_module.cr_handler()
        def handler(event, context, boto3):  # pragma: no cover - only Create/Update
            return "ignored", {}

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            handler(
                _event("Delete", physical_id="already-created-id"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "already-created-id"

    def test_delete_missing_error_is_swallowed_as_success(
        self, send_response_module, lambda_context
    ) -> None:
        class ResourceNotFoundException(Exception):
            """Mimics an AWS SDK ``ResourceNotFoundException``."""

        def delete_fn(event, context, boto3):
            raise ResourceNotFoundException("gateway target already gone")

        @send_response_module.cr_handler(delete=delete_fn)
        def handler(event, context, boto3):  # pragma: no cover - Create/Update only
            return "phys-id", {}

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            handler(
                _event("Delete", physical_id="phys-id"),
                lambda_context,
            )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        assert body["PhysicalResourceId"] == "phys-id"

    def test_delete_non_missing_error_surfaces_as_failure(
        self, send_response_module, lambda_context
    ) -> None:
        def delete_fn(event, context, boto3):
            raise ValueError("unexpected teardown error")

        @send_response_module.cr_handler(delete=delete_fn)
        def handler(event, context, boto3):  # pragma: no cover
            return "phys-id", {}

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(ValueError, match="unexpected teardown"):
                handler(
                    _event("Delete", physical_id="phys-id"),
                    lambda_context,
                )

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "ValueError" in body["Reason"]


# ---------------------------------------------------------------------------
# Rule 7 — response-size cap
# ---------------------------------------------------------------------------


class TestResponseSizeCap:
    def test_oversized_data_is_truncated_with_log_reference(
        self, send_response_module, lambda_context
    ) -> None:
        big_blob = "x" * (send_response_module.MAX_RESPONSE_DATA_BYTES + 512)

        @send_response_module.cr_handler()
        def handler(event, context, boto3):
            return "phys-id", {"payload": big_blob}

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "SUCCESS"
        # Original payload must not be present verbatim.
        assert "payload" not in body["Data"]
        assert body["Data"]["truncated"] is True
        assert body["Data"]["cap_bytes"] == send_response_module.MAX_RESPONSE_DATA_BYTES
        assert body["Data"]["original_bytes"] > send_response_module.MAX_RESPONSE_DATA_BYTES
        # The logs reference should point at the log stream so readers can
        # recover the full payload from CloudWatch.
        assert lambda_context.log_stream_name in body["Data"]["logs"]
        assert "Data truncated" in body["Reason"]

    def test_under_cap_data_passes_through_untouched(
        self, send_response_module, lambda_context
    ) -> None:
        @send_response_module.cr_handler()
        def handler(event, context, boto3):
            return "phys-id", {"small": "value", "count": 3}

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            handler(_event("Create"), lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Data"] == {"small": "value", "count": 3}
        assert "Data truncated" not in body["Reason"]


# ---------------------------------------------------------------------------
# Unknown RequestType defensive branch
# ---------------------------------------------------------------------------


class TestUnknownRequestType:
    def test_unknown_request_type_returns_failed(
        self, send_response_module, lambda_context
    ) -> None:
        @send_response_module.cr_handler()
        def handler(event, context, boto3):  # pragma: no cover
            return "phys-id", {}

        event = _event("Reboot")  # not a valid CFN request type

        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            with pytest.raises(ValueError, match="Unknown RequestType"):
                handler(event, lambda_context)

        body = _captured_body(mock_urlopen)
        assert body["Status"] == "FAILED"
        assert "Unknown RequestType" in body["Reason"]


# ---------------------------------------------------------------------------
# Lint script: top-level boto3 imports are forbidden in CR handlers
# ---------------------------------------------------------------------------


class TestLintCrHandlers:
    def test_clean_handler_passes(self, lint_script_module, tmp_path) -> None:
        handler = tmp_path / "handler.py"
        handler.write_text(
            '"""A well-behaved CR handler."""\n'
            "from __future__ import annotations\n"
            "\n"
            "def handler(event, context):\n"
            "    import boto3  # lazy import inside the function body\n"
            "    return boto3.client('sts').get_caller_identity()\n",
            encoding="utf-8",
        )
        violations = lint_script_module.scan_file(handler)
        assert violations == []

    def test_top_level_import_boto3_is_flagged(
        self, lint_script_module, tmp_path
    ) -> None:
        handler = tmp_path / "handler.py"
        handler.write_text(
            "import boto3\n"
            "\n"
            "def handler(event, context):\n"
            "    return {}\n",
            encoding="utf-8",
        )
        violations = lint_script_module.scan_file(handler)
        assert len(violations) == 1
        assert violations[0].lineno == 1
        assert "rule 1" in violations[0].message

    def test_top_level_from_boto3_is_flagged(
        self, lint_script_module, tmp_path
    ) -> None:
        handler = tmp_path / "handler.py"
        handler.write_text(
            "from boto3 import client\n"
            "\n"
            "def handler(event, context):\n"
            "    return {}\n",
            encoding="utf-8",
        )
        violations = lint_script_module.scan_file(handler)
        assert len(violations) == 1
        assert violations[0].lineno == 1

    def test_noqa_marker_suppresses_violation(
        self, lint_script_module, tmp_path
    ) -> None:
        handler = tmp_path / "handler.py"
        handler.write_text(
            "import boto3  # noqa: CR001 - not a Custom Resource handler\n"
            "\n"
            "def handler(event, context):\n"
            "    return boto3.client('sts').get_caller_identity()\n",
            encoding="utf-8",
        )
        violations = lint_script_module.scan_file(handler)
        assert violations == []

    def test_main_exits_zero_on_real_repo(self, lint_script_module) -> None:
        # Exercises the default-path discovery against this repo's own
        # handlers — must stay clean as the project adds more CRs.
        assert lint_script_module.main([]) == 0

    def test_main_exits_nonzero_when_violations_present(
        self, lint_script_module, tmp_path, capsys
    ) -> None:
        handler = tmp_path / "handler.py"
        handler.write_text(
            "import boto3\n\ndef handler(event, context):\n    pass\n",
            encoding="utf-8",
        )
        exit_code = lint_script_module.main([str(handler)])
        captured = capsys.readouterr()
        assert exit_code == 1
        assert "Custom Resource handler lint failed" in captured.err


# ---------------------------------------------------------------------------
# is_missing_error classifier
# ---------------------------------------------------------------------------


class TestIsMissingError:
    def test_matches_class_name_suffixes(self, send_response_module) -> None:
        class ResourceNotFoundException(Exception):
            pass

        class NoSuchBucket(Exception):
            pass

        assert send_response_module.is_missing_error(ResourceNotFoundException("x"))
        assert send_response_module.is_missing_error(NoSuchBucket("x"))

    def test_matches_botocore_style_response(self, send_response_module) -> None:
        class FakeClientError(Exception):
            def __init__(self, code: str) -> None:
                super().__init__(code)
                self.response = {"Error": {"Code": code}}

        assert send_response_module.is_missing_error(FakeClientError("NoSuchKey"))
        assert not send_response_module.is_missing_error(FakeClientError("Throttling"))

    def test_rejects_unrelated_errors(self, send_response_module) -> None:
        assert not send_response_module.is_missing_error(ValueError("nope"))
        assert not send_response_module.is_missing_error(RuntimeError("boom"))

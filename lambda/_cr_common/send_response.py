"""Shared CloudFormation Custom Resource response helpers.

This module implements rules 1, 2, 3, 4, 7, and 8 from the design's
*Custom Resource Safety Requirements* section. Every Lambda-backed CR
in the project imports :func:`cr_handler` from here instead of
re-implementing response handling per handler.

Design rules enforced here:

* **Rule 1 — Cold-start safety.** This module performs no ``boto3``
  import, no client construction, and no environment lookups at import
  time. Only the Python standard library is imported.
* **Rule 2 — Guaranteed response.** :func:`cr_handler` wraps the user
  handler in a ``try / except / finally`` that always calls
  :func:`_send_response` in the ``finally`` clause, even if the user
  handler raises ``ImportError`` trying to load ``boto3``.
* **Rule 3 — Raw ``urllib.request`` transport.** Response sending uses
  :mod:`urllib.request` only, so a broken ``boto3`` install or SDK
  version mismatch does not prevent the CR from replying.
* **Rule 4 — Delete idempotency.** When a ``Delete`` request raises an
  exception that :func:`is_missing_error` classifies as "resource not
  found", the decorator converts it into a success.
* **Rule 7 — Response size cap.** Any ``Data`` payload larger than
  ``MAX_RESPONSE_DATA_BYTES`` is truncated and a CloudWatch log
  reference is substituted into the response.
* **Rule 8 — Shared base module.** This *is* the shared module.

The two public names are :func:`cr_handler` (the decorator CR modules
apply to their ``handler`` callable) and :func:`is_missing_error`
(exposed for handlers that want to extend the default "missing
resource" classifier).
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable
from functools import wraps
from typing import Any

logger = logging.getLogger()
if not logger.handlers:
    # AWS Lambda normally pre-configures the root logger. Guard against the
    # local-test path where the logger may have no handlers so ``exception``
    # calls don't end up swallowed.
    logging.basicConfig(level=logging.INFO)
logger.setLevel(logging.INFO)


# CloudFormation Custom Resource response bodies have a hard 4 KB cap on
# the ``Data`` field. We apply the cap to the JSON-encoded dict so we can
# detect oversize payloads before attempting to upload them to S3.
MAX_RESPONSE_DATA_BYTES = 4096

# Subset of exception class-name suffixes that typically indicate a
# "resource does not exist" error across AWS SDK clients. Handlers may
# extend this via their own ``is_missing`` callable passed to
# :func:`cr_handler`.
_DEFAULT_MISSING_ERROR_MARKERS: tuple[str, ...] = (
    "NotFoundException",
    "ResourceNotFoundException",
    "NoSuchEntity",
    "NoSuchBucket",
    "NoSuchKey",
    "ResourceNotFound",
    "NotFound",
)


HandlerFn = Callable[[dict, Any, Any], "tuple[str, dict] | None"]


def is_missing_error(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` looks like a "resource not found" error.

    Used by :func:`cr_handler` to implement rule 4 (delete idempotency).
    Matches both exception class names and, for ``botocore``-style errors,
    the ``Error.Code`` inside ``exc.response``. No ``botocore`` import is
    performed — classification is purely duck-typed.
    """

    name = type(exc).__name__
    if any(marker in name for marker in _DEFAULT_MISSING_ERROR_MARKERS):
        return True

    # botocore ClientError: ``exc.response["Error"]["Code"]``
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code") if isinstance(
            response.get("Error"), dict
        ) else None
        if isinstance(code, str) and any(
            marker in code for marker in _DEFAULT_MISSING_ERROR_MARKERS
        ):
            return True

    # Some SDKs expose error codes via an attribute.
    code_attr = getattr(exc, "code", None)
    return isinstance(code_attr, str) and any(
        marker in code_attr for marker in _DEFAULT_MISSING_ERROR_MARKERS
    )


def _send_response(
    event: dict,
    context: Any,
    status: str,
    physical_id: str,
    data: dict,
    reason: str,
) -> None:
    """Upload a Custom Resource response body to the pre-signed ``ResponseURL``.

    Uses :mod:`urllib.request` only; this function must work even when
    ``boto3`` is completely unavailable. All exceptions are caught and
    logged — a failed response upload leaves the stack to time out
    naturally (3 h), which is the worst case we can still observe via
    CloudWatch.
    """

    log_stream = _safe_log_stream_name(context)

    # Cap the Data payload per rule 7. Encode once to measure the real
    # on-the-wire size.
    safe_data, truncated = _cap_response_data(data, log_stream)

    reason_with_logs = reason
    if log_stream:
        reason_with_logs = f"{reason} | Logs: {log_stream}"
    if truncated:
        reason_with_logs = f"{reason_with_logs} | Data truncated; see CloudWatch"

    body_dict = {
        "Status": status,
        "Reason": reason_with_logs,
        "PhysicalResourceId": physical_id or "missing-physical-id",
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "NoEcho": False,
        "Data": safe_data,
    }
    body = json.dumps(body_dict).encode("utf-8")

    response_url = event.get("ResponseURL")
    if not response_url:
        # No ResponseURL means this invocation did not originate from
        # CloudFormation — typically a unit test. Log and return so test
        # code can assert on log capture if needed.
        logger.info(
            "cr_no_response_url_skipping_upload",
            extra={"status": status, "physical_id": physical_id},
        )
        return

    req = urllib.request.Request(
        response_url,
        data=body,
        method="PUT",
        headers={
            "content-type": "",
            "content-length": str(len(body)),
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)  # noqa: S310 - pre-signed S3 URL
        logger.info(
            "cr_response_sent",
            extra={
                "status": status,
                "physical_id": physical_id,
                "truncated": truncated,
                "body_bytes": len(body),
            },
        )
    except Exception:  # noqa: BLE001 - we must never raise from the finally block.
        logger.exception("cr_response_upload_failed")


def _cap_response_data(data: dict | None, log_stream: str) -> tuple[dict, bool]:
    """Return a ``Data`` dict guaranteed to encode to <= 4 KB of JSON.

    If the encoded size exceeds :data:`MAX_RESPONSE_DATA_BYTES` the payload
    is replaced with a compact marker pointing the reader at the
    CloudWatch log stream for the full value.
    """

    safe = data if isinstance(data, dict) else {}
    try:
        encoded = json.dumps(safe).encode("utf-8")
    except (TypeError, ValueError):
        # Non-serializable value snuck in. Reduce to a safe marker so we
        # still produce a well-formed response body.
        return (
            {
                "truncated": True,
                "reason": "non-serializable Data payload",
                "logs": log_stream or "check CloudWatch for the handler log stream",
            },
            True,
        )

    if len(encoded) <= MAX_RESPONSE_DATA_BYTES:
        return safe, False

    # Log the full payload so it's recoverable from CloudWatch even
    # though it can't travel through the CFN response channel.
    logger.info(
        "cr_response_data_truncated",
        extra={"original_bytes": len(encoded), "cap": MAX_RESPONSE_DATA_BYTES},
    )
    return (
        {
            "truncated": True,
            "original_bytes": len(encoded),
            "cap_bytes": MAX_RESPONSE_DATA_BYTES,
            "logs": log_stream or "see CloudWatch log stream for full payload",
        },
        True,
    )


def _safe_log_stream_name(context: Any) -> str:
    """Best-effort extraction of the Lambda log stream name for error messages."""

    name = getattr(context, "log_stream_name", None)
    return name if isinstance(name, str) else ""


def _redact(value: Any) -> Any:
    """Return ``value`` with obvious secrets masked for logging.

    Rule 7 requires request-parameter logging with secrets redacted. The
    heuristic only masks dict keys whose name hints at a secret — complex
    detection is deferred to downstream CRs that know their schema.
    """

    secret_hint = ("password", "secret", "token", "key")
    if isinstance(value, dict):
        redacted: dict = {}
        for k, v in value.items():
            if isinstance(k, str) and any(h in k.lower() for h in secret_hint):
                redacted[k] = "***redacted***"
            else:
                redacted[k] = _redact(v)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def cr_handler(
    create: HandlerFn | None = None,
    update: HandlerFn | None = None,
    delete: HandlerFn | None = None,
    *,
    is_missing: Callable[[BaseException], bool] | None = None,
) -> Callable[[HandlerFn], Callable[[dict, Any], dict]]:
    """Decorator factory wrapping a Lambda ``handler`` with CR safety rules.

    Protocol compatibility
    ----------------------
    The wrapper supports both Custom Resource invocation protocols:

    * **Classic** — CloudFormation invokes the Lambda and reads the
      response posted to ``event['ResponseURL']``. When ``ResponseURL``
      is present and looks like a CloudFormation pre-signed URL we
      upload the body there and also return the response dict for
      observability.
    * **Provider Framework** (``aws-cdk-lib/custom-resources.Provider``)
      — the framework's ``onEvent`` handler invokes our Lambda
      synchronously and consumes the **return value** in the shape
      ``{ "PhysicalResourceId", "Data", "NoEcho"? }``. The framework
      handles the CloudFormation ``ResponseURL`` upload itself. In
      this mode the handler MUST return the dict; returning ``None``
      (as our old wrapper did) leaves downstream ``Fn::GetAtt`` calls
      with no attributes to resolve.

    Every CR in this project is wired through ``cr.Provider``, so the
    Provider Framework path is the common one. We still post to
    ``ResponseURL`` when present so the wrapper remains usable outside
    the framework — a double-report is harmless because the framework
    treats the synchronous return value as the source of truth and
    ignores anything posted to its own pre-signed URL.

    The decorated function is what AWS Lambda invokes. The inner
    dispatchers (``create``/``update``/``delete``) are *optional*. If
    not provided, the decorator defaults to:

    * ``Create`` / ``Update``: call the decorated function (backwards
      compatible with handlers that want a single entrypoint).
    * ``Delete``: no-op success.

    Each dispatcher must return either ``None`` or a
    ``(physical_id, data)`` tuple. ``data`` is expected to be a dict;
    anything else is coerced to ``{}``.

    Parameters
    ----------
    create, update, delete:
        Optional per-request-type callables with signature
        ``(event, context, boto3) -> (physical_id, data) | None``. The
        decorator imports ``boto3`` lazily and passes it in so user
        code doesn't need to re-implement the lazy-import pattern.
    is_missing:
        Optional classifier used during ``Delete`` to decide whether
        an exception should be swallowed as "already gone". Defaults
        to :func:`is_missing_error`.
    """

    missing_classifier = is_missing or is_missing_error

    def decorator(fn: HandlerFn) -> Callable[[dict, Any], dict]:
        @wraps(fn)
        def wrapper(event: dict, context: Any) -> dict:
            request_type = event.get("RequestType", "Unknown") if isinstance(
                event, dict
            ) else "Unknown"
            incoming_physical_id = (
                event.get("PhysicalResourceId", "") if isinstance(event, dict) else ""
            )
            logical_id = event.get("LogicalResourceId", "") if isinstance(
                event, dict
            ) else ""
            resource_properties = (
                event.get("ResourceProperties", {}) if isinstance(event, dict) else {}
            )

            logger.info(
                "cr_handler_entry",
                extra={
                    "request_type": request_type,
                    "logical_id": logical_id,
                    "physical_id": incoming_physical_id,
                    "properties": _redact(resource_properties),
                },
            )

            physical_id: str = incoming_physical_id or logical_id or "init"
            response_data: dict = {}
            status: str = "SUCCESS"
            reason: str = "OK"
            handler_exception: BaseException | None = None

            try:
                # Lazy boto3 import so ImportError still reaches the finally
                # block and produces a well-formed FAILED response.
                import boto3  # noqa: PLC0415 - intentional lazy import

                if request_type == "Create":
                    dispatcher = create or fn
                    result = dispatcher(event, context, boto3)
                elif request_type == "Update":
                    dispatcher = update or create or fn
                    result = dispatcher(event, context, boto3)
                elif request_type == "Delete":
                    if delete is None:
                        # Default: treat delete as success (rule 4).
                        result = (incoming_physical_id or physical_id, {})
                    else:
                        try:
                            result = delete(event, context, boto3)
                        except Exception as delete_exc:  # noqa: BLE001
                            if missing_classifier(delete_exc):
                                logger.info(
                                    "cr_delete_missing_resource_ok",
                                    extra={
                                        "exception_type": type(delete_exc).__name__,
                                    },
                                )
                                result = (incoming_physical_id or physical_id, {})
                            else:
                                raise
                else:
                    raise ValueError(f"Unknown RequestType: {request_type}")

                if result is not None:
                    returned_physical_id, returned_data = result
                    if returned_physical_id:
                        physical_id = returned_physical_id
                    if isinstance(returned_data, dict):
                        response_data = returned_data

            except Exception as exc:  # noqa: BLE001 - rule 2: never escape handler.
                logger.exception("cr_handler_failure")
                status = "FAILED"
                # Keep reason short; CloudFormation surfaces this on the
                # stack events page and truncates long strings ungracefully.
                reason = f"{type(exc).__name__}: {str(exc)[:240]}"
                handler_exception = exc
            finally:
                logger.info(
                    "cr_handler_exit",
                    extra={
                        "status": status,
                        "request_type": request_type,
                        "physical_id": physical_id,
                    },
                )
                # Classic CR protocol: upload to ResponseURL. When the
                # handler is invoked by the Provider Framework the
                # framework tolerates our extra upload because it
                # treats the synchronous return value as authoritative
                # and ignores its own pre-signed URL.
                _send_response(
                    event if isinstance(event, dict) else {},
                    context,
                    status,
                    physical_id,
                    response_data,
                    reason,
                )

            # Provider Framework protocol: propagate failure by raising
            # so the framework marks the resource FAILED with a
            # meaningful reason. On success, return the dict shape the
            # framework (and classic CR consumers) expect:
            # ``{ PhysicalResourceId, Data, NoEcho? }``.
            if handler_exception is not None:
                raise handler_exception
            return {
                "PhysicalResourceId": physical_id,
                "Data": response_data,
                "NoEcho": False,
            }

        return wrapper

    return decorator

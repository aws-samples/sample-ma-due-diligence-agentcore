"""Shared Custom Resource (CR) runtime helpers for Lambda-backed CRs.

Every CR Lambda in this project imports from :mod:`_cr_common.send_response`
to enforce the Custom Resource Safety Requirements documented in
``.kiro/specs/ma-due-diligence-agentcore/design.md`` (section
*Custom Resource Safety Requirements*):

1. No ``boto3`` import at module top-level (cold-start safety).
2. A guaranteed ``try / except / finally`` that always sends a response.
3. Raw ``urllib.request`` response transport (boto3-independent).
4. Delete-of-missing treated as success.
5. Response size capped at 4 KB.
6. Structured entry / exit logging.

This package deliberately avoids importing anything outside the Python
standard library so an accidentally misconfigured Lambda layer or broken
container image still lets the handler send a FAILED response to
CloudFormation.
"""

from __future__ import annotations

from .send_response import (
    MAX_RESPONSE_DATA_BYTES,
    cr_handler,
    is_missing_error,
)

__all__ = [
    "MAX_RESPONSE_DATA_BYTES",
    "cr_handler",
    "is_missing_error",
]

"""AgentCore Memory Custom Resource handler.

Manages the lifecycle of a Bedrock AgentCore Memory resource via the
``bedrock-agentcore-control`` service (``CreateMemory`` / ``UpdateMemory``
/ ``DeleteMemory``) and seeds the two namespaces the sample depends on:

* ``prior_deals``       - long-term memos the Strategic Fit agent reads
  during analysis (design §Components → Strategic Fit, §Data Model →
  AgentCore Memory Namespaces).
* ``session_<id>``      - per-session short-term context; the CR seeds a
  placeholder ``session_seed`` namespace so downstream tooling has a
  well-known prefix to target at runtime.

The handler is **required** in every release of this sample because
``aws-cdk-lib`` 2.173 does not yet ship a native
``AWS::BedrockAgentCore::Memory`` resource. When a future CDK release
does expose one, :class:`infra.stacks.agent_stack.AgentStack` switches
over to the native L1 via its ``HAS_AGENTCORE_MEMORY_NATIVE`` probe,
and this handler becomes unreferenced (but still shipped for
backwards-compatibility with older deploys).

Design reference: ``.kiro/specs/ma-due-diligence-agentcore/design.md``
sections *Custom Resources Inventory* (item 4) and *Custom Resource
Safety Requirements*. Safety rules enforced by the shared CR base
(``lambda/_cr_common/send_response.py``):

* Rule 1 — no ``boto3`` at module scope.
* Rule 2 — guaranteed response via the shared ``cr_handler``.
* Rule 4 — ``Delete`` treats a missing Memory resource as success.
* Rule 6 — ``PhysicalResourceId`` is the Memory ID itself so Update
  never triggers a replace cycle.
* Rule 7 — returned ``Data`` is a handful of short strings (Memory ID,
  ARN, seeded namespace names) well under the 4 KB response cap.

Resource properties consumed (``event['ResourceProperties']``):

``MemoryName``
    Short human-readable name for the Memory resource. Required.
``Description``
    Free-form description surfaced in the AgentCore console. Optional.
``EventExpiryDays``
    Retention window for session events. Defaults to 30 days which
    lines up with the sample's 7-day TTL on the DynamoDB session
    table plus a buffer for cross-session analysis.
``Namespaces``
    Optional list of namespace identifiers to seed (in addition to the
    always-created ``prior_deals`` and ``session_seed`` defaults).
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys
import time
from typing import Any

# --------------------------------------------------------------------------- #
# Shared CR base loader. Mirrors the pattern used by every other CR
# handler in this project — the ``lambda`` directory is not importable
# as a Python package (the name clashes with the keyword), so the
# handler loads ``_cr_common/send_response.py`` by filesystem path.
# --------------------------------------------------------------------------- #

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_cr_common() -> Any:
    """Return the shared ``send_response`` module."""

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
is_missing_error = _cr_common.is_missing_error


# --------------------------------------------------------------------------- #
# Defaults and constants
# --------------------------------------------------------------------------- #

# Namespaces the sample always expects to be present, regardless of
# caller-supplied ``Namespaces``. Keeping the defaults here — rather
# than relying on the CDK caller to pass them — makes the CR
# self-contained and immune to a stack-side refactor that accidentally
# drops a seed namespace.
_DEFAULT_SEED_NAMESPACES: tuple[str, ...] = ("prior_deals", "session_seed")

# Default event retention window. Memory is the canonical store for
# session turns in the sample; 30 days gives the evaluator a rolling
# window for audit queries without accumulating indefinite state.
_DEFAULT_EVENT_EXPIRY_DAYS = 30

# CreateMemory is asynchronous — a freshly created memory may briefly
# report status ``CREATING`` before the API accepts namespace seeding
# calls. We poll DescribeMemory for up to ~3 minutes before giving up.
_CREATE_POLL_INTERVAL_SECONDS = 5
_CREATE_POLL_MAX_SECONDS = 180

# DeleteMemory is also asynchronous in some regions; we do not wait
# here because CloudFormation's delete path runs under the Lambda's
# 15-minute cap and the shared CR base already treats a missing
# resource as success (rule 4). If the delete is still processing at
# the end of the invocation, the next poll from CloudFormation
# observes the same "missing" state.


# --------------------------------------------------------------------------- #
# Resource-property helpers
# --------------------------------------------------------------------------- #


def _resource_properties(event: dict) -> dict:
    """Return ``ResourceProperties`` as a dict, even when absent."""

    props = event.get("ResourceProperties") or {}
    return props if isinstance(props, dict) else {}


def _required_str(props: dict, key: str) -> str:
    """Fetch a required string resource property or raise :class:`ValueError`."""

    value = props.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"ResourceProperties.{key} is required")
    return value


def _optional_str(props: dict, key: str, default: str) -> str:
    value = props.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _optional_int(props: dict, key: str, default: int) -> int:
    raw = props.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"ResourceProperties.{key} must be an integer (got {raw!r})"
        ) from exc


def _optional_namespace_list(props: dict) -> list[str]:
    """Return the caller-supplied ``Namespaces`` list, deduplicated and sanitized."""

    raw = props.get("Namespaces") or []
    if isinstance(raw, str):
        # CloudFormation sometimes serializes single-element lists as a
        # comma-separated string — handle it defensively.
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, list):
        raise ValueError("ResourceProperties.Namespaces must be a list of strings")

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        cleaned.append(name)
    return cleaned


# --------------------------------------------------------------------------- #
# AgentCore Memory client wrappers
# --------------------------------------------------------------------------- #


def _make_client(boto3: Any) -> Any:
    """Construct the ``bedrock-agentcore-control`` boto3 client."""

    # The control-plane service name is ``bedrock-agentcore-control``
    # per the upstream AgentCore documentation. Using the dedicated
    # control-plane client keeps this handler aligned with the
    # ``CreateAgentRuntime`` + ``CreateMemory`` APIs rather than the
    # dataplane ``bedrock-agentcore`` client used by runtime traffic.
    return boto3.client("bedrock-agentcore-control")


def _create_memory(
    client: Any,
    *,
    name: str,
    description: str,
    event_expiry_days: int,
) -> dict:
    """Create a new Memory resource and return the service response."""

    logger.info(
        "agentcore_memory_create_start",
        extra={"memory_name": name, "event_expiry_days": event_expiry_days},
    )
    return client.create_memory(
        name=name,
        description=description,
        eventExpiryDuration=event_expiry_days,
    )


def _update_memory(
    client: Any,
    *,
    memory_id: str,
    description: str,
    event_expiry_days: int,
) -> dict:
    """Update the mutable attributes of an existing Memory resource."""

    logger.info(
        "agentcore_memory_update_start",
        extra={"memory_id": memory_id, "event_expiry_days": event_expiry_days},
    )
    return client.update_memory(
        memoryId=memory_id,
        description=description,
        eventExpiryDuration=event_expiry_days,
    )


def _wait_for_memory_active(
    client: Any,
    *,
    memory_id: str,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
) -> dict:
    """Poll ``DescribeMemory`` until the resource leaves ``CREATING``/``UPDATING``.

    AgentCore Memory provisioning is fast (typically sub-minute) but
    the API is eventually consistent, so we poll instead of trusting
    the synchronous ``CreateMemory`` return status. Raises
    :class:`RuntimeError` if the resource reports a terminal failure
    state or we exceed the polling cap.
    """

    deadline = now() + _CREATE_POLL_MAX_SECONDS
    last_status = "CREATING"
    while True:
        response = client.get_memory(memoryId=memory_id)
        memory = (response or {}).get("memory") or response or {}
        last_status = memory.get("status") or last_status
        if last_status == "ACTIVE":
            return memory
        if last_status in {"FAILED", "DELETING", "DELETED"}:
            failure = memory.get("failureReason") or "status reported terminal"
            raise RuntimeError(
                f"Memory {memory_id} entered terminal state {last_status!r}: {failure}"
            )
        if now() >= deadline:
            raise RuntimeError(
                f"Memory {memory_id} did not reach ACTIVE within "
                f"{_CREATE_POLL_MAX_SECONDS}s (last status: {last_status!r})"
            )
        sleep(_CREATE_POLL_INTERVAL_SECONDS)


def _seed_namespaces(
    client: Any,
    *,
    memory_id: str,
    namespaces: list[str],
) -> list[str]:
    """Seed each namespace with an empty marker memory record.

    AgentCore Memory does not have a notion of a "namespace resource"
    — namespaces come into existence the first time a memory record is
    written under them. To guarantee the ``prior_deals`` and
    ``session_seed`` namespaces exist (so the agents and evaluator see
    a valid empty state rather than "namespace not found"), we write a
    single marker record per namespace.

    Returns the list of namespaces that were successfully seeded. We
    swallow per-namespace failures (logging them) so a transient
    error in one namespace does not fail the entire CR invocation —
    the sample is resilient to empty namespaces by design, and the
    operator can always re-run the deploy to retry.
    """

    seeded: list[str] = []
    for namespace in namespaces:
        try:
            client.create_event(
                memoryId=memory_id,
                actorId="mna-bootstrap",
                sessionId=f"bootstrap-{namespace}",
                eventTimestamp=int(time.time()),
                payload=[
                    {
                        "conversational": {
                            "content": {
                                "text": (
                                    f"SYNTHETIC seed event for namespace "
                                    f"{namespace!r}. Created by the "
                                    "AgentCore Memory CR to ensure the "
                                    "namespace exists before agents run."
                                ),
                            },
                            "role": "ASSISTANT",
                        },
                    },
                ],
            )
            seeded.append(namespace)
            logger.info(
                "agentcore_memory_namespace_seeded",
                extra={"memory_id": memory_id, "namespace": namespace},
            )
        except Exception as exc:  # noqa: BLE001 - best-effort per-namespace
            logger.warning(
                "agentcore_memory_namespace_seed_failed",
                extra={
                    "memory_id": memory_id,
                    "namespace": namespace,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                },
            )
    return seeded


# --------------------------------------------------------------------------- #
# Dispatchers
# --------------------------------------------------------------------------- #


def _on_create(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Create the Memory resource and seed default namespaces."""

    props = _resource_properties(event)
    name = _required_str(props, "MemoryName")
    description = _optional_str(
        props,
        "Description",
        "AgentCore Memory for the M&A Due Diligence sample.",
    )
    event_expiry_days = _optional_int(props, "EventExpiryDays", _DEFAULT_EVENT_EXPIRY_DAYS)
    extra_namespaces = _optional_namespace_list(props)
    namespaces = list(_DEFAULT_SEED_NAMESPACES) + [
        n for n in extra_namespaces if n not in _DEFAULT_SEED_NAMESPACES
    ]

    client = _make_client(boto3)
    response = _create_memory(
        client,
        name=name,
        description=description,
        event_expiry_days=event_expiry_days,
    )
    memory = (response or {}).get("memory") or response or {}
    memory_id = memory.get("id") or memory.get("memoryId")
    memory_arn = memory.get("arn") or memory.get("memoryArn") or ""
    if not memory_id:
        raise RuntimeError(
            f"CreateMemory response missing memory identifier: {response!r}"
        )

    # Wait for ACTIVE before seeding. An eventually-consistent create
    # can fail namespace writes with "memory not found" for a few
    # seconds post-create.
    try:
        active = _wait_for_memory_active(client, memory_id=memory_id)
        memory_arn = active.get("arn") or active.get("memoryArn") or memory_arn
    except Exception:
        # The shared CR base converts the raised exception into a
        # FAILED response; logging here keeps the reason visible in
        # CloudWatch even after the shared base truncates the
        # response-level message.
        logger.exception("agentcore_memory_wait_failed")
        raise

    seeded = _seed_namespaces(client, memory_id=memory_id, namespaces=namespaces)

    data = {
        "MemoryId": memory_id,
        "MemoryArn": memory_arn,
        "SeededNamespaces": ",".join(seeded),
        "EventExpiryDays": str(event_expiry_days),
    }
    return memory_id, data


def _on_update(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Apply mutable property changes to an existing Memory resource.

    The Memory *name* is immutable. If a caller changes ``MemoryName``
    CloudFormation executes a replace cycle (create-new + delete-old),
    which this handler supports via its ``Create`` and ``Delete`` paths.
    All other properties (description, retention, additional
    namespaces) are applied in-place.
    """

    props = _resource_properties(event)
    physical_id = event.get("PhysicalResourceId") or ""
    if not physical_id:
        # Shouldn't happen — CloudFormation always echoes the previous
        # physical ID on Update — but guard against a malformed event.
        raise ValueError("Update event missing PhysicalResourceId")

    description = _optional_str(
        props,
        "Description",
        "AgentCore Memory for the M&A Due Diligence sample.",
    )
    event_expiry_days = _optional_int(props, "EventExpiryDays", _DEFAULT_EVENT_EXPIRY_DAYS)
    extra_namespaces = _optional_namespace_list(props)
    namespaces = list(_DEFAULT_SEED_NAMESPACES) + [
        n for n in extra_namespaces if n not in _DEFAULT_SEED_NAMESPACES
    ]

    client = _make_client(boto3)
    _update_memory(
        client,
        memory_id=physical_id,
        description=description,
        event_expiry_days=event_expiry_days,
    )

    try:
        active = _wait_for_memory_active(client, memory_id=physical_id)
        memory_arn = active.get("arn") or active.get("memoryArn") or ""
    except Exception:
        logger.exception("agentcore_memory_update_wait_failed")
        raise

    # Re-seed any namespaces that might have been added since Create.
    # Seeding is idempotent because the marker events land under a
    # stable ``bootstrap-<namespace>`` session ID — writing a second
    # time simply adds a second event under the same session, which is
    # harmless.
    seeded = _seed_namespaces(client, memory_id=physical_id, namespaces=namespaces)

    data = {
        "MemoryId": physical_id,
        "MemoryArn": memory_arn,
        "SeededNamespaces": ",".join(seeded),
        "EventExpiryDays": str(event_expiry_days),
    }
    return physical_id, data


def _on_delete(event: dict, _context: Any, boto3: Any) -> tuple[str, dict]:
    """Delete the Memory resource; missing resource treated as success."""

    physical_id = event.get("PhysicalResourceId") or ""
    if not physical_id or physical_id in {"init", ""}:
        logger.info("agentcore_memory_delete_no_physical_id")
        return physical_id or "init", {}

    client = _make_client(boto3)
    try:
        client.delete_memory(memoryId=physical_id)
        logger.info("agentcore_memory_deleted", extra={"memory_id": physical_id})
    except Exception as exc:  # noqa: BLE001 - checked via classifier
        if is_missing_error(exc):
            logger.info(
                "agentcore_memory_delete_already_gone",
                extra={"memory_id": physical_id, "exception_type": type(exc).__name__},
            )
            return physical_id, {}
        raise

    return physical_id, {}


# Wire Create/Update/Delete through the shared CR base. The base
# handles rule 1 (lazy boto3 import), rule 2 (guaranteed response),
# rule 4 (delete-of-missing → success via ``is_missing`` classifier),
# and rules 7/8.
handler = cr_handler(
    create=_on_create,
    update=_on_update,
    delete=_on_delete,
)(_on_create)


__all__ = ["handler"]


if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
    logger.info("agentcore_memory_module_loaded")

"""Argparse-based CLI for the M&A Due Diligence sample.

The CLI is intentionally a thin wrapper around :mod:`mna.client` and
:mod:`mna.evaluators.citation_check` — every subcommand is pure
delegation, satisfying Requirement 7.4 ("notebook and CLI route
invocations through a shared Python package without duplicating
logic").

Subcommands (design.md → "Components and Interfaces" → "CLI"):

* ``invoke <agent> "<prompt>"``      — call the runtime and render the
  response text plus citations.
* ``list-agents``                    — print the five agent names.
* ``trace <trace_id>``               — fetch and print the X-Ray trace
  for the most recent invocation.
* ``evaluate``                       — run the local citation-check
  evaluator against a response + citations pair supplied on stdin or
  via ``--response-file`` / ``--citations-file``.

Every subcommand supports ``--json`` for machine-readable output; the
default is human-friendly text with citations rendered inline.

The module exposes :func:`main` as the entry point registered in
``pyproject.toml`` (``mna = "cli.invoke:main"``). The entry point is
``main(argv=None)`` so unit tests can drive it directly without
monkey-patching :data:`sys.argv`.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, TextIO

from mna.client import ClientError, get_last_trace, invoke_agent, list_agents
from mna.evaluators.citation_check import check_citations
from mna.types import AgentResponse, Citation, EvaluationResult

# Human-readable exit codes. 0/1/2 follow POSIX convention: 0 is success,
# 2 is argparse usage errors (set automatically by ArgumentParser), and
# 1 is reserved for runtime failures raised by the shared client layer.
_EXIT_OK = 0
_EXIT_RUNTIME_ERROR = 1


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser.

    Exposed as a standalone helper so tests and ``python -m cli.invoke
    --help`` can introspect the parser without running any command.
    """

    parser = argparse.ArgumentParser(
        prog="mna",
        description=(
            "M&A Due Diligence sample CLI — invoke agents, inspect traces, "
            "and run the local citation-check evaluator."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # -------- invoke --------
    invoke_p = subparsers.add_parser(
        "invoke",
        help="Invoke a named agent with a prompt string.",
        description="Invoke a named agent with a prompt string.",
    )
    invoke_p.add_argument(
        "agent",
        help="Agent name. Use 'list-agents' to enumerate valid values.",
    )
    invoke_p.add_argument("prompt", help="Prompt text to send to the agent.")
    invoke_p.add_argument(
        "--session-id",
        dest="session_id",
        default=None,
        help="Optional session identifier for multi-turn continuity via AgentCore Memory.",
    )

    # -------- list-agents --------
    subparsers.add_parser(
        "list-agents",
        help="Print the list of agents hosted on the runtime.",
        description="Print the list of agents hosted on the runtime.",
    )

    # -------- trace --------
    trace_p = subparsers.add_parser(
        "trace",
        help="Fetch the X-Ray trace for a completed invocation.",
        description="Fetch the X-Ray trace for a completed invocation.",
    )
    trace_p.add_argument("trace_id", help="X-Ray trace id (e.g. 1-abcdef12-...).")

    # -------- evaluate --------
    evaluate_p = subparsers.add_parser(
        "evaluate",
        help="Run the local citation-check evaluator on a response + citations pair.",
        description=(
            "Run the local citation-check evaluator. The response text may be "
            "supplied via --response-file or on stdin. Citations must be provided "
            "via --citations-file as a JSON array of citation objects "
            "({text, source, page?, score?})."
        ),
    )
    evaluate_p.add_argument(
        "--response-file",
        type=Path,
        default=None,
        help="Path to a UTF-8 text file containing the agent response. Defaults to stdin.",
    )
    evaluate_p.add_argument(
        "--citations-file",
        type=Path,
        required=True,
        help="Path to a JSON file containing a list of citation objects.",
    )
    evaluate_p.add_argument(
        "--no-strict",
        action="store_true",
        help="Disable strict claim extraction (numeric/quoted sentences only).",
    )

    return parser


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_citations_text(citations: Iterable[Citation]) -> str:
    """Render a list of citations as indented bullet lines for the terminal."""

    lines: list[str] = []
    for idx, cite in enumerate(citations, start=1):
        location_bits: list[str] = [cite.source or "(no source)"]
        if cite.page is not None:
            location_bits.append(f"p. {cite.page}")
        if cite.score is not None:
            location_bits.append(f"score={cite.score:.2f}")
        header = f"  [{idx}] " + ", ".join(location_bits)
        lines.append(header)
        if cite.text:
            snippet = cite.text.strip().replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:237] + "..."
            lines.append(f"      {snippet}")
    return "\n".join(lines)


def _render_agent_response_text(response: AgentResponse) -> str:
    """Render an :class:`AgentResponse` for human consumption."""

    parts: list[str] = []
    parts.append(response.text.strip() or "(empty response)")
    if response.citations:
        parts.append("")
        parts.append(f"Citations ({len(response.citations)}):")
        parts.append(_render_citations_text(response.citations))
    footer_bits: list[str] = []
    if response.session_id:
        footer_bits.append(f"session_id={response.session_id}")
    if response.trace_id:
        footer_bits.append(f"trace_id={response.trace_id}")
    if footer_bits:
        parts.append("")
        parts.append(" | ".join(footer_bits))
    return "\n".join(parts)


def _render_evaluation_text(result: EvaluationResult) -> str:
    status = "PASS" if result.passed else "FAIL"
    lines = [
        f"Citation check: {status}",
        f"  total_claims={result.total_claims}",
        f"  supported={result.supported_claims}",
        f"  unsupported={len(result.unsupported_claims)}",
    ]
    if result.unsupported_claims:
        lines.append("  Unsupported claims:")
        for claim in result.unsupported_claims:
            lines.append(f"    - {claim}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spinner — stdlib-only status indicator for long-running invocations
# ---------------------------------------------------------------------------


class _Spinner:
    """Minimal thread-based spinner that writes to a stream (usually stderr).

    The spinner is a no-op when the target stream is not a TTY (for
    example when the CLI is run under ``capsys`` in the unit tests, or
    when stderr is redirected to a file in a CI pipeline). This keeps
    the default UX friendly for interactive callers without leaking
    control characters into captured logs.

    Intentionally dependency-free: no ``rich``, no ``tqdm``, no
    ``yaspin``. A supervisor call typically takes 10-30 seconds, and
    the reader experience goal in the spec (Requirement 7) favours a
    zero-install path over nicer rendering.
    """

    _FRAMES = ("|", "/", "-", "\\")

    def __init__(self, message: str, *, stream: TextIO, enabled: bool) -> None:
        self._message = message
        self._stream = stream
        # Auto-disable when not attached to a terminal — keeps redirected
        # stderr (pipes, log files, pytest capture) free of escape chars.
        self._enabled = enabled and _stream_is_tty(stream)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _Spinner:
        if not self._enabled:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self._enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        # Erase the spinner line so subsequent output starts clean.
        self._stream.write("\r" + " " * (len(self._message) + 4) + "\r")
        self._stream.flush()

    def _run(self) -> None:
        idx = 0
        while not self._stop.is_set():
            frame = self._FRAMES[idx % len(self._FRAMES)]
            self._stream.write(f"\r{frame} {self._message}")
            self._stream.flush()
            idx += 1
            # 100 ms cadence — snappy enough to feel alive, slow enough
            # to stay well under 1% CPU on the calling machine.
            self._stop.wait(0.1)


def _stream_is_tty(stream: TextIO) -> bool:
    """Return True when ``stream`` is attached to an interactive terminal."""

    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except (ValueError, OSError):
        # Closed streams or streams that don't support isatty() — treat
        # as non-interactive to stay safe.
        return False


# ---------------------------------------------------------------------------
# Subcommand dispatchers
# ---------------------------------------------------------------------------


def _cmd_invoke(
    args: argparse.Namespace, *, stdout: TextIO, stderr: TextIO
) -> int:
    # Spinner is stderr-only, TTY-only, and suppressed in --json mode
    # so machine-readable output stays pristine on stdout.
    with _Spinner(
        f"Invoking {args.agent}...",
        stream=stderr,
        enabled=not args.json,
    ):
        response = invoke_agent(args.agent, args.prompt, session_id=args.session_id)
    if args.json:
        json.dump(response.to_dict(), stdout, indent=2, default=str)
        stdout.write("\n")
    else:
        stdout.write(_render_agent_response_text(response))
        stdout.write("\n")
    return _EXIT_OK


def _cmd_list_agents(args: argparse.Namespace, *, stdout: TextIO) -> int:
    names = list_agents()
    if args.json:
        json.dump(names, stdout, indent=2)
        stdout.write("\n")
    else:
        for name in names:
            stdout.write(name + "\n")
    return _EXIT_OK


def _cmd_trace(args: argparse.Namespace, *, stdout: TextIO) -> int:
    trace = get_last_trace(args.trace_id)
    # ``trace`` is already a plain JSON-friendly dict (see
    # ``mna.client.get_last_trace`` contract). Render it as JSON either
    # way — trace data is not useful as prose, and the ``--json`` flag
    # just toggles indentation vs. a compact summary header.
    if args.json:
        json.dump(trace, stdout, indent=2, default=str)
        stdout.write("\n")
        return _EXIT_OK

    summary = trace.get("summary") or {}
    segments = trace.get("segments") or []
    stdout.write(f"Trace {trace.get('trace_id', args.trace_id)}\n")
    if summary:
        stdout.write("Summary:\n")
        json.dump(summary, stdout, indent=2, default=str)
        stdout.write("\n")
    else:
        stdout.write("Summary: (not yet indexed)\n")
    stdout.write(f"Segments ({len(segments)}):\n")
    json.dump(segments, stdout, indent=2, default=str)
    stdout.write("\n")
    return _EXIT_OK


def _load_citations(path: Path) -> list[Citation]:
    """Load a citations JSON file and coerce entries into :class:`Citation`.

    The file must contain a JSON array; each entry is either a
    :class:`Citation`-shaped dict (``{text, source, page?, score?}``) or
    a superset of those keys. Unknown keys are ignored so the evaluator
    stays forward-compatible with richer citation shapes.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClientError(f"Citations file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ClientError(f"Citations file is not valid JSON: {path} ({exc.msg})") from exc

    if not isinstance(raw, list):
        raise ClientError(f"Citations file must contain a JSON array, got {type(raw).__name__}")

    citations: list[Citation] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ClientError(f"Citations[{idx}] is not an object")
        text = entry.get("text") or entry.get("content") or ""
        source = entry.get("source") or entry.get("uri") or entry.get("location") or ""
        page_raw = entry.get("page")
        score_raw = entry.get("score")
        page = int(page_raw) if isinstance(page_raw, int) else None
        score = float(score_raw) if isinstance(score_raw, int | float) else None
        citations.append(Citation(text=str(text), source=str(source), page=page, score=score))
    return citations


def _read_response_text(path: Path | None, *, stdin: TextIO) -> str:
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ClientError(f"Response file not found: {path}") from exc
    # Stdin fallback — lets the CLI compose with shell pipelines.
    return stdin.read()


def _cmd_evaluate(
    args: argparse.Namespace,
    *,
    stdout: TextIO,
    stdin: TextIO,
) -> int:
    response_text = _read_response_text(args.response_file, stdin=stdin)
    if not response_text.strip():
        raise ClientError("Response text is empty — pass --response-file or pipe text on stdin")

    citations = _load_citations(args.citations_file)
    result = check_citations(response_text, citations, strict=not args.no_strict)

    if args.json:
        json.dump(result.to_dict(), stdout, indent=2)
        stdout.write("\n")
    else:
        stdout.write(_render_evaluation_text(result))
        stdout.write("\n")
    return _EXIT_OK if result.passed else _EXIT_RUNTIME_ERROR


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Dispatch the requested subcommand.

    ``argv`` defaults to ``sys.argv[1:]``. The ``stdout``/``stderr``/
    ``stdin`` parameters exist for dependency injection in tests; real
    callers should let them default to the process streams.
    """

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    inp = stdin if stdin is not None else sys.stdin

    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch: dict[str, Any] = {
        "invoke": lambda: _cmd_invoke(args, stdout=out, stderr=err),
        "list-agents": lambda: _cmd_list_agents(args, stdout=out),
        "trace": lambda: _cmd_trace(args, stdout=out),
        "evaluate": lambda: _cmd_evaluate(args, stdout=out, stdin=inp),
    }
    handler = dispatch.get(args.command)
    if handler is None:  # pragma: no cover - argparse enforces choices
        parser.error(f"Unknown command: {args.command}")
        return _EXIT_RUNTIME_ERROR

    try:
        return handler()
    except ClientError as exc:
        err.write(f"error: {exc}\n")
        return _EXIT_RUNTIME_ERROR


if __name__ == "__main__":  # pragma: no cover - convenience for ``python -m``
    raise SystemExit(main())

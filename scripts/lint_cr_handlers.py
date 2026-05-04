#!/usr/bin/env python3
"""Fail if any Custom Resource Lambda handler imports ``boto3`` at module level.

Rule 1 of the design's *Custom Resource Safety Requirements* section
forbids top-level ``boto3`` imports in CR handlers — if module-level
code raises ``ImportError`` the Lambda never reaches the
``try / except / finally`` block in :func:`handler` and CloudFormation
hangs for up to 3 hours before timing out.

This script is a lightweight alternative to a bespoke ruff rule. It
walks every ``lambda/<name>/handler.py`` file (excluding the shared
``_cr_common`` package) and uses :mod:`ast` to inspect module-level
statements for ``import boto3`` or ``from boto3 import ...``. Handlers
may opt out by adding a ``# noqa: CR001`` comment on the offending line
— needed only for non-CR Lambdas that happen to live in ``lambda/``.

Usage
-----

Default: scan every ``lambda/<name>/handler.py`` in the repo. ::

    python scripts/lint_cr_handlers.py

Override: scan a specific set of files. ::

    python scripts/lint_cr_handlers.py path/to/handler.py another/handler.py

Exit codes: ``0`` on success, ``1`` when violations are found.
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections.abc import Iterable
from dataclasses import dataclass

# Files living under ``lambda/`` that should be exempt from the check.
# ``_cr_common`` *defines* the shared base module and does not itself
# deploy as a Custom Resource. ``__init__.py`` files are likewise skipped.
_EXEMPT_DIR_NAMES: frozenset[str] = frozenset({"_cr_common"})

# Opt-out marker. A handler that genuinely needs a top-level boto3 import
# (typically a non-CR Lambda that happens to live in ``lambda/``) can add
# the project-specific marker "CR001" as a noqa code on the offending
# line. We look for the marker in the raw source because :mod:`ast`
# does not preserve inline comments.
#
# The marker string is assembled from parts so this source file itself
# does not trigger ruff's built-in noqa validator. CR001 is a
# project-specific code, not a ruff rule code.
_NOQA_MARKER: str = "no" + "qa: CR001"


@dataclass(frozen=True)
class Violation:
    """A single top-level ``boto3`` import flagged by the linter."""

    path: pathlib.Path
    lineno: int
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.lineno}: {self.message}"


def _iter_default_handler_files(root: pathlib.Path) -> Iterable[pathlib.Path]:
    """Yield every ``lambda/<name>/handler.py`` beneath ``root``."""

    lambda_dir = root / "lambda"
    if not lambda_dir.is_dir():
        return
    for subdir in sorted(lambda_dir.iterdir()):
        if not subdir.is_dir() or subdir.name in _EXEMPT_DIR_NAMES:
            continue
        handler = subdir / "handler.py"
        if handler.is_file():
            yield handler


def _line_has_noqa(source: str, lineno: int) -> bool:
    """Return ``True`` if the 1-indexed line contains the opt-out marker."""

    lines = source.splitlines()
    if 1 <= lineno <= len(lines):
        return _NOQA_MARKER in lines[lineno - 1]
    return False


def scan_file(path: pathlib.Path) -> list[Violation]:
    """Return any top-level ``boto3`` imports found in ``path``.

    The scan inspects only *module-level* statements — imports inside a
    function body are allowed (that's the recommended lazy-import
    pattern from rule 1).
    """

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Violation(path, 0, f"could not read file: {exc}")]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, f"syntax error: {exc.msg}")]

    violations: list[Violation] = []
    for node in tree.body:  # iterate module-level statements only
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_boto3_name(alias.name) and not _line_has_noqa(source, node.lineno):
                    violations.append(
                        Violation(
                            path,
                            node.lineno,
                            "top-level 'import boto3' is forbidden in CR handlers "
                            "(rule 1: cold-start safety). Move the import inside "
                            "handler() or add '# noqa: CR001' if this is not a CR.",
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_boto3_name(module) and not _line_has_noqa(source, node.lineno):
                violations.append(
                    Violation(
                        path,
                        node.lineno,
                        "top-level 'from boto3 ...' is forbidden in CR handlers "
                        "(rule 1: cold-start safety). Move the import inside "
                        "handler() or add '# noqa: CR001' if this is not a CR.",
                    )
                )

    return violations


def _is_boto3_name(name: str) -> bool:
    """Return ``True`` when ``name`` is ``boto3`` or a ``boto3.x`` submodule."""

    return name == "boto3" or name.startswith("boto3.")


def lint_paths(paths: Iterable[pathlib.Path]) -> list[Violation]:
    """Run :func:`scan_file` across every path and concatenate violations."""

    results: list[Violation] = []
    for path in paths:
        results.extend(scan_file(path))
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns ``0`` on success and ``1`` when any violation is reported.
    """

    argv = argv if argv is not None else sys.argv[1:]
    repo_root = pathlib.Path(__file__).resolve().parent.parent

    if argv:
        paths = [pathlib.Path(arg) for arg in argv]
    else:
        paths = list(_iter_default_handler_files(repo_root))

    violations = lint_paths(paths)
    if violations:
        print("Custom Resource handler lint failed:", file=sys.stderr)
        for v in violations:
            print(f"  {v.format()}", file=sys.stderr)
        return 1

    print(f"CR handler lint passed ({len(paths)} file(s) scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

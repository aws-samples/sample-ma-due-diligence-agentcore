"""Typed data classes used across the ``mna`` package.

These shapes are shared by the notebook, the CLI, the agents, and the
evaluator Lambda so that a response rendered in one surface is
structurally identical to one produced by another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Citation:
    """A single retrieved passage used to ground an agent response.

    Matches the output shape of ``mna.tools.kb_retrieve`` (see design.md
    → "Components and Interfaces" → "Tools" → ``tools/kb_retrieve.py``).
    """

    text: str
    source: str
    page: int | None = None
    score: float | None = None

    def supports(self, claim: str) -> bool:
        """Return ``True`` when this citation plausibly supports ``claim``.

        Simple substring/overlap heuristic. The Lambda-backed evaluator
        (``lambda/citation_check/handler.py``) implements the same contract
        so local and remote citation checks agree.
        """

        if not claim or not self.text:
            return False
        claim_tokens = {t.lower() for t in claim.split() if len(t) > 3}
        if not claim_tokens:
            return claim.lower() in self.text.lower()
        source_tokens = {t.lower() for t in self.text.split() if len(t) > 3}
        overlap = claim_tokens & source_tokens
        # Consider the claim supported if at least half its salient tokens
        # appear in the cited passage.
        return len(overlap) >= max(1, len(claim_tokens) // 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "source": self.source,
            "page": self.page,
            "score": self.score,
        }


@dataclass(frozen=True)
class AgentResponse:
    """Response envelope returned by ``mna.client.invoke_agent``.

    Fields mirror the shape documented in design.md → "Components and
    Interfaces" → "Shared Python Package: mna".
    """

    text: str
    citations: list[Citation] = field(default_factory=list)
    trace_id: str | None = None
    session_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "citations": [c.to_dict() for c in self.citations],
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class EvaluationResult:
    """Outcome of the citation-check evaluator.

    Mirrors the ``lambda/citation_check/handler.py`` return shape so that
    downstream code can consume either transparently.
    """

    passed: bool
    unsupported_claims: list[str] = field(default_factory=list)
    total_claims: int = 0

    @property
    def supported_claims(self) -> int:
        return max(0, self.total_claims - len(self.unsupported_claims))

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "unsupported_claims": list(self.unsupported_claims),
            "total_claims": self.total_claims,
        }

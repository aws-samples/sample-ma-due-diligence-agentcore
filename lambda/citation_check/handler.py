"""Citation-check evaluator Lambda handler.

This Lambda is the canonical deployment of the citation evaluator
described in the design document (section *Evaluator: Citation Check*).
A local mirror of the same logic lives at
``src/mna/evaluators/citation_check.py`` — the two implementations are
kept in sync by a shared unit test suite so the notebook and the
Lambda return identical results for the same input.

Contract
--------

* **Input event.** A JSON-serializable dict::

      {
          "response_text": "<agent response>",
          "citations": [
              {"text": "...", "source": "...", "page": 3, "score": 0.87},
              ...
          ]
      }

* **Output.** A dict matching :class:`mna.types.EvaluationResult`::

      {
          "passed": bool,
          "unsupported_claims": [str, ...],
          "total_claims": int
      }

The handler is deliberately dependency-free — no ``boto3``, no
environment lookups at module scope, no imports from the ``mna``
package (that package is not bundled into the Lambda asset). This
keeps the deployment package tiny (pure standard library) and cold
starts fast, and means a misconfigured deploy still lands a handler
that can respond.

Algorithm
---------

1. Split ``response_text`` into sentences on ``.``, ``!``, ``?``
   followed by whitespace.
2. Mark a sentence as a *claim* when it contains a numeric token, a
   quoted passage, or (in strict mode) enough salient tokens to carry
   a factual statement. Strict mode is the default and matches the
   design's "ALL sentences if strict mode (default)" guidance.
3. For each claim, check whether any citation plausibly supports it:

   * substring match (case-insensitive), or
   * ≥50% token overlap on salient tokens (length > 3, excluding a
     small stop-word set and stripping punctuation).

4. The response passes when zero claims are unsupported.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Regex + tuning constants.
#
# These constants mirror the ones in
# ``src/mna/evaluators/citation_check.py`` line-for-line so the two
# implementations stay behavior-compatible. When changing one, update
# the other.
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_NUMERIC_RE = re.compile(r"\d")
_QUOTE_RE = re.compile(r"[\"'“”‘’]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

_MIN_SALIENT_TOKEN_LEN = 4
_STRICT_MIN_TOKENS = 4
_OVERLAP_RATIO = 0.5

_STOP_TOKENS: frozenset[str] = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "have",
        "been",
        "they",
        "them",
        "there",
        "these",
        "those",
        "their",
        "were",
        "will",
        "would",
        "could",
        "should",
        "also",
        "into",
        "upon",
        "than",
        "then",
        "when",
        "where",
        "which",
        "what",
        "your",
        "ours",
    }
)


# ---------------------------------------------------------------------------
# Tokenization helpers
# ---------------------------------------------------------------------------


def _split_sentences(response_text: str) -> list[str]:
    """Split text into trimmed, non-empty sentences."""

    if not response_text:
        return []
    sentences: list[str] = []
    for chunk in _SENTENCE_SPLIT_RE.split(response_text.strip()):
        trimmed = chunk.strip()
        if trimmed:
            sentences.append(trimmed)
    return sentences


def _salient_tokens(text: str) -> set[str]:
    """Return the lowercase salient tokens from ``text``.

    Pure digits are kept regardless of length so numeric anchors
    ("12%", "340") still count toward the overlap ratio.
    """

    tokens: set[str] = set()
    for match in _TOKEN_RE.findall(text.lower()):
        if match.isdigit():
            tokens.add(match)
            continue
        if len(match) < _MIN_SALIENT_TOKEN_LEN:
            continue
        if match in _STOP_TOKENS:
            continue
        tokens.add(match)
    return tokens


def _is_claim_sentence(sentence: str, *, strict: bool = True) -> bool:
    """Identify sentences worth citation-checking."""

    if not sentence:
        return False
    if _NUMERIC_RE.search(sentence):
        return True
    if _QUOTE_RE.search(sentence):
        return True
    if not strict:
        return False
    return len(_salient_tokens(sentence)) >= _STRICT_MIN_TOKENS


def _extract_claims(response_text: str, *, strict: bool = True) -> list[str]:
    return [s for s in _split_sentences(response_text) if _is_claim_sentence(s, strict=strict)]


# ---------------------------------------------------------------------------
# Citation matching
# ---------------------------------------------------------------------------


def _citation_text(citation: Any) -> str:
    """Extract the ``text`` field from a dict-shaped citation.

    The evaluator is invoked with citations serialized to JSON so
    every entry is a plain dict by the time it reaches the handler.
    Malformed entries (non-dict, missing ``text``) are treated as
    empty so a single bad record can't break the whole check.
    """

    if isinstance(citation, dict):
        raw = citation.get("text", "")
        return str(raw) if raw is not None else ""
    return ""


def _citation_supports(claim: str, citation_text: str) -> bool:
    """Return ``True`` when the citation plausibly supports the claim.

    Mirrors :meth:`mna.types.Citation.supports` and the local
    evaluator's matcher so Lambda and in-process results agree.
    """

    if not claim or not citation_text:
        return False
    claim_lower = claim.lower()
    citation_lower = citation_text.lower()
    if claim_lower in citation_lower:
        return True
    claim_tokens = _salient_tokens(claim)
    if not claim_tokens:
        return claim_lower in citation_lower
    citation_tokens = _salient_tokens(citation_text)
    if not citation_tokens:
        return False
    overlap = claim_tokens & citation_tokens
    return len(overlap) / len(claim_tokens) >= _OVERLAP_RATIO


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Run the citation check and return an EvaluationResult-shaped dict.

    The handler is tolerant of missing keys — an empty ``event`` or a
    malformed payload yields an empty ``EvaluationResult`` rather than
    raising, so an upstream bug can't take down the Compliance
    Validation agent.
    """

    if not isinstance(event, dict):
        return {"passed": True, "unsupported_claims": [], "total_claims": 0}

    response_text_raw = event.get("response_text", "")
    response_text = response_text_raw if isinstance(response_text_raw, str) else ""

    citations_raw = event.get("citations", [])
    citations = citations_raw if isinstance(citations_raw, list) else []

    # Strict mode can be disabled by the caller for debugging, but the
    # design specifies strict-by-default so the default branch is
    # maximally conservative.
    strict_raw = event.get("strict", True)
    strict = bool(strict_raw) if isinstance(strict_raw, (bool, int)) else True

    claim_sentences = _extract_claims(response_text, strict=strict)
    citation_texts = [t for t in (_citation_text(c) for c in citations) if t]

    unsupported: list[str] = [
        claim
        for claim in claim_sentences
        if not any(_citation_supports(claim, ct) for ct in citation_texts)
    ]

    return {
        "passed": not unsupported,
        "unsupported_claims": unsupported,
        "total_claims": len(claim_sentences),
    }

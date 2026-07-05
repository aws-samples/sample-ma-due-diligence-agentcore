"""Citation-check evaluator — local mirror of the Lambda handler.

The canonical deployment of this evaluator is the Python 3.11 Lambda
at ``lambda/citation_check/handler.py`` (see :class:`EvaluatorStack`).
This local module implements the same logic in pure Python so the
notebook, CLI, and unit tests can run the check without invoking AWS.

Design reference:
``.kiro/specs/ma-due-diligence-agentcore/design.md`` section
*Components and Interfaces → Evaluator: Citation Check*.

Contract
--------

Input:

* ``response_text`` — the agent's natural-language answer.
* ``citations`` — an iterable of :class:`mna.types.Citation` (or plain
  dicts with ``text``/``source``/``page``/``score`` keys) returned by
  the retrieval tools that grounded the response.

Output: :class:`mna.types.EvaluationResult` with

* ``passed`` — ``True`` when every extracted claim is supported by at
  least one citation.
* ``unsupported_claims`` — the list of claim strings that no citation
  plausibly backs.
* ``total_claims`` — total claim sentences considered.

Claim extraction
----------------

1. Split ``response_text`` into sentences on ``.``, ``!``, ``?``
   followed by whitespace or end of text.
2. A sentence is a *claim* when any of the following holds:

   * it contains a numeric token (``\\d+``),
   * it contains a quoted passage, or
   * ``strict=True`` (default) and the sentence is long enough to
     carry a factual statement (4+ tokens after normalization).

   Short filler sentences (``"Thanks."``, ``"Sure!"``) are skipped so
   the evaluator does not punish conversational phrasing.

Citation matching
-----------------

A citation supports a claim when either:

* the claim string appears as a substring of the citation text
  (case-insensitive), or
* at least half of the claim's salient tokens (lowercased tokens of
  length > 3, excluding a small stop-word set) also appear in the
  citation text.

Matching operates at the token level to avoid punctuation sensitivity
and stays dependency-free (no NLTK/spaCy) so the Lambda package stays
small and cold starts stay fast.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from mna.types import Citation, EvaluationResult

# ---------------------------------------------------------------------------
# Tokenization helpers
# ---------------------------------------------------------------------------

# Sentence terminator followed by whitespace/EOL. We deliberately avoid
# the full complexity of sentence segmentation (abbreviations, ellipses)
# because the evaluator's failure mode is a false-negative claim split,
# which simply means a long compound claim gets evaluated as one unit —
# still correct, just slightly coarser.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Markdown line break. Specialist responses are markdown — bullet
# points, table rows, and headers each occupy their own line, and
# very often end with a citation bracket (``[source: ...]``) rather
# than terminal punctuation. Splitting on ``.!?`` alone left an entire
# multi-bullet section glued into one "sentence" whenever a bullet's
# citation bracket was the last thing before the newline, so no single
# citation's token overlap could ever clear the 50% threshold against
# the combined blob. Pre-splitting on newlines first — before the
# existing sentence-terminator split runs on what remains of each
# line — fixes this because every markdown block element reliably
# starts a new line in the agents' output.
_LINE_BREAK_RE = re.compile(r"\n+")

# A claim sentence that carries numeric content is almost always a
# factual claim worth citing ("grew revenue 12%", "fleet of 340 trucks").
_NUMERIC_RE = re.compile(r"\d")

# Straight or smart quotes count as quoted passages.
_QUOTE_RE = re.compile(r"[\"'“”‘’]")

# Token pattern: contiguous alphanumerics. Strips punctuation so
# "$100M," and "100M" compare equal at the token level.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")

# Minimum character length for a token to count as "salient". Filters
# out articles and prepositions without needing a full stop-word list.
_MIN_SALIENT_TOKEN_LEN = 4

# Extra stop-tokens that survive the length filter but carry no
# topical signal. Kept tiny on purpose — the length filter does most
# of the work, and a long list would risk discarding domain words.
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

# Minimum salient-token count for a non-numeric, non-quoted sentence to
# count as a claim in strict mode. Chosen so "Thanks." and "Yes." fall
# below the bar but any real statement clears it.
_STRICT_MIN_TOKENS = 4

# Token-overlap ratio required to consider a citation as supporting
# a claim. Matches the local :meth:`Citation.supports` heuristic
# (``overlap >= len(tokens) // 2``), re-expressed as a ratio so the
# Lambda handler — which cannot import :class:`Citation` — stays in
# sync without copying a magic number.
_OVERLAP_RATIO = 0.5


# ---------------------------------------------------------------------------
# Sentence / claim extraction
# ---------------------------------------------------------------------------


def split_sentences(response_text: str) -> list[str]:
    """Split ``response_text`` into non-empty, trimmed sentences.

    Splits on newlines first, then on sentence terminators (``.!?``)
    within each line. The newline pre-split is what makes this
    markdown-aware: bullet points, table rows, and headers each start
    a new line and frequently end with a citation bracket rather than
    terminal punctuation, so splitting on ``.!?`` alone would glue an
    entire multi-bullet section into a single claim that no citation's
    token overlap could satisfy. See :data:`_LINE_BREAK_RE` for the
    full rationale.

    An empty or whitespace-only input returns ``[]`` so callers don't
    have to special-case it.
    """

    if not response_text:
        return []
    sentences: list[str] = []
    for line in _LINE_BREAK_RE.split(response_text.strip()):
        line = line.strip()
        if not line:
            continue
        for chunk in _SENTENCE_SPLIT_RE.split(line):
            trimmed = chunk.strip()
            if trimmed:
                sentences.append(trimmed)
    return sentences


def _salient_tokens(text: str) -> set[str]:
    """Return the lowercase salient tokens from ``text``.

    Salient tokens are alphanumeric sequences of length ≥
    :data:`_MIN_SALIENT_TOKEN_LEN` that are not in :data:`_STOP_TOKENS`.
    Numbers of any length are always retained so "12%" and "340" still
    anchor a claim even when every other word is a stop word.
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


def is_claim_sentence(sentence: str, *, strict: bool = True) -> bool:
    """Decide whether ``sentence`` should be checked for citation support.

    Sentences qualify as claims when they contain numeric content or a
    quoted passage (both strong signals of a factual assertion). In
    strict mode — the default — any sentence with enough salient
    tokens to carry a statement also qualifies; this matches the
    design's "ALL sentences if strict mode (default)" behavior.
    """

    if not sentence:
        return False
    if _NUMERIC_RE.search(sentence):
        return True
    if _QUOTE_RE.search(sentence):
        return True
    if not strict:
        return False
    return len(_salient_tokens(sentence)) >= _STRICT_MIN_TOKENS


def extract_claims(response_text: str, *, strict: bool = True) -> list[str]:
    """Return the list of claim sentences from ``response_text``."""

    return [s for s in split_sentences(response_text) if is_claim_sentence(s, strict=strict)]


# ---------------------------------------------------------------------------
# Citation matching
# ---------------------------------------------------------------------------


def _normalize_citation(citation: Any) -> tuple[str, str]:
    """Return ``(text, source)`` from a :class:`Citation` or dict.

    ``source`` is retained only to preserve call-site symmetry with
    :class:`Citation`; the matcher itself only needs ``text``. The
    Lambda handler consumes dicts straight off the JSON event, and
    :mod:`mna` callers pass :class:`Citation` instances — both work.
    """

    if isinstance(citation, Citation):
        return citation.text or "", citation.source or ""
    if isinstance(citation, Mapping):
        return str(citation.get("text", "") or ""), str(citation.get("source", "") or "")
    # Unknown citation shapes are treated as empty — a defensive
    # choice so a malformed entry can't raise and mask a real bug in
    # the response pipeline.
    return "", ""


def _citation_supports(claim: str, citation_text: str) -> bool:
    """Return ``True`` when ``citation_text`` plausibly supports ``claim``.

    Mirrors :meth:`mna.types.Citation.supports` but operates on raw
    strings so the Lambda handler — which cannot import from
    :mod:`mna` — can reuse the same logic by inlining this function.
    """

    if not claim or not citation_text:
        return False
    claim_lower = claim.lower()
    citation_lower = citation_text.lower()
    if claim_lower in citation_lower:
        return True
    claim_tokens = _salient_tokens(claim)
    if not claim_tokens:
        # Fall back to a loose substring check for sentences with no
        # salient tokens (extremely short claims). Being lenient here
        # avoids false negatives on conversational phrasing that
        # nonetheless echoes a citation verbatim.
        return claim_lower in citation_lower
    citation_tokens = _salient_tokens(citation_text)
    if not citation_tokens:
        return False
    overlap = claim_tokens & citation_tokens
    return len(overlap) / len(claim_tokens) >= _OVERLAP_RATIO


def check_citations(
    response_text: str,
    citations: Iterable[Any],
    *,
    strict: bool = True,
) -> EvaluationResult:
    """Run the citation check and return the structured result.

    ``citations`` may contain :class:`Citation` instances or plain
    dicts with a ``text`` key (as produced by
    :func:`mna.tools.kb_retrieve.retrieve` and consumed by the
    Lambda handler).
    """

    claim_sentences = extract_claims(response_text, strict=strict)
    citation_texts = [text for text, _src in (_normalize_citation(c) for c in citations) if text]

    unsupported: list[str] = []
    for claim in claim_sentences:
        if not any(_citation_supports(claim, ct) for ct in citation_texts):
            unsupported.append(claim)

    return EvaluationResult(
        passed=not unsupported,
        unsupported_claims=unsupported,
        total_claims=len(claim_sentences),
    )


__all__ = [
    "check_citations",
    "extract_claims",
    "is_claim_sentence",
    "split_sentences",
]

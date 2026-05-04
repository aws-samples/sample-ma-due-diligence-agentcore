"""Local mirrors of the evaluator Lambdas.

Each module under :mod:`mna.evaluators` reimplements the pure
Python logic of its Lambda counterpart so the notebook, CLI, and unit
tests can exercise the evaluator without a round trip through
CloudWatch Logs and IAM. The Lambda handler is the canonical
deployment artifact (see ``lambda/citation_check/handler.py`` and the
*Evaluator: Citation Check* section of the design document); the local
mirror exists strictly for fast iteration.

The two surfaces MUST agree on inputs and outputs. Tests enforce this
by asserting both implementations return the same
:class:`mna.types.EvaluationResult` for a shared fixture set.
"""

from __future__ import annotations

from mna.evaluators.citation_check import (
    check_citations,
    extract_claims,
    is_claim_sentence,
    split_sentences,
)

__all__ = [
    "check_citations",
    "extract_claims",
    "is_claim_sentence",
    "split_sentences",
]

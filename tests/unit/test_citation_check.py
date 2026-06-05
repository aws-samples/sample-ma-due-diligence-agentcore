"""Unit tests for the citation-check evaluator.

Covers both surfaces of the evaluator:

* :mod:`mna.evaluators.citation_check` — the local mirror used by the
  notebook, CLI, and in-process callers.
* ``lambda/citation_check/handler.py`` — the canonical Lambda
  deployment invoked by the Compliance Validation agent.

The Lambda module lives under a top-level ``lambda/`` directory whose
name collides with the Python keyword, so we load it via
:mod:`importlib.util` the same way ``tests/unit/test_cr_common.py``
loads ``_cr_common/send_response.py``.

Requirements exercised:

* Requirement 4.2 — every factual claim has at least one supporting
  citation (pass case, fail case with unsupported numeric claim).
* Requirement 4.3 — evaluator produces a pass/fail result with
  per-claim detail (``unsupported_claims`` enumerated).
* Requirement 15.3 — unit tests in ``tests/unit/`` exercising the
  evaluator with synthetic pass and fail fixtures.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

from mna.evaluators.citation_check import (
    check_citations,
    extract_claims,
    is_claim_sentence,
    split_sentences,
)
from mna.types import Citation

# ---------------------------------------------------------------------------
# Lambda handler loader.
#
# ``lambda`` is a Python keyword, so ``import lambda.citation_check`` is
# not valid syntax. Load the module by file path and register it under
# a benign name.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LAMBDA_HANDLER_PATH = _REPO_ROOT / "lambda" / "citation_check" / "handler.py"


@pytest.fixture(scope="module")
def lambda_handler_module():
    spec = importlib.util.spec_from_file_location(
        "citation_check_handler", _LAMBDA_HANDLER_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["citation_check_handler"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("citation_check_handler", None)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _supporting_citations_for_acme() -> list[Citation]:
    """Citations that back the claims in :func:`_acme_pass_response`."""

    return [
        Citation(
            text=(
                "Example Corp reported revenue of $250 million in fiscal 2023, "
                "driven by growth in regional freight brokerage operations."
            ),
            source="s3://mna-docs/cims/acme_logistics.pdf",
            page=4,
            score=0.91,
        ),
        Citation(
            text=(
                "The company operates a fleet of 340 trucks across twelve distribution "
                "centers concentrated in the southeastern United States."
            ),
            source="s3://mna-docs/cims/acme_logistics.pdf",
            page=7,
            score=0.88,
        ),
        Citation(
            text=(
                "EBITDA margin expanded from 11 percent in 2021 to 14 percent in 2023, "
                "exceeding the transportation sector median."
            ),
            source="s3://mna-docs/financials/acme_logistics_statements.pdf",
            page=2,
            score=0.82,
        ),
    ]


def _acme_pass_response() -> str:
    """A response whose claims are all supported by the fixture citations."""

    return (
        "Example Corp reported revenue of $250 million in fiscal 2023. "
        "The company operates a fleet of 340 trucks across twelve distribution centers. "
        "EBITDA margin expanded from 11 percent in 2021 to 14 percent in 2023."
    )


# ---------------------------------------------------------------------------
# split_sentences
# ---------------------------------------------------------------------------


class TestSplitSentences:
    def test_empty_input_returns_empty_list(self) -> None:
        assert split_sentences("") == []

    def test_whitespace_only_input_returns_empty_list(self) -> None:
        assert split_sentences("   \n\t ") == []

    def test_splits_on_sentence_terminators(self) -> None:
        text = "First sentence. Second sentence! Third sentence?"
        assert split_sentences(text) == [
            "First sentence.",
            "Second sentence!",
            "Third sentence?",
        ]

    def test_preserves_internal_punctuation(self) -> None:
        text = 'He said "buy now" and left. Others disagreed.'
        sentences = split_sentences(text)
        assert len(sentences) == 2
        assert '"buy now"' in sentences[0]


# ---------------------------------------------------------------------------
# is_claim_sentence
# ---------------------------------------------------------------------------


class TestIsClaimSentence:
    def test_numeric_sentence_is_a_claim(self) -> None:
        assert is_claim_sentence("Revenue grew 12 percent last year.")

    def test_quoted_sentence_is_a_claim(self) -> None:
        assert is_claim_sentence('The CEO said "we will expand" in the memo.')

    def test_substantive_sentence_is_a_claim_in_strict_mode(self) -> None:
        assert is_claim_sentence(
            "Management projects strong regional freight brokerage expansion.",
            strict=True,
        )

    def test_substantive_sentence_is_not_a_claim_in_non_strict_mode(self) -> None:
        assert not is_claim_sentence(
            "Management projects strong regional freight brokerage expansion.",
            strict=False,
        )

    def test_short_filler_is_not_a_claim(self) -> None:
        assert not is_claim_sentence("Thanks.")
        assert not is_claim_sentence("Sure!")
        assert not is_claim_sentence("Okay then.")

    def test_empty_sentence_is_not_a_claim(self) -> None:
        assert not is_claim_sentence("")


# ---------------------------------------------------------------------------
# extract_claims
# ---------------------------------------------------------------------------


class TestExtractClaims:
    def test_skips_filler_keeps_factual_sentences(self) -> None:
        text = (
            "Sure. Example Corp reported revenue of $250 million. "
            "Thanks for asking."
        )
        claims = extract_claims(text)
        assert claims == ["Example Corp reported revenue of $250 million."]

    def test_returns_empty_for_empty_input(self) -> None:
        assert extract_claims("") == []


# ---------------------------------------------------------------------------
# check_citations — pass fixtures
# ---------------------------------------------------------------------------


class TestCheckCitationsPass:
    def test_all_claims_supported_passes(self) -> None:
        result = check_citations(_acme_pass_response(), _supporting_citations_for_acme())
        assert result.passed is True
        assert result.unsupported_claims == []
        assert result.total_claims == 3
        assert result.supported_claims == 3

    def test_empty_response_passes_with_zero_claims(self) -> None:
        result = check_citations("", [])
        assert result.passed is True
        assert result.total_claims == 0
        assert result.unsupported_claims == []

    def test_conversational_only_response_has_no_claims(self) -> None:
        result = check_citations("Sure. Thanks. Okay!", [])
        assert result.passed is True
        assert result.total_claims == 0

    def test_accepts_dict_shaped_citations(self) -> None:
        citations = [
            {
                "text": "Example Corp revenue reached 250 million in 2023.",
                "source": "s3://mna-docs/cims/acme.pdf",
                "page": 4,
                "score": 0.9,
            }
        ]
        result = check_citations(
            "Example Corp revenue reached 250 million in 2023.",
            citations,
        )
        assert result.passed is True


# ---------------------------------------------------------------------------
# check_citations — fail fixtures
# ---------------------------------------------------------------------------


class TestCheckCitationsFail:
    def test_numeric_claim_without_supporting_citation_fails(self) -> None:
        response = (
            "Example Corp reported revenue of $250 million in fiscal 2023. "
            "The company also operates 900 warehouses in twenty states."
        )
        # Only the revenue claim is supported; the warehouse claim has
        # no matching citation.
        citations = [
            Citation(
                text="Example Corp reported revenue of $250 million in fiscal 2023.",
                source="s3://mna-docs/cims/acme_logistics.pdf",
                page=4,
            )
        ]

        result = check_citations(response, citations)

        assert result.passed is False
        assert result.total_claims == 2
        assert result.supported_claims == 1
        assert len(result.unsupported_claims) == 1
        assert "900 warehouses" in result.unsupported_claims[0]

    def test_no_citations_fails_every_claim(self) -> None:
        response = _acme_pass_response()
        result = check_citations(response, [])
        assert result.passed is False
        assert result.total_claims == 3
        assert len(result.unsupported_claims) == 3

    def test_irrelevant_citation_does_not_support_claim(self) -> None:
        response = "Bluewave Freight grew ocean-container volume 18 percent in 2023."
        citations = [
            Citation(
                text=(
                    "Cascade Transport operates regional trucking routes "
                    "primarily in the Pacific Northwest."
                ),
                source="s3://mna-docs/cims/cascade.pdf",
            )
        ]
        result = check_citations(response, citations)
        assert result.passed is False
        assert result.unsupported_claims == [response]


# ---------------------------------------------------------------------------
# EvaluationResult shape
# ---------------------------------------------------------------------------


class TestEvaluationResultShape:
    def test_to_dict_matches_lambda_contract(self) -> None:
        result = check_citations("Some 42-fact claim.", [])
        as_dict = result.to_dict()
        assert set(as_dict.keys()) == {"passed", "unsupported_claims", "total_claims"}
        assert as_dict["passed"] is False
        assert as_dict["total_claims"] == 1


# ---------------------------------------------------------------------------
# Lambda handler — direct invocation parity with the local mirror
# ---------------------------------------------------------------------------


class TestLambdaHandler:
    def test_pass_case_matches_local_evaluator(self, lambda_handler_module) -> None:
        citations_dicts = [c.to_dict() for c in _supporting_citations_for_acme()]
        event = {"response_text": _acme_pass_response(), "citations": citations_dicts}
        result = lambda_handler_module.handler(event, object())

        expected = check_citations(
            _acme_pass_response(), _supporting_citations_for_acme()
        ).to_dict()
        assert result == expected
        assert result["passed"] is True
        assert result["total_claims"] == 3

    def test_fail_case_matches_local_evaluator(self, lambda_handler_module) -> None:
        response = (
            "Example Corp reported revenue of $250 million in fiscal 2023. "
            "The company also operates 900 warehouses in twenty states."
        )
        citation = Citation(
            text="Example Corp reported revenue of $250 million in fiscal 2023.",
            source="s3://mna-docs/cims/acme_logistics.pdf",
            page=4,
        )
        event = {
            "response_text": response,
            "citations": [citation.to_dict()],
        }
        result = lambda_handler_module.handler(event, object())
        expected = check_citations(response, [citation]).to_dict()
        assert result == expected
        assert result["passed"] is False
        assert len(result["unsupported_claims"]) == 1

    def test_empty_event_returns_passing_zero_claims(self, lambda_handler_module) -> None:
        result = lambda_handler_module.handler({}, object())
        assert result == {"passed": True, "unsupported_claims": [], "total_claims": 0}

    def test_non_dict_event_does_not_raise(self, lambda_handler_module) -> None:
        # The Lambda may be invoked with a stray JSON array in tests or
        # malformed client calls. The handler must degrade gracefully.
        result = lambda_handler_module.handler([], object())  # type: ignore[arg-type]
        assert result == {"passed": True, "unsupported_claims": [], "total_claims": 0}

    def test_malformed_citation_entries_are_ignored(self, lambda_handler_module) -> None:
        # A mix of valid and invalid entries should still produce a
        # coherent result using only the valid citations.
        event = {
            "response_text": "Example Corp reported revenue of $250 million.",
            "citations": [
                None,
                "not a dict",
                {"source": "s3://missing-text.pdf"},  # no text key
                {
                    "text": (
                        "Example Corp reported revenue of $250 million in 2023."
                    ),
                    "source": "s3://mna-docs/cims/acme.pdf",
                },
            ],
        }
        result = lambda_handler_module.handler(event, object())
        assert result["passed"] is True
        assert result["total_claims"] == 1

    def test_non_strict_mode_skips_unquoted_non_numeric_sentences(
        self, lambda_handler_module
    ) -> None:
        event = {
            "response_text": (
                "Management projects strong regional freight brokerage expansion."
            ),
            "citations": [],
            "strict": False,
        }
        result = lambda_handler_module.handler(event, object())
        # In non-strict mode, sentences without numbers or quotes
        # aren't treated as claims.
        assert result == {"passed": True, "unsupported_claims": [], "total_claims": 0}

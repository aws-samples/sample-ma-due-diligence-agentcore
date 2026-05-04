"""Unit tests for the market-data Gateway tool Lambda.

The handler returns deterministic synthetic comparable multiples for
the Financial Analysis specialist (design §Gateway Targets: Lambda
and §Components → Tools → market_data.py). These tests cover:

* **Determinism.** Repeated calls with the same inputs must produce
  byte-for-byte identical payloads (design determinism requirement,
  Requirement 3.2).
* **Differentiation.** Different industry or deal-size inputs must
  produce different comparables so the tool is actually useful.
* **Synthetic labeling.** Every response must carry the
  ``synthetic=True`` flag and the disclaimer string so downstream
  consumers are reminded the data is illustrative (Requirement 14.5).
* **Graceful degradation.** Missing or malformed events never raise.

The Lambda module lives under a top-level ``lambda/`` directory whose
name collides with the Python keyword, so we load it via
:mod:`importlib.util` following the same pattern as
``tests/unit/test_citation_check.py``.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Lambda handler loader
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_LAMBDA_HANDLER_PATH = _REPO_ROOT / "lambda" / "market_data" / "handler.py"


@pytest.fixture(scope="module")
def market_data_module():
    spec = importlib.util.spec_from_file_location("market_data_handler", _LAMBDA_HANDLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["market_data_handler"] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("market_data_handler", None)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_produce_identical_payload(self, market_data_module) -> None:
        event = {"industry_code": "transportation", "deal_size_band": "100M-500M"}
        first = market_data_module.handler(event, object())
        second = market_data_module.handler(event, object())
        assert first == second

    def test_same_inputs_across_many_calls(self, market_data_module) -> None:
        event = {"industry_code": "logistics", "deal_size_band": "500M-1B"}
        results = [market_data_module.handler(event, object()) for _ in range(10)]
        for result in results[1:]:
            assert result == results[0]

    def test_empty_event_is_deterministic(self, market_data_module) -> None:
        first = market_data_module.handler({}, object())
        second = market_data_module.handler({}, object())
        assert first == second


# ---------------------------------------------------------------------------
# Differentiation
# ---------------------------------------------------------------------------


class TestDifferentiation:
    def test_different_industry_codes_produce_different_comparables(
        self, market_data_module
    ) -> None:
        base = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "100M-500M"},
            object(),
        )
        alt = market_data_module.handler(
            {"industry_code": "logistics", "deal_size_band": "100M-500M"},
            object(),
        )
        assert base["comparables"] != alt["comparables"]

    def test_different_deal_size_bands_produce_different_comparables(
        self, market_data_module
    ) -> None:
        small = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "<100M"},
            object(),
        )
        large = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": ">1B"},
            object(),
        )
        assert small["comparables"] != large["comparables"]

    def test_revenue_scales_with_deal_size_band(self, market_data_module) -> None:
        small = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "<100M"},
            object(),
        )
        large = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": ">1B"},
            object(),
        )
        max_small_revenue = max(c["revenue_usd"] for c in small["comparables"])
        min_large_revenue = min(c["revenue_usd"] for c in large["comparables"])
        # The bands don't overlap, so every large revenue beats every
        # small revenue by construction.
        assert min_large_revenue > max_small_revenue


# ---------------------------------------------------------------------------
# Synthetic labeling
# ---------------------------------------------------------------------------


class TestSyntheticLabeling:
    def test_response_always_contains_synthetic_flag(self, market_data_module) -> None:
        result = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "100M-500M"},
            object(),
        )
        assert result["synthetic"] is True

    def test_response_always_contains_disclaimer(self, market_data_module) -> None:
        result = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "100M-500M"},
            object(),
        )
        assert result["disclaimer"] == "SYNTHETIC DATA - NOT REAL MARKET DATA"

    def test_empty_event_still_labeled_synthetic(self, market_data_module) -> None:
        result = market_data_module.handler({}, object())
        assert result["synthetic"] is True
        assert "SYNTHETIC" in result["disclaimer"]

    def test_comparable_names_are_anonymous(self, market_data_module) -> None:
        result = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "100M-500M"},
            object(),
        )
        for comparable in result["comparables"]:
            assert comparable["name"].startswith("Anonymous Tracker ")


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------


class TestPayloadShape:
    @pytest.fixture(scope="class")
    def default_result(self, market_data_module):
        return market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "100M-500M"},
            object(),
        )

    def test_top_level_keys(self, default_result) -> None:
        expected_keys = {
            "synthetic",
            "disclaimer",
            "industry_code",
            "deal_size_band",
            "comparables",
            "median_ev_ebitda",
            "median_ev_revenue",
            "p25_ev_ebitda",
            "p75_ev_ebitda",
        }
        assert expected_keys.issubset(default_result.keys())

    def test_inputs_are_echoed(self, default_result) -> None:
        assert default_result["industry_code"] == "transportation"
        assert default_result["deal_size_band"] == "100M-500M"

    def test_comparable_count_is_between_three_and_five(self, default_result) -> None:
        count = len(default_result["comparables"])
        assert 3 <= count <= 5

    def test_each_comparable_has_required_fields(self, default_result) -> None:
        required = {"name", "ev_ebitda", "ev_revenue", "revenue_usd", "ebitda_margin_pct"}
        for comparable in default_result["comparables"]:
            assert required.issubset(comparable.keys())

    def test_multiples_fall_within_documented_ranges(self, default_result) -> None:
        for comparable in default_result["comparables"]:
            assert 6.0 <= comparable["ev_ebitda"] <= 12.0
            assert 0.5 <= comparable["ev_revenue"] <= 2.5
            assert 8.0 <= comparable["ebitda_margin_pct"] <= 18.0
            assert 50_000_000 <= comparable["revenue_usd"] <= 3_000_000_000

    def test_quartiles_bracket_the_median(self, default_result) -> None:
        p25 = default_result["p25_ev_ebitda"]
        median = default_result["median_ev_ebitda"]
        p75 = default_result["p75_ev_ebitda"]
        assert p25 <= median <= p75


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_missing_industry_code(self, market_data_module) -> None:
        result = market_data_module.handler({"deal_size_band": "100M-500M"}, object())
        assert result["synthetic"] is True
        assert result["industry_code"] == ""
        assert len(result["comparables"]) > 0

    def test_missing_deal_size_band(self, market_data_module) -> None:
        result = market_data_module.handler({"industry_code": "transportation"}, object())
        assert result["synthetic"] is True
        assert result["deal_size_band"] == ""
        assert len(result["comparables"]) > 0

    def test_unknown_deal_size_band_falls_back_to_default_range(self, market_data_module) -> None:
        result = market_data_module.handler(
            {"industry_code": "transportation", "deal_size_band": "XYZ"},
            object(),
        )
        assert result["synthetic"] is True
        for comparable in result["comparables"]:
            assert 50_000_000 <= comparable["revenue_usd"] <= 1_000_000_000

    def test_non_dict_event_does_not_raise(self, market_data_module) -> None:
        result = market_data_module.handler([], object())  # type: ignore[arg-type]
        assert result["synthetic"] is True
        assert result["industry_code"] == ""
        assert result["deal_size_band"] == ""

    def test_none_values_do_not_raise(self, market_data_module) -> None:
        result = market_data_module.handler(
            {"industry_code": None, "deal_size_band": None}, object()
        )
        assert result["synthetic"] is True
        # None collapses to the empty string during coercion.
        assert result["industry_code"] == ""
        assert result["deal_size_band"] == ""

"""Market-data Gateway tool Lambda handler.

This Lambda backs the AgentCore Gateway target that the Financial
Analysis specialist calls when it needs comparable-company multiples
for a valuation exercise. Per the design document (section
*Gateway Targets: Lambda* and *Components and Interfaces → Tools →
tools/market_data.py*) the payload is **entirely synthetic** — no
real market data is ever returned, and every response is labeled as
such so downstream agents, evaluators, and readers are reminded the
numbers are illustrative.

Contract
--------

* **Input event.** A JSON-serializable dict::

      {"industry_code": "transportation", "deal_size_band": "100M-500M"}

  When invoked via the AgentCore Gateway the event shape mirrors the
  MCP tool-call arguments. Unknown or missing fields degrade to
  sensible defaults rather than raising — the handler must never take
  down the calling agent.

* **Output.** A dict with the shape documented in the design::

      {
          "synthetic": True,
          "disclaimer": "SYNTHETIC DATA - NOT REAL MARKET DATA",
          "industry_code": "transportation",
          "deal_size_band": "100M-500M",
          "comparables": [
              {
                  "name": "Anonymous Tracker 1",
                  "ev_ebitda": 8.4,
                  "ev_revenue": 1.2,
                  "revenue_usd": 210_000_000,
                  "ebitda_margin_pct": 13.2,
              },
              ...
          ],
          "median_ev_ebitda": 8.9,
          "median_ev_revenue": 1.3,
          "p25_ev_ebitda": 7.8,
          "p75_ev_ebitda": 10.1,
      }

Determinism
-----------

Repeated calls with the same ``(industry_code, deal_size_band)`` pair
MUST produce byte-for-byte identical payloads. Callers (agent traces,
evaluator fixtures, smoke tests) rely on this property. Determinism is
achieved by seeding a local :class:`random.Random` instance from a
SHA-256 digest of the two input fields — we never touch the global
random state so concurrent invocations stay isolated.

Security and labeling (Requirement 14.5)
----------------------------------------

* No real company names, tickers, or financials are embedded.
* Comparable names are always of the form ``Anonymous Tracker <N>``.
* The ``synthetic`` flag and ``disclaimer`` string are always present.
* The module imports only the standard library — no ``boto3``, no
  environment lookups at import time — so an accidental early deploy
  or a misconfigured Gateway target still returns a safe response.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

# ---------------------------------------------------------------------------
# Tunable ranges.
#
# Anchored on plausible transportation/logistics industry norms as of
# design time. Values are intentionally conservative: wide enough to
# look realistic in a demo, narrow enough that p25/p75 statistics
# don't degenerate to extremes.
# ---------------------------------------------------------------------------

_DISCLAIMER = "SYNTHETIC DATA - NOT REAL MARKET DATA"

_EV_EBITDA_MIN = 6.0
_EV_EBITDA_MAX = 12.0

_EV_REVENUE_MIN = 0.5
_EV_REVENUE_MAX = 2.5

_EBITDA_MARGIN_MIN_PCT = 8.0
_EBITDA_MARGIN_MAX_PCT = 18.0

# Deal-size bands map to a revenue window for the synthetic comparables.
# Bands not present here fall back to ``_DEFAULT_REVENUE_BAND``.
_REVENUE_BANDS_USD: dict[str, tuple[int, int]] = {
    "<100M": (50_000_000, 100_000_000),
    "100M-500M": (100_000_000, 500_000_000),
    "500M-1B": (500_000_000, 1_000_000_000),
    ">1B": (1_000_000_000, 3_000_000_000),
}
_DEFAULT_REVENUE_BAND: tuple[int, int] = (50_000_000, 1_000_000_000)

# Fixed count keeps responses under the 4 KB Custom Resource-style cap
# used elsewhere in the project and keeps median/quartile math stable.
_COMPARABLE_COUNT = 5


# ---------------------------------------------------------------------------
# Deterministic RNG seeding
# ---------------------------------------------------------------------------


def _seed_from_inputs(industry_code: str, deal_size_band: str) -> int:
    """Derive a stable integer seed from the two input fields.

    SHA-256 is used (not Python's ``hash()``) because ``hash()`` is
    salted per-process for strings and would break determinism across
    Lambda cold starts.
    """

    digest = hashlib.sha256(f"{industry_code}|{deal_size_band}".encode()).digest()
    # Take the first 8 bytes to build a 64-bit unsigned integer seed.
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def _rng_for(industry_code: str, deal_size_band: str) -> random.Random:
    """Return a local RNG seeded deterministically from the inputs."""

    return random.Random(_seed_from_inputs(industry_code, deal_size_band))


# ---------------------------------------------------------------------------
# Statistics helpers
#
# Keeping these local avoids a ``statistics`` import dance and lets us
# apply deterministic rounding rules in one place.
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the linearly-interpolated percentile of a sorted list.

    ``sorted_values`` must be pre-sorted ascending and non-empty.
    ``pct`` is a fraction in ``[0, 1]``. The function is small enough
    to include inline rather than pulling in :mod:`numpy`.
    """

    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    # Linear interpolation between adjacent rank positions.
    position = pct * (len(sorted_values) - 1)
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(sorted_values) - 1)
    fraction = position - lower_index
    lower = sorted_values[lower_index]
    upper = sorted_values[upper_index]
    return lower + (upper - lower) * fraction


def _median(sorted_values: list[float]) -> float:
    return _percentile(sorted_values, 0.5)


# ---------------------------------------------------------------------------
# Comparable generation
# ---------------------------------------------------------------------------


def _revenue_band_for(deal_size_band: str) -> tuple[int, int]:
    """Look up the revenue window for a deal-size band, with fallback."""

    band = _REVENUE_BANDS_USD.get(deal_size_band)
    if band is None:
        return _DEFAULT_REVENUE_BAND
    return band


def _build_comparable(
    index: int, rng: random.Random, revenue_band: tuple[int, int]
) -> dict[str, Any]:
    """Generate a single synthetic comparable record."""

    ev_ebitda = round(rng.uniform(_EV_EBITDA_MIN, _EV_EBITDA_MAX), 2)
    ev_revenue = round(rng.uniform(_EV_REVENUE_MIN, _EV_REVENUE_MAX), 2)
    revenue_usd = rng.randint(revenue_band[0], revenue_band[1])
    ebitda_margin_pct = round(rng.uniform(_EBITDA_MARGIN_MIN_PCT, _EBITDA_MARGIN_MAX_PCT), 2)
    return {
        "name": f"Anonymous Tracker {index}",
        "ev_ebitda": ev_ebitda,
        "ev_revenue": ev_revenue,
        "revenue_usd": revenue_usd,
        "ebitda_margin_pct": ebitda_margin_pct,
    }


def _build_comparables(rng: random.Random, revenue_band: tuple[int, int]) -> list[dict[str, Any]]:
    return [_build_comparable(i, rng, revenue_band) for i in range(1, _COMPARABLE_COUNT + 1)]


# ---------------------------------------------------------------------------
# Input coercion
# ---------------------------------------------------------------------------


def _coerce_str(event: dict[str, Any], key: str) -> str:
    """Pull a string field from ``event`` with graceful fallback.

    Missing or non-string values collapse to an empty string so the
    seeding and banding logic stays deterministic and total.
    """

    raw = event.get(key, "") if isinstance(event, dict) else ""
    if raw is None:
        return ""
    return str(raw)


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Return deterministic synthetic comparable multiples.

    The handler tolerates missing or malformed input by substituting
    empty strings, so an upstream agent bug can't take down the
    calling flow. See module docstring for the full contract.
    """

    if not isinstance(event, dict):
        event = {}

    industry_code = _coerce_str(event, "industry_code")
    deal_size_band = _coerce_str(event, "deal_size_band")

    rng = _rng_for(industry_code, deal_size_band)
    revenue_band = _revenue_band_for(deal_size_band)

    comparables = _build_comparables(rng, revenue_band)

    ev_ebitda_sorted = sorted(c["ev_ebitda"] for c in comparables)
    ev_revenue_sorted = sorted(c["ev_revenue"] for c in comparables)

    return {
        "synthetic": True,
        "disclaimer": _DISCLAIMER,
        "industry_code": industry_code,
        "deal_size_band": deal_size_band,
        "comparables": comparables,
        "median_ev_ebitda": round(_median(ev_ebitda_sorted), 2),
        "median_ev_revenue": round(_median(ev_revenue_sorted), 2),
        "p25_ev_ebitda": round(_percentile(ev_ebitda_sorted, 0.25), 2),
        "p75_ev_ebitda": round(_percentile(ev_ebitda_sorted, 0.75), 2),
    }

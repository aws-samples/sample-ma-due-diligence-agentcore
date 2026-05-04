"""Unit tests for ``mna.tools.market_data``.

The tool is a thin wrapper over the AgentCore Gateway. Tests stub the
boto3 client with ``MagicMock`` so no AWS calls are made.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import pytest

from mna.tools.market_data import (
    TOOL_NAME,
    MarketDataError,
    get_comparable_multiples,
)

_GATEWAY_ARN = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/mna"


def _gateway_payload() -> dict:
    """Representative market-data Lambda payload."""

    return {
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
        ],
        "median_ev_ebitda": 8.9,
        "median_ev_revenue": 1.3,
        "p25_ev_ebitda": 7.8,
        "p75_ev_ebitda": 10.1,
    }


def _streaming_response(payload: dict) -> dict:
    """Mimic the ``{"response": <file-like>}`` shape of the data plane."""

    body = io.BytesIO(json.dumps(payload).encode("utf-8"))
    return {"response": body}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestGetComparableMultiplesHappyPath:
    def test_invokes_gateway_with_correct_args(self) -> None:
        client = MagicMock()
        client.invoke_gateway.return_value = _streaming_response(_gateway_payload())

        result = get_comparable_multiples(
            "transportation",
            "100M-500M",
            gateway_arn=_GATEWAY_ARN,
            agentcore_client=client,
        )

        client.invoke_gateway.assert_called_once()
        call = client.invoke_gateway.call_args
        assert call.kwargs["gatewayArn"] == _GATEWAY_ARN

        body = json.loads(call.kwargs["payload"])
        assert body["tool"] == TOOL_NAME
        assert body["arguments"] == {
            "industry_code": "transportation",
            "deal_size_band": "100M-500M",
        }

        # Return shape matches what the Financial Analysis agent expects.
        assert result["synthetic"] is True
        assert result["industry_code"] == "transportation"
        assert result["deal_size_band"] == "100M-500M"
        assert len(result["comparables"]) == 1

    def test_accepts_bytes_response(self) -> None:
        client = MagicMock()
        client.invoke_gateway.return_value = {
            "response": json.dumps(_gateway_payload()).encode("utf-8"),
        }

        result = get_comparable_multiples(
            "logistics",
            "500M-1B",
            gateway_arn=_GATEWAY_ARN,
            agentcore_client=client,
        )

        assert result["synthetic"] is True
        assert result["disclaimer"].startswith("SYNTHETIC")

    def test_falls_back_to_generic_invoke_when_invoke_gateway_missing(self) -> None:
        # Some preview SDKs only expose ``invoke``.
        class _PartialClient:
            def __init__(self) -> None:
                self.invoke = MagicMock(return_value=_streaming_response(_gateway_payload()))

        client = _PartialClient()
        result = get_comparable_multiples(
            "transportation",
            "100M-500M",
            gateway_arn=_GATEWAY_ARN,
            agentcore_client=client,  # type: ignore[arg-type]
        )
        assert result["synthetic"] is True
        client.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# Gateway ARN resolution
# ---------------------------------------------------------------------------


class TestGatewayArnResolution:
    def test_falls_back_to_config_when_gateway_arn_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_config = MagicMock()
        fake_config.gateway_arn = _GATEWAY_ARN
        monkeypatch.setattr("mna.tools.market_data.load_config", lambda **_: fake_config)

        client = MagicMock()
        client.invoke_gateway.return_value = _streaming_response(_gateway_payload())

        get_comparable_multiples(
            "transportation",
            "100M-500M",
            agentcore_client=client,
        )

        assert client.invoke_gateway.call_args.kwargs["gatewayArn"] == _GATEWAY_ARN

    def test_raises_when_config_lookup_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(**_: object) -> object:
            raise RuntimeError("SSM parameter /mna/gateway/arn not found")

        monkeypatch.setattr("mna.tools.market_data.load_config", _boom)

        client = MagicMock()
        with pytest.raises(MarketDataError, match="could not be resolved"):
            get_comparable_multiples(
                "transportation",
                "100M-500M",
                agentcore_client=client,
            )
        client.invoke_gateway.assert_not_called()

    def test_raises_when_config_returns_empty_gateway_arn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_config = MagicMock()
        fake_config.gateway_arn = ""
        monkeypatch.setattr("mna.tools.market_data.load_config", lambda **_: fake_config)

        client = MagicMock()
        with pytest.raises(MarketDataError, match="empty string"):
            get_comparable_multiples(
                "transportation",
                "100M-500M",
                agentcore_client=client,
            )
        client.invoke_gateway.assert_not_called()


# ---------------------------------------------------------------------------
# Input validation and error wrapping
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_rejects_empty_industry_code(self) -> None:
        client = MagicMock()
        with pytest.raises(MarketDataError, match="industry_code"):
            get_comparable_multiples(
                "   ",
                "100M-500M",
                gateway_arn=_GATEWAY_ARN,
                agentcore_client=client,
            )
        client.invoke_gateway.assert_not_called()

    def test_rejects_empty_deal_size_band(self) -> None:
        client = MagicMock()
        with pytest.raises(MarketDataError, match="deal_size_band"):
            get_comparable_multiples(
                "transportation",
                "",
                gateway_arn=_GATEWAY_ARN,
                agentcore_client=client,
            )
        client.invoke_gateway.assert_not_called()

    def test_rejects_non_string_inputs(self) -> None:
        client = MagicMock()
        with pytest.raises(MarketDataError):
            get_comparable_multiples(
                None,  # type: ignore[arg-type]
                "100M-500M",
                gateway_arn=_GATEWAY_ARN,
                agentcore_client=client,
            )


class TestErrorWrapping:
    def test_wraps_underlying_client_error(self) -> None:
        client = MagicMock()
        client.invoke_gateway.side_effect = RuntimeError("AccessDeniedException")

        with pytest.raises(MarketDataError, match="Gateway invocation failed"):
            get_comparable_multiples(
                "transportation",
                "100M-500M",
                gateway_arn=_GATEWAY_ARN,
                agentcore_client=client,
            )

    def test_raises_on_non_json_body(self) -> None:
        client = MagicMock()
        client.invoke_gateway.return_value = {"response": b"<html>404</html>"}

        with pytest.raises(MarketDataError, match="non-JSON body"):
            get_comparable_multiples(
                "transportation",
                "100M-500M",
                gateway_arn=_GATEWAY_ARN,
                agentcore_client=client,
            )

    def test_raises_on_unexpected_json_shape(self) -> None:
        client = MagicMock()
        client.invoke_gateway.return_value = {"response": b"[]"}

        with pytest.raises(MarketDataError, match="unexpected JSON type"):
            get_comparable_multiples(
                "transportation",
                "100M-500M",
                gateway_arn=_GATEWAY_ARN,
                agentcore_client=client,
            )

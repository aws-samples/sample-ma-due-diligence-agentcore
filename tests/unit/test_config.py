"""Unit tests for ``mna.config`` SSM parameter resolution.

These tests use ``unittest.mock`` to stub the SSM client so they run
with no AWS calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mna.config import (
    ALL_PARAMETERS,
    PARAM_AURORA_CLUSTER_ARN,
    PARAM_AURORA_SECRET_ARN,
    PARAM_DOCS_BUCKET,
    PARAM_EVALUATOR_ARN,
    PARAM_GATEWAY_ARN,
    PARAM_KB_ID,
    PARAM_RUNTIME_ARN,
    PARAM_SESSIONS_TABLE,
    ConfigError,
    clear_config_cache,
    get_parameter,
    get_parameters,
    load_config,
)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """Ensure the lru_cache on ``load_config`` doesn't leak between tests."""

    clear_config_cache()
    yield
    clear_config_cache()


def _deployed_params() -> dict[str, str]:
    return {
        PARAM_RUNTIME_ARN: "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/mna",
        PARAM_KB_ID: "KB123ABC",
        PARAM_DOCS_BUCKET: "mna-docs-111122223333-us-east-1",
        PARAM_AURORA_CLUSTER_ARN: "arn:aws:rds:us-east-1:111122223333:cluster:mna-aurora",
        PARAM_AURORA_SECRET_ARN: "arn:aws:secretsmanager:us-east-1:111122223333:secret:mna-aurora",
        PARAM_GATEWAY_ARN: "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/mna",
        PARAM_EVALUATOR_ARN: "arn:aws:lambda:us-east-1:111122223333:function:mna-citation-check",
        PARAM_SESSIONS_TABLE: "mna-sessions",
    }


def _make_get_parameters_response(values: dict[str, str], invalid: list[str] | None = None):
    return {
        "Parameters": [{"Name": n, "Value": v} for n, v in values.items()],
        "InvalidParameters": list(invalid or []),
    }


class TestGetParameter:
    def test_returns_value_for_known_parameter(self) -> None:
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Name": PARAM_KB_ID, "Value": "KB-XYZ"}}

        assert get_parameter(PARAM_KB_ID, ssm_client=ssm) == "KB-XYZ"
        ssm.get_parameter.assert_called_once_with(Name=PARAM_KB_ID, WithDecryption=True)

    def test_raises_config_error_when_value_is_empty(self) -> None:
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Name": PARAM_KB_ID, "Value": ""}}

        with pytest.raises(ConfigError, match="empty"):
            get_parameter(PARAM_KB_ID, ssm_client=ssm)

    def test_wraps_underlying_exception_in_config_error(self) -> None:
        ssm = MagicMock()
        ssm.get_parameter.side_effect = RuntimeError("boom")

        with pytest.raises(ConfigError, match="Failed to fetch SSM parameter"):
            get_parameter(PARAM_RUNTIME_ARN, ssm_client=ssm)

    def test_raises_config_error_when_parameter_not_found(self) -> None:
        """An SSM ParameterNotFound surfaces as a ConfigError with the parameter name.

        This is the shape botocore raises when the parameter doesn't
        exist in the target account/region — the most common failure
        mode when the stack has been torn down (Requirement 7.4, 10.2).
        """

        ssm = MagicMock()

        class _ParameterNotFound(Exception):
            def __init__(self) -> None:
                self.response = {"Error": {"Code": "ParameterNotFound"}}

        ssm.get_parameter.side_effect = _ParameterNotFound()

        with pytest.raises(ConfigError, match=PARAM_KB_ID) as exc_info:
            get_parameter(PARAM_KB_ID, ssm_client=ssm)
        # The message should mention "not found" specifically so the
        # reader can tell this is a missing-parameter error rather
        # than a transient AWS failure.
        assert "not found" in str(exc_info.value).lower()

    def test_returns_empty_string_treated_as_missing_even_with_parameter_key(self) -> None:
        """A malformed SSM response with a missing 'Value' key also raises."""

        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Name": PARAM_KB_ID}}

        with pytest.raises(ConfigError, match="empty"):
            get_parameter(PARAM_KB_ID, ssm_client=ssm)

    def test_malformed_response_without_parameter_key_raises(self) -> None:
        """Defensive: SSM returning a response with no Parameter key is rare but possible."""

        ssm = MagicMock()
        ssm.get_parameter.return_value = {}

        with pytest.raises(ConfigError, match="empty"):
            get_parameter(PARAM_KB_ID, ssm_client=ssm)


class TestGetParameters:
    def test_returns_all_requested_values_in_request_order(self) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        ssm.get_parameters.return_value = _make_get_parameters_response(params)

        resolved = get_parameters(list(ALL_PARAMETERS), ssm_client=ssm)

        assert list(resolved.keys()) == list(ALL_PARAMETERS)
        assert resolved == params
        ssm.get_parameters.assert_called_once_with(
            Names=list(ALL_PARAMETERS),
            WithDecryption=True,
        )

    def test_empty_names_short_circuits_without_calling_ssm(self) -> None:
        ssm = MagicMock()
        assert get_parameters([], ssm_client=ssm) == {}
        ssm.get_parameters.assert_not_called()

    def test_invalid_parameters_raise_config_error_listing_missing(self) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        del params[PARAM_GATEWAY_ARN]
        del params[PARAM_EVALUATOR_ARN]
        ssm.get_parameters.return_value = _make_get_parameters_response(
            params, invalid=[PARAM_GATEWAY_ARN, PARAM_EVALUATOR_ARN]
        )

        with pytest.raises(ConfigError) as exc_info:
            get_parameters(list(ALL_PARAMETERS), ssm_client=ssm)

        message = str(exc_info.value)
        assert PARAM_GATEWAY_ARN in message
        assert PARAM_EVALUATOR_ARN in message

    def test_empty_parameter_value_is_treated_as_missing(self) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        params[PARAM_KB_ID] = ""
        ssm.get_parameters.return_value = _make_get_parameters_response(params)

        with pytest.raises(ConfigError, match=PARAM_KB_ID):
            get_parameters(list(ALL_PARAMETERS), ssm_client=ssm)

    def test_wraps_underlying_exception_in_config_error(self) -> None:
        ssm = MagicMock()
        ssm.get_parameters.side_effect = RuntimeError("network down")

        with pytest.raises(ConfigError, match="Failed to fetch SSM parameters"):
            get_parameters(list(ALL_PARAMETERS), ssm_client=ssm)

    def test_partially_populated_response_surfaces_every_missing_name(self) -> None:
        """When SSM returns half the requested parameters the error lists all misses.

        This mirrors the real-world failure mode where a previous
        deployment only got halfway through (e.g., DataStack deployed
        but AgentStack failed). Callers need the full list of missing
        names so they know which stack to redeploy.
        """

        ssm = MagicMock()
        params = _deployed_params()
        # Simulate a partial deployment where only the Data-stack
        # parameters exist. The rest come back in InvalidParameters.
        partial = {
            PARAM_KB_ID: params[PARAM_KB_ID],
            PARAM_DOCS_BUCKET: params[PARAM_DOCS_BUCKET],
            PARAM_AURORA_CLUSTER_ARN: params[PARAM_AURORA_CLUSTER_ARN],
        }
        missing = [PARAM_RUNTIME_ARN, PARAM_GATEWAY_ARN, PARAM_EVALUATOR_ARN]
        ssm.get_parameters.return_value = _make_get_parameters_response(
            partial, invalid=missing
        )

        with pytest.raises(ConfigError) as exc_info:
            get_parameters(list(ALL_PARAMETERS), ssm_client=ssm)

        message = str(exc_info.value)
        # Every missing parameter must be surfaced, not just the first.
        for name in missing:
            assert name in message, f"expected {name} in error message, got {message!r}"

    def test_malformed_response_with_missing_keys_is_defensive(self) -> None:
        """SSM returning a response with no Parameters/InvalidParameters keys raises."""

        ssm = MagicMock()
        ssm.get_parameters.return_value = {}

        with pytest.raises(ConfigError, match="missing or empty"):
            get_parameters(list(ALL_PARAMETERS), ssm_client=ssm)


class TestLoadConfig:
    def test_returns_typed_mna_config_with_injected_client(self) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        ssm.get_parameters.return_value = _make_get_parameters_response(params)

        config = load_config(ssm_client=ssm)

        assert config.runtime_arn == params[PARAM_RUNTIME_ARN]
        assert config.kb_id == params[PARAM_KB_ID]
        assert config.docs_bucket == params[PARAM_DOCS_BUCKET]
        assert config.aurora_cluster_arn == params[PARAM_AURORA_CLUSTER_ARN]
        assert config.gateway_arn == params[PARAM_GATEWAY_ARN]
        assert config.evaluator_arn == params[PARAM_EVALUATOR_ARN]

    def test_as_dict_round_trips_via_ssm_parameter_names(self) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        ssm.get_parameters.return_value = _make_get_parameters_response(params)

        config = load_config(ssm_client=ssm)
        assert config.as_dict() == params

    def test_missing_parameter_raises_config_error(self) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        del params[PARAM_RUNTIME_ARN]
        ssm.get_parameters.return_value = _make_get_parameters_response(
            params, invalid=[PARAM_RUNTIME_ARN]
        )

        with pytest.raises(ConfigError, match=PARAM_RUNTIME_ARN):
            load_config(ssm_client=ssm)

    def test_caches_result_per_region_when_no_client_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        ssm.get_parameters.return_value = _make_get_parameters_response(params)

        build_calls: list[str | None] = []

        def fake_builder(region_name: str | None = None):
            build_calls.append(region_name)
            return ssm

        monkeypatch.setattr("mna.config._build_ssm_client", fake_builder)

        first = load_config(region_name="us-east-1")
        second = load_config(region_name="us-east-1")

        assert first is second
        assert build_calls == ["us-east-1"]  # built once, second call hits the cache
        assert ssm.get_parameters.call_count == 1

    def test_injected_client_bypasses_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ssm = MagicMock()
        params = _deployed_params()
        ssm.get_parameters.return_value = _make_get_parameters_response(params)

        # Builder should never be used when ssm_client is supplied.
        def explode(*_args: object, **_kwargs: object):  # pragma: no cover - guard
            raise AssertionError("builder must not be invoked when client is injected")

        monkeypatch.setattr("mna.config._build_ssm_client", explode)

        config_a = load_config(ssm_client=ssm)
        config_b = load_config(ssm_client=ssm)

        assert config_a == config_b
        assert ssm.get_parameters.call_count == 2  # no caching path

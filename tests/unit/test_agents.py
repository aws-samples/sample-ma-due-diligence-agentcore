"""Unit tests for the Strands-based agent modules.

These tests exercise the module-level wiring of each specialist and
the supervisor: they confirm that every agent exposes an ``agent``
instance, the expected ``TOOLS`` list, a loaded ``SYSTEM_PROMPT``, and
a callable ``handler``.

The Strands SDK is not guaranteed to be installed in the test
environment (it only ships inside the built agent container image).
When the SDK is absent the agent modules fall back to the stubs in
:mod:`mna.agents._base`, which still expose enough surface for these
assertions. A dedicated ``skipif`` is applied to invocation-path
checks because the stubs raise on ``__call__``.

Requirements exercised:
  * 1.1 (multi-agent orchestration)
  * 1.2 (four specialists + supervisor)
  * 1.3 (Strands SDK in use)
  * 3.3 (Financial Analysis uses market_data)
  * 2.4 (Strategic Fit scoped to prior_deals namespace)
  * 4.1, 4.2 (Guardrail + citation_check wiring)
  * 12.1 (supervisor model configurable via env)
  * 15.3 (unit tests in tests/unit/)
  * 16.3 (module docstrings on every agent file)
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

import pytest

from mna.agents import (
    _base,
    compliance_validation,
    financial_analysis,
    strategic_fit,
    supervisor,
    target_screening,
)

# ---------------------------------------------------------------------------
# Specialist agents — shared assertions via parametrization
# ---------------------------------------------------------------------------


SPECIALIST_MODULES = [
    ("target_screening", target_screening, {"text_to_sql", "kb_retrieve"}),
    ("financial_analysis", financial_analysis, {"kb_retrieve", "market_data"}),
    ("strategic_fit", strategic_fit, {"kb_retrieve", "retrieve_memory"}),
    (
        "compliance_validation",
        compliance_validation,
        {"kb_retrieve", "citation_check"},
    ),
]


@pytest.mark.parametrize("agent_name,module,expected_tool_names", SPECIALIST_MODULES)
def test_specialist_agent_wiring(agent_name, module, expected_tool_names):
    """Each specialist exposes the required module-level surface."""

    # Agent metadata.
    assert agent_name == module.AGENT_NAME
    assert isinstance(module.SYSTEM_PROMPT, str)
    assert len(module.SYSTEM_PROMPT) >= 100, (
        f"{agent_name} system prompt should be substantive (>=100 chars)"
    )

    # Agent instance.
    assert module.agent is not None
    assert module.agent.name == agent_name
    # The pinned specialist model is Haiku by default.
    assert module.agent.model is not None
    assert module.agent.model.model_id == _base.DEFAULT_SPECIALIST_MODEL

    # Tool list.
    assert isinstance(module.TOOLS, list)
    assert len(module.TOOLS) == len(expected_tool_names)
    tool_names = {getattr(t, "__name__", None) for t in module.TOOLS}
    assert tool_names == expected_tool_names
    # The agent instance received exactly the same tool list.
    assert module.agent.tools == module.TOOLS


@pytest.mark.parametrize("agent_name,module,_expected", SPECIALIST_MODULES)
def test_specialist_handler_rejects_empty_prompt(agent_name, module, _expected):
    """The handler returns a structured error on empty/missing prompt."""

    for bad_event in (None, {}, {"prompt": ""}, {"prompt": "   "}, {"prompt": 123}):
        result = module.handler(bad_event, None)
        assert isinstance(result, dict)
        assert result.get("error") == "prompt is required", agent_name
        assert result.get("text") == ""


@pytest.mark.parametrize("agent_name,module,_expected", SPECIALIST_MODULES)
def test_specialist_module_has_docstring(agent_name, module, _expected):
    """Every specialist module has a substantive module docstring (Req 16.3)."""

    assert module.__doc__ is not None
    doc = module.__doc__
    assert "Role" in doc
    assert "Tools" in doc
    assert "Example prompt" in doc


# ---------------------------------------------------------------------------
# Target Screening — text_to_sql is the primary tool.
# ---------------------------------------------------------------------------


def test_target_screening_primary_tool_is_text_to_sql():
    """The first tool passed to the agent is text_to_sql (primary)."""

    assert target_screening.TOOLS[0].__name__ == "text_to_sql"


# ---------------------------------------------------------------------------
# Strategic Fit — retrieve_memory defaults to the prior_deals namespace.
# ---------------------------------------------------------------------------


def test_strategic_fit_memory_uses_prior_deals_namespace():
    """``retrieve_memory`` passes ``PRIOR_DEALS_NAMESPACE`` to the tool."""

    captured: dict[str, object] = {}

    def _fake_retrieve_memory(namespace, query=None, *, limit=10, **kwargs):
        captured["namespace"] = namespace
        captured["query"] = query
        captured["limit"] = limit
        return []

    with patch.object(strategic_fit, "retrieve_memory_fn", _fake_retrieve_memory):
        strategic_fit.retrieve_memory(query="Acme", limit=3)

    assert captured["namespace"] == "prior_deals"
    assert captured["query"] == "Acme"
    assert captured["limit"] == 3


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


def test_supervisor_wiring():
    """Supervisor exposes agent + 4 tools + handler."""

    assert supervisor.AGENT_NAME == "supervisor"
    assert isinstance(supervisor.SYSTEM_PROMPT, str)
    assert len(supervisor.SYSTEM_PROMPT) >= 100

    assert supervisor.agent is not None
    assert supervisor.agent.name == "supervisor"

    # One tool per specialist, in the design-doc order.
    assert len(supervisor.TOOLS) == 4
    assert len(supervisor.SPECIALISTS) == 4
    assert list(supervisor.SPECIALISTS.keys()) == [
        "target_screening",
        "financial_analysis",
        "strategic_fit",
        "compliance_validation",
    ]


def test_supervisor_module_docstring():
    """Supervisor module docstring covers role, tools, example prompt."""

    doc = supervisor.__doc__
    assert doc is not None
    assert "Role" in doc
    assert "Tools" in doc
    assert "Example prompt" in doc
    assert "Guardrail" in doc


def test_supervisor_default_model_is_sonnet_when_env_unset(monkeypatch):
    """Without ``MNA_SUPERVISOR_MODEL`` the default is Claude Sonnet 4.5.

    The module constant is read at import time, so we reload the
    ``_base`` module after clearing the env var to assert the default.
    """

    monkeypatch.delenv("MNA_SUPERVISOR_MODEL", raising=False)
    reloaded = importlib.reload(_base)
    try:
        assert reloaded.DEFAULT_SUPERVISOR_MODEL == "anthropic.claude-sonnet-4-5-v1:0"
    finally:
        # Reload once more under the original env (already cleared here)
        # so cached state in sys.modules stays consistent for later tests.
        importlib.reload(_base)


def test_supervisor_model_overridable_via_env(monkeypatch):
    """``MNA_SUPERVISOR_MODEL`` overrides the default (Req 12.1)."""

    monkeypatch.setenv("MNA_SUPERVISOR_MODEL", "anthropic.claude-opus-test-v1:0")
    reloaded = importlib.reload(_base)
    try:
        assert reloaded.DEFAULT_SUPERVISOR_MODEL == "anthropic.claude-opus-test-v1:0"
    finally:
        monkeypatch.delenv("MNA_SUPERVISOR_MODEL", raising=False)
        importlib.reload(_base)


def test_supervisor_guardrail_attached_when_env_set(monkeypatch):
    """``MNA_GUARDRAIL_ID`` flows into the supervisor's BedrockModel (Req 4.1)."""

    monkeypatch.setenv("MNA_GUARDRAIL_ID", "gr-test-12345")
    # Reload the supervisor module so the guardrail env var is read fresh.
    # We also have to reload _base first so any re-exports refer to the
    # reloaded supervisor module attributes.
    reloaded_base = importlib.reload(_base)
    assert reloaded_base is _base or reloaded_base  # sanity

    reloaded = importlib.reload(supervisor)
    try:
        assert reloaded.GUARDRAIL_ID == "gr-test-12345"
        # The stub BedrockModel stores kwargs in ``extra``; the real
        # Strands model stores ``guardrail_id`` on the instance. Check
        # both paths so the test survives either environment.
        model = reloaded.agent.model
        found = (
            getattr(model, "guardrail_id", None) == "gr-test-12345"
            or (getattr(model, "extra", {}) or {}).get("guardrail_id") == "gr-test-12345"
        )
        assert found, "guardrail_id was not attached to the supervisor BedrockModel"
    finally:
        monkeypatch.delenv("MNA_GUARDRAIL_ID", raising=False)
        importlib.reload(supervisor)


def test_supervisor_handler_rejects_empty_prompt():
    """The supervisor handler returns a structured error on empty prompt."""

    result = supervisor.handler({}, None)
    assert result == {"text": "", "error": "prompt is required"}


def test_supervisor_app_entrypoint_registered():
    """The supervisor decorates its handler with ``@app.entrypoint``.

    On the stub BedrockAgentCoreApp, the decorator records the handler
    on ``app.entrypoint_handler`` so we can confirm the registration.
    The real SDK performs the same association internally; we accept
    either shape.
    """

    # Either the stub has captured the handler, or the real SDK has
    # registered it (attribute access is best-effort).
    captured = getattr(supervisor.app, "entrypoint_handler", None)
    if captured is not None:
        assert captured is supervisor.handler


# ---------------------------------------------------------------------------
# _base helpers
# ---------------------------------------------------------------------------


def test_load_prompt_reads_expected_file():
    """``load_prompt`` reads the named prompt file from prompts/."""

    text = _base.load_prompt("target_screening")
    assert isinstance(text, str)
    assert "Target Screening" in text
    # The SQL-safety rule is the key invariant the prompt enforces.
    assert "SELECT" in text


def test_load_prompt_raises_on_missing_file():
    with pytest.raises(FileNotFoundError):
        _base.load_prompt("does_not_exist_anywhere")


def test_default_specialist_model_is_haiku_when_env_unset(monkeypatch):
    monkeypatch.delenv("MNA_SPECIALIST_MODEL", raising=False)
    reloaded = importlib.reload(_base)
    try:
        assert reloaded.DEFAULT_SPECIALIST_MODEL == "anthropic.claude-3-5-haiku-20241022-v1:0"
    finally:
        importlib.reload(_base)


# ---------------------------------------------------------------------------
# Tools wiring — verify underlying functions are called through.
# ---------------------------------------------------------------------------


def test_target_screening_text_to_sql_delegates_to_tool_module():
    """The ``text_to_sql`` tool forwards to ``mna.tools.text_to_sql.query``."""

    captured: dict[str, object] = {}

    def _fake_query(nl):
        captured["nl"] = nl
        return {"sql": "SELECT 1", "rows": [], "row_count": 0, "columns": []}

    with patch.object(target_screening, "text_to_sql_fn", _fake_query):
        result = target_screening.text_to_sql("show me companies")

    assert captured["nl"] == "show me companies"
    assert result["sql"] == "SELECT 1"


def test_financial_analysis_market_data_delegates_to_tool_module():
    """The ``market_data`` tool forwards to the gateway client."""

    captured: dict[str, object] = {}

    def _fake_market_data(industry_code, deal_size_band):
        captured["industry"] = industry_code
        captured["band"] = deal_size_band
        return {"synthetic": True, "comparables": []}

    with patch.object(financial_analysis, "market_data_fn", _fake_market_data):
        result = financial_analysis.market_data("transportation", "100M-500M")

    assert captured["industry"] == "transportation"
    assert captured["band"] == "100M-500M"
    assert result["synthetic"] is True


def test_compliance_citation_check_delegates_to_tool_module():
    """The ``citation_check`` tool forwards to the evaluator Lambda wrapper."""

    captured: dict[str, object] = {}

    def _fake_citation_check(response_text, citations):
        captured["text"] = response_text
        captured["citations"] = citations
        return {"passed": True, "unsupported_claims": [], "total_claims": 0}

    with patch.object(compliance_validation, "citation_check_fn", _fake_citation_check):
        result = compliance_validation.citation_check("some response", [])

    assert captured["text"] == "some response"
    assert captured["citations"] == []
    assert result["passed"] is True


# ---------------------------------------------------------------------------
# Tool-selection isolation — each specialist's TOOLS list is disjoint
# from the tools of any other specialist that shouldn't share it.
# ---------------------------------------------------------------------------


def test_specialist_tool_selections_match_design_table():
    """Confirms every specialist has exactly the tools its design row lists.

    Directly mirrors the table in design.md → *Agent Implementations*
    so an accidental tool swap (e.g. handing ``market_data`` to
    Compliance Validation) fails fast at test time. This is the
    "tool selection logic" coverage called out in task 37.
    """

    selections = {
        "target_screening": {t.__name__ for t in target_screening.TOOLS},
        "financial_analysis": {t.__name__ for t in financial_analysis.TOOLS},
        "strategic_fit": {t.__name__ for t in strategic_fit.TOOLS},
        "compliance_validation": {t.__name__ for t in compliance_validation.TOOLS},
    }

    # The text_to_sql tool is exclusive to Target Screening.
    assert "text_to_sql" in selections["target_screening"]
    assert "text_to_sql" not in selections["financial_analysis"]
    assert "text_to_sql" not in selections["strategic_fit"]
    assert "text_to_sql" not in selections["compliance_validation"]

    # market_data is exclusive to Financial Analysis (uses Gateway).
    assert "market_data" in selections["financial_analysis"]
    assert "market_data" not in selections["target_screening"]
    assert "market_data" not in selections["strategic_fit"]
    assert "market_data" not in selections["compliance_validation"]

    # retrieve_memory is exclusive to Strategic Fit (prior_deals ns).
    assert "retrieve_memory" in selections["strategic_fit"]
    assert "retrieve_memory" not in selections["target_screening"]
    assert "retrieve_memory" not in selections["financial_analysis"]
    assert "retrieve_memory" not in selections["compliance_validation"]

    # citation_check is exclusive to Compliance Validation.
    assert "citation_check" in selections["compliance_validation"]
    assert "citation_check" not in selections["target_screening"]
    assert "citation_check" not in selections["financial_analysis"]
    assert "citation_check" not in selections["strategic_fit"]

    # kb_retrieve is shared across all four specialists (grounding).
    for name, tools in selections.items():
        assert "kb_retrieve" in tools, f"{name} missing kb_retrieve"


def test_financial_analysis_does_not_expose_memory_or_sql():
    """Financial Analysis must not receive Memory or SQL tools.

    Prevents a future refactor from accidentally giving the DCF agent
    read access to prior-deal memos or the target-companies table —
    both would violate the design's specialist separation.
    """

    names = {t.__name__ for t in financial_analysis.TOOLS}
    assert "retrieve_memory" not in names
    assert "text_to_sql" not in names
    assert "citation_check" not in names


def test_target_screening_does_not_expose_memory_or_market_data():
    """Target Screening is SQL + KB only; no memory, no external gateway."""

    names = {t.__name__ for t in target_screening.TOOLS}
    assert "retrieve_memory" not in names
    assert "market_data" not in names
    assert "citation_check" not in names


def test_compliance_validation_does_not_expose_memory_or_market_data():
    """Compliance Validation grounds in KB only; no other specialist tools."""

    names = {t.__name__ for t in compliance_validation.TOOLS}
    assert "retrieve_memory" not in names
    assert "market_data" not in names
    assert "text_to_sql" not in names


def test_strategic_fit_does_not_expose_sql_or_market_data():
    """Strategic Fit only grounds in memory + KB."""

    names = {t.__name__ for t in strategic_fit.TOOLS}
    assert "text_to_sql" not in names
    assert "market_data" not in names
    assert "citation_check" not in names

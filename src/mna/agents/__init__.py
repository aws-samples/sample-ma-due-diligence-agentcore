"""Strands-based specialist and supervisor agents for the sample.

Each submodule (``target_screening``, ``financial_analysis``,
``strategic_fit``, ``compliance_validation``, ``supervisor``) exposes a
module-level ``agent`` instance and an AgentCore-compatible
``handler(event, context)`` callable. The ``supervisor`` module is also
the container entrypoint (``python -m mna.agents.supervisor``) used by
the agent runtime image.

Importing this package never fails when the Strands SDK is not
installed locally — each agent module wraps the SDK import in a
try/except and degrades to an inert stub so the tests can still import
the module and verify its tool wiring.
"""

from __future__ import annotations

__all__ = [
    "compliance_validation",
    "financial_analysis",
    "strategic_fit",
    "supervisor",
    "target_screening",
]

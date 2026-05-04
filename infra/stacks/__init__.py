"""CDK stack definitions for the M&A Due Diligence AgentCore sample.

Each stack lives in its own module and is instantiated by :mod:`infra.app`
in dependency order. The stacks currently ship as empty placeholders; the
resources they own are added in later phases of the implementation plan:

    - :class:`infra.stacks.network_stack.NetworkStack`      (task 5)
    - :class:`infra.stacks.data_stack.DataStack`            (tasks 6, 7)
    - :class:`infra.stacks.evaluator_stack.EvaluatorStack`  (task 8)
    - :class:`infra.stacks.gateway_stack.GatewayStack`      (task 9)
    - :class:`infra.stacks.agent_stack.AgentStack`          (task 13)
"""

from infra.stacks.agent_stack import AgentStack
from infra.stacks.data_stack import DataStack
from infra.stacks.evaluator_stack import EvaluatorStack
from infra.stacks.gateway_stack import GatewayStack
from infra.stacks.network_stack import NetworkStack

__all__ = [
    "AgentStack",
    "DataStack",
    "EvaluatorStack",
    "GatewayStack",
    "NetworkStack",
]

"""Reusable CDK constructs for the M&A Due Diligence AgentCore sample.

Constructs in this package encapsulate infrastructure patterns that
span multiple AWS resources but belong inside a single stack — typical
examples are "a CodeBuild-based image build pipeline" or "an MCP tool
stack composed of Lambda + Gateway + SSM parameter". Keeping these in
a dedicated constructs package rather than inlining them into stacks
lets tasks downstream (task 13) compose ``AgentStack`` from a small
set of well-named building blocks without each stack growing into a
multi-hundred-line monolith.
"""

from infra.constructs.build_pipeline import BuildPipelineConstruct

__all__ = ["BuildPipelineConstruct"]

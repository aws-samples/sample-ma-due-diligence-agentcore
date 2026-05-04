"""CDK application package for the M&A Due Diligence AgentCore sample.

This package contains the AWS CDK v2 (Python) infrastructure definition.
The entrypoint is :mod:`infra.app`, which instantiates the stacks in
dependency order:

    NetworkStack  -> DataStack -> EvaluatorStack -> GatewayStack -> AgentStack

Stack implementations live in :mod:`infra.stacks` and are progressively
filled in by later tasks (5-13 in the plan). For the bootstrap task the
stacks are empty placeholders so ``cdk synth`` can run end-to-end.
"""

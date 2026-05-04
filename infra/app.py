"""CDK v2 application entrypoint for the M&A Due Diligence AgentCore sample.

Running ``cdk synth`` (from the ``infra/`` directory, via ``cdk.json``) will
instantiate each stack in dependency order:

    NetworkStack -> DataStack -> EvaluatorStack -> GatewayStack -> AgentStack

Environment resolution
----------------------
The target ``aws_cdk.Environment`` is resolved from, in order of precedence:

    1. Context values on ``cdk.json`` (``mnaRegion``, ``mnaAccount``).
    2. CLI context overrides (``cdk deploy -c mnaRegion=us-east-1 ...``).
    3. ``CDK_DEFAULT_REGION`` / ``CDK_DEFAULT_ACCOUNT`` environment variables
       set by the CDK CLI from the reader's configured AWS profile.

Readers invoking ``deploy.sh`` / ``deploy.ps1`` (task 35) run the preflight
scripts under ``scripts/`` before this app is synthesized so an unsupported
region fails fast with a clear error, per Requirement 10.2.
"""

from __future__ import annotations

import os
import pathlib
import sys

# CDK runs ``python app.py`` from inside the ``infra/`` directory (per
# ``cdk.json``), which puts ``infra/`` on ``sys.path`` as its first
# entry. That causes two problems we need to fix before *anything*
# else runs:
#
#  1. ``aws_cdk`` imports the pip-installed ``constructs`` package at
#     module load. With ``infra/`` on ``sys.path``, Python resolves
#     bare ``import constructs`` to our local ``infra/constructs/``
#     directory instead, which crashes the CDK import chain.
#  2. ``from infra.stacks import ...`` below only resolves if the
#     *parent* of ``infra/`` (the repo root) is on ``sys.path`` so
#     ``infra`` is importable as a top-level package.
#
# Fix both by pointing ``sys.path`` at the repo root and dropping the
# ``infra/`` entry. ``pathlib.Path(__file__).resolve().parent`` is the
# ``infra/`` directory; ``.parent`` above that is the repo root.
_INFRA_DIR = pathlib.Path(__file__).resolve().parent
_REPO_ROOT = _INFRA_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# Remove the infra/ entry if CDK (or Python's launcher) inserted it so
# a bare ``import constructs`` can only resolve to the pip package.
sys.path[:] = [p for p in sys.path if pathlib.Path(p).resolve() != _INFRA_DIR]

import aws_cdk as cdk  # noqa: E402 — path setup above must run first

from infra.stacks import (  # noqa: E402
    AgentStack,
    DataStack,
    EvaluatorStack,
    GatewayStack,
    NetworkStack,
)


def _resolve_env(app: cdk.App) -> cdk.Environment:
    """Build a :class:`cdk.Environment` from context plus CDK env vars.

    Context values in ``cdk.json`` (``mnaRegion`` / ``mnaAccount``) take
    precedence over the CDK defaults so readers can pin a target without
    touching their AWS profile. Both fall back to ``None`` when unset,
    which yields an environment-agnostic stack that CDK can still synth.
    """

    region = app.node.try_get_context("mnaRegion") or os.environ.get("CDK_DEFAULT_REGION")
    account = app.node.try_get_context("mnaAccount") or os.environ.get("CDK_DEFAULT_ACCOUNT")
    return cdk.Environment(account=account, region=region)


def build_app() -> cdk.App:
    """Instantiate and wire the CDK application.

    Extracted from the module-level ``__main__`` block so tests and
    importers can exercise stack construction without calling
    :meth:`cdk.App.synth`.
    """

    app = cdk.App()
    env = _resolve_env(app)

    # Stage prefix keeps stack names unique when a reader deploys the
    # sample more than once (e.g. dev + scratch) into a single account.
    stage = app.node.try_get_context("mnaStage") or "Mna"

    network = NetworkStack(app, f"{stage}NetworkStack", env=env)

    data = DataStack(app, f"{stage}DataStack", env=env, network_stack=network)
    data.add_dependency(network)

    evaluator = EvaluatorStack(app, f"{stage}EvaluatorStack", env=env)

    gateway = GatewayStack(app, f"{stage}GatewayStack", env=env)

    agent = AgentStack(
        app,
        f"{stage}AgentStack",
        env=env,
        data_stack=data,
        evaluator_stack=evaluator,
        gateway_stack=gateway,
    )
    agent.add_dependency(data)
    agent.add_dependency(evaluator)
    agent.add_dependency(gateway)

    return app


app = build_app()


if __name__ == "__main__":
    app.synth()

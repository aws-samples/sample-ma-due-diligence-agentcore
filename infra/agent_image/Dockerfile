# Agent runtime container image for the M&A Due Diligence sample.
#
# This Dockerfile is built by AWS CodeBuild on managed ARM64 Linux
# (``aws/codebuild/amazonlinux2-aarch64-standard:3.0``) as part of the
# AgentStack build pipeline (task 11 in
# ``.kiro/specs/ma-due-diligence-agentcore/tasks.md``). It is never
# built on the reader's machine -- Requirement NFR-RT-4 forbids that.
#
# The image is intentionally minimal:
#
# 1. Base on ``public.ecr.aws/docker/library/python:3.11-slim`` so the
#    pull happens against AWS's public ECR mirror, avoiding Docker Hub
#    anonymous rate limits inside CodeBuild.
# 2. Install only the agent runtime packages listed in
#    ``infra/agent_image/requirements.txt`` (boto3/botocore, strands,
#    bedrock-agentcore). The top-level ``requirements.txt`` at the repo
#    root pulls in dev tooling and infra packages (aws-cdk-lib,
#    reportlab, psycopg, ruff, pytest) that the agent container has no
#    business carrying; installing that list here balloons the image
#    to several hundred MB and slows cold starts.
# 3. Copy the ``src/mna/`` package under ``/app/mna`` so
#    ``python -m mna.agents.supervisor`` resolves without a
#    ``PYTHONPATH`` hack.
#
# Build context: the CodeBuild project uploads a zipped asset that
# contains ``infra/agent_image/``, ``src/mna/``, and this Dockerfile.
# The COPY paths below are relative to that asset layout.
FROM public.ecr.aws/docker/library/python:3.11-slim

# Deterministic, cache-friendly layers: dependencies first, then code.
WORKDIR /app

# Copy the agent-specific requirements first so a code-only change
# does not invalidate the pip install layer.
COPY infra/agent_image/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent source tree. The asset places ``src/mna/`` at the
# archive root as ``mna/`` so the container image ends up with
# ``/app/mna/<modules>`` and ``python -m mna.agents.supervisor`` works.
COPY src/mna/ ./mna/

# Copy the target-company schema DDL so the text-to-SQL tool can
# inject it into the LLM's system prompt. Without this file the tool
# falls back to ``(schema unavailable)`` and the LLM generates SQL
# without the ``mna.`` schema prefix, causing "relation does not
# exist" errors at query time.
COPY data/schemas/ ./data/schemas/

ENV PYTHONUNBUFFERED=1

# AgentCore Runtime invokes the supervisor via the bedrock-agentcore
# HTTP server. The ``opentelemetry-instrument`` wrapper is required so
# the container's stdout/stderr (including Python tracebacks) reaches
# CloudWatch through AgentCore's built-in OTEL observability pipeline.
# Without it, the container runs but logs nothing visible — every
# handler failure surfaces only as a 500 on the client side.
CMD ["opentelemetry-instrument", "python", "-m", "mna.agents.supervisor"]

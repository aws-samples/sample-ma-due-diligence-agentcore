# M&A Due Diligence Multi-Agent Sample — Design Document

## Overview

This document describes the technical design for the M&A Due Diligence Multi-Agent sample, implementing the requirements defined in `requirements.md`. The sample is a self-contained, deployable AWS solution that demonstrates a supervisor-plus-specialists agent pattern on Amazon Bedrock AgentCore, grounded in synthetic transportation and logistics M&A data.

The design prioritizes three qualities in this order:

1. **Experience** — one command to deploy, one command to tear down, notebook as the primary surface.
2. **Faithfulness to the blog's architecture diagram** — the layers in Figure 1 map to actual resources in the stack.
3. **Production-adjacency** — least-privilege IAM, private networking for Aurora, observability on every path, but not over-engineered.

The design assumes Python 3.11+, AWS CDK v2 (Python), and deployment to a single commercial AWS region where Amazon Bedrock AgentCore is generally available.

## Architecture

### High-Level Component View

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Reader Surfaces                                │
│   ┌──────────────────────┐          ┌──────────────────────────┐        │
│   │ walkthrough.ipynb    │          │ CLI (python -m mna ...)  │        │
│   └──────────┬───────────┘          └─────────────┬────────────┘        │
│              └──────────────┬────────────────────┘                      │
│                             ▼                                           │
│                ┌────────────────────────┐                               │
│                │   mna.client (shared)  │                               │
│                └───────────┬────────────┘                               │
└────────────────────────────┼────────────────────────────────────────────┘
                             │ boto3 InvokeAgentRuntime
                             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                    Amazon Bedrock AgentCore Runtime                     │
│   ┌───────────────────────────────────────────────────────────────┐     │
│   │                    Supervisor Agent (Strands)                 │     │
│   │   Guardrails │ Memory (session) │ Trace (X-Ray) │ CloudWatch  │     │
│   └────┬──────────────┬──────────────┬──────────────┬─────────────┘     │
│        │              │              │              │                   │
│        ▼              ▼              ▼              ▼                   │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐              │
│  │  Target  │   │ Financial│   │ Strategic│   │Compliance│              │
│  │Screening │   │ Analysis │   │   Fit    │   │Validation│              │
│  └────┬─────┘   └────┬─────┘   └────┬─────┘   └────┬─────┘              │
└───────┼──────────────┼──────────────┼──────────────┼────────────────────┘
        │              │              │              │
        │ SQL tool     │ KB retrieve  │ Memory read  │ Citation check
        ▼              ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
│ Aurora PG    │ │ Bedrock KB   │ │ AgentCore    │ │ Custom Evaluator │
│ Serverless v2│ │ (S3 backed)  │ │ Memory       │ │ (Lambda)         │
│ (private VPC)│ │              │ │              │ │                  │
└──────────────┘ └──────┬───────┘ └──────────────┘ └──────────────────┘
                        │
                        ▼
                ┌───────────────┐
                │   S3 Bucket   │
                │  (documents)  │
                └───────────────┘

Cross-cutting (one tool routed via AgentCore Gateway):
   Financial Analysis ──▶ AgentCore Gateway ──▶ Lambda (market data mock)
```

### Mapping to Architecture Diagram Layers

| Diagram Layer | v1 Implementation | Status |
|---|---|---|
| Presentation | Notebook + CLI (no React/Cognito/CloudFront) | Intentional substitution |
| Agent Orchestration: Runtime | AgentCore Runtime hosting Strands supervisor + 4 specialists | Implemented |
| Agent Orchestration: Strands SDK | Python Strands `Agent` per role, supervisor uses agents-as-tools | Implemented |
| Agent Orchestration: Memory | AgentCore Memory with session + long-term prior-deal memos | Implemented |
| Agent Orchestration: Guardrails | Amazon Bedrock Guardrail attached to supervisor | Implemented |
| Agent Orchestration: Identity | Not deployed (no JWT flow without frontend) | Extension |
| Agent Orchestration: Gateway | Single Lambda-backed tool via Gateway | Implemented (minimal) |
| Agent Orchestration: Observability | CloudWatch + X-Ray on all invocations | Implemented |
| Data: DynamoDB | Session/cache table | Implemented |
| Data: Aurora PostgreSQL | Serverless v2, target-company schema, text-to-SQL | Implemented |
| Data: Bedrock KB | Unstructured RAG over synthetic CIMs, memos, filings | Implemented |
| Data: S3 | Source documents + generated data | Implemented |
| Gateway Targets: Lambda | One market-data mock Lambda | Implemented |
| Gateway Targets: External APIs, MCP servers, API Gateway | Not deployed | Extension |
| Observability: CloudWatch/X-Ray | Implemented | Implemented |
| Evaluation: AgentCore Evaluations | Not deployed | Extension |
| Evaluation: Custom Evaluator | One Lambda checking citation presence | Implemented |
| Evaluation: Continuous Monitoring | Not deployed | Extension |
| Security: IAM, S3 block public, AWS-managed KMS | Implemented | Implemented |
| Security: customer managed keys, PrivateLink, Config, CloudTrail setup | Not deployed (CloudTrail assumed at account level) | Extension |

### Request Flow for a Typical Prompt

1. Reader executes a notebook cell or CLI command with a prompt string.
2. `mna.client.invoke_agent()` calls `bedrock-agentcore:InvokeAgentRuntime` with the prompt and a session ID.
3. The supervisor agent receives the prompt; the Guardrail applies input filtering.
4. The supervisor uses its LLM to decide which specialist(s) to call and invokes them as tools.
5. Each specialist performs its work:
   - Target Screening calls the SQL tool (translates NL → SQL → Aurora query).
   - Financial Analysis calls the KB retrieve tool and the Gateway-backed market-data Lambda.
   - Strategic Fit calls the KB retrieve tool and reads prior-deal memos from AgentCore Memory.
   - Compliance Validation calls the KB retrieve tool and the citation-check evaluator.
6. The supervisor assembles the final response with citations and returns it.
7. Trace data is written to CloudWatch Logs and X-Ray throughout.

## Repository Layout

```
sample-ma-due-diligence-agentcore/
├── README.md
├── LICENSE                       # MIT-0
├── CONTRIBUTING.md
├── .gitignore
├── pyproject.toml                # Project config, ruff, pytest
├── requirements.txt              # Top-level dev deps (cdk, ruff, pytest)
├── deploy.sh / deploy.ps1        # One-command deploy
├── cleanup.sh / cleanup.ps1      # One-command cleanup
│
├── src/mna/                      # Core package (notebook + CLI share this)
│   ├── __init__.py
│   ├── client.py                 # invoke_agent(name, prompt, session_id)
│   ├── config.py                 # Resource ARN/name resolution from SSM
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── supervisor.py
│   │   ├── target_screening.py
│   │   ├── financial_analysis.py
│   │   ├── strategic_fit.py
│   │   └── compliance_validation.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── kb_retrieve.py        # Bedrock KB retrieval with citation shape
│   │   ├── text_to_sql.py        # NL → SQL → Aurora via RDS Data API
│   │   ├── market_data.py        # Calls Gateway-backed Lambda
│   │   └── memory.py             # AgentCore Memory wrappers
│   └── evaluators/
│       ├── __init__.py
│       └── citation_check.py     # Local helper; Lambda mirrors this
│
├── cli/
│   └── invoke.py                 # argparse wrapper over mna.client
│
├── notebooks/
│   └── walkthrough.ipynb
│
├── data/
│   ├── generate.py               # Synthetic data generator
│   ├── schemas/
│   │   └── target_companies.sql  # DDL for Aurora
│   └── templates/                # Document templates used by generate.py
│
├── infra/
│   ├── app.py                    # CDK entrypoint
│   ├── cdk.json
│   ├── stacks/
│   │   ├── network_stack.py      # VPC + subnets for Aurora
│   │   ├── data_stack.py         # Aurora, DynamoDB, S3, KB
│   │   ├── agent_stack.py        # AgentCore Runtime + Memory + Guardrail
│   │   ├── gateway_stack.py      # Gateway + market-data Lambda
│   │   └── evaluator_stack.py    # Citation-check Lambda
│   └── constructs/               # Reusable CDK constructs
│
├── lambda/
│   ├── market_data/              # Gateway-backed mock tool
│   │   └── handler.py
│   └── citation_check/           # Evaluator Lambda
│       └── handler.py
│
├── prompts.md                    # Four example prompts, copy-paste
│
└── tests/
    ├── smoke_test.py
    └── unit/
        └── test_citation_check.py
```

Top-level directory count: 9 (under the 10-directory limit in the requirements).

## Components and Interfaces

### Shared Python Package: `mna`

A single entry point for agent invocation keeps the notebook, CLI, and smoke test aligned.

**`mna.client.invoke_agent(agent_name, prompt, session_id=None) -> AgentResponse`**

- Resolves the AgentCore Runtime ARN from an SSM parameter (`/mna/runtime/arn`).
- Calls `bedrock-agentcore` `InvokeAgentRuntime` with a qualifier indicating the requested specialist (or the supervisor if none specified).
- Returns a typed `AgentResponse` containing `text`, `citations`, `trace_id`, `session_id`, and `raw`.

**`mna.client.list_agents() -> list[str]`**

- Returns the static list of agent names: `["supervisor", "target_screening", "financial_analysis", "strategic_fit", "compliance_validation"]`.

**`mna.client.get_last_trace(trace_id) -> dict`**

- Fetches the X-Ray trace for inspection in the notebook.

### Agent Implementations

All agents use the Strands `Agent` class. Each module exports an `agent` instance and a `handler(event, context)` callable suitable for AgentCore Runtime.

**Supervisor (`agents/supervisor.py`)**

```python
import os

from strands import Agent
from strands.models import BedrockModel
from strands_tools import use_agent  # agents-as-tools pattern

from mna.agents import target_screening, financial_analysis, strategic_fit, compliance_validation
from mna.agents._base import load_prompt

MODEL_ID = os.getenv("MNA_SUPERVISOR_MODEL", "anthropic.claude-sonnet-4-5-v1:0")

supervisor = Agent(
    model=BedrockModel(model_id=MODEL_ID, guardrail_id=os.getenv("MNA_GUARDRAIL_ID")),
    system_prompt=load_prompt("supervisor.txt"),
    tools=[
        use_agent(target_screening.agent, name="target_screening"),
        use_agent(financial_analysis.agent, name="financial_analysis"),
        use_agent(strategic_fit.agent, name="strategic_fit"),
        use_agent(compliance_validation.agent, name="compliance_validation"),
    ],
)
```

**Target Screening (`agents/target_screening.py`)**

Tools: `text_to_sql` (primary), `kb_retrieve` (for narrative context on surfaced targets).

System prompt instructs the agent to (a) translate user criteria into SQL against the `target_companies` schema, (b) run and format results, (c) optionally enrich the top results with KB snippets.

**Financial Analysis (`agents/financial_analysis.py`)**

Tools: `kb_retrieve` (CIMs, financial statements), `market_data` (external Gateway tool).

System prompt instructs the agent to perform DCF or comparable-company analysis, cite every numeric input to its source document, and flag assumptions that diverge materially from historical performance.

**Strategic Fit (`agents/strategic_fit.py`)**

Tools: `kb_retrieve`, `memory.recall_prior_deals`.

System prompt instructs the agent to compare the current target against prior deals stored in AgentCore Memory long-term context, and return integration risks with citations.

**Compliance Validation (`agents/compliance_validation.py`)**

Tools: `kb_retrieve` (governance checklist), `citation_check` (invoke evaluator Lambda).

System prompt instructs the agent to verify the response pipeline's outputs against the M&A governance checklist and flag any claim lacking a citation.

### Tools

**`tools/kb_retrieve.py`**

Calls `bedrock-agent-runtime:Retrieve` against the KB. Returns a structured response:

```python
{
  "passages": [
    {"text": "...", "source": "s3://bucket/doc.pdf", "page": 3, "score": 0.87},
    ...
  ]
}
```

**`tools/text_to_sql.py`**

Two-step tool:

1. Generate SQL from natural language using a small Amazon Bedrock model with a system prompt that includes the schema (loaded from `data/schemas/target_companies.sql`).
2. Run via RDS Data API (no persistent DB connection from agent runtime).

Safety:
- Agent is constrained to `SELECT` statements only (system prompt + SQL parse check).
- Queries run under a read-only IAM role scoped to the `target_companies` schema.
- Generated SQL is echoed into the response and trace for auditability.

**`tools/market_data.py`**

Invokes the AgentCore Gateway with the `market_data.get_comparable_multiples` tool name. The Gateway routes to the Lambda in `lambda/market_data/handler.py`, which returns deterministic synthetic comparables based on the provided industry code.

**`tools/memory.py`**

Thin wrappers over `bedrock-agentcore:RetrieveMemoryRecords` and `CreateMemoryRecord`. Namespace `prior_deals` for long-term memos, `session_<id>` for turn-level context.

### Evaluator: Citation Check

Implemented twice (local for fast testing, Lambda for integration):

**Local (`evaluators/citation_check.py`)**

```python
from mna.types import Citation, EvaluationResult
from mna.evaluators.citation_check import extract_claims


def check_citations(response_text: str, citations: list[Citation]) -> EvaluationResult:
    claims = extract_claims(response_text)  # sentence-level split + numeric detection
    unsupported = [c for c in claims if not any(cite.supports(c) for cite in citations)]
    return EvaluationResult(
        passed=not unsupported,
        unsupported_claims=unsupported,
        total_claims=len(claims),
    )
```

**Lambda (`lambda/citation_check/handler.py`)**

Same logic, packaged for invocation by the Compliance Validation agent via a tool. Result shape matches the local version so downstream code is identical.

### CLI

```
python -m mna invoke <agent_name> "<prompt>"        # Invoke an agent
python -m mna list-agents                           # List agents
python -m mna trace <trace_id>                      # Print last trace
python -m mna evaluate <response_id>                # Run citation check
```

Implemented with argparse in `cli/invoke.py`. No new logic — pure delegation to `mna.client` and `mna.evaluators`.

## Data Model

### Aurora PostgreSQL Schema

```sql
CREATE SCHEMA mna;

CREATE TABLE mna.target_companies (
    company_id          TEXT PRIMARY KEY,
    legal_name          TEXT NOT NULL,
    headquarters_region TEXT,
    revenue_usd         NUMERIC(14, 2),
    ebitda_margin_pct   NUMERIC(5, 2),
    fleet_size          INTEGER,
    employee_count      INTEGER,
    customer_concentration_top1_pct NUMERIC(5, 2),
    service_lines       TEXT[],
    last_updated        TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_tc_revenue ON mna.target_companies (revenue_usd);
CREATE INDEX idx_tc_ebitda ON mna.target_companies (ebitda_margin_pct);
```

Seed volume: 20+ rows (exceeds requirement minimum).

### DynamoDB: Session and Response Cache

Table: `mna-sessions`

| Attribute | Type | Role |
|---|---|---|
| `session_id` (PK) | S | Reader's invocation session |
| `turn_id` (SK) | S | ISO timestamp of the turn |
| `prompt` | S | User prompt |
| `response` | S | Agent response text |
| `citations` | L | List of citation objects |
| `trace_id` | S | X-Ray trace ID |
| `evaluation` | M | Latest citation-check result |

On-demand billing. TTL of 7 days on items to keep sample costs bounded.

### S3 Layout

```
s3://mna-docs-<account>-<region>/
├── cims/
│   ├── acme_logistics.pdf
│   ├── bluewave_freight.pdf
│   └── cascade_transport.pdf
├── financials/
│   └── <company>_statements.pdf
├── press/
│   └── <company>_press_pack.pdf
├── memos/
│   └── <prior-deal>_memo.md
└── governance/
    └── ma_checklist.md
```

All files flagged as synthetic in frontmatter.

### Knowledge Bases for Amazon Bedrock

- Data source: the S3 bucket above.
- Chunking: default hierarchical chunking (works well for long documents).
- Embedding model: `amazon.titan-embed-text-v2:0`.
- Vector store: Aurora PostgreSQL (pgvector) — reuses the same Aurora cluster to keep component count low.

### AgentCore Memory Namespaces

- `session_<session_id>` — short-term, auto-expires.
- `prior_deals` — long-term, seeded by `data/generate.py` with 3–5 synthetic memo summaries. Strategic Fit agent reads from this namespace.

## Infrastructure as Code Design

CDK v2 Python stacks, deployed in dependency order:

### 1. `NetworkStack`
- VPC with 2 AZs.
- 2 private isolated subnets (Aurora).
- 2 private subnets with NAT (Lambda reaching AWS APIs if needed).
- VPC endpoints for `secretsmanager`, `rds-data`, `s3`.

### 2. `DataStack`
- Aurora Serverless v2 cluster (min 0.5 ACU, max 2 ACU, PostgreSQL 15+).
- Aurora credentials in Secrets Manager.
- DynamoDB `mna-sessions` table.
- S3 bucket for documents (block public, SSE-S3, versioned).
- Knowledge Bases for Amazon Bedrock pointing at the S3 bucket and Aurora pgvector.
- SSM parameters for ARNs: `/mna/aurora/cluster_arn`, `/mna/kb/id`, `/mna/docs/bucket`.

### 3. `EvaluatorStack`
- Citation-check Lambda (Python 3.11, 512 MB, 30 s timeout).
- Log group with 7-day retention.
- SSM parameter `/mna/evaluator/arn`.

### 4. `GatewayStack`
- Market-data Lambda.
- AgentCore Gateway with one MCP target pointing at the Lambda.
- SSM parameter `/mna/gateway/arn`.

### 5. `AgentStack`
- Amazon Bedrock Guardrail (harmful content filters, financial-advice denial topic).
- AgentCore Memory resource (session + `prior_deals` namespace).
- Build pipeline for the agent container (see "Container Build Pipeline" below).
- AgentCore Runtime pointing at the ECR image produced by the build pipeline.
- IAM role with permissions to:
  - Invoke Amazon Bedrock models.
  - Read from the KB (`bedrock-agent-runtime:Retrieve`).
  - Run statements via RDS Data API against the Aurora cluster (read-only policy on `mna.target_companies`).
  - Read/write DynamoDB `mna-sessions`.
  - Read/write AgentCore Memory.
  - Invoke the Gateway and the evaluator Lambda.
- SSM parameter `/mna/runtime/arn`.

### Container Build Pipeline (No Local Docker Required)

AgentCore Runtime requires an ARM64 Linux container image in ECR. To honor the "no Docker on your machine" requirement (NFR-RT-4), all container builds happen in AWS CodeBuild. This mirrors the pattern used by the FAST template.

Components (created inside `AgentStack` or a small `BuildStack` that `AgentStack` depends on):

- **ECR repository** — stores the agent image, lifecycle policy keeps the last 3 images.
- **S3 source bucket** — CDK uploads `src/mna/`, `requirements.txt`, and `Dockerfile` as a zipped asset each deploy.
- **CodeBuild project** — managed ARM64 build environment (`aws/codebuild/amazonlinux2-aarch64-standard`), runs `docker build` inside AWS, tags as `latest` plus a content hash, pushes to ECR.
- **Build trigger Custom Resource** — CDK Custom Resource that starts the CodeBuild build on every deploy when the source hash changes.
- **Build waiter Lambda** — Custom Resource that polls CodeBuild every 30 seconds (15-minute timeout) and returns a compact success/failure response to CloudFormation. Required because CodeBuild's full `BatchGetBuilds` response can exceed the 4 KB Custom Resource limit.
- **Dockerfile** (shipped in `infra/agent_image/Dockerfile`):

  ```dockerfile
  FROM public.ecr.aws/docker/library/python:3.11-slim
  WORKDIR /app
  COPY requirements.txt ./
  RUN pip install --no-cache-dir -r requirements.txt
  COPY src/mna/ ./mna/
  ENV PYTHONUNBUFFERED=1
  CMD ["python", "-m", "mna.agents.supervisor"]
  ```

Flow on each `deploy.ps1` / `deploy.sh` run:

1. CDK packages the agent source as an asset and uploads to S3.
2. CDK starts CodeBuild via the Custom Resource if the source hash changed.
3. CodeBuild pulls from S3, builds ARM64 image in AWS, pushes to ECR.
4. Waiter Lambda polls until build succeeds.
5. CDK creates or updates the AgentCore Runtime with the new image URI.

You never run `docker build`. Your local machine only needs AWS CLI, Node.js, and Python.

### Custom Resources Inventory

CloudFormation Custom Resources (CRs) are used wherever CloudFormation does not yet ship a native resource type for an operation we need, or where an asynchronous/imperative action must complete during stack creation. This design requires 6–8 CRs depending on CloudFormation and CDK support for Amazon Bedrock AgentCore at implementation time.

| # | Custom Resource | Stack | Wraps | Always required? |
|---|---|---|---|---|
| 1 | Container build trigger | AgentStack | `codebuild:StartBuild` on source hash change | Yes |
| 2 | Container build waiter | AgentStack | Poll `codebuild:BatchGetBuilds` until done (14-min cap) | Yes |
| 3 | AgentCore Runtime manager | AgentStack | `bedrock-agentcore:CreateAgentRuntime` / Update / Delete | Only if no native resource ships |
| 4 | AgentCore Memory manager | AgentStack | `bedrock-agentcore:CreateMemory` / Delete + namespace seeding | Only if no native resource ships |
| 5 | AgentCore Gateway + target manager | GatewayStack | `CreateGateway` + `CreateGatewayTarget` lifecycle | Only if no native resource ships |
| 6 | Aurora schema bootstrap | DataStack | `CREATE EXTENSION vector;` + DDL + read-only DB role via RDS Data API | Yes |
| 7 | KB ingestion trigger/waiter | DataStack | `bedrock-agent:StartIngestionJob` + poll | Optional (alternative: run from `generate.py`) |
| 8 | Region preflight | (root) | Verifies AgentCore availability in deploy region, fails stack early | Optional, recommended |

Before implementation, task 4 validates which of CRs 3, 4, and 5 are needed by attempting to import the native `AWS::BedrockAgentCore::*` resource types from `cdk-lib`. If a native resource is available, the CR is skipped.

### Custom Resource Safety Requirements

Every CR Lambda in this project must follow these rules. They prevent the two failure modes that cause the worst reader experience: a CR that cannot start because initialization fails, and a CR that never responds to CloudFormation, leaving the stack stuck for up to 3 hours before CloudFormation times out.

**1. Cold-start safety.** The Lambda module top-level code must never raise. No `boto3` clients, no SDK imports outside the standard library, no environment variable lookups at import time. All initialization happens lazily inside `handler()` and is wrapped in the guaranteed-response block below.

```python
# lambda/<cr_name>/handler.py
import json
import logging
import urllib.request
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# No boto3 import here. No client construction here. No env lookups here.

def handler(event: dict, context: Any) -> None:
    response_data: dict = {}
    physical_id = event.get("PhysicalResourceId") or "init"
    status = "SUCCESS"
    reason = "OK"
    try:
        import boto3  # Lazy import so ImportError still reaches the finally block.
        request_type = event["RequestType"]
        if request_type == "Create":
            physical_id, response_data = _on_create(event, boto3)
        elif request_type == "Update":
            physical_id, response_data = _on_update(event, boto3)
        elif request_type == "Delete":
            _on_delete(event, boto3)
        else:
            raise ValueError(f"Unknown RequestType: {request_type}")
    except Exception as exc:
        logger.exception("Custom resource failure")
        status = "FAILED"
        reason = f"{type(exc).__name__}: {str(exc)[:240]}"
    finally:
        _send_response(event, context, status, physical_id, response_data, reason)
```

**2. Designed to provide reliable response to CloudFormation.** A `try / except / finally` block must wrap the entire handler body. The `finally` clause sends a response using raw `urllib.request` so a Lambda import error or runtime exception still produces a response. The helper:

```python
def _send_response(event, context, status, physical_id, data, reason):
    body = json.dumps({
        "Status": status,
        "Reason": f"{reason} | Logs: {context.log_stream_name}",
        "PhysicalResourceId": physical_id,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": False,
        "Data": data,
    }).encode("utf-8")
    req = urllib.request.Request(
        event["ResponseURL"], data=body, method="PUT",
        headers={"content-type": "", "content-length": str(len(body))},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        logger.exception("Failed to send CFN response; stack may hang")
```

**3. Response size safety.** The `Data` dict must remain under 4 KB. Large strings (CodeBuild build outputs, SQL result sets, long ARN lists) must be truncated or replaced with a CloudWatch log reference.

**4. Delete idempotency.** Every `_on_delete` must succeed when the target resource does not exist. Catch "not found" exceptions and treat as success. Prevents stack-delete loops when a resource was already manually removed.

**5. Polling with hard caps.** Any CR that waits on asynchronous work (build waiter, ingestion waiter) must cap total wait time at 14 minutes to stay below the 15-minute Lambda timeout, respond to CloudFormation with a clear `FAILED` status on timeout, and log the failure reason with next-steps guidance.

**6. Physical ID stability.** The physical ID returned on `Update` must match the `Create` physical ID when the underlying resource is the same. A changed physical ID triggers a delete-old + create-new cycle that can orphan resources if the old physical ID no longer exists.

**7. Defensive logging.** Every handler logs `RequestType`, `PhysicalResourceId`, and request parameters (with secrets redacted) on entry. Every failure path logs the exception with `logger.exception()` before the finally block fires.

**8. Shared base module.** All CR handlers import from `lambda/_cr_common/send_response.py` which provides `_send_response` and a `cr_handler` decorator that implements rules 1, 2, and 7 consistently. No handler reimplements response-sending logic.

Enforcement:

- Unit tests for each CR verify that a simulated import error, a runtime exception, and a successful invocation all produce a well-formed response body.
- A custom ruff rule (or a simple grep check in CI) fails if a CR handler module imports boto3 at the top level.
- `CONTRIBUTING.md` includes a CR review checklist referencing this section.

### Deployment Script

`deploy.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

# Preflight
./scripts/check_region.sh           # Fail fast if not an AgentCore region
./scripts/check_bedrock_access.sh   # Verify model access enabled

# Deploy infra in order
cd infra
cdk bootstrap
cdk deploy NetworkStack DataStack EvaluatorStack GatewayStack AgentStack --require-approval never

# Seed synthetic data
cd ..
python data/generate.py --seed-all

echo "Deployment complete. Open notebooks/walkthrough.ipynb to begin."
```

### Cleanup Script

`cleanup.sh` reverses the order with `cdk destroy --all --force`, then runs `scripts/verify_cleanup.sh` which checks for orphaned S3 objects, Knowledge Base data sources, and AgentCore resources.

## Synthetic Data Generation

`data/generate.py` is a single-file script with subcommands:

```
python data/generate.py companies           # Seeds Aurora target_companies
python data/generate.py documents           # Generates PDFs/MDs, uploads to S3
python data/generate.py memory              # Populates AgentCore Memory
python data/generate.py --seed-all          # All of the above
```

### Company Generation

Uses a deterministic seed + faker-style name list to produce 20+ fictional transportation/logistics companies. Numeric fields sampled from plausible industry ranges. Inserted via RDS Data API.

### Document Generation

For 3 "spotlight" companies, generates:
- CIM (Confidential Information Memorandum) — 5–10 pages of templated sections: executive summary, company overview, financial summary, growth strategy, risks, management team.
- Financial statements summary — tabular income statement, balance sheet, cash flow.
- Press release pack — 3–5 templated announcements.

Text generation uses Claude via Bedrock with strict templates. Every document includes a `SYNTHETIC DATA - NOT REAL` header.

PDFs rendered via ReportLab. Markdown documents written directly.

### Memory Seeding

3 prior-deal memos, each ~500 words, summarizing hypothetical past transactions with outcomes (successful integration, synergy realization, lessons learned). Inserted into AgentCore Memory `prior_deals` namespace.

## Example Prompts

These four prompts drive the notebook and `prompts.md`:

| Agent | Prompt | What it demonstrates |
|---|---|---|
| Target Screening | "Screen our target pipeline for transportation companies with revenue between $100M and $500M, EBITDA margin above 12%, and no single-customer concentration above 25%. Rank the top 5." | Text-to-SQL on Aurora |
| Financial Analysis | "Run a DCF on Example Corp using the CIM in the knowledge base. Flag any management projection that diverges from historical performance by more than 20%, and pull comparable multiples for transportation-logistics mid-market." | KB retrieval + Gateway tool + citations |
| Strategic Fit | "Compare Example Corp' integration profile against our three most recent completed acquisitions. Identify the top three integration risks and cite the source memos." | Memory long-term retrieval |
| Compliance Validation | "Review the Example Corp analysis in this session for completeness against our M&A governance checklist. List any claims without source citations." | Evaluator invocation, audit trail |

## Platform Support

The sample is designed to run on Windows 10/11, macOS, and Linux. No Docker, WSL, or container runtime is required on your machine.

### Reader Machine Prerequisites

| Tool | Version | Windows install | macOS/Linux install |
|---|---|---|---|
| AWS CLI | v2.15+ | MSI installer | `brew install awscli` / package manager |
| Python | 3.11+ | python.org installer | `brew install python@3.11` / pyenv |
| Node.js | 20+ | nodejs.org installer | `brew install node` / nvm |
| AWS CDK | v2 | `npm install -g aws-cdk` | `npm install -g aws-cdk` |
| PowerShell or Bash | Any recent | Built-in (PowerShell 5.1+) | Built-in |

Not required: Docker, WSL, buildx, QEMU, Git Bash (PowerShell scripts are native).

### Cross-Platform Implementation Notes

- **Paths** — CDK Python code and `generate.py` use `pathlib.Path`; no hardcoded forward slashes.
- **Scripts** — both `deploy.ps1`/`cleanup.ps1` (Windows) and `deploy.sh`/`cleanup.sh` (macOS/Linux) are shipped. Feature parity is enforced by a CI matrix covering both.
- **Line endings** — repo `.gitattributes` forces LF for `.sh` and `.py`, CRLF for `.ps1`, preventing execution failures on Windows clones.
- **Virtual environment** — deploy scripts create a `.venv` via `python -m venv`, activate it with the platform-correct command, and install dependencies there. Prevents polluting your global Python.
- **CodeBuild handles the container** — see "Container Build Pipeline" above. Your platform architecture (x86 vs ARM) is irrelevant; the build targets ARM64 inside AWS.
- **PDF generation** — ReportLab is pure Python and ships wheels for all three platforms.
- **psycopg2 or psycopg** — the agent runtime container uses `psycopg[binary]`; the data-generator script uses the RDS Data API (no driver needed on your machine).

### Windows-Specific Acceptance Test

A first-time Windows 11 reader with only AWS CLI v2, Python 3.11, Node.js 20, and PowerShell installed must be able to run `.\deploy.ps1`, execute all four example prompts, and run `.\cleanup.ps1` to completion without installing any other tool.

## Error Handling

### Deployment-time Errors

- **Unsupported region** — `check_region.sh` queries the AgentCore regions list (hardcoded + documented) and exits with a clear link.
- **Amazon Bedrock model access not enabled** — `check_bedrock_access.sh` performs a cheap `bedrock:ListFoundationModels` + test invoke; surfaces the model access console link on failure.
- **Aurora provisioning timeout** — CDK waits up to 20 minutes; if exceeded, surface CloudFormation event stream.
- **KB ingestion failure** — `generate.py` polls ingestion status and prints failed document IDs.
- **CodeBuild failure** — waiter Lambda reports failure with a link to the CodeBuild build URL and the CloudWatch log group (`/aws/codebuild/mna-agent-builder`). Most common causes: base image pull rate limit (retry), Python dependency resolution failure (surface in logs).
- **CodeBuild timeout (>15 min)** — waiter returns failure; reader re-runs deploy which re-triggers the build.
- **Image not found in ECR after build "success"** — rare, usually a cross-region mismatch. `deploy` script verifies the image exists before creating/updating the AgentCore Runtime.

### Runtime Errors

- **LLM timeouts** — Strands `Agent` configured with retries (max 2) and exponential backoff.
- **SQL generation producing non-SELECT** — `text_to_sql` tool validates parsed SQL and returns a user-facing error asking the agent to rephrase.
- **KB returns zero passages** — agent returns a response explicitly stating "no supporting documents found" rather than hallucinating.
- **Gateway tool failure** — caught by the agent, which degrades gracefully (e.g., Financial Analysis proceeds without comparables and notes the missing data).
- **Citation-check evaluator timeout** — Compliance Validation agent falls back to a local check and logs a warning.

### Observability for Errors

Every tool wraps its core call in try/except with structured logging:

```python
logger.error(
    "tool_invocation_failed",
    extra={"tool": "text_to_sql", "session_id": session_id, "error_type": type(e).__name__},
)
```

Errors propagate as X-Ray trace exceptions so the notebook's trace cell shows them clearly.

## Testing Strategy

### Unit Tests (`tests/unit/`)

- `test_citation_check.py` — exercises the evaluator logic with synthetic inputs.
- `test_text_to_sql_guardrails.py` — verifies non-SELECT statements are rejected.
- `test_memory_namespace_scoping.py` — verifies session and long-term namespaces don't bleed.

Fast, no AWS calls, run in CI on every PR.

### Smoke Test (`tests/smoke_test.py`)

Run against a deployed stack. Invokes each agent once with its example prompt, asserts a non-empty response with at least one citation. Used as the CI gate for main-branch deployments and as the final verification step in the deployment script.

### Manual Verification Checklist

Documented in the README:
1. Deploy completes without CloudFormation errors.
2. `generate.py --seed-all` completes and reports document/row counts.
3. Notebook runs top-to-bottom without errors.
4. Each prompt returns a response with citations.
5. Evaluator flags at least one contrived unsupported claim correctly.
6. X-Ray trace shows supervisor → specialist → tool call hierarchy.
7. Cleanup removes all billable resources (verification script reports empty).

## Security Design

### IAM Summary

| Role | Principal | Key Permissions |
|---|---|---|
| `AgentRuntimeRole` | AgentCore Runtime | Invoke Bedrock, Retrieve KB, RDS Data API (read-only), DynamoDB RW, Memory RW, Invoke Gateway, Invoke evaluator Lambda |
| `EvaluatorLambdaRole` | Lambda | CloudWatch Logs only |
| `MarketDataLambdaRole` | Lambda | CloudWatch Logs only |
| `KnowledgeBaseRole` | Bedrock KB service | Read S3 bucket, write Aurora pgvector |
| `DeploymentRole` | Reader's credentials (not created by sample) | Documented prereq |

Every agent-callable AWS permission is scoped with resource ARNs — no `*` resources except where required (e.g., `bedrock:InvokeModel` for model ARNs).

### Network

- Aurora in private isolated subnets, no public IP.
- Agent runtime connects to Aurora via VPC-routed RDS Data API (no direct TCP).
- Gateway-backed Lambda runs in the VPC to reach private resources cleanly.
- VPC endpoints eliminate NAT egress cost for Secrets Manager, RDS Data, S3.

### Secrets

- Aurora admin credentials stored in Secrets Manager, rotated manually (sample doesn't ship automatic rotation — extension).
- No API keys, no hardcoded account IDs.
- `.env.example` file documents overridable environment variables but ships empty.

### Data Handling

- Synthetic data clearly labeled, no real PII.
- S3 bucket with public access block, SSE-S3, versioning enabled.
- DynamoDB with AWS-managed KMS, TTL enforced.
- CloudWatch log groups with 7-day retention to bound cost.

## Cost Design

Target: under $5 USD for a full deploy-run-cleanup cycle (~1 hour).

| Service | Expected Cost | Notes |
|---|---|---|
| Aurora Serverless v2 | ~$0.12 | 1 hour at 0.5 ACU minimum |
| AgentCore Runtime | ~$0.30 | ~5 minutes of active compute across prompts |
| Bedrock (Claude + Titan Embed) | ~$1.00 | 4 prompts + embedding ingestion |
| Bedrock KB (vector ops) | ~$0.20 | Serverless pricing |
| DynamoDB | <$0.01 | On-demand, minimal writes |
| Lambda (evaluator + market data + waiter) | <$0.01 | Free tier |
| CodeBuild (ARM64) | <$0.05 | ~5 min per deploy, free tier covers first 100 min/month |
| ECR storage | <$0.01 | One ~500 MB image, minimal duration |
| S3 | <$0.01 | Minimal storage |
| CloudWatch Logs | <$0.10 | 7-day retention |
| NAT Gateway (if used) | ~$0.05 | 1 hour, mostly VPC endpoints |
| **Total (estimate)** | **~$1.85** | Leaves headroom under $5 target |

Cost controls:
- Aurora scales to minimum ACU when idle.
- CloudWatch retention capped at 7 days.
- DynamoDB TTL expires session items.
- `cleanup.sh` is the primary mechanism; README strongly emphasizes running it.

## Documentation Plan

- **README.md** — deploy, run, cleanup, cost, troubleshooting, architecture mapping table, extension points.
- **prompts.md** — the four example prompts with expected output characteristics.
- **Per-agent module docstrings** — role, tools, example prompt, known limitations.
- **CONTRIBUTING.md** — how to add a new agent, how to add a new tool, code style, PR process.
- **Companion blog post references** — inline links where you can dive deeper on AgentCore, Strands, Knowledge Bases.

## Open Questions and Future Work

Not blockers for the v1 design, but noted for follow-up:

1. Should the sample use the new AgentCore CLI (`npm install -g @aws/agentcore`) instead of CDK? CLI is the recommended path per the upstream samples repo migration, but CDK gives finer control over Aurora and VPC. **Current choice: CDK** for completeness, with a README note pointing to the CLI as a simpler alternative for agent-only workflows.
2. Should the structured data live in DynamoDB instead of Aurora for the v2 of this sample if cost becomes a concern? Deferred.
3. Should the Financial Analysis agent call a second LLM explicitly for comparables rather than a hard-coded Lambda response? Out of scope for v1 — the Lambda-backed deterministic mock better demonstrates the Gateway pattern.

## Success Criteria (Design-Level)

This design is considered complete when:

- Every functional requirement from `requirements.md` maps to at least one component in this document.
- Every non-functional requirement maps to an implementation mechanism (IAM role, config flag, deployment step, etc.).
- A reviewer can read this document and implement the sample without ambiguity on structure, module boundaries, or data flow.

## Conclusion

This design satisfies every requirement in `requirements.md` while staying within the cost and complexity targets appropriate for an AWS sample. The architecture choices prioritise reader clarity and production-adjacency without over-engineering. The implementation plan (`tasks.md`) maps each design element to an executable task in dependency order.

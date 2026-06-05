# M&A Due Diligence Multi-Agent Sample — Implementation Plan

This plan breaks the M&A Due Diligence Multi-Agent sample into executable tasks that build on each other. Each task references the requirements it satisfies. Tasks are grouped by phase to minimize rework — earlier phases produce artifacts that later phases depend on. Run top to bottom unless a task is explicitly marked independent.

## Phase 1: Repository Foundation

- [x] 1. Initialize the repository structure and tooling
  - Create top-level directories: `src/mna/`, `cli/`, `notebooks/`, `data/`, `infra/`, `lambda/`, `tests/`
  - Add `pyproject.toml` with ruff, pytest, and project metadata
  - Add `requirements.txt` pinning cdk-lib, boto3, strands-agents, bedrock-agentcore, reportlab, psycopg[binary]
  - Add `.gitignore` (Python, Node, CDK, `.venv`, `cdk.out`)
  - Add `.gitattributes` enforcing LF for `.sh`/`.py`, CRLF for `.ps1`
  - Add `LICENSE` (MIT-0) and empty `CONTRIBUTING.md`, `README.md`, `prompts.md`
  - _Requirements: 15.1, 15.4, 17.1, 17.3, NFR-RT-3, NFR-RT-7_

- [x] 2. Scaffold the shared Python package `mna`
  - Create `src/mna/__init__.py` exporting the public API surface
  - Add `src/mna/config.py` for SSM parameter resolution (`/mna/runtime/arn`, `/mna/kb/id`, `/mna/docs/bucket`, `/mna/aurora/cluster_arn`, `/mna/gateway/arn`, `/mna/evaluator/arn`)
  - Add typed data classes: `AgentResponse`, `Citation`, `EvaluationResult`
  - Add package-level logger with structured JSON formatter
  - Write unit tests for config resolution with mocked SSM client
  - _Requirements: 7.4_

- [x] 3. Implement `mna.client` shared invocation layer
  - Add `invoke_agent(agent_name, prompt, session_id=None) -> AgentResponse`
  - Add `list_agents() -> list[str]` with the 5 agent names
  - Add `get_last_trace(trace_id) -> dict` via X-Ray `GetTraceSummaries`
  - Write unit tests mocking `bedrock-agentcore` and X-Ray calls
  - _Requirements: 7.4, 9.3_

## Phase 2: Infrastructure as Code

- [x] 4. Bootstrap the CDK app
  - Create `infra/app.py`, `infra/cdk.json`, and empty `infra/stacks/` package
  - Add environment and context resolution from `cdk.json`
  - Add preflight scripts `scripts/check_region.sh`/`.ps1` and `scripts/check_bedrock_access.sh`/`.ps1`
  - _Requirements: 10.1, 10.2, 10.3, 11.2_

- [x] 5. Implement `NetworkStack`
  - VPC with 2 AZs, 2 private-isolated subnets (Aurora), 2 private subnets with NAT
  - VPC endpoints for Secrets Manager, RDS Data, S3
  - Export subnet IDs and security group IDs for downstream stacks
  - _Requirements: 14.6_

- [x] 6. Implement `DataStack` (part 1 of 2): Aurora + DynamoDB + S3
  - Amazon Aurora Serverless v2 cluster (PostgreSQL 15+, min 0.5 ACU, max 2 ACU) in private subnets
  - Aurora credentials in AWS Secrets Manager; IAM database authentication enabled
  - Read-only IAM policy scoped to the `mna` schema (for the agent runtime role)
  - Amazon DynamoDB `mna-sessions` table, on-demand billing, TTL on `expires_at`
  - Amazon S3 documents bucket with block-public-access, SSE-S3, versioning
  - SSM parameters for downstream discovery
  - _Requirements: 2.5, 2.6, 13.3, 14.3, 14.4, 14.6, 14.7_

- [x] 7. Implement `DataStack` (part 2 of 2): Amazon Bedrock Knowledge Bases with pgvector
  - Amazon Bedrock Knowledge Bases with data source pointing at the S3 bucket
  - Embeddings model: `amazon.titan-embed-text-v2:0`
  - Vector store: reuse the Aurora cluster with pgvector extension
  - KB service role with S3 read + Aurora write permissions
  - Expose KB ID via SSM `/mna/kb/id`
  - _Requirements: 2.1, 2.2_

- [x] 8. Implement `EvaluatorStack`
  - Python 3.11 AWS Lambda function for citation check (512 MB, 30 s timeout — a design choice for this sample, not an AWS limit)
  - Amazon CloudWatch log group with 7-day retention
  - Lambda code imported from `lambda/citation_check/handler.py` (implemented in Phase 3)
  - Expose ARN via SSM `/mna/evaluator/arn`
  - _Requirements: 4.2, 4.3, 4.4_

- [x] 9. Implement `GatewayStack`
  - Market-data Lambda (Python 3.11, deterministic synthetic comparables)
  - AgentCore Gateway with one MCP target mapped to the Lambda
  - Expose Gateway ARN via SSM `/mna/gateway/arn`
  - _Requirements: 3.1, 3.2_

- [x] 10. Implement the shared Custom Resource base module
  - Create `lambda/_cr_common/send_response.py` with `_send_response` helper and `cr_handler` decorator
  - Helper uses raw `urllib.request` (no boto3 dependency)
  - Decorator enforces: lazy boto3 import, try/except/finally, structured entry/exit logging, response size cap at 4 KB
  - Unit tests cover: simulated ImportError, runtime exception, successful Create/Update/Delete, oversized Data payload truncation
  - Add flake8/ruff rule or simple grep check that fails if a CR handler imports boto3 at module level
  - _Requirements: 11a.1, 11a.2, 11a.3, 11a.7, 11a.8, 11a.9_

- [x] 11. Implement the container build pipeline
  - Amazon ECR repository with lifecycle policy (keep 3 images)
  - Amazon S3 source bucket for CodeBuild inputs
  - AWS CodeBuild project on managed ARM64 Linux (`aws/codebuild/amazonlinux2-aarch64-standard`)
  - Dockerfile at `infra/agent_image/Dockerfile` based on `python:3.11-slim`
  - **Build trigger CR** (Lambda handler at `lambda/build_trigger/handler.py`) starts CodeBuild on source hash change, using the shared CR base from task 10
  - **Build waiter CR** (Lambda handler at `lambda/build_waiter/handler.py`) polls every 30 s with a 14-minute cap, returns compact success/failure to CloudFormation
  - Unit tests for both CRs covering success, CodeBuild failure, timeout, and delete-of-missing
  - _Requirements: NFR-RT-4, NFR-RT-5, NFR-RT-6, 11a.1–11a.9_

- [x] 12. Implement the Aurora schema bootstrap Custom Resource
  - Lambda handler at `lambda/aurora_bootstrap/handler.py` using the shared CR base
  - On Create: run `CREATE EXTENSION IF NOT EXISTS vector`, apply `data/schemas/target_companies.sql`, create the read-only IAM-authenticated DB role
  - On Update: apply schema diff (idempotent DDL)
  - On Delete: no-op (Aurora deletion handled by native CDK resource)
  - Executes via RDS Data API (no driver required in Lambda)
  - Unit tests cover ImportError path, successful Create, schema-drift Update, Delete-of-missing
  - _Requirements: 2.5, 2a.5, 14.6, 14.7, 11a.1–11a.9_

- [x] 13. Implement `AgentStack` (with conditional AgentCore CRs)
  - Amazon Bedrock Guardrail (harmful content filters + financial-advice denial topic) via native resource
  - **Native-or-CR probe** at synth time: if `cdk-lib` exposes `AWS::BedrockAgentCore::Runtime` and `::Memory`, use the native resources; otherwise fall back to CRs
  - **AgentCore Memory manager CR** (if needed) at `lambda/agentcore_memory/handler.py`: creates Memory resource and seeds `prior_deals` and `session_*` namespaces; delete is idempotent
  - **AgentCore Runtime manager CR** (if needed) at `lambda/agentcore_runtime/handler.py`: creates runtime pointing at ECR image, supports updates to image URI and IAM role, delete is idempotent; depends on the build waiter CR in task 11
  - Agent runtime IAM role with least-privilege permissions per design (Bedrock, KB, RDS Data API read-only, DynamoDB, Memory, Gateway, evaluator)
  - Expose runtime ARN via SSM `/mna/runtime/arn`
  - Document every IAM statement in `infra/stacks/agent_stack.py` comments
  - Unit tests for any CRs present, cover ImportError, Create, Update with image change, Delete-of-missing
  - _Requirements: 1.4, 2.3, 2.4, 4.1, 14.1, 11a.1–11a.9_

## Phase 3: Lambda Handlers

- [x] 14. Implement the citation-check Lambda handler
  - `lambda/citation_check/handler.py` accepts `{response_text, citations}` and returns `EvaluationResult`
  - Sentence-level claim extraction; numeric-claim heuristic
  - Returns `passed`, `unsupported_claims`, `total_claims`
  - Unit tests at `tests/unit/test_citation_check.py` with pass and fail fixtures
  - _Requirements: 4.2, 4.3, 15.3_

- [x] 15. Implement the market-data Lambda handler
  - `lambda/market_data/handler.py` accepts `{industry_code, deal_size_band}`
  - Returns deterministic synthetic comparable multiples (EV/EBITDA, EV/Revenue)
  - All data clearly labeled synthetic in the response payload
  - Unit tests verifying determinism across repeated calls
  - _Requirements: 3.2, 3.3, 14.5_

- [x] 16. Implement the Gateway + target manager (conditional CR)
  - At synth time probe for native `AWS::BedrockAgentCore::Gateway` support; if absent, implement a CR
  - Lambda handler at `lambda/agentcore_gateway/handler.py` using the shared CR base
  - Create: `CreateGateway` then `CreateGatewayTarget` for the market-data Lambda
  - Delete: delete target then gateway, both idempotent on missing
  - Unit tests cover ImportError, Create, Update, Delete-of-missing
  - _Requirements: 3.1, 3.2, 11a.1–11a.9_

- [x] 17. Implement the KB ingestion trigger/waiter (optional CR)
  - Decision point: implement as CR, or keep in `data/generate.py`. Default: keep in `generate.py` for simplicity.
  - If implemented as CR: `lambda/kb_ingest/handler.py` using shared CR base, polls `bedrock-agent:GetIngestionJob` with 14-minute cap
  - _Requirements: 5.4, 11a.1–11a.9 (if implemented as CR)_

## Phase 4: Agent Tools

- [x] 18. Implement `tools/kb_retrieve.py`
  - Wrap `bedrock-agent-runtime:Retrieve`
  - Return structured passages with `text`, `source`, `page`, `score`
  - Surface KB-empty-result case explicitly
  - Unit tests with mocked Bedrock responses
  - _Requirements: 2.1, 2.2, 1.6_

- [x] 19. Implement `tools/text_to_sql.py`
  - Load schema from `data/schemas/target_companies.sql`
  - Step 1: generate SQL from natural language via a small Amazon Bedrock model with schema-aware system prompt
  - Step 2: validate generated SQL is SELECT-only (sqlparse)
  - Step 3: run via RDS Data API
  - Echo generated SQL into the response and trace
  - Unit tests covering: valid SELECT, rejected INSERT/UPDATE/DELETE, rejected DDL
  - _Requirements: 2a.1, 2a.2, 2a.3, 2a.4, 2a.5_

- [x] 20. Implement `tools/market_data.py`
  - Thin client that invokes the Gateway-backed Lambda via the AgentCore Gateway
  - Return shape matches what the Financial Analysis agent expects
  - _Requirements: 3.3_

- [x] 21. Implement `tools/memory.py`
  - Wrappers around AgentCore Memory `RetrieveMemoryRecords` and `CreateMemoryRecord`
  - Namespace scoping helpers (`session_<id>` vs `prior_deals`)
  - Unit tests verifying namespace isolation
  - _Requirements: 2.3, 2.4_

## Phase 5: Agents

- [x] 22. Implement the Target Screening agent
  - `src/mna/agents/target_screening.py` using Strands `Agent`
  - Tools: `text_to_sql` (primary), `kb_retrieve` (enrichment)
  - System prompt in `src/mna/agents/prompts/target_screening.txt`
  - Module docstring with role, tools, and example prompt
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 16.3_

- [x] 23. Implement the Financial Analysis agent
  - `src/mna/agents/financial_analysis.py`
  - Tools: `kb_retrieve`, `market_data`
  - System prompt instructs DCF and comparable-company analysis with inline citations
  - Module docstring with role, tools, and example prompt
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 3.3, 16.3_

- [x] 24. Implement the Strategic Fit agent
  - `src/mna/agents/strategic_fit.py`
  - Tools: `kb_retrieve`, `memory` (prior-deals namespace)
  - System prompt instructs comparison against prior deals with memo citations
  - Module docstring with role, tools, and example prompt
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 2.4, 16.3_

- [x] 25. Implement the Compliance Validation agent
  - `src/mna/agents/compliance_validation.py`
  - Tools: `kb_retrieve` (governance checklist), `citation_check` (invokes evaluator Lambda)
  - System prompt enforces citation presence on every factual claim
  - Module docstring with role, tools, and example prompt
  - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 4.2, 16.3_

- [x] 26. Implement the supervisor agent and AgentCore Runtime entry point
  - `src/mna/agents/supervisor.py` using Strands `Agent` + `use_agent` pattern
  - Supervisor model configurable via `MNA_SUPERVISOR_MODEL` environment variable (default: Claude Sonnet 4.5)
  - Attach Guardrail via `MNA_GUARDRAIL_ID`
  - Decorate module with `@BedrockAgentCoreApp` for runtime hosting
  - Container entrypoint: `python -m mna.agents.supervisor`
  - _Requirements: 1.1, 1.5, 4.1, 12.1, 12.2_

## Phase 6: Synthetic Data and Prompts

- [x] 27. Implement `data/schemas/target_companies.sql`
  - DDL for `mna.target_companies` table and indexes per design
  - `SYNTHETIC DATA` header comment
  - _Requirements: 2.5, 5.3_

- [x] 28. Implement the synthetic data generator (companies)
  - `data/generate.py companies` subcommand
  - Deterministic seed, faker-style name list
  - 20+ rows spanning plausible transportation/logistics profiles
  - Insert via RDS Data API
  - Unit tests verifying determinism and row count
  - _Requirements: 5.2, 5.4, 5.5, 14.5_

- [x] 29. Implement the synthetic data generator (documents)
  - `data/generate.py documents` subcommand
  - 3 spotlight companies × (CIM, financials, press pack)
  - Templated content via Bedrock; ReportLab for PDF rendering
  - Every document includes `SYNTHETIC DATA - NOT REAL` header
  - Upload to S3 bucket at the paths defined in design
  - Trigger Bedrock KB ingestion job and poll for completion
  - _Requirements: 5.1, 5.3, 5.4, 14.5_

- [x] 30. Implement the synthetic data generator (memory)
  - `data/generate.py memory` subcommand
  - 3 prior-deal memos (~500 words each), templated via Bedrock
  - Insert into AgentCore Memory `prior_deals` namespace
  - _Requirements: 5.1, 5.4, 2.4_

- [x] 31. Add the `--seed-all` orchestrator
  - `data/generate.py --seed-all` chains companies → documents → memory
  - Print progress and final counts
  - Idempotent: re-running doesn't duplicate data
  - _Requirements: 5.4, 5.5_

- [x] 32. Author `prompts.md` and agent-aligned example prompts
  - Four prompts per design table (Target Screening, Financial Analysis, Strategic Fit, Compliance Validation)
  - Each prompt includes: expected agent, expected citation sources, what it demonstrates
  - _Requirements: 6.1, 6.2, 6.3_

## Phase 7: Reader Surfaces

- [x] 33. Implement the CLI
  - `cli/invoke.py` with argparse subcommands: `invoke`, `list-agents`, `trace`, `evaluate`
  - Entry point registered in `pyproject.toml` as `mna = "cli.invoke:main"`
  - Smoke test invoking each subcommand against a mock backend
  - _Requirements: 7.3, 7.4_

- [x] 34. Implement `notebooks/walkthrough.ipynb`
  - Cell 1: environment validation (region, credentials, SSM params populated)
  - Cell 2: data overview (KB documents + row counts)
  - Cells 3–6: one per specialist with prompt, invocation, rendered response + citations
  - Cell 7: trace inspection for the last invocation
  - All cells import from `mna.client` (no duplicated logic)
  - _Requirements: 7.1, 7.2, 7.4, 9.3_

## Phase 8: Deploy and Cleanup Scripts

- [x] 35. Implement `deploy.sh` and `deploy.ps1`
  - Preflight checks (region, Bedrock access)
  - Create `.venv`, install dependencies
  - `cdk bootstrap` (idempotent)
  - Deploy stacks in order: Network → Data → Evaluator → Gateway → Agent
  - Invoke `data/generate.py --seed-all`
  - Print notebook open instructions
  - Feature parity across both script variants
  - _Requirements: 8.1, 8.2, NFR-RT-6, NFR-RT-7_

- [x] 36. Implement `cleanup.sh` and `cleanup.ps1`
  - `cdk destroy --all --force` in reverse order
  - `scripts/verify_cleanup.sh`/`.ps1` checks for orphaned S3 objects, KB data sources, ECR images, AgentCore resources
  - Prints verification commands for the reader to run manually
  - _Requirements: 8.3, 8.4, 8.5_

## Phase 9: Testing

- [x] 37. Expand unit test coverage
  - Agents: verify tool selection logic for each specialist with mocked tools
  - Config: SSM resolution edge cases
  - Memory: namespace scoping isolation
  - Run: `pytest tests/unit/` with ruff + flake8 clean
  - _Requirements: 15.1, 15.3_

- [x] 38. Implement `tests/smoke_test.py`
  - Invokes each of the 4 specialists with its `prompts.md` prompt
  - Asserts non-empty response with at least one citation
  - Asserts evaluator returns a pass/fail result for each
  - Asserts at least one invocation produces an X-Ray trace with a Gateway call
  - Designed to run post-deploy as the final step of `deploy.sh`/`.ps1`
  - _Requirements: 15.3, Sample-level AC 2, 3, 4_

## Phase 10: Documentation

- [x] 39. Write the full `README.md`
  - Overview, architecture diagram (reuse Figure 1 from the blog), prerequisites table (Windows/macOS/Linux)
  - Deploy, run, cleanup sections with copy-paste commands
  - Cost estimate table (from design §Cost Design)
  - Troubleshooting section covering every error case from design §Error Handling
  - Extension points section including the "what's implemented vs. extension" table mapped to architecture diagram layers
  - Links to companion blog post, AgentCore docs, Strands SDK docs, Bedrock KB docs
  - _Requirements: 10.3, 13.2, 14.1, 16.1, 16.2, 16.4_

- [x] 40. Write `CONTRIBUTING.md`
  - How to add a new agent (file layout, Strands pattern, system prompt, registration in supervisor)
  - How to add a new tool (file layout, IAM permission updates, testing)
  - Custom Resource review checklist referencing design §Custom Resource Safety Requirements
  - Code style (ruff rules), PR checklist, CLA reference
  - _Requirements: 15.4, 17.2, 11a.8_

- [x] 41. Final review pass
  - Verify every functional and non-functional requirement in `requirements.md` is addressed by at least one task
  - Verify sample-level acceptance criteria are exercised by smoke tests or manual checks
  - Tag `v1.0.0` and prepare release notes referencing the companion blog post
  - _Requirements: all sample-level acceptance criteria_

## Task Dependency Graph

High-level ordering (tasks within a phase can sometimes run in parallel):

```
Phase 1 ──▶ Phase 2 ──▶ Phase 3 ──▶ Phase 4 ──▶ Phase 5 ──▶ Phase 6 ──▶ Phase 7 ──▶ Phase 8 ──▶ Phase 9 ──▶ Phase 10
```

Key cross-phase dependencies:

- Every CR-bearing stack task (11, 12, 13, 16, 17) depends on task 10 (shared CR base module).
- Task 13 (`AgentStack`) depends on task 11 (build pipeline) which depends on task 26 (supervisor entrypoint) being importable.
- Task 29 (document generator) depends on task 7 (KB ready for ingestion).
- Task 33 (CLI) and task 34 (notebook) both depend on task 3 (`mna.client`) and task 26 (deployed supervisor).
- Task 38 (smoke test) depends on a fully deployed stack from task 35 plus seeded data from task 31.

## Requirements Coverage Matrix

Every numbered requirement from `requirements.md` is covered by at least one task above:

| Requirement | Covered by task(s) |
|---|---|
| 1 (Multi-agent orchestration) | 13, 22–26 |
| 2 (Knowledge, structured data, memory) | 6, 7, 12, 13, 21, 30 |
| 2a (Text-to-SQL) | 12, 19, 27 |
| 3 (External tool) | 9, 15, 16, 20, 23 |
| 4 (Safety + evaluation) | 8, 13, 14, 25, 26 |
| 5 (Synthetic data) | 28–31 |
| 6 (Example prompts) | 32 |
| 7 (User interfaces) | 2, 3, 33, 34 |
| 8 (Deploy + cleanup) | 35, 36 |
| 9 (Observability) | 3, 13, 34 |
| 10 (Deployment / regions) | 4, 39 |
| 11 (Runtime + dependencies) | 1, 4, 11, 35, 36 |
| 11a (CR safety) | 10, 11, 12, 13, 16, 17, 40 |
| 12 (Foundation model config) | 26 |
| 13 (Cost) | 6, 39 |
| 14 (Security) | 5, 6, 12, 13, 28, 29, 30 |
| 15 (Code quality) | 1, 14, 37, 38, 40 |
| 16 (Documentation) | 22–25, 39 |
| 17 (Maintainability) | 1, 40 |

## Conclusion

All implementation tasks are complete and verified against the requirements coverage matrix above. The sample is deployable, demonstrable, and ready for release.

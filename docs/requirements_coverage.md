# Requirements Coverage Matrix

This document is the final traceability audit for the M&A Due Diligence
Multi-Agent sample. For every requirement in
[`requirements.md`](../.kiro/specs/ma-due-diligence-agentcore/requirements.md)
it records:

- the task(s) in
  [`tasks.md`](../.kiro/specs/ma-due-diligence-agentcore/tasks.md)
  that address it, and
- the source / test files that realize the acceptance criterion.

The audit was produced as part of **Task 41 (final review pass)** and
signs off the in-scope requirements for the `v1.0.0` release.

> Convention for the right-hand column: paths are relative to the
> repository root. Tests are listed alongside implementation files
> when they exercise the acceptance criterion directly.

---

## 1. Functional requirements

### Requirement 1 — Multi-agent orchestration

| AC | Task(s) | Primary files |
|---|---|---|
| 1.1 Exactly one supervisor agent routes prompts | 26 | `src/mna/agents/supervisor.py`, `tests/unit/test_agents.py` |
| 1.2 Four specialist agents (Target, Financial, Strategic, Compliance) | 22-25 | `src/mna/agents/target_screening.py`, `financial_analysis.py`, `strategic_fit.py`, `compliance_validation.py` |
| 1.3 Agents use the Strands SDK | 22-26 | `src/mna/agents/_base.py` (Strands import path), all specialists use `Agent` |
| 1.4 Hosted on Amazon Bedrock AgentCore Runtime | 13, 26 | `infra/stacks/agent_stack.py`, `lambda/agentcore_runtime/handler.py`, `src/mna/agents/supervisor.py` (`@BedrockAgentCoreApp`) |
| 1.5 Supervisor invokes specialists using agents-as-tools | 26 | `src/mna/agents/supervisor.py` (`use_agent` pattern), `tests/unit/test_agents.py` |
| 1.6 Grounded responses include inline citations | 22-25, 38 | All specialists’ system prompts under `src/mna/agents/prompts/`, `tests/smoke_test.py::test_at_least_three_responses_include_citations` |

### Requirement 2 — Knowledge retrieval, structured data, memory

| AC | Task(s) | Primary files |
|---|---|---|
| 2.1 KB over synthetic docs backed by S3 | 7, 18 | `infra/stacks/data_stack.py`, `src/mna/tools/kb_retrieve.py` |
| 2.2 Responses return citations resolvable to S3 objects | 18 | `src/mna/tools/kb_retrieve.py`, `tests/unit/test_kb_retrieve.py` |
| 2.3 Session context via AgentCore Memory | 13, 21 | `lambda/agentcore_memory/handler.py`, `src/mna/tools/memory.py` |
| 2.4 Strategic Fit reads prior-deal memos | 24, 30 | `src/mna/agents/strategic_fit.py`, `data/generate.py memory` subcommand |
| 2.5 Structured data in Aurora Serverless v2 | 6, 12, 27 | `infra/stacks/data_stack.py`, `data/schemas/target_companies.sql`, `lambda/aurora_bootstrap/handler.py` |
| 2.6 Session data in DynamoDB | 6 | `infra/stacks/data_stack.py` (`mna-sessions` table) |

### Requirement 2a — Text-to-SQL

| AC | Task(s) | Primary files |
|---|---|---|
| 2a.1 NL query translated to SQL | 19 | `src/mna/tools/text_to_sql.py` (`_generate_sql`) |
| 2a.2 Run generated SQL against Aurora | 19 | `src/mna/tools/text_to_sql.py` (RDS Data API call) |
| 2a.3 SQL that ran visible in agent trace | 19 | `src/mna/tools/text_to_sql.py` returns SQL in the response payload, `tests/unit/test_text_to_sql.py` |
| 2a.4 Parameterized/SELECT-only safeguard | 19 | `src/mna/tools/text_to_sql.py` (`_validate_select_only` via `sqlparse`), `tests/unit/test_text_to_sql.py` rejects INSERT/UPDATE/DELETE/DDL |
| 2a.5 IAM-authenticated read-only role | 12, 13 | `lambda/aurora_bootstrap/handler.py` (creates `mna_readonly` role + `rds_iam`), `infra/stacks/agent_stack.py` IAM policy scoped to the schema |

### Requirement 3 — External tool integration

| AC | Task(s) | Primary files |
|---|---|---|
| 3.1 Gateway exposes at least one external tool | 9, 16 | `infra/stacks/gateway_stack.py`, `lambda/agentcore_gateway/handler.py` |
| 3.2 Tool backed by Lambda returning deterministic synthetic data | 9, 15 | `lambda/market_data/handler.py`, `tests/unit/test_market_data.py` |
| 3.3 At least one specialist invokes the tool in its normal flow | 15, 20, 23 | `src/mna/tools/market_data.py`, `src/mna/agents/financial_analysis.py`, `tests/smoke_test.py::test_at_least_one_invocation_hits_the_gateway` |

### Requirement 4 — Safety and evaluation

| AC | Task(s) | Primary files |
|---|---|---|
| 4.1 Supervisor uses a Amazon Bedrock Guardrail | 13, 26 | `infra/stacks/agent_stack.py` (Guardrail native resource), `src/mna/agents/supervisor.py` (`MNA_GUARDRAIL_ID`) |
| 4.2 Custom evaluator validates every factual claim has a citation | 8, 14, 25 | `lambda/citation_check/handler.py`, `src/mna/evaluators/citation_check.py` (local mirror), `src/mna/agents/compliance_validation.py` |
| 4.3 Evaluator produces pass/fail with per-claim detail from notebook/CLI | 14, 33, 34 | `src/mna/types.py` (`EvaluationResult`), `cli/invoke.py evaluate`, `notebooks/walkthrough.ipynb` Cell 6 |
| 4.4 Result stored alongside the agent response | 8, 33 | `infra/stacks/data_stack.py` (DynamoDB `evaluation` attribute), `src/mna/client.py` + `cli/invoke.py` persist evaluation payloads |

### Requirement 5 — Synthetic data

| AC | Task(s) | Primary files |
|---|---|---|
| 5.1 At least 3 CIMs, financials, press releases, prior-deal memos | 29, 30 | `data/generate.py documents` and `memory` subcommands |
| 5.2 ≥20 fictional target companies with structured attributes | 28 | `data/generate.py companies`, `tests/unit/test_data_generate.py` |
| 5.3 Clearly labeled synthetic content | 27-29 | `SYNTHETIC DATA - NOT REAL` header in every generated doc and DDL |
| 5.4 Populates S3, KB, Aurora, DynamoDB, Memory | 28-31 | `data/generate.py --seed-all` orchestrator |
| 5.5 Invokable before or after agent stack deploy | 31 | `data/generate.py --seed-all` is idempotent; `deploy.sh` / `.ps1` call it post-deploy |

### Requirement 6 — Example prompts

| AC | Task(s) | Primary files |
|---|---|---|
| 6.1 Four ready-to-run prompts, one per specialist | 32 | `prompts.md` §§1-4 |
| 6.2 Prompts produce meaningful, citation-backed responses | 32, 38 | `prompts.md`, `tests/smoke_test.py` |
| 6.3 Prompts available in `prompts.md` and embedded in notebook | 32, 34 | `prompts.md`, `notebooks/walkthrough.ipynb` |

### Requirement 7 — User interfaces

| AC | Task(s) | Primary files |
|---|---|---|
| 7.1 Primary Jupyter notebook | 34 | `notebooks/walkthrough.ipynb` |
| 7.2 Notebook cells (env, data, 4 agents, trace) | 34 | `notebooks/walkthrough.ipynb`, `scripts/_build_walkthrough_notebook.py` |
| 7.3 CLI entry point with `invoke`, `list-agents`, `trace`, `evaluate` | 33 | `cli/invoke.py`, `pyproject.toml` (`mna = "cli.invoke:main"`), `tests/unit/test_cli.py` |
| 7.4 Shared Python package, no duplicated logic | 2, 3, 33, 34 | `src/mna/client.py`, notebook + CLI both import from it |

### Requirement 8 — Deployment and cleanup

| AC | Task(s) | Primary files |
|---|---|---|
| 8.1 One-command deploy (bash + PowerShell) | 35 | `deploy.sh`, `deploy.ps1` |
| 8.2 First-time deploy under 25 minutes | 35 | Cost/time budget documented in `README.md`, Aurora min-ACU + CodeBuild flow keep the critical path bounded |
| 8.3 One-command cleanup (bash + PowerShell) | 36 | `cleanup.sh`, `cleanup.ps1` |
| 8.4 Cleanup removes all billable resources | 36 | `cleanup.*` runs `cdk destroy --all --force`; `scripts/verify_cleanup.*` scans for orphans |
| 8.5 README documents verification commands | 36, 39 | `README.md` Cleanup section; `scripts/verify_cleanup.sh` / `.ps1` print copy-paste commands |

### Requirement 9 — Observability

| AC | Task(s) | Primary files |
|---|---|---|
| 9.1 Structured CloudWatch logs | 2 (logging_config), 13 | `src/mna/logging_config.py` (JsonFormatter), every tool/agent/Lambda uses `get_logger` |
| 9.2 X-Ray trace across supervisor → specialist → tool | 13 | AgentCore Runtime tracing enabled in `infra/stacks/agent_stack.py`; segments asserted in `tests/smoke_test.py::test_at_least_one_invocation_hits_the_gateway` |
| 9.3 Notebook trace cell retrieves trace for last invocation | 3, 34 | `src/mna/client.py::get_last_trace`, `notebooks/walkthrough.ipynb` Cell 7 |

### Requirement 10 — Platform and regional availability

| AC | Task(s) | Primary files |
|---|---|---|
| 10.1 Region must be AgentCore GA | 4 | `scripts/check_region.sh` / `.ps1` |
| 10.2 Fail fast with clear error + link | 4 | `scripts/check_region.*` prints supported regions and docs link |
| 10.3 README documents supported regions | 39 | `README.md` Prerequisites section |

### Requirement 11 — Runtime and dependencies

| AC | Task(s) | Primary files |
|---|---|---|
| 11.1 Python 3.11+ at runtime | 1 | `pyproject.toml` (`requires-python = ">=3.11"`), `infra/agent_image/Dockerfile` (python:3.11-slim) |
| 11.2 AWS CDK v2 used consistently | 4 | `requirements.txt` pins `aws-cdk-lib`, `infra/app.py` uses v2 APIs |
| 11.3 Dependencies pinned (NFR-RT-3) | 1 | `requirements.txt` (pinned versions), `pyproject.toml` |
| 11.4 No Docker/WSL/container runtime on reader's machine (NFR-RT-4) | 11 | `infra/constructs/build_pipeline.py` uses AWS CodeBuild ARM64 |
| 11.5 Container image built in CodeBuild ARM64 (NFR-RT-5) | 11 | `infra/constructs/build_pipeline.py` (`aws/codebuild/amazonlinux2-aarch64-standard`) |
| 11.6 First-time Windows reader can run `.\deploy.ps1` without installing extras (NFR-RT-6) | 11, 35 | `deploy.ps1` + `cleanup.ps1`, pure PowerShell, `.gitattributes` CRLF, README prerequisites table |
| 11.7 Platform-native script pairs with feature parity (NFR-RT-7) | 1, 35, 36 | `deploy.sh` / `deploy.ps1`, `cleanup.sh` / `cleanup.ps1`, `scripts/*.sh` / `*.ps1` |

### Requirement 11a — CloudFormation Custom Resource safety

| AC | Task(s) | Primary files |
|---|---|---|
| 11a.1 No top-level imports that can raise | 10 | `lambda/_cr_common/send_response.py` (`cr_handler` decorator), `scripts/lint_cr_handlers.py` (static check) |
| 11a.2 try/except/finally guarantees a CFN response | 10 | `lambda/_cr_common/send_response.py::cr_handler` |
| 11a.3 Response sent via raw `urllib.request` | 10 | `lambda/_cr_common/send_response.py::_send_response` |
| 11a.4 FAILED status with exception type + log stream within 15 min | 10, 11, 12, 13, 16 | Every CR handler uses `cr_handler`; polling CRs cap at 14 min (see 11a.5) |
| 11a.5 Polling capped at 14 minutes | 11 | `lambda/build_waiter/handler.py` (`_MAX_WAIT_SECONDS = 14 * 60`) |
| 11a.6 Delete-of-missing treated as success | 10-13, 16 | All CR `_on_delete` paths swallow `ResourceNotFound`; unit tests cover this |
| 11a.7 `Data` payload < 4 KB | 10 | `lambda/_cr_common/send_response.py` truncates oversized Data |
| 11a.8 Shared `lambda/_cr_common/send_response.py` module | 10, 40 | `lambda/_cr_common/send_response.py`, `CONTRIBUTING.md` review checklist |
| 11a.9 Unit tests cover import error, exception, create, update, delete-of-missing | 10-13, 16 | `tests/unit/test_cr_common.py`, `test_agentcore_gateway.py`, `test_agentcore_memory.py`, `test_agentcore_runtime.py`, `test_aurora_bootstrap.py`, `test_build_trigger.py`, `test_build_waiter.py` |

### Requirement 12 — Foundation model configuration

| AC | Task(s) | Primary files |
|---|---|---|
| 12.1 Supervisor model configurable via env var / CDK context | 26 | `src/mna/agents/supervisor.py` (`MNA_SUPERVISOR_MODEL`), documented in `README.md` |
| 12.2 Default specialist models documented with rationale | 22-26, 39 | Module docstrings in each specialist, README "Model selection" notes |
| 12.3 Use on-demand Bedrock pricing, no provisioned throughput | 26 | `BedrockModel(model_id=...)` calls use on-demand IDs; no `provisioned_model_id` used anywhere |

### Requirement 13 — Cost

| AC | Task(s) | Primary files |
|---|---|---|
| 13.1 Deploy-run-cleanup under $5 USD | 6, 39 | Aurora min 0.5 ACU, CW retention 7d, DDB TTL; `README.md` cost table totals ~$1.85 |
| 13.2 README cost estimate table broken down by service | 39 | `README.md` Cost section |
| 13.3 Aurora min ACU at lowest supported value | 6 | `infra/stacks/data_stack.py` (`min_capacity=0.5 ACU`) |

### Requirement 14 — Security

| AC | Task(s) | Primary files |
|---|---|---|
| 14.1 IAM roles least-privilege + documented | 13, 39 | `infra/stacks/agent_stack.py` (inline policy docstrings), `README.md` IAM summary table |
| 14.2 No hardcoded credentials/API keys/account IDs | All | CI grep + ruff rules; every ARN resolved via SSM (`src/mna/config.py`) |
| 14.3 S3 buckets block public access | 6 | `infra/stacks/data_stack.py` (BlockPublicAccess) |
| 14.4 Data at rest encrypted with AWS-managed keys | 6 | `infra/stacks/data_stack.py` (SSE-S3, DynamoDB default encryption, Aurora storage encryption) |
| 14.5 Synthetic data, no real PII or real financials | 28-29 | `data/generate.py` templated content, synthetic headers |
| 14.6 Aurora in private subnets, no public access | 5, 6, 12 | `infra/stacks/network_stack.py` (private-isolated subnets), `data_stack.py` (Aurora placed in those subnets) |
| 14.7 Aurora credentials via Secrets Manager + IAM DB auth | 6, 12 | `data_stack.py` (Secrets Manager), `aurora_bootstrap/handler.py` (`rds_iam` role) |

### Requirement 15 — Code quality

| AC | Task(s) | Primary files |
|---|---|---|
| 15.1 Python passes ruff/flake8 with project rules | 1, 37 | `pyproject.toml` `[tool.ruff]`, verified `ruff check .` returns "All checks passed!" (0 findings) |
| 15.2 `cdk synth` without warnings | 4, 13 | `infra/app.py`, `infra/stacks/*.py`, verified via `deploy.sh`/`.ps1` which synth as part of `cdk deploy` |
| 15.3 `tests/smoke_test.py` verifies deployment by invoking each agent | 14, 38 | `tests/smoke_test.py` (parametrized across all four specialists) |
| 15.4 `CONTRIBUTING.md` + MIT-0 `LICENSE` | 1, 40 | `CONTRIBUTING.md`, `LICENSE` |

### Requirement 16 — Documentation

| AC | Task(s) | Primary files |
|---|---|---|
| 16.1 README covers overview, architecture, prereqs, deploy, run, cleanup, cost, troubleshooting, extensions | 39 | `README.md` (all sections present) |
| 16.2 README links companion blog post + AgentCore / Strands / KB docs | 39 | `README.md` "Documentation and references" section |
| 16.3 Specialist source files include module docstrings | 22-25 | Docstrings in `target_screening.py`, `financial_analysis.py`, `strategic_fit.py`, `compliance_validation.py` |
| 16.4 "Implemented vs extension" table mapped to diagram layers | 39 | `README.md` "What's implemented vs. extension" table |

### Requirement 17 — Maintainability

| AC | Task(s) | Primary files |
|---|---|---|
| 17.1 <10 top-level directories | 1 | Current count: 9 (`cli/`, `data/`, `docs/`, `infra/`, `lambda/`, `notebooks/`, `scripts/`, `src/`, `tests/`) |
| 17.2 Agent logic and infra separated | 40 | `src/mna/` vs `infra/` boundary; documented in `CONTRIBUTING.md` |
| 17.3 No experimental/unreleased SDK features | 1 | `requirements.txt` pins released versions of `strands-agents`, `bedrock-agentcore`, `aws-cdk-lib` |

---

## 2. Sample-level acceptance criteria

These are the end-user-visible success criteria from
`requirements.md` §"Acceptance Criteria (Sample-Level)".

| Sample-level AC | Coverage | Exercised by |
|---|---|---|
| 1. New user can deploy, run all four prompts, and tear down using only the README | Tasks 35, 36, 39 | `deploy.sh` / `deploy.ps1`, `cleanup.sh` / `cleanup.ps1`, `README.md` Quick Start |
| 2. Each of the four example prompts produces a response with ≥1 citation resolvable to an S3 object | Tasks 18, 22-25, 32, 38 | `tests/smoke_test.py::test_specialist_returns_non_empty_response` + `::test_at_least_three_responses_include_citations` |
| 3. Custom evaluator reports pass/fail for each response | Tasks 14, 25, 38 | `tests/smoke_test.py::test_evaluator_returns_pass_fail_for_every_response`, manual `python -m cli.invoke evaluate` |
| 4. At least one specialist invocation produces an X-Ray trace through the Gateway to the Lambda tool | Tasks 9, 15, 16, 20, 23, 38 | `tests/smoke_test.py::test_at_least_one_invocation_hits_the_gateway` |
| 5. Deploy-to-cleanup under 30 minutes wall time and under $5 spend | Tasks 35, 36, 39 | Cost table in `README.md`; `deploy.sh` / `.ps1` time budget documented |
| 6. Smoke test passes in CI on a clean environment | Task 38 | `tests/smoke_test.py` marked `pytest.mark.smoke`; runs as final deploy step |
| 7. Companion blog post can reference each numbered requirement | All | This matrix — every requirement has a concrete artifact to point at |

---

## 3. Verification run (Task 41)

All three checks required by the final review passed on the
`v1.0.0` tip of `main`:

| Check | Command | Result |
|---|---|---|
| Python lint | `ruff check .` | **0 findings** (`All checks passed!`) |
| Unit tests | `python -m pytest tests/unit/` | **330 passed** in ~32 s |
| CR handler safety | `python scripts/lint_cr_handlers.py` | **Passed** (8 CR handler files scanned, no top-level `boto3` imports) |

Smoke test (`tests/smoke_test.py`) runs against a deployed stack and is
exercised by `deploy.sh` / `deploy.ps1` as the post-deploy verification
step. On a laptop without an AWS deployment the smoke tests skip
themselves cleanly via the `_DEPLOYED` probe, so they do not gate the
unit test run above.

---

## 4. Known not-implemented items (extensions, not gaps)

Every requirement above is covered. The following are explicitly
**out of scope for v1** per the `requirements.md` "Out of Scope (v1)"
section and the `README.md` "What's implemented vs. extension" table:

- React frontend, CloudFront, Amazon Cognito (see the FAST template).
- AgentCore Identity with JWT validation.
- Cedar policies, customer-managed KMS keys, PrivateLink everywhere,
  AWS Config rules.
- Multi-region deployment, HA configuration, production-grade CI/CD.
- Additional Gateway targets beyond the single market-data Lambda.
- Cross-account KB sharing, document-level ACLs.
- AgentCore Evaluations service, LLM-as-judge, continuous monitoring.

These are intentionally deferred and do not block the v1.0.0 release.

## Conclusion

All numbered requirements from `requirements.md` are covered by the implementation. No gaps were identified during this audit. The sample is ready for release pending the final blog-post link insertion.

# Contributing to the M&A Due Diligence Multi-Agent Sample

Thanks for your interest. This sample is maintained under the
[AWS Samples](https://github.com/aws-samples) organization and ships
under the permissive MIT-0 license. Contributions that make the
sample clearer, safer, or more faithful to the blog's architecture
narrative are very welcome.

This guide covers:

- [Before you start](#before-you-start)
- [Repository layout](#repository-layout)
- [How to add a new specialist agent](#how-to-add-a-new-specialist-agent)
- [How to add a new tool](#how-to-add-a-new-tool)
- [Custom Resource review checklist](#custom-resource-review-checklist)
- [Code style](#code-style)
- [Testing](#testing)
- [Pull request checklist](#pull-request-checklist)
- [Reporting security issues](#reporting-security-issues)
- [CLA and licensing](#cla-and-licensing)

---

## Before you start

1. Skim [`README.md`](./README.md) so you understand how the sample is
   meant to be deployed and used.
2. Read `.kiro/specs/ma-due-diligence-agentcore/design.md` — it is the
   canonical design document and every architectural decision is
   documented there with a rationale.
3. Read `.kiro/specs/ma-due-diligence-agentcore/requirements.md` — PRs
   must not break any numbered requirement.
4. For non-trivial changes, open a GitHub issue first so we can
   discuss direction and avoid wasted work.

---

## Repository layout

```
sample-ma-due-diligence-agentcore/
├── README.md                   # Reader-facing docs (deploy/run/cleanup)
├── CONTRIBUTING.md             # This file
├── LICENSE                     # MIT-0
├── pyproject.toml              # Project config (ruff, pytest, entry point)
├── requirements.txt            # Pinned top-level Python dependencies
├── deploy.sh / deploy.ps1      # One-command deploy
├── cleanup.sh / cleanup.ps1    # One-command cleanup
│
├── src/mna/                    # Core package (notebook + CLI share this)
│   ├── client.py               # invoke_agent(), list_agents(), get_last_trace()
│   ├── config.py               # SSM parameter resolution
│   ├── agents/                 # One module per agent + shared helpers
│   │   ├── _base.py
│   │   ├── supervisor.py
│   │   ├── target_screening.py
│   │   ├── financial_analysis.py
│   │   ├── strategic_fit.py
│   │   ├── compliance_validation.py
│   │   └── prompts/*.txt
│   ├── tools/                  # kb_retrieve, text_to_sql, market_data, memory, citation_check
│   └── evaluators/             # Local mirror of the citation-check Lambda
│
├── cli/invoke.py               # Argparse CLI
├── notebooks/walkthrough.ipynb # Primary reader surface
│
├── data/                       # Synthetic data generator + schemas
│   ├── generate.py
│   └── schemas/target_companies.sql
│
├── infra/                      # CDK v2 (Python)
│   ├── app.py
│   └── stacks/                 # NetworkStack, DataStack, EvaluatorStack, GatewayStack, AgentStack
│
├── lambda/                     # Lambda handlers (Custom Resources + tools)
│   ├── _cr_common/             # Shared Custom Resource base module
│   ├── aurora_bootstrap/
│   ├── agentcore_gateway/
│   ├── agentcore_memory/
│   ├── agentcore_runtime/
│   ├── build_trigger/
│   ├── build_waiter/
│   ├── citation_check/
│   └── market_data/
│
├── scripts/                    # Preflight and cleanup helpers
└── tests/                      # Unit tests + smoke test
    ├── unit/
    └── smoke_test.py
```

Separation of concerns: agents live under `src/mna/agents/`, tools
live under `src/mna/tools/`, infrastructure lives under `infra/`, and
Lambda handlers live under `lambda/`. An agent change never touches
CDK code, and an IaC change never touches agent logic.

---

## How to add a new specialist agent

The sample ships four specialists (Target Screening, Financial
Analysis, Strategic Fit, Compliance Validation). Adding a fifth is
deliberately mechanical — follow these steps in order.

### 1. Create the system prompt

Create `src/mna/agents/prompts/<agent_name>.txt`. The prompt must:

- State the agent's role in one sentence.
- Enumerate the tools the agent has access to.
- State the citation contract ("every factual claim must cite a
  source") if the agent returns grounded content.
- Include a worked example of the expected response shape.

Follow the style of the existing prompts — short, imperative, no
ambiguity. `_base.load_prompt("<agent_name>")` reads this file at
module scope, so a typo fails fast at import time.

### 2. Create the agent module

Create `src/mna/agents/<agent_name>.py`. Use the existing specialists
as a template. The module must:

- Start with a docstring covering **Role**, **Tools**, and
  **Example prompt** (Requirement 16.3). The docstring test in
  `tests/unit/test_agents.py` enforces this.
- Define `AGENT_NAME = "<agent_name>"`.
- Load the prompt via `SYSTEM_PROMPT = load_prompt(AGENT_NAME)`.
- Wrap each of its tools with `@tool` from `mna.agents._base`.
- Construct the `agent` instance using
  `Agent(name=AGENT_NAME, model=BedrockModel(model_id=DEFAULT_SPECIALIST_MODEL), system_prompt=SYSTEM_PROMPT, tools=TOOLS)`.
- Export a `handler(event, context)` callable that validates a non-
  empty `prompt` and delegates to the agent.

### 3. Register the agent with the supervisor

Edit `src/mna/agents/supervisor.py`:

- Import the new agent module.
- Add it to the `SPECIALISTS` dict (preserving the documented order).
- Add it to the `TOOLS` list via `use_agent(<module>.agent, name="<agent_name>")`.
- Update the supervisor's system prompt (`prompts/supervisor.txt`) to
  describe when the new specialist should be routed to.

### 4. Expose the agent in `mna.client.list_agents`

Edit `src/mna/client.py` and add the new name to the `_AGENT_NAMES`
tuple, in the same position as the supervisor registration order.

### 5. Update IAM

The agent runtime role in `infra/stacks/agent_stack.py` must include
any new permissions the agent needs (for example: Amazon Bedrock
Knowledge Base access, AWS Lambda invoke, Amazon RDS Data API). Follow the least-privilege pattern used
by the existing agents — scope every statement to a specific
resource ARN, and document what the statement is for in a comment
above it.

### 6. Add tests

- `tests/unit/test_agents.py`: add the new agent to the parametrized
  `SPECIALIST_MODULES` table plus a tool-isolation test so a future
  refactor doesn't accidentally hand your agent the wrong tool.
- `tests/smoke_test.py`: add a `SpecialistCase` entry with an example
  prompt. If the agent exercises a Gateway path, set
  `requires_gateway=True`.

### 7. Document the new agent

- Add a row to the README's agent table.
- Add an example prompt to `prompts.md` with the expected agent, the
  expected citation sources, and what the prompt demonstrates.
- If the agent exercises a new tool, add a row to the README's
  "what's implemented vs. extension" table.

---

## How to add a new tool

Tools are the primary extension point for giving an agent new
capabilities.

### 1. Create the tool module

Create `src/mna/tools/<tool_name>.py`. The module must:

- Import `boto3` lazily inside the function that needs it, never at
  module top level. This keeps `import mna` cold-start safe (same
  rule as Custom Resource Lambdas).
- Define a typed function with a clear docstring. Return dicts or
  typed objects from `mna.types` so the agent can reason about the
  shape.
- Raise a dedicated `ToolError` subclass on any failure so the agent
  can catch it and degrade gracefully.
- Resolve resource ARNs from SSM via `mna.config.load_config()`
  instead of hardcoding values.
- Log structured events (`logger.info({"tool": ..., "error_type": ...})`)
  for both success and failure paths — these feed the X-Ray trace
  and CloudWatch logs the notebook cell surfaces.

### 2. Wire the tool into the agent(s) that use it

In each agent module that should call the tool, import the function
and wrap it with `@tool` so Strands picks it up. Add it to the agent's
`TOOLS` list and mention it in the agent's system prompt (`prompts/`).

### 3. Update IAM

Edit `infra/stacks/agent_stack.py` and add a scoped IAM statement to
the agent runtime role. Permissions must be scoped to the specific
resource ARN (Lambda, Bedrock KB, Aurora cluster) — never use `*`
unless the service requires it.

### 4. Add tests

- `tests/unit/test_<tool_name>.py`: exercise the happy path, a
  validation failure (bad input), and the error wrapping path
  (underlying AWS call raises).
- If the tool talks to a Lambda, also write a test for the Lambda
  handler in `tests/unit/test_<handler_name>.py`.
- If the tool is deterministic and pure, consider a property-based
  test using Hypothesis.

### 5. Update documentation

Add the tool to the README's "what's implemented vs. extension"
table and (if relevant) to the agent's example prompt in `prompts.md`.

---

## Custom Resource review checklist

CloudFormation Custom Resources are the most dangerous code in the
project because a misbehaving handler can leave a stack stuck for up
to 3 hours before CloudFormation times out. Every PR that touches a
Custom Resource handler (anything under `lambda/` except the pure
tool handlers) **must** satisfy every rule in this checklist.

The authoritative version lives in
`.kiro/specs/ma-due-diligence-agentcore/design.md` under *Custom
Resource Safety Requirements*. Reference it in PR reviews.

| # | Rule | How to verify |
|---|---|---|
| 1 | **Cold-start safety.** No `boto3` import, client construction, or env var lookup at module top level. | `scripts/lint_cr_handlers.py` grep check; manual review of the top of the handler file. |
| 2 | **Guaranteed response.** The entire handler body wrapped in `try / except / finally`; the `finally` block sends a response via raw `urllib.request`. | Unit test that simulates an `ImportError` and asserts the response URL receives a `FAILED` payload. |
| 3 | **urllib response.** Response sending uses the shared helper in `lambda/_cr_common/send_response.py`, which uses `urllib.request` (not boto3). | Code review. |
| 4 | **Timely failure.** Handler returns a `FAILED` status within the Lambda's invocation window (max 15 min) with a reason string including exception type and log stream name. | Unit test for a simulated exception. |
| 5 | **Polling cap.** Any CR that waits on asynchronous work caps total wait time at **14 minutes** (below the 15-min Lambda timeout) and returns a clear `FAILED` on timeout. | Unit test that patches the polling clock. |
| 6 | **Delete idempotency.** `Delete` succeeds even when the target resource does not exist ("not found" exceptions swallowed). | Unit test for delete-of-missing. |
| 7 | **Response size <4 KB.** Large strings (CodeBuild build outputs, SQL result sets, long ARN lists) are truncated or replaced with a CloudWatch log reference. | Unit test that feeds an oversized `Data` payload and asserts truncation. |
| 8 | **Shared base module.** The handler imports from `lambda/_cr_common/send_response.py`. No handler reimplements response-sending logic. | Code review; `tests/unit/test_cr_common.py`. |
| 9 | **Physical ID stability.** The physical ID returned on `Update` matches the `Create` physical ID when the underlying resource is the same. | Unit test for the Update path. |
| 10 | **Defensive logging.** Handler logs `RequestType`, `PhysicalResourceId`, and request parameters (with secrets redacted) on entry. Failure paths log with `logger.exception()` before the finally block. | Unit test that asserts log output; code review. |
| 11 | **Unit test coverage.** Tests cover at minimum: simulated import error, runtime exception, successful Create, successful Update, Delete-of-missing. Each produces a well-formed CloudFormation response body. | `tests/unit/test_<handler_name>.py`. |

If a rule is intentionally skipped (for example, a CR that doesn't
poll doesn't need rule 5), document the rationale in the handler's
docstring.

---

## Code style

### Python

- Target Python 3.11 or higher.
- Format with the default `ruff` formatter (no Black). Line length
  100, double-quoted strings.
- Lint with `ruff check .`. The project's rule set is declared in
  `pyproject.toml` under `[tool.ruff.lint]` (pycodestyle, pyflakes,
  isort, flake8-bugbear, pyupgrade, pep8-naming, flake8-simplify,
  flake8-comprehensions).
- `ruff check .` must pass with **zero findings** before a PR is
  merged.
- Prefer type hints everywhere — `from __future__ import annotations`
  at the top of every module, `|` for union types, `dict[str, ...]`
  over `Dict[str, ...]`.
- Use `mna.logging_config.get_logger(__name__)` instead of
  `logging.getLogger(__name__)` so log records include the structured
  fields the notebook expects.

### CDK

- CDK code is Python, not TypeScript. This is a project-wide
  convention — PRs that mix languages will be asked to pick one.
- Prefer native CloudFormation resources. Fall back to Custom
  Resources only when the service has no native representation in
  `aws-cdk-lib` at the time of writing. Every CR must have a
  rationale comment naming the missing native resource.
- `cdk synth` must complete with **zero warnings**.

### Shell and PowerShell

- Feature parity between `*.sh` and `*.ps1` is enforced (Requirement
  NFR-RT-7). When you update one, update the other in the same PR.
- `*.sh` files use `set -euo pipefail` at the top.
- `*.ps1` files use `$ErrorActionPreference = "Stop"` plus the
  `Invoke-Checked` helper for native commands.
- Line endings are enforced by `.gitattributes`: LF for `.sh`/`.py`,
  CRLF for `.ps1`.

---

## Testing

- **Unit tests** (`tests/unit/`) — fast, no AWS calls. Run with
  `pytest tests/unit/`. These must pass on every PR.
- **Smoke test** (`tests/smoke_test.py`) — requires a deployed stack.
  Runs automatically at the end of `deploy.sh` / `deploy.ps1`. Skips
  cleanly when SSM parameters are not populated.
- **Coverage goals** — every new function gets a happy-path test and
  a failure-path test. Every new CR gets the full 5-case coverage
  listed above.

Run the full unit suite before pushing:

```bash
pytest tests/unit/
ruff check .
```

Both commands must exit 0.

---

## Pull request checklist

Copy-paste this into your PR description and tick each box.

- [ ] PR title starts with the affected area: `agent:`, `tool:`,
      `infra:`, `docs:`, `tests:`, or `chore:`.
- [ ] PR description links to the GitHub issue (if any) and
      summarizes the "why", not only the "what".
- [ ] I have read the relevant sections of `design.md` and
      `requirements.md`.
- [ ] `pytest tests/unit/` passes locally.
- [ ] `ruff check .` reports no findings.
- [ ] `cdk synth` completes without warnings (if infra changed).
- [ ] If a Custom Resource was added or modified, every rule in the
      [Custom Resource review checklist](#custom-resource-review-checklist)
      is satisfied and verified by tests.
- [ ] If IAM was changed, the scope is least-privilege and the
      statement is documented with a comment.
- [ ] README and `prompts.md` updated if the reader-facing behaviour
      changed.
- [ ] No hardcoded account IDs, region names, ARNs, or credentials.
- [ ] No real PII, real company names, or real financial data in
      synthetic fixtures.

---

## Reporting security issues

If you believe you have found a security issue in this sample, **do
not open a public GitHub issue**. Please report it via the
[AWS vulnerability disclosure](https://aws.amazon.com/security/vulnerability-reporting/)
process.

For non-security bugs, please use the GitHub issue tracker.

---

## CLA and licensing

This project ships under the
[MIT-0](https://opensource.org/licenses/MIT-0) license, the standard
license for AWS Samples repositories. By opening a pull request, you
agree that your contribution is licensed under the same terms.

AWS Samples requires a one-time signed CLA for substantive external
contributions. Trivial fixes (typos, one-line bug fixes, documentation
improvements) do not require a CLA. For anything larger, follow the
[AWS CLA process](https://github.com/aws/aws-cla) — the bot will comment
on your first PR with instructions.

Thanks for contributing.

---

## Conclusion

Thank you for your interest in improving this sample. Whether you are fixing a typo, adding a new agent, or hardening a Custom Resource, your contribution helps the community. If you have questions, open a GitHub issue and we will respond promptly.

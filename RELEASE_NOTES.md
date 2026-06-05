# M&A Due Diligence Multi-Agent Sample — Release Notes

## v1.0.0 — Initial public release

**Release date:** _pending tag_

First public release of the **M&A Due Diligence Multi-Agent** AWS
sample. This release accompanies the AWS Machine Learning blog post
_"Build a multi-agent M&A due-diligence assistant on Amazon Bedrock
AgentCore"_ (link will be added once the post is published).

### What ships in v1.0.0

**Multi-agent system (Strands SDK on AgentCore Runtime)**

- One supervisor agent using the agents-as-tools pattern.
- Four specialist agents — Target Screening, Financial Analysis,
  Strategic Fit, Compliance Validation.
- Claude Sonnet 4.5 as the default supervisor model, overridable via
  the `MNA_SUPERVISOR_MODEL` environment variable.
- Container image built in AWS CodeBuild (ARM64); readers do **not**
  need Docker or WSL on their machine.

**Grounding and memory**

- Amazon Bedrock Knowledge Base over a synthetic S3-backed document
  corpus (CIMs, financial summaries, press packs, governance
  checklist), indexed into Aurora pgvector.
- Text-to-SQL tool that translates natural language into read-only
  PostgreSQL queries against an Amazon Aurora Serverless v2 target-company
  schema (20+ fictional transportation and logistics companies).
- AgentCore Memory with `session_<id>` (short-term) and `prior_deals`
  (long-term) namespaces, seeded with three prior-deal memos.

**Tools and evaluation**

- One external tool (synthetic market-data comparable multiples)
  routed via an AgentCore Gateway MCP target to an AWS Lambda function.
- One custom evaluator Lambda enforcing citation validation on every
  factual claim.
- Amazon Bedrock Guardrail on the supervisor for harmful-content filtering
  and a financial-advice denial topic.

**Infrastructure-as-Code (AWS CDK v2, Python)**

- Five stacks: `NetworkStack`, `DataStack`, `EvaluatorStack`,
  `GatewayStack`, `AgentStack`, deployed in dependency order.
- Six to eight Custom Resources (depending on CDK-native AgentCore
  support at synth time), all backed by the shared
  `lambda/_cr_common/send_response.py` base module with guaranteed
  CFN responses, 14-minute polling caps, and delete-of-missing
  idempotency.
- Aurora in private isolated subnets with IAM database
  authentication. Every agent-callable IAM permission scoped to
  resource ARNs.
- Observability via CloudWatch Logs (JSON-formatted) and X-Ray
  tracing on every invocation.

**Reader surfaces**

- `notebooks/walkthrough.ipynb` with cells for environment
  validation, data overview, one scenario per specialist, and
  trace inspection.
- `python -m cli.invoke` with `invoke`, `list-agents`, `trace`, and
  `evaluate` subcommands.
- `prompts.md` with four ready-to-run prompts (one per specialist),
  each annotated with expected citation sources and the capability
  it demonstrates.

**Operations**

- One-command deploy (`./deploy.sh` or `.\deploy.ps1`) and cleanup
  (`./cleanup.sh` or `.\cleanup.ps1`).
- Post-deploy smoke test (`tests/smoke_test.py`) that exercises every
  specialist and the Gateway path.
- Preflight checks for AgentCore region GA and Amazon Bedrock model access.

**Documentation**

- Full `README.md` with architecture, prerequisites, deploy/run/
  cleanup, cost table, IAM summary, troubleshooting, and an
  implemented-vs-extension map to the blog's Figure 1.
- `CONTRIBUTING.md` with recipes for adding new agents and tools plus
  a Custom Resource review checklist.
- `docs/requirements_coverage.md` — the traceability audit produced
  as part of the final review pass.
- Every specialist source file carries a module docstring describing
  role, tools, and an example prompt.

### Cost profile

A full deploy → run four prompts → cleanup cycle (~1 hour of wall
time) should cost **under $5 USD** in a supported region. The
detailed breakdown is in the `README.md` Cost section. Aurora
Serverless v2 is the dominant line item; cleanup is the primary
cost control and is heavily emphasized in the README.

### Known limitations (out of scope for v1)

These are explicitly deferred and documented in both
`requirements.md` ("Out of Scope (v1)") and the README
"Implemented vs. extension" table:

- No React frontend, CloudFront, or Amazon Cognito. Pair this
  sample with the [FAST template](https://github.com/awslabs/fast)
  when a user-facing web surface is needed.
- No AgentCore Identity with JWT validation flows.
- No Cedar policies.
- No customer-managed KMS keys, no PrivateLink everywhere, no AWS
  Config rules. Aurora is placed in a VPC with private subnets but
  additional network-isolation controls are deferred.
- No multi-region deployment, no HA, no production-grade CI/CD.
- Only one Gateway target (the market-data Lambda). Additional
  targets (HTTP APIs, MCP servers, third-party SaaS) are left as
  extension hooks.
- No AgentCore Evaluations service integration, no LLM-as-judge, no
  continuous monitoring. The sample ships one custom evaluator
  (citation check) that demonstrates the pattern.

### Upgrade / migration notes

This is the initial release. There are no prior versions to migrate
from and no breaking changes to flag. Readers deploying v1.0.0
should follow the quick start in `README.md`.

### Companion blog post

This sample accompanies an AWS Machine Learning blog post that
walks through the architecture, the agent orchestration pattern,
and the reader journey:

- **Blog post:** _"Build a multi-agent M&A due-diligence assistant
  on Amazon Bedrock AgentCore"_ — link will be added here once the
  post is published.

The README's "Documentation and references" section is the
canonical place to find the blog link after publication.

### Supported regions

`us-east-1`, `us-west-2`, `ap-southeast-2`, `eu-central-1`.
Enforced at deploy time by `scripts/check_region.sh` / `.ps1`.

### Verification performed for the release

All three checks on `main` at the v1.0.0 tip:

| Check | Command | Result |
|---|---|---|
| Python lint | `ruff check .` | 0 findings |
| Unit tests | `python -m pytest tests/unit/` | 330 passed in ~32 s |
| Custom Resource safety | `python scripts/lint_cr_handlers.py` | 8 CR handler files scanned, clean |

The sample-level acceptance criteria (prompts cited, evaluator
reports pass/fail, Gateway trace segment, deploy/cleanup under 30
minutes) are exercised by `tests/smoke_test.py` against a deployed
stack. See `docs/requirements_coverage.md` for the full
traceability matrix.

### Tagging procedure for maintainers

The orchestrator that produced this release does not have commit
access; tagging is performed by the repository maintainer. To cut
the v1.0.0 tag from a clean `main`:

```bash
# From a clean main with the release commit at HEAD
git status                                  # verify clean working tree
git log -1 --oneline                        # note the commit to tag
git tag -a v1.0.0 -m "M&A Due Diligence Multi-Agent sample v1.0.0"
git push origin v1.0.0
```

On GitHub, create the release from the tag and paste the
"v1.0.0 — Initial public release" section above into the release
description. Attach the companion blog post link once it is
published.

---

## Conclusion

This initial release provides a complete, deployable reference for the supervisor-plus-specialists pattern on Amazon Bedrock AgentCore. We welcome feedback via GitHub issues and contributions following `CONTRIBUTING.md`. Thank you for trying the sample.

# Example Prompts

Copy-paste prompts that drive the walkthrough notebook and the post-deploy
smoke test. Each prompt exercises a single specialist agent end-to-end,
including its primary tool and citation shape, against the synthetic
dataset seeded by `data/generate.py --seed-all`. The expected agent,
citation sources, and demonstration points are listed alongside each
prompt so a reader can tell at a glance what they should see when it
runs.

Run a prompt from the CLI (after deploy completes):

```bash
python -m mna invoke supervisor "<prompt text here>"
# or to target a specialist directly:
python -m mna invoke financial_analysis "<prompt text here>"
```

Run the same prompts from the notebook by replacing the empty string in
cells 3–6 of `notebooks/walkthrough.ipynb`.

All four prompts are backed by the synthetic companies in
`mna.target_companies`, the CIMs and financial statements under
`s3://<docs-bucket>/cims|financials|press|governance/`, and the
prior-deal memos in the AgentCore Memory `prior_deals` namespace.

---

## 1. Target Screening

**Prompt:**

> Screen the target pipeline for transportation companies with revenue
> between $100M and $500M, EBITDA margin above 12%, and fleet size
> above 200. Surface the top three and tell me what the CIM says about
> the leader's growth trajectory.

**Expected agent:** `target_screening`

**Expected citation sources:**

- Structured-data audit trail: the generated SQL (a `SELECT` statement
  against `mna.target_companies`) echoed in the response and the X-Ray
  trace — the rows themselves are the audit trail and do not require
  KB citations.
- Narrative enrichment: `s3://<docs-bucket>/cims/<spotlight_slug>.md`
  (or `.pdf`) for the leader, cited inline as
  `[source: s3://.../cims/<slug>.md, p. N]`.

**What this demonstrates:** Text-to-SQL over AWS Aurora PostgreSQL
via the RDS Data API (Requirement 2a), SELECT-only SQL safety, and
Bedrock Knowledge Base retrieval used as enrichment (Requirement 2.1,
2.2).

---

## 2. Financial Analysis

**Prompt:**

> Run a DCF on Example Corp using the CIM in the knowledge base.
> Flag any management projection that diverges from historical
> performance by more than 20%, and pull comparable multiples for
> transportation-logistics mid-market.

**Expected agent:** `financial_analysis`

**Expected citation sources:**

- `s3://<docs-bucket>/cims/example_corp.md` — trailing revenue, EBITDA
  margin, historical CAGR, management projections.
- `s3://<docs-bucket>/financials/example_corp_statements.md` — income
  statement and cash flow summary feeding the DCF.
- `(synthetic: market_data)` — comparable-company multiples returned by
  the AgentCore Gateway-backed Lambda, labelled synthetic inline.

**What this demonstrates:** Multi-source grounding (KB + external tool
via AgentCore Gateway), inline citations on every numeric input
(Requirement 1.6), synthetic-data labelling on market inputs
(Requirement 5.3), and Gateway-backed tool invocation showing up in
the X-Ray trace (Sample-level AC 4).

---

## 3. Strategic Fit

**Prompt:**

> Compare Example Corp's integration profile against our three most
> recent completed acquisitions. Identify the top three integration
> risks and cite the source memos.

**Expected agent:** `strategic_fit`

**Expected citation sources:**

- `memory:prior_deals/prior_deal_northwind_2022` — revenue-synergy
  case with overrun.
- `memory:prior_deals/prior_deal_pinnacle_2021` — cost-synergy case
  with smooth integration.
- `memory:prior_deals/prior_deal_bastion_2020` — failed-integration
  case ending in writedown.
- `s3://<docs-bucket>/cims/example_corp.md` — target-profile context
  (customer concentration, service-line mix) used to benchmark against
  each memo.

**What this demonstrates:** AgentCore Memory long-term retrieval from
the `prior_deals` namespace (Requirement 2.3, 2.4), namespace isolation
(the agent only reads `prior_deals`, never another session's data),
and combined grounding across memory and the KB.

---

## 4. Compliance Validation

**Prompt:**

> Review the Example Corp analysis in this session for completeness
> against our M&A governance checklist. List any claims without source
> citations.

**Expected agent:** `compliance_validation`

**Expected citation sources:**

- `s3://<docs-bucket>/governance/ma_checklist.md` — the ten-item M&A
  governance checklist, cited on every compliance-table row.
- The citation-check evaluator Lambda's structured result
  (`{passed, unsupported_claims, total_claims}`) attached to the
  response envelope for downstream auditability.

**What this demonstrates:** End-to-end safety and evaluation
(Requirement 4): Bedrock Guardrail on the supervisor, the custom
citation-check evaluator producing a pass/fail result with per-claim
detail (Requirement 4.2, 4.3), and the evaluator result stored
alongside the agent response (Requirement 4.4). Running this prompt
after the first three exercises the full audit loop the sample is
designed to demonstrate.

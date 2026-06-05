"""Synthetic data generator for the M&A Due Diligence sample.

Three subcommands plus a ``--seed-all`` orchestrator wire together the
three data stores the sample exercises:

* ``companies``  — 25 fictional transportation/logistics targets inserted
  into Amazon Aurora PostgreSQL via the RDS Data API.
* ``documents``  — CIM, financial statements, and press-release packs for
  the three spotlight companies, uploaded to S3 and indexed into the
  Amazon Bedrock Knowledge Bases.
* ``memory``     — 3 prior-deal memos written to AgentCore Memory under
  the ``prior_deals`` namespace for the Strategic Fit specialist to read.

Design references:
  .kiro/specs/ma-due-diligence-agentcore/design.md
    §"Synthetic Data Generation"
    §"Repository Layout" → ``data/generate.py``
    §"S3 Layout"

Operational guarantees:

* **Deterministic** — every sampled number uses ``random.Random(seed=42)``
  so a re-run of ``companies`` produces the same rows.
* **Idempotent** — ``companies`` uses ``INSERT ... ON CONFLICT DO UPDATE``,
  ``documents`` uses fixed S3 keys (``put_object`` overwrites), and
  ``memory`` stamps each memo with a deterministic ``memo_id`` so callers
  can skip or upsert on re-run.
* **Clearly synthetic** — every generated document includes
  ``SYNTHETIC DATA - NOT REAL`` in the header and memo bodies lead with
  the same banner.
* **No heavy deps at import time** — boto3, reportlab, and
  ``mna.tools.memory`` are imported lazily inside the subcommand
  handlers so ``python data/generate.py --help`` works on a fresh clone
  without the full requirements installed.

Usage::

    python data/generate.py companies
    python data/generate.py documents
    python data/generate.py memory
    python data/generate.py --seed-all
    python data/generate.py companies --dry-run
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from botocore.client import BaseClient

logger = logging.getLogger("mna.generate")


# ---------------------------------------------------------------------------
# Constants: deterministic company inputs
# ---------------------------------------------------------------------------


#: Deterministic RNG seed — all numeric attributes derive from this.
DEFAULT_SEED = 42

#: Hand-rolled name list so we don't pull ``faker`` into requirements.
#: Ordered alphabetically and sized at 25 so ``companies`` produces
#: more than the Requirement 5.2 minimum of 20 rows.
COMPANY_NAMES: tuple[str, ...] = (
    "Example Corp",
    "Example Anchor Freight",
    "AnyCompany Freight",
    "Cascade Transport",
    "Continental Drayage",
    "Example Haulage",
    "Everglade Express",
    "Frontier Pacific Carriers",
    "Granite State Logistics",
    "Harbor Point Shipping",
    "Ironclad Trucking",
    "Juniper Fleet Services",
    "Keystone Intermodal",
    "Lighthouse Freight Systems",
    "Meridian Cargo Group",
    "Northwind Distribution",
    "Overland Pacific",
    "Pinnacle Carriers",
    "Quicksilver Haul",
    "Redwood Freight Lines",
    "Summit Cold Chain",
    "Tideway Bulk Transport",
    "Unity Last Mile",
    "Vanguard Ocean Lines",
    "Westgate Rail & Road",
)

#: Plausible HQ regions for transportation/logistics operators in NA.
HEADQUARTERS_REGIONS: tuple[str, ...] = (
    "US-Northeast",
    "US-Southeast",
    "US-Midwest",
    "US-Southwest",
    "US-Pacific",
    "US-Mountain",
    "Canada-East",
    "Canada-West",
    "Mexico-North",
)

#: Service-line catalog. Each company picks 2–4 lines deterministically.
SERVICE_LINES: tuple[str, ...] = (
    "truckload",
    "less-than-truckload",
    "intermodal",
    "cold-chain",
    "last-mile",
    "drayage",
    "warehousing",
    "freight-brokerage",
    "ocean-freight",
    "rail",
)

#: The three "spotlight" targets that ``documents`` generates for.
#: Slugs are derived from the canonical company id (below) so the S3
#: paths match the design.
SPOTLIGHT_COMPANY_NAMES: tuple[str, ...] = (
    "Example Corp",
    "AnyCompany Freight",
    "Cascade Transport",
)


# ---------------------------------------------------------------------------
# Constants: prior-deal memory seeds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriorDealSeed:
    """Structured input for a prior-deal memo.

    Keeps the human-written thesis separate from the ~500-word
    narrative body so we can template the body via Amazon Bedrock when it is
    available and fall back to a static template otherwise.
    """

    memo_id: str
    target_name: str
    close_year: int
    deal_size_usd: int
    synergy_type: str
    outcome: str
    headline_lesson: str
    integration_duration_months: int


#: Three prior deals with distinct outcomes so the Strategic Fit agent
#: can benchmark both successes and failures.
PRIOR_DEALS: tuple[PriorDealSeed, ...] = (
    PriorDealSeed(
        memo_id="prior_deal_example_express_2022",
        target_name="Northwind Express",
        close_year=2022,
        deal_size_usd=420_000_000,
        synergy_type="revenue synergy",
        outcome="integration overran by 8 months; thesis partially realized",
        headline_lesson=(
            "Customer-overlap assumptions underestimated churn at the top-5 "
            "accounts; protect-and-grow playbooks must ship in the first 90 days."
        ),
        integration_duration_months=20,
    ),
    PriorDealSeed(
        memo_id="prior_deal_anycompany_freight_2021",
        target_name="Pinnacle Freight",
        close_year=2021,
        deal_size_usd=180_000_000,
        synergy_type="cost synergy",
        outcome="smooth integration; synergy targets exceeded by 12 percent",
        headline_lesson=(
            "Terminal-network rationalisation started before day-one close and "
            "delivered 80 percent of the run-rate savings by month nine."
        ),
        integration_duration_months=10,
    ),
    PriorDealSeed(
        memo_id="prior_deal_example_cargo_2020",
        target_name="Bastion Cargo",
        close_year=2020,
        deal_size_usd=265_000_000,
        synergy_type="capability acquisition",
        outcome="failed integration; goodwill writedown in year two",
        headline_lesson=(
            "Legacy TMS consolidation was treated as a post-close workstream; "
            "the IT integration failure cascaded into customer losses."
        ),
        integration_duration_months=28,
    ),
)


# ---------------------------------------------------------------------------
# Small helpers shared across subcommands
# ---------------------------------------------------------------------------


#: Banner inserted into every generated document and memo body.
SYNTHETIC_BANNER = "SYNTHETIC DATA - NOT REAL"


def _slug(name: str) -> str:
    """Convert a display name like ``"Example Corp"`` to ``"acme_logistics"``."""

    cleaned = [c.lower() if c.isalnum() else "_" for c in name]
    text = "".join(cleaned)
    # Collapse runs of underscores.
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def _company_id(name: str) -> str:
    """Primary key used in Aurora and as the S3 path prefix."""

    return _slug(name)


def _default_region() -> str | None:
    import os

    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompanyRow:
    """One row of the ``mna.target_companies`` table."""

    company_id: str
    legal_name: str
    headquarters_region: str
    revenue_usd: float
    ebitda_margin_pct: float
    fleet_size: int
    employee_count: int
    customer_concentration_top1_pct: float
    service_lines: list[str] = field(default_factory=list)


def generate_companies(seed: int = DEFAULT_SEED) -> list[CompanyRow]:
    """Generate the deterministic company roster.

    Every numeric attribute is sampled from a fresh ``random.Random``
    seeded with ``seed``, so two runs with the same seed produce
    byte-identical output. 25 rows exceed the Requirement 5.2 minimum
    of 20.
    """

    rng = random.Random(seed)
    rows: list[CompanyRow] = []
    for name in COMPANY_NAMES:
        # Revenue band: $50M–$800M
        revenue = round(rng.uniform(50_000_000, 800_000_000), 2)
        # EBITDA margin: 8%–16%
        ebitda_margin = round(rng.uniform(8.0, 16.0), 2)
        # Fleet size: 50–500 units
        fleet = rng.randint(50, 500)
        # Employees: roughly 1.8x fleet, with noise.
        employees = max(25, int(fleet * rng.uniform(1.5, 2.4)))
        # Top-1 customer concentration: 5%–35%
        top1 = round(rng.uniform(5.0, 35.0), 2)

        # 2–4 service lines, distinct and order-stable.
        k = rng.randint(2, 4)
        lines = sorted(rng.sample(SERVICE_LINES, k))

        rows.append(
            CompanyRow(
                company_id=_company_id(name),
                legal_name=name,
                headquarters_region=rng.choice(HEADQUARTERS_REGIONS),
                revenue_usd=revenue,
                ebitda_margin_pct=ebitda_margin,
                fleet_size=fleet,
                employee_count=employees,
                customer_concentration_top1_pct=top1,
                service_lines=lines,
            )
        )
    return rows


_UPSERT_SQL = """
INSERT INTO mna.target_companies (
    company_id,
    legal_name,
    headquarters_region,
    revenue_usd,
    ebitda_margin_pct,
    fleet_size,
    employee_count,
    customer_concentration_top1_pct,
    service_lines,
    last_updated
) VALUES (
    :company_id,
    :legal_name,
    :headquarters_region,
    :revenue_usd,
    :ebitda_margin_pct,
    :fleet_size,
    :employee_count,
    :customer_concentration_top1_pct,
    CAST(:service_lines AS TEXT[]),
    NOW()
)
ON CONFLICT (company_id) DO UPDATE SET
    legal_name                      = EXCLUDED.legal_name,
    headquarters_region             = EXCLUDED.headquarters_region,
    revenue_usd                     = EXCLUDED.revenue_usd,
    ebitda_margin_pct               = EXCLUDED.ebitda_margin_pct,
    fleet_size                      = EXCLUDED.fleet_size,
    employee_count                  = EXCLUDED.employee_count,
    customer_concentration_top1_pct = EXCLUDED.customer_concentration_top1_pct,
    service_lines                   = EXCLUDED.service_lines,
    last_updated                    = NOW()
""".strip()


def _row_to_parameters(row: CompanyRow) -> list[dict[str, Any]]:
    """Convert a :class:`CompanyRow` into RDS Data API ``SqlParameter`` dicts.

    PostgreSQL ``TEXT[]`` is serialised as a literal ``{a,b,c}`` string
    and cast with ``CAST(:service_lines AS TEXT[])`` in the statement —
    the RDS Data API doesn't accept ``arrayValue`` parameters on
    PostgreSQL engines as of this writing.
    """

    service_lines_literal = "{" + ",".join(row.service_lines) + "}"
    return [
        {"name": "company_id", "value": {"stringValue": row.company_id}},
        {"name": "legal_name", "value": {"stringValue": row.legal_name}},
        {"name": "headquarters_region", "value": {"stringValue": row.headquarters_region}},
        {"name": "revenue_usd", "value": {"doubleValue": float(row.revenue_usd)}},
        {"name": "ebitda_margin_pct", "value": {"doubleValue": float(row.ebitda_margin_pct)}},
        {"name": "fleet_size", "value": {"longValue": int(row.fleet_size)}},
        {"name": "employee_count", "value": {"longValue": int(row.employee_count)}},
        {
            "name": "customer_concentration_top1_pct",
            "value": {"doubleValue": float(row.customer_concentration_top1_pct)},
        },
        {"name": "service_lines", "value": {"stringValue": service_lines_literal}},
    ]


def insert_companies(
    rows: list[CompanyRow],
    *,
    cluster_arn: str,
    secret_arn: str,
    database: str = "mna",
    rds_data_client: BaseClient | None = None,
    region_name: str | None = None,
) -> int:
    """Insert or upsert every row via the RDS Data API.

    Uses ``batch_execute_statement`` so a full re-seed is one round
    trip per batch of 25. Returns the number of rows submitted.
    """

    client = rds_data_client or _build_rds_data_client(region_name=region_name)

    parameter_sets = [_row_to_parameters(r) for r in rows]

    client.batch_execute_statement(
        resourceArn=cluster_arn,
        secretArn=secret_arn,
        database=database,
        sql=_UPSERT_SQL,
        parameterSets=parameter_sets,
    )
    logger.info("companies_inserted count=%d", len(rows))
    return len(rows)


def _build_rds_data_client(region_name: str | None = None) -> BaseClient:
    """Construct a boto3 RDS Data API client (lazy import)."""

    import boto3

    kwargs: dict[str, Any] = {}
    resolved = region_name or _default_region()
    if resolved:
        kwargs["region_name"] = resolved
    return boto3.client("rds-data", **kwargs)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


#: Amazon Bedrock model used to fill in document narrative when available.
#: Haiku is cheap and fast; if Amazon Bedrock is unreachable the static
#: template still produces a valid document.
DOCUMENT_MODEL_ID = "anthropic.claude-3-5-haiku-20241022-v1:0"


#: Governance checklist markdown — a small static document so the
#: Compliance Validation agent always has something to cite.
GOVERNANCE_CHECKLIST = f"""# M&A Governance Checklist

> {SYNTHETIC_BANNER}

This checklist is applied by the Compliance Validation specialist to
every analyst response before it leaves the due diligence workspace.

1. **Target identity** — legal name, jurisdiction, and ticker (if public)
   are stated and match the CIM.
2. **Revenue basis** — trailing twelve-month revenue is cited to the CIM
   or to the financial statements summary.
3. **EBITDA adjustments** — any management adjustment to EBITDA is
   enumerated with its rationale and source page.
4. **Customer concentration** — top-1 and top-5 customer concentration
   percentages are cited, and any customer representing more than 20%
   of revenue is named.
5. **Fleet and workforce** — fleet size and headcount figures are cited
   to the CIM.
6. **Valuation approach** — both DCF and comparable-company outputs are
   reported, with the implied enterprise value range.
7. **Stretch assumptions** — every management projection that diverges
   from trailing three-year history by more than 20% is flagged.
8. **Prior-deal benchmarking** — at least one comparable prior deal is
   referenced with its integration outcome.
9. **Integration risks** — the top three integration risks are listed
   with a mitigation for each.
10. **Citations** — every factual claim (numeric value, quoted passage,
    named entity) is backed by at least one inline citation resolvable
    to an S3 object in the knowledge base or to a prior-deal memo id.
"""


def _bedrock_fill(prompt: str, *, region_name: str | None = None) -> str | None:
    """Ask Bedrock to expand a prompt; return ``None`` if unavailable.

    The document generator templates work fine without Bedrock — this
    just lets the content read more naturally when the caller has
    model access configured. Any failure (missing model access,
    Bedrock import error, network error) degrades to ``None`` and the
    static template is used.
    """

    try:
        import boto3
    except Exception:  # pragma: no cover - boto3 is pinned, but be defensive.
        return None

    kwargs: dict[str, Any] = {}
    resolved = region_name or _default_region()
    if resolved:
        kwargs["region_name"] = resolved

    try:
        client = boto3.client("bedrock-runtime", **kwargs)
        response = client.converse(
            modelId=DOCUMENT_MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 700, "temperature": 0.2},
        )
    except Exception as exc:
        logger.info("bedrock_fill_skipped reason=%s", type(exc).__name__)
        return None

    content = (
        (response or {})
        .get("output", {})
        .get("message", {})
        .get("content")
    )
    if not isinstance(content, list):
        return None
    for part in content:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def _cim_markdown(company: CompanyRow, *, region_name: str | None = None) -> str:
    """Render a CIM as markdown, optionally with Amazon Bedrock-expanded narrative."""

    bedrock_prompt = (
        "Write a 6-paragraph Confidential Information Memorandum section covering "
        f"company overview, growth strategy, and risks for a fictional transportation "
        f"and logistics operator named {company.legal_name}. The company reports "
        f"trailing revenue of ${company.revenue_usd:,.0f}, an EBITDA margin of "
        f"{company.ebitda_margin_pct:.1f}%, a fleet of {company.fleet_size} power units, "
        f"and is headquartered in {company.headquarters_region}. Service lines: "
        f"{', '.join(company.service_lines)}. Label the content as synthetic. "
        "Do not mention any real company. Output plain prose only."
    )
    narrative = _bedrock_fill(bedrock_prompt, region_name=region_name) or (
        f"{company.legal_name} is a fictional {company.headquarters_region} "
        f"transportation and logistics operator covering "
        f"{', '.join(company.service_lines)}. Trailing revenue is "
        f"${company.revenue_usd:,.0f} with an EBITDA margin of "
        f"{company.ebitda_margin_pct:.1f}%. The company operates a fleet of "
        f"{company.fleet_size} power units and employs approximately "
        f"{company.employee_count} people. Growth plans emphasise cross-sell "
        f"into existing accounts, selective terminal expansion, and continued "
        f"investment in driver retention programs. Key risks include customer "
        f"concentration (top-1 customer represents "
        f"{company.customer_concentration_top1_pct:.1f}% of revenue), fuel "
        f"price exposure, and regulatory changes affecting driver hours."
    )

    return f"""# Confidential Information Memorandum — {company.legal_name}

> {SYNTHETIC_BANNER}

## Executive Summary

| Metric | Value |
| --- | --- |
| Legal name | {company.legal_name} |
| Headquarters | {company.headquarters_region} |
| Trailing revenue (USD) | ${company.revenue_usd:,.0f} |
| EBITDA margin | {company.ebitda_margin_pct:.1f}% |
| Fleet size | {company.fleet_size} |
| Employees | {company.employee_count} |
| Top-1 customer concentration | {company.customer_concentration_top1_pct:.1f}% |
| Service lines | {", ".join(company.service_lines)} |

## Company Overview

{narrative}

## Financial Highlights

Revenue has grown at an implied mid-single-digit CAGR over the trailing
three-year period. EBITDA conversion is in line with the mid-market
transportation peer set. Working capital is seasonally concentrated in
Q4 owing to retail peak volumes.

## Growth Strategy

Management's plan centers on three pillars: (1) deepening wallet share
with existing top-20 accounts, (2) selective terminal expansion in the
adjacent {company.headquarters_region} corridor, and (3) targeted
acquisitions of sub-scale regional carriers to fill network gaps.

## Risks and Mitigations

* Customer concentration at {company.customer_concentration_top1_pct:.1f}% of
  revenue with the top-1 account.
* Driver availability and wage inflation remain structural headwinds.
* Fuel price exposure is partially hedged via customer surcharge
  programs.

## Management Team

A synthetic leadership team with ~20 years of combined industry
experience leads the business.
"""


def _financials_markdown(company: CompanyRow) -> str:
    """Render a minimalist financial statements summary."""

    ebitda = company.revenue_usd * (company.ebitda_margin_pct / 100.0)
    return f"""# Financial Statements Summary — {company.legal_name}

> {SYNTHETIC_BANNER}

## Income Statement (Trailing Twelve Months)

| Line | USD |
| --- | --- |
| Revenue | ${company.revenue_usd:,.0f} |
| Operating expenses | ${company.revenue_usd - ebitda:,.0f} |
| EBITDA | ${ebitda:,.0f} |
| EBITDA margin | {company.ebitda_margin_pct:.1f}% |

## Balance Sheet Highlights

Working capital dynamics are seasonally concentrated in Q4. Rolling
stock is the largest balance-sheet item; financing mix is
approximately 60% operating leases and 40% owned.

## Cash Flow Summary

Operating cash conversion is consistent with mid-market peers.
Capital expenditure runs at approximately 4-6% of revenue, reflecting
steady fleet replacement cadence.
"""


def _press_pack_markdown(company: CompanyRow) -> str:
    """Render a simple press release pack."""

    return f"""# Press Release Pack — {company.legal_name}

> {SYNTHETIC_BANNER}

## Announcement 1 — Terminal Expansion

{company.legal_name} announced the opening of a new terminal in its
{company.headquarters_region} footprint to support continued growth in
{company.service_lines[0]} volumes.

## Announcement 2 — Sustainability Milestone

{company.legal_name} reported a 12% reduction in diesel usage per load
year-over-year, driven by route optimisation investments.

## Announcement 3 — Customer Win

{company.legal_name} secured a multi-year contract with a Fortune 500
retailer covering dedicated capacity across {company.headquarters_region}.
"""


def _markdown_to_pdf_bytes(markdown_text: str) -> bytes | None:
    """Render markdown into a PDF using reportlab; return ``None`` if unavailable.

    Reportlab ships wheels for every supported platform; the ``None``
    path is just defensive so an editable install without the package
    doesn't crash the whole subcommand.
    """

    try:
        from io import BytesIO

        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except Exception as exc:
        logger.info("pdf_render_skipped reason=%s", type(exc).__name__)
        return None

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, title="Synthetic Document")
    styles = getSampleStyleSheet()
    body_style = styles["BodyText"]
    heading_style = styles["Heading1"]

    story: list[Any] = []
    for line in markdown_text.splitlines():
        stripped = line.rstrip()
        if not stripped:
            story.append(Spacer(1, 8))
            continue
        if stripped.startswith("# "):
            story.append(Paragraph(stripped[2:], heading_style))
        elif stripped.startswith("## "):
            story.append(Paragraph(stripped[3:], styles["Heading2"]))
        elif stripped.startswith("### "):
            story.append(Paragraph(stripped[4:], styles["Heading3"]))
        else:
            # Escape the handful of characters reportlab's mini-markup
            # treats specially so raw tables render as-is.
            safe = stripped.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(safe, body_style))

    doc.build(story)
    return buffer.getvalue()


@dataclass(frozen=True)
class DocumentArtifact:
    """A single generated document ready for upload."""

    s3_key: str
    content_type: str
    body: bytes


def build_company_documents(
    company: CompanyRow, *, region_name: str | None = None
) -> list[DocumentArtifact]:
    """Build the three document artifacts for a spotlight company.

    Each company produces a markdown CIM, a PDF CIM (when reportlab is
    available), a markdown + PDF financial statements summary, and a
    markdown press release pack. The markdown variants are uploaded in
    every case so the KB always has something to ingest; the PDFs are
    added when reportlab is importable.
    """

    artifacts: list[DocumentArtifact] = []
    slug = _company_id(company.legal_name)

    cim_md = _cim_markdown(company, region_name=region_name)
    financials_md = _financials_markdown(company)
    press_md = _press_pack_markdown(company)

    artifacts.append(
        DocumentArtifact(
            s3_key=f"cims/{slug}.md",
            content_type="text/markdown",
            body=cim_md.encode("utf-8"),
        )
    )
    cim_pdf = _markdown_to_pdf_bytes(cim_md)
    if cim_pdf is not None:
        artifacts.append(
            DocumentArtifact(
                s3_key=f"cims/{slug}.pdf",
                content_type="application/pdf",
                body=cim_pdf,
            )
        )

    artifacts.append(
        DocumentArtifact(
            s3_key=f"financials/{slug}_statements.md",
            content_type="text/markdown",
            body=financials_md.encode("utf-8"),
        )
    )
    financials_pdf = _markdown_to_pdf_bytes(financials_md)
    if financials_pdf is not None:
        artifacts.append(
            DocumentArtifact(
                s3_key=f"financials/{slug}_statements.pdf",
                content_type="application/pdf",
                body=financials_pdf,
            )
        )

    artifacts.append(
        DocumentArtifact(
            s3_key=f"press/{slug}_press_pack.md",
            content_type="text/markdown",
            body=press_md.encode("utf-8"),
        )
    )
    return artifacts


def upload_documents(
    artifacts: list[DocumentArtifact],
    *,
    bucket: str,
    s3_client: BaseClient | None = None,
    region_name: str | None = None,
) -> int:
    """Upload each artifact to S3, overwriting on re-run (idempotent)."""

    import boto3

    if s3_client is None:
        kwargs: dict[str, Any] = {}
        resolved = region_name or _default_region()
        if resolved:
            kwargs["region_name"] = resolved
        s3_client = boto3.client("s3", **kwargs)

    for artifact in artifacts:
        s3_client.put_object(
            Bucket=bucket,
            Key=artifact.s3_key,
            Body=artifact.body,
            ContentType=artifact.content_type,
        )
    logger.info("documents_uploaded bucket=%s count=%d", bucket, len(artifacts))
    return len(artifacts)


def _resolve_data_source_id(
    kb_id: str,
    *,
    region_name: str | None = None,
) -> str | None:
    """Return the first data-source id attached to ``kb_id``, or ``None``.

    Our DataStack creates exactly one S3 data source per KB, so the
    first entry in ``list_data_sources`` is the right one. If the KB
    has no data sources (shouldn't happen after a successful deploy)
    we return ``None`` and let the caller decide what to do.
    """

    import boto3  # noqa: PLC0415 - lazy import

    kwargs: dict[str, Any] = {}
    resolved_region = region_name or _default_region()
    if resolved_region:
        kwargs["region_name"] = resolved_region
    client = boto3.client("bedrock-agent", **kwargs)

    try:
        response = client.list_data_sources(knowledgeBaseId=kb_id)
        summaries = response.get("dataSourceSummaries") or []
        if summaries:
            return summaries[0].get("dataSourceId")
    except Exception as exc:
        logger.warning(
            "resolve_data_source_id_failed",
            extra={"kb_id": kb_id, "error_type": type(exc).__name__},
        )
    return None


def trigger_and_poll_ingestion(
    *,
    kb_id: str,
    data_source_id: str,
    bedrock_agent_client: BaseClient | None = None,
    region_name: str | None = None,
    poll_interval_seconds: int = 15,
    timeout_seconds: int = 14 * 60,
) -> str:
    """Start a KB ingestion job and poll until it reaches a terminal state.

    Terminal states: ``COMPLETE``, ``FAILED``, ``STOPPED``. Returns the
    final status. Caps wall-clock time at 14 minutes so this helper is
    safe to call from a Lambda as well as from the CLI.
    """

    import boto3

    if bedrock_agent_client is None:
        kwargs: dict[str, Any] = {}
        resolved = region_name or _default_region()
        if resolved:
            kwargs["region_name"] = resolved
        bedrock_agent_client = boto3.client("bedrock-agent", **kwargs)

    response = bedrock_agent_client.start_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=data_source_id
    )
    job_id = (response.get("ingestionJob") or {}).get("ingestionJobId")
    if not job_id:
        raise RuntimeError("StartIngestionJob did not return an ingestionJobId")

    logger.info("ingestion_started kb=%s job=%s", kb_id, job_id)

    deadline = time.monotonic() + timeout_seconds
    terminal_statuses = {"COMPLETE", "FAILED", "STOPPED"}
    while True:
        status_response = bedrock_agent_client.get_ingestion_job(
            knowledgeBaseId=kb_id,
            dataSourceId=data_source_id,
            ingestionJobId=job_id,
        )
        status = (status_response.get("ingestionJob") or {}).get("status", "UNKNOWN")
        if status in terminal_statuses:
            logger.info("ingestion_finished kb=%s job=%s status=%s", kb_id, job_id, status)
            return status
        if time.monotonic() >= deadline:
            logger.warning("ingestion_timed_out kb=%s job=%s", kb_id, job_id)
            return "TIMEOUT"
        time.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def _memo_body(deal: PriorDealSeed, *, region_name: str | None = None) -> str:
    """Render a ~500-word prior-deal memo body.

    Amazon Bedrock is used to expand the narrative when available; the static
    template is detailed enough to stand alone when it is not.
    """

    bedrock_prompt = (
        "Write a 500-word M&A post-close review memo for a synthetic past deal. "
        f"Target: {deal.target_name}, closed in {deal.close_year}, "
        f"deal size USD {deal.deal_size_usd:,}. Synergy thesis: "
        f"{deal.synergy_type}. Outcome: {deal.outcome}. Headline lesson: "
        f"{deal.headline_lesson} Integration duration: "
        f"{deal.integration_duration_months} months. "
        "Cover: strategic rationale, integration plan, synergy realisation, "
        "what went well, what went poorly, and three lessons learned. "
        f"Open the memo with the literal banner '{SYNTHETIC_BANNER}' on its own line. "
        "Do not mention any real company. Output plain prose."
    )
    dynamic = _bedrock_fill(bedrock_prompt, region_name=region_name)
    if dynamic and SYNTHETIC_BANNER in dynamic:
        return dynamic

    static = f"""{SYNTHETIC_BANNER}

Prior-Deal Memo — {deal.target_name} ({deal.close_year})

Strategic rationale. The acquisition of {deal.target_name} was framed as
a {deal.synergy_type} play. At signing, the target generated comparable
scale in adjacent lanes, and the deal thesis assumed that a combined
network would deliver density benefits within eighteen months of close.
Deal size was approximately USD {deal.deal_size_usd:,}.

Integration plan. The post-close plan was structured around three
workstreams: network integration, customer protect-and-grow, and
back-office consolidation. A joint integration management office was
stood up at signing and operated through month {deal.integration_duration_months}.
The network team owned terminal rationalisation and lane rebalancing;
the customer team owned top-account retention; the back-office team
owned finance, HR, and IT convergence.

Synergy realisation. {deal.outcome.capitalize()}. Run-rate savings were
tracked against a quarterly scorecard reviewed by the steering
committee. Revenue synergies lagged cost synergies, in line with the
historical pattern in this operator's acquisition portfolio.

What went well. The {deal.synergy_type} thesis was articulated clearly
to line operators inside the first thirty days, and the joint operating
rhythm created accountability for the run-rate targets. Terminal
rationalisation moved faster than baseline thanks to proactive
customer notification.

What went poorly. Downstream customer communications around routing
changes slipped in several weeks, which created short-term service
disruption in the top-10 accounts. Second-tier technology integration
(driver apps, yard management) took longer than budgeted and delayed
the single-pane operations dashboard.

Lessons learned.

1. Protect-and-grow playbooks must ship in the first ninety days, not
   the second hundred. Every week of delay at the top-5 accounts costs
   disproportionate revenue synergy.
2. Terminal network rationalisation benefits from parallel, not
   sequential, customer notification. Pre-close planning should produce
   a customer-by-customer notification calendar signed off by sales.
3. Back-office system integration timelines should be budgeted with
   explicit contingency for legacy TMS consolidation. Treating TMS as
   a post-close workstream repeatedly underestimates complexity.

Headline lesson. {deal.headline_lesson}

The full integration completed approximately month
{deal.integration_duration_months}. The memo is retained in
long-term memory for benchmarking purposes.
"""
    return static


def seed_memory(
    deals: tuple[PriorDealSeed, ...] = PRIOR_DEALS,
    *,
    memory_id: str,
    client: BaseClient | None = None,
    region_name: str | None = None,
) -> int:
    """Write each prior-deal memo into the ``prior_deals`` namespace.

    Delegates to :func:`mna.tools.memory.create_memory_record` so the
    namespace and content shape stay consistent with what the
    Strategic Fit agent reads back at runtime.
    """

    # Lazy import so ``python data/generate.py --help`` works on a
    # fresh clone without the ``mna`` package installed.
    from mna.tools.memory import PRIOR_DEALS_NAMESPACE, create_memory_record

    written = 0
    for deal in deals:
        body = _memo_body(deal, region_name=region_name)
        create_memory_record(
            PRIOR_DEALS_NAMESPACE,
            body,
            metadata={
                "memo_id": deal.memo_id,
                "target_name": deal.target_name,
                "close_year": deal.close_year,
                "deal_size_usd": deal.deal_size_usd,
                "synergy_type": deal.synergy_type,
                "synthetic": True,
            },
            memory_id=memory_id,
            client=client,
            region_name=region_name,
        )
        written += 1
        logger.info("memo_written memo_id=%s target=%s", deal.memo_id, deal.target_name)
    return written


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _load_mna_config(region_name: str | None):
    """Import ``mna.config`` lazily and resolve the shared configuration."""

    from mna.config import load_config

    return load_config(region_name=region_name)


def cmd_companies(args: argparse.Namespace) -> int:
    """Handler for ``python data/generate.py companies``."""

    rows = generate_companies(seed=args.seed)
    print(f"Generated {len(rows)} company rows (seed={args.seed}).")

    if args.dry_run:
        for row in rows[:5]:
            print(
                f"  - {row.company_id}: revenue=${row.revenue_usd:,.0f}, "
                f"ebitda={row.ebitda_margin_pct:.1f}%, "
                f"fleet={row.fleet_size}, region={row.headquarters_region}"
            )
        if len(rows) > 5:
            print(f"  ... plus {len(rows) - 5} more")
        print("Dry run: no rows were written to Aurora.")
        return 0

    cluster_arn = args.cluster_arn
    secret_arn = args.secret_arn
    if not cluster_arn or not secret_arn:
        # Fall back to SSM for whichever identifier the caller did not
        # pass explicitly. ``load_config`` reads both from ``/mna/*``
        # parameters populated by :class:`DataStack`.
        config = _load_mna_config(args.region)
        if not cluster_arn:
            cluster_arn = config.aurora_cluster_arn
        if not secret_arn:
            secret_arn = config.aurora_secret_arn
    if not secret_arn:
        raise SystemExit(
            "Could not resolve the Aurora secret ARN. Pass --secret-arn, "
            "set MNA_AURORA_SECRET_ARN, or confirm /mna/aurora/secret_arn "
            "is populated in SSM."
        )

    inserted = insert_companies(
        rows,
        cluster_arn=cluster_arn,
        secret_arn=secret_arn,
        database=args.database,
        region_name=args.region,
    )
    print(f"Upserted {inserted} rows into mna.target_companies.")
    return 0


def cmd_documents(args: argparse.Namespace) -> int:
    """Handler for ``python data/generate.py documents``."""

    rows = generate_companies(seed=args.seed)
    spotlight = [r for r in rows if r.legal_name in SPOTLIGHT_COMPANY_NAMES]
    if not spotlight:
        raise SystemExit(
            "No spotlight companies matched the generated roster — "
            "check SPOTLIGHT_COMPANY_NAMES / COMPANY_NAMES."
        )

    artifacts: list[DocumentArtifact] = []
    for row in spotlight:
        artifacts.extend(build_company_documents(row, region_name=args.region))

    # Governance checklist is a single fixed document used by the
    # Compliance Validation specialist.
    artifacts.append(
        DocumentArtifact(
            s3_key="governance/ma_checklist.md",
            content_type="text/markdown",
            body=GOVERNANCE_CHECKLIST.encode("utf-8"),
        )
    )

    print(
        f"Prepared {len(artifacts)} document artifacts for "
        f"{len(spotlight)} spotlight companies."
    )

    if args.dry_run:
        for artifact in artifacts:
            print(f"  - s3://{args.bucket or '<bucket>'}/{artifact.s3_key} ({len(artifact.body)} bytes)")
        print("Dry run: nothing uploaded and no ingestion triggered.")
        return 0

    bucket = args.bucket
    kb_id = args.kb_id
    if not bucket or not kb_id:
        cfg = _load_mna_config(args.region)
        bucket = bucket or cfg.docs_bucket
        kb_id = kb_id or cfg.kb_id

    upload_documents(artifacts, bucket=bucket, region_name=args.region)
    print(f"Uploaded {len(artifacts)} documents to s3://{bucket}/")

    if args.data_source_id:
        status = trigger_and_poll_ingestion(
            kb_id=kb_id,
            data_source_id=args.data_source_id,
            region_name=args.region,
        )
        print(f"KB ingestion job finished with status: {status}")
    else:
        # Auto-resolve the data source id when the caller didn't pass
        # one explicitly (the common case for ``--seed-all``). A KB
        # created by our DataStack has exactly one S3 data source.
        resolved_ds_id = _resolve_data_source_id(kb_id, region_name=args.region)
        if resolved_ds_id:
            status = trigger_and_poll_ingestion(
                kb_id=kb_id,
                data_source_id=resolved_ds_id,
                region_name=args.region,
            )
            print(f"KB ingestion job finished with status: {status}")
        else:
            print(
                "Could not resolve a data source id for the KB; "
                "skipping ingestion trigger. Pass --data-source-id "
                "to ingest documents on upload."
            )
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    """Handler for ``python data/generate.py memory``."""

    print(f"Prepared {len(PRIOR_DEALS)} prior-deal memos for the 'prior_deals' namespace.")
    if args.dry_run:
        for deal in PRIOR_DEALS:
            print(f"  - {deal.memo_id}: {deal.target_name} ({deal.close_year}) — {deal.outcome}")
        print("Dry run: nothing written to AgentCore Memory.")
        return 0

    if not args.memory_id:
        raise SystemExit(
            "--memory-id is required for the memory subcommand; "
            "supply the AgentCore Memory resource id."
        )

    written = seed_memory(memory_id=args.memory_id, region_name=args.region)
    print(f"Wrote {written} memos to AgentCore Memory (namespace: prior_deals).")
    return 0


def cmd_seed_all(args: argparse.Namespace) -> int:
    """Chain ``companies`` → ``documents`` → ``memory`` in order.

    Prints per-phase progress and a final summary so a reader running
    ``deploy.sh`` sees a clear audit trail. Re-running is idempotent
    because each subcommand upserts deterministic keys (company_id,
    S3 key, memo_id).
    """

    print("=== Seeding companies ===")
    rc = cmd_companies(args)
    if rc:
        return rc

    print("\n=== Seeding documents ===")
    rc = cmd_documents(args)
    if rc:
        return rc

    print("\n=== Seeding memory ===")
    rc = cmd_memory(args)
    if rc:
        return rc

    print("\n=== Seed summary ===")
    print(f"Companies: {len(COMPANY_NAMES)} rows (deterministic, seed={args.seed})")
    print(f"Spotlight document companies: {len(SPOTLIGHT_COMPANY_NAMES)}")
    print(f"Prior-deal memos: {len(PRIOR_DEALS)}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generate.py",
        description=(
            "Synthetic data generator for the M&A Due Diligence sample. "
            "Seeds Aurora, S3/KB, and AgentCore Memory."
        ),
    )
    parser.add_argument(
        "--seed-all",
        action="store_true",
        help="Chain companies → documents → memory in a single idempotent run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be produced without touching AWS.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help="RNG seed for deterministic company generation (default: 42).",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="AWS region override (defaults to AWS_REGION / AWS_DEFAULT_REGION).",
    )

    subparsers = parser.add_subparsers(dest="command")

    p_companies = subparsers.add_parser(
        "companies", help="Seed mna.target_companies via the RDS Data API."
    )
    p_companies.add_argument("--cluster-arn", default=None)
    p_companies.add_argument("--secret-arn", default=None)
    p_companies.add_argument("--database", default="mna")
    p_companies.set_defaults(func=cmd_companies)

    p_documents = subparsers.add_parser(
        "documents", help="Generate CIM/financial/press documents and upload to S3."
    )
    p_documents.add_argument("--bucket", default=None)
    p_documents.add_argument("--kb-id", default=None)
    p_documents.add_argument(
        "--data-source-id",
        default=None,
        help="KB data source id — if provided, triggers an ingestion job.",
    )
    p_documents.set_defaults(func=cmd_documents)

    p_memory = subparsers.add_parser(
        "memory", help="Write prior-deal memos to AgentCore Memory."
    )
    p_memory.add_argument(
        "--memory-id",
        default=None,
        help="AgentCore Memory resource identifier (required).",
    )
    p_memory.set_defaults(func=cmd_memory)

    return parser


def _apply_subcommand_defaults(args: argparse.Namespace) -> None:
    """Backfill attributes that ``--seed-all`` expects but a subcommand didn't set."""

    for attr, default in (
        ("cluster_arn", None),
        ("secret_arn", None),
        ("database", "mna"),
        ("bucket", None),
        ("kb_id", None),
        ("data_source_id", None),
        ("memory_id", None),
    ):
        if not hasattr(args, attr):
            setattr(args, attr, default)


def main(argv: list[str] | None = None) -> int:
    """Console entry point; returns the process exit code."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    if args.seed_all:
        _apply_subcommand_defaults(args)
        return cmd_seed_all(args)

    func: Callable[[argparse.Namespace], int] | None = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2
    _apply_subcommand_defaults(args)
    return func(args)


__all__ = [
    "COMPANY_NAMES",
    "CompanyRow",
    "DEFAULT_SEED",
    "DocumentArtifact",
    "GOVERNANCE_CHECKLIST",
    "PRIOR_DEALS",
    "PriorDealSeed",
    "SPOTLIGHT_COMPANY_NAMES",
    "SYNTHETIC_BANNER",
    "build_company_documents",
    "cmd_companies",
    "cmd_documents",
    "cmd_memory",
    "cmd_seed_all",
    "generate_companies",
    "insert_companies",
    "main",
    "seed_memory",
    "trigger_and_poll_ingestion",
    "upload_documents",
]


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())

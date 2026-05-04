"""Helper script that (re)generates ``notebooks/walkthrough.ipynb``.

The walkthrough notebook is a user-editable artifact that must remain a
syntactically valid ``.ipynb`` JSON document with ``nbformat==4`` and a
``python3`` kernelspec (design.md → "Reader Surfaces").

Editing a 7-cell notebook by hand across merges is a recipe for
inconsistent metadata and broken JSON. This script builds the notebook
from a structured list of cells so the output is deterministic and the
source of truth lives in version-controlled Python.

Run locally from the repo root:

    python scripts/_build_walkthrough_notebook.py

The script is re-entrant — it overwrites the notebook every run.
"""

from __future__ import annotations

import json
from pathlib import Path


def _md(source: str) -> dict:
    """Build a Jupyter markdown cell."""

    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": source.splitlines(keepends=True),
    }


def _code(source: str) -> dict:
    """Build a Jupyter code cell. Outputs are intentionally empty — the
    walkthrough is a template readers execute in their own environment."""

    return {
        "cell_type": "code",
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


# ---------------------------------------------------------------------------
# Cell sources
# ---------------------------------------------------------------------------

HEADER_MD = """# M&A Due Diligence Walkthrough

This notebook mirrors the narrative in the companion blog post. Each
code cell imports from the shared `mna` package — the CLI at
`cli/invoke.py` calls the same functions, so what you see here matches
what `python -m mna invoke ...` produces.

Run the cells top-to-bottom after `deploy.sh` / `deploy.ps1` completes
and `data/generate.py --seed-all` has finished.
"""

# Cell 1 — Environment validation (Req 7.2, 9.3 prerequisites)
CELL1_MD = """## 1. Environment validation

Verifies the AWS region, credential chain, and every `/mna/*` SSM
parameter published by the CDK stacks. Stop here and re-run deploy if
any parameter is missing.
"""

CELL1_CODE = """import os

import boto3

from mna.config import ALL_PARAMETERS, load_config

session = boto3.Session()
region = session.region_name or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
credentials = session.get_credentials()

print(f"Region: {region}")
print(f"Credentials resolved: {credentials is not None}")

config = load_config(region_name=region)
for param_name in ALL_PARAMETERS:
    value = config.as_dict()[param_name]
    print(f"  {param_name}: {'OK' if value else 'MISSING'}")
"""

# Cell 2 — Data overview (Req 7.2)
CELL2_MD = """## 2. Data overview

Counts the rows seeded into Aurora by `data/generate.py companies` and
lists the document keys uploaded to the documents S3 bucket. Gives a
quick confidence check that the dataset is present before any agent is
invoked.
"""

CELL2_CODE = """import boto3

from mna.config import load_config

config = load_config()
region = boto3.Session().region_name

rds_data = boto3.client("rds-data", region_name=region)
secrets = boto3.client("secretsmanager", region_name=region)

# Aurora row count via the RDS Data API (no driver needed in the notebook).
aurora_secret_arn = secrets.list_secrets(
    Filters=[{"Key": "name", "Values": ["mna"]}],
).get("SecretList", [{}])[0].get("ARN")

row_count = None
if aurora_secret_arn:
    result = rds_data.execute_statement(
        resourceArn=config.aurora_cluster_arn,
        secretArn=aurora_secret_arn,
        database="postgres",
        sql="SELECT COUNT(*) FROM mna.target_companies",
    )
    row_count = result["records"][0][0]["longValue"]
print(f"Aurora target_companies rows: {row_count}")

# KB document inventory from the documents bucket.
s3 = boto3.client("s3", region_name=region)
paginator = s3.get_paginator("list_objects_v2")
doc_keys: list[str] = []
for page in paginator.paginate(Bucket=config.docs_bucket):
    doc_keys.extend(obj["Key"] for obj in page.get("Contents", []))
print(f"KB document objects in s3://{config.docs_bucket}: {len(doc_keys)}")
for key in doc_keys[:10]:
    print(f"  {key}")
"""

# Helper for specialist cells. We keep prompts aligned with prompts.md
# so readers can map each cell back to its documented scenario.
_SPECIALIST_PROMPTS: list[tuple[str, str, str, str]] = [
    (
        "3",
        "target_screening",
        "Target Screening — text-to-SQL + KB enrichment",
        (
            "Screen our target pipeline for transportation companies with revenue "
            "between $100M and $500M, EBITDA margin above 12%, and fleet size above "
            "200. Surface the top three and tell me what the CIM says about the "
            "leader's growth trajectory."
        ),
    ),
    (
        "4",
        "financial_analysis",
        "Financial Analysis — KB + Gateway-backed tool",
        (
            "Run a DCF on Acme Logistics using the CIM in the knowledge base. "
            "Flag any management projection that diverges from historical "
            "performance by more than 20%, and pull comparable multiples for "
            "transportation-logistics mid-market."
        ),
    ),
    (
        "5",
        "strategic_fit",
        "Strategic Fit — long-term memory over prior deals",
        (
            "Compare Acme Logistics' integration profile against our three most "
            "recent completed acquisitions. Identify the top three integration "
            "risks and cite the source memos."
        ),
    ),
    (
        "6",
        "compliance_validation",
        "Compliance Validation — evaluator invocation",
        (
            "Review the Acme Logistics analysis in this session for completeness "
            "against our M&A governance checklist. List any claims without source "
            "citations."
        ),
    ),
]


def _specialist_md(number: str, title: str) -> str:
    return f"## {number}. {title}\n"


def _specialist_code(agent: str, prompt: str) -> str:
    return f'''from IPython.display import Markdown, display

from mna.client import invoke_agent

PROMPT = (
    {prompt!r}
)

response = invoke_agent("{agent}", PROMPT, session_id="walkthrough")

display(Markdown(f"**Response:**\\n\\n{{response.text}}"))

if response.citations:
    display(Markdown("**Citations:**"))
    for idx, cite in enumerate(response.citations, start=1):
        location = cite.source
        if cite.page is not None:
            location += f", p. {{cite.page}}"
        display(Markdown(f"{{idx}}. `{{location}}` — {{cite.text[:160]}}"))
else:
    display(Markdown("_No citations returned._"))

print(f"session_id={{response.session_id}} trace_id={{response.trace_id}}")
'''


# Cell 7 — Trace inspection (Req 9.3)
CELL7_MD = """## 7. Trace inspection

Fetches the X-Ray trace for the most recent invocation so you can see
the supervisor → specialist → tool hierarchy described in the blog.
Re-run the cell with a different `trace_id` to inspect earlier turns.
"""

CELL7_CODE = """from mna.client import get_last_trace

# Replace with the trace_id printed by the cell above whose trace you
# want to inspect. Defaults to the Compliance Validation invocation.
trace_id = response.trace_id

if not trace_id:
    print("No trace_id available — run a specialist cell first.")
else:
    trace = get_last_trace(trace_id)
    summary = trace.get("summary") or {}
    segments = trace.get("segments") or []
    print(f"Trace {trace_id}")
    print(f"  duration: {summary.get('Duration')}")
    print(f"  segments: {len(segments)}")
    for seg in segments:
        name = seg.get("name") or "(anonymous)"
        origin = seg.get("origin") or "Unknown"
        print(f"    - {name} ({origin})")
"""


def build_notebook() -> dict:
    cells: list[dict] = [
        _md(HEADER_MD),
        _md(CELL1_MD),
        _code(CELL1_CODE),
        _md(CELL2_MD),
        _code(CELL2_CODE),
    ]
    for number, agent, title, prompt in _SPECIALIST_PROMPTS:
        cells.append(_md(_specialist_md(number, title)))
        cells.append(_code(_specialist_code(agent, prompt)))
    cells.append(_md(CELL7_MD))
    cells.append(_code(CELL7_CODE))

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {
                "name": "python",
                "version": "3.11",
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out_path = repo_root / "notebooks" / "walkthrough.ipynb"
    notebook = build_notebook()
    out_path.write_text(json.dumps(notebook, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} with {len(notebook['cells'])} cells")


if __name__ == "__main__":
    main()

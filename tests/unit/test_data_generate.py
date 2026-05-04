"""Unit tests for ``data/generate.py``.

Exercises the pure functions (generators, document builders, argparse
wiring) without touching AWS. Anywhere the generator would normally
call AWS we substitute ``MagicMock``s or rely on ``--dry-run``.

The ``data`` directory isn't a Python package, so we import the module
via :mod:`importlib.util` rather than ``import data.generate``.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GENERATE_PATH = _REPO_ROOT / "data" / "generate.py"


def _load_generate_module() -> ModuleType:
    """Load ``data/generate.py`` as an importable module.

    Registers the module in ``sys.modules`` before executing it so
    ``dataclass`` field type resolution (which does a ``sys.modules``
    lookup on Python 3.12) succeeds.
    """

    module_name = "mna_generate"
    spec = importlib.util.spec_from_file_location(module_name, _GENERATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


@pytest.fixture(scope="module")
def generate() -> ModuleType:
    return _load_generate_module()


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


class TestGenerateCompanies:
    def test_produces_at_least_twenty_rows(self, generate: ModuleType) -> None:
        rows = generate.generate_companies()
        # Requirement 5.2: at least 20 fictional transportation companies.
        assert len(rows) >= 20

    def test_is_deterministic_for_fixed_seed(self, generate: ModuleType) -> None:
        first = generate.generate_companies(seed=42)
        second = generate.generate_companies(seed=42)
        assert first == second

    def test_different_seeds_produce_different_rows(self, generate: ModuleType) -> None:
        a = generate.generate_companies(seed=1)
        b = generate.generate_companies(seed=2)
        # Names are fixed, so compare the numeric attributes to prove
        # the RNG actually varied with the seed.
        assert [r.revenue_usd for r in a] != [r.revenue_usd for r in b]

    def test_rows_have_plausible_attributes(self, generate: ModuleType) -> None:
        rows = generate.generate_companies()
        for row in rows:
            assert 50_000_000 <= row.revenue_usd <= 800_000_000
            assert 8.0 <= row.ebitda_margin_pct <= 16.0
            assert 50 <= row.fleet_size <= 500
            assert row.employee_count >= 25
            assert 5.0 <= row.customer_concentration_top1_pct <= 35.0
            assert 2 <= len(row.service_lines) <= 4
            # Service lines are unique per company.
            assert len(row.service_lines) == len(set(row.service_lines))
            # company_id is a slug of the legal name — lowercased,
            # non-alphanumeric characters replaced with underscores,
            # and runs of underscores collapsed.
            expected = "".join(
                c.lower() if c.isalnum() else "_" for c in row.legal_name
            )
            while "__" in expected:
                expected = expected.replace("__", "_")
            expected = expected.strip("_")
            assert row.company_id == expected


class TestInsertCompanies:
    def test_batch_executes_expected_upsert(self, generate: ModuleType) -> None:
        rows = generate.generate_companies()[:3]
        client = MagicMock()

        inserted = generate.insert_companies(
            rows,
            cluster_arn="arn:aws:rds:us-east-1:111122223333:cluster:mna",
            secret_arn="arn:aws:secretsmanager:us-east-1:111122223333:secret:mna",
            rds_data_client=client,
        )

        assert inserted == 3
        client.batch_execute_statement.assert_called_once()
        kwargs = client.batch_execute_statement.call_args.kwargs
        assert kwargs["resourceArn"].endswith(":cluster:mna")
        assert kwargs["database"] == "mna"
        assert "INSERT INTO mna.target_companies" in kwargs["sql"]
        assert "ON CONFLICT (company_id) DO UPDATE" in kwargs["sql"]
        # Three rows → three parameter sets.
        assert len(kwargs["parameterSets"]) == 3
        # Every parameter set carries the expected keys.
        for params in kwargs["parameterSets"]:
            names = {p["name"] for p in params}
            assert {
                "company_id",
                "legal_name",
                "headquarters_region",
                "revenue_usd",
                "ebitda_margin_pct",
                "fleet_size",
                "employee_count",
                "customer_concentration_top1_pct",
                "service_lines",
            } <= names


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


class TestDocuments:
    def test_spotlight_names_are_present_in_roster(self, generate: ModuleType) -> None:
        names = {r.legal_name for r in generate.generate_companies()}
        for spotlight in generate.SPOTLIGHT_COMPANY_NAMES:
            assert spotlight in names, spotlight

    def test_build_company_documents_includes_banner(self, generate: ModuleType) -> None:
        rows = generate.generate_companies()
        company = next(r for r in rows if r.legal_name == generate.SPOTLIGHT_COMPANY_NAMES[0])
        artifacts = generate.build_company_documents(company)
        assert artifacts, "expected at least one artifact"

        # Every markdown artifact must contain the synthetic banner.
        md_artifacts = [a for a in artifacts if a.content_type == "text/markdown"]
        assert md_artifacts, "expected at least one markdown artifact"
        for artifact in md_artifacts:
            body = artifact.body.decode("utf-8")
            assert generate.SYNTHETIC_BANNER in body, artifact.s3_key

    def test_build_company_documents_uses_expected_keys(self, generate: ModuleType) -> None:
        company = next(
            r
            for r in generate.generate_companies()
            if r.legal_name == "Acme Logistics"
        )
        keys = [a.s3_key for a in generate.build_company_documents(company)]
        # At minimum the three markdown artifacts must exist; PDFs are
        # optional because reportlab may not be installed.
        assert "cims/acme_logistics.md" in keys
        assert "financials/acme_logistics_statements.md" in keys
        assert "press/acme_logistics_press_pack.md" in keys

    def test_governance_checklist_banner(self, generate: ModuleType) -> None:
        assert generate.SYNTHETIC_BANNER in generate.GOVERNANCE_CHECKLIST

    def test_upload_documents_calls_put_object_per_artifact(
        self, generate: ModuleType
    ) -> None:
        artifacts = [
            generate.DocumentArtifact(
                s3_key="cims/example.md",
                content_type="text/markdown",
                body=b"# doc",
            ),
            generate.DocumentArtifact(
                s3_key="governance/ma_checklist.md",
                content_type="text/markdown",
                body=b"# checklist",
            ),
        ]
        client = MagicMock()
        count = generate.upload_documents(artifacts, bucket="mna-docs", s3_client=client)

        assert count == 2
        assert client.put_object.call_count == 2
        first_call = client.put_object.call_args_list[0].kwargs
        assert first_call["Bucket"] == "mna-docs"
        assert first_call["Key"] == "cims/example.md"
        assert first_call["ContentType"] == "text/markdown"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


class TestSeedMemory:
    def test_writes_three_memos_to_prior_deals_namespace(self, generate: ModuleType) -> None:
        client = MagicMock()
        client.create_memory_record.return_value = {"memoryRecordId": "rec"}

        count = generate.seed_memory(memory_id="mem-abc", client=client)

        assert count == len(generate.PRIOR_DEALS) == 3
        assert client.create_memory_record.call_count == 3

        # Every write must use the ``prior_deals`` namespace.
        for call in client.create_memory_record.call_args_list:
            kwargs = call.kwargs
            assert kwargs["namespace"] == "prior_deals"
            assert kwargs["memoryId"] == "mem-abc"
            # Body begins with the synthetic banner.
            text = kwargs["content"]["text"]
            assert generate.SYNTHETIC_BANNER in text.splitlines()[0]
            # Metadata carries the memo id.
            meta = kwargs["metadata"]
            assert meta["synthetic"] is True
            assert meta["memo_id"].startswith("prior_deal_")


# ---------------------------------------------------------------------------
# CLI wiring and --seed-all
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_companies_dry_run_prints_rows_and_skips_aws(
        self, generate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = generate.main(["--dry-run", "companies"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Generated 25 company rows" in out
        assert "Dry run" in out

    def test_documents_dry_run_lists_artifacts(
        self, generate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = generate.main(["--dry-run", "documents"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "spotlight" in out
        assert "governance/ma_checklist.md" in out
        assert "Dry run" in out

    def test_memory_dry_run_lists_memos(
        self, generate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = generate.main(["--dry-run", "memory"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "prior_deals" in out
        assert "Dry run" in out

    def test_seed_all_chains_each_subcommand(
        self,
        generate: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        calls: list[str] = []

        def fake_companies(args):
            calls.append("companies")
            return 0

        def fake_documents(args):
            calls.append("documents")
            return 0

        def fake_memory(args):
            calls.append("memory")
            return 0

        monkeypatch.setattr(generate, "cmd_companies", fake_companies)
        monkeypatch.setattr(generate, "cmd_documents", fake_documents)
        monkeypatch.setattr(generate, "cmd_memory", fake_memory)

        rc = generate.main(["--seed-all", "--dry-run"])
        out = capsys.readouterr().out

        assert rc == 0
        assert calls == ["companies", "documents", "memory"]
        assert "Seeding companies" in out
        assert "Seeding documents" in out
        assert "Seeding memory" in out
        assert "Seed summary" in out

    def test_seed_all_halts_on_failure(
        self,
        generate: ModuleType,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def failing_companies(args):
            return 1

        def should_not_run(args):  # pragma: no cover - asserted via mock below
            raise AssertionError("later subcommand ran after a failure")

        monkeypatch.setattr(generate, "cmd_companies", failing_companies)
        monkeypatch.setattr(generate, "cmd_documents", should_not_run)
        monkeypatch.setattr(generate, "cmd_memory", should_not_run)

        rc = generate.main(["--seed-all", "--dry-run"])
        assert rc == 1

    def test_help_flag_works_without_subcommand(
        self, generate: ModuleType, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # argparse exits with code 0 on --help.
        buffer = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = buffer
        try:
            with pytest.raises(SystemExit) as exc_info:
                generate.main(["--help"])
        finally:
            sys.stdout = old_stdout
        assert exc_info.value.code == 0

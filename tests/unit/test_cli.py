"""Unit tests for the :mod:`cli.invoke` argparse dispatch.

These tests mock out :mod:`mna.client` functions so no AWS calls are
made, and use ``capsys`` to capture stdout/stderr produced by the CLI.
The only real logic exercised end-to-end is the local citation-check
evaluator, which is pure-Python and happily runs in unit tests.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.invoke import build_parser, main
from mna.types import AgentResponse, Citation

_TRACE_ID = "1-abcdef12-1234567890abcdef12345678"


# ---------------------------------------------------------------------------
# Parser sanity
# ---------------------------------------------------------------------------


class TestParser:
    def test_lists_every_subcommand(self) -> None:
        parser = build_parser()
        # Find the subparsers action (a ``_SubParsersAction`` holds the
        # ``choices`` mapping for every registered subcommand).
        subparsers_action = next(
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        )
        assert set(subparsers_action.choices.keys()) == {
            "invoke",
            "list-agents",
            "trace",
            "evaluate",
        }

    def test_requires_a_subcommand(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ---------------------------------------------------------------------------
# invoke
# ---------------------------------------------------------------------------


class TestInvokeCommand:
    def _fake_response(self) -> AgentResponse:
        return AgentResponse(
            text="Acme Logistics shows 12% YoY growth.",
            citations=[
                Citation(
                    text="Revenue grew 12% YoY in 2023.",
                    source="s3://mna-docs/cims/acme.pdf",
                    page=4,
                    score=0.91,
                ),
                Citation(
                    text="Fleet of 220 tractors operating in six states.",
                    source="s3://mna-docs/cims/acme.pdf",
                ),
            ],
            trace_id=_TRACE_ID,
            session_id="sess-cli-1",
        )

    def test_prints_response_text_and_citations(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("cli.invoke.invoke_agent", return_value=self._fake_response()) as mocked:
            exit_code = main(
                [
                    "invoke",
                    "target_screening",
                    "Screen our pipeline",
                    "--session-id",
                    "sess-cli-1",
                ]
            )

        assert exit_code == 0
        mocked.assert_called_once_with(
            "target_screening", "Screen our pipeline", session_id="sess-cli-1"
        )
        captured = capsys.readouterr().out
        assert "Acme Logistics shows 12% YoY growth." in captured
        # Both citations should be rendered with their source.
        assert "s3://mna-docs/cims/acme.pdf" in captured
        # The first citation has a page and a score; both should appear.
        assert "p. 4" in captured
        assert "score=0.91" in captured
        # Footer surfaces trace + session metadata for trace follow-up.
        assert _TRACE_ID in captured
        assert "sess-cli-1" in captured

    def test_json_mode_emits_valid_json_payload(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("cli.invoke.invoke_agent", return_value=self._fake_response()):
            exit_code = main(
                ["--json", "invoke", "supervisor", "Run DCF on Acme Logistics"]
            )

        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["text"].startswith("Acme Logistics")
        assert payload["trace_id"] == _TRACE_ID
        assert len(payload["citations"]) == 2
        assert payload["citations"][0]["source"] == "s3://mna-docs/cims/acme.pdf"

    def test_reports_runtime_error_via_stderr_and_nonzero_exit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mna.client import ClientError

        with patch("cli.invoke.invoke_agent", side_effect=ClientError("boom")):
            exit_code = main(["invoke", "supervisor", "hi"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "error: boom" in captured.err


# ---------------------------------------------------------------------------
# list-agents
# ---------------------------------------------------------------------------


class TestListAgentsCommand:
    _EXPECTED = [
        "supervisor",
        "target_screening",
        "financial_analysis",
        "strategic_fit",
        "compliance_validation",
    ]

    def test_prints_every_agent_name_on_its_own_line(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = main(["list-agents"])
        assert exit_code == 0

        out = capsys.readouterr().out
        printed = [line for line in out.splitlines() if line.strip()]
        assert printed == self._EXPECTED

    def test_json_mode_emits_ordered_array(self, capsys: pytest.CaptureFixture[str]) -> None:
        exit_code = main(["--json", "list-agents"])
        assert exit_code == 0
        assert json.loads(capsys.readouterr().out) == self._EXPECTED

    def test_mocked_list_agents_is_used(self, capsys: pytest.CaptureFixture[str]) -> None:
        # Verify the CLI delegates to ``mna.client.list_agents`` rather
        # than hardcoding the list — a regression guard against
        # duplicated logic (Requirement 7.4).
        with patch("cli.invoke.list_agents", return_value=["only-one"]) as mocked:
            exit_code = main(["list-agents"])
        assert exit_code == 0
        mocked.assert_called_once_with()
        assert capsys.readouterr().out.strip() == "only-one"


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


class TestTraceCommand:
    _TRACE_PAYLOAD = {
        "trace_id": _TRACE_ID,
        "summary": {"Id": _TRACE_ID, "Duration": 1.23},
        "segments": [
            {"name": "supervisor", "origin": "AgentCore"},
            {"name": "text_to_sql", "origin": "Tool"},
        ],
    }

    def test_json_mode_prints_full_trace_as_structured_json(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("cli.invoke.get_last_trace", return_value=self._TRACE_PAYLOAD) as mocked:
            exit_code = main(["--json", "trace", _TRACE_ID])

        assert exit_code == 0
        mocked.assert_called_once_with(_TRACE_ID)
        payload = json.loads(capsys.readouterr().out)
        assert payload == self._TRACE_PAYLOAD

    def test_default_mode_prints_summary_and_segment_count(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("cli.invoke.get_last_trace", return_value=self._TRACE_PAYLOAD):
            exit_code = main(["trace", _TRACE_ID])

        assert exit_code == 0
        out = capsys.readouterr().out
        assert _TRACE_ID in out
        assert "Summary:" in out
        assert "Segments (2):" in out
        # Segment names should appear in the rendered JSON.
        assert "text_to_sql" in out
        assert "supervisor" in out


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


class TestEvaluateCommand:
    def _write_citations(self, tmp_path: Path, citations: list[dict]) -> Path:
        path = tmp_path / "citations.json"
        path.write_text(json.dumps(citations), encoding="utf-8")
        return path

    def test_passes_when_every_claim_is_supported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response_file = tmp_path / "response.txt"
        response_file.write_text(
            "Acme Logistics grew revenue 12% in 2023.", encoding="utf-8"
        )
        citations_file = self._write_citations(
            tmp_path,
            [
                {
                    "text": "Acme Logistics grew revenue 12% in 2023 "
                    "across all service lines.",
                    "source": "s3://mna-docs/cims/acme.pdf",
                    "page": 4,
                }
            ],
        )

        exit_code = main(
            [
                "evaluate",
                "--response-file",
                str(response_file),
                "--citations-file",
                str(citations_file),
            ]
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "Citation check: PASS" in out
        assert "total_claims=1" in out

    def test_fails_with_nonzero_exit_when_claim_is_unsupported(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response_file = tmp_path / "response.txt"
        response_file.write_text(
            "Acme Logistics employs 5000 drivers nationwide.",
            encoding="utf-8",
        )
        citations_file = self._write_citations(
            tmp_path,
            [
                {
                    "text": "Bluewave Freight operates in the Pacific Northwest only.",
                    "source": "s3://mna-docs/cims/bluewave.pdf",
                }
            ],
        )

        exit_code = main(
            [
                "evaluate",
                "--response-file",
                str(response_file),
                "--citations-file",
                str(citations_file),
            ]
        )

        assert exit_code == 1
        out = capsys.readouterr().out
        assert "Citation check: FAIL" in out
        assert "Unsupported claims:" in out

    def test_reads_response_text_from_stdin_when_no_file_passed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        citations_file = self._write_citations(
            tmp_path,
            [
                {
                    "text": "Revenue grew 12% in 2023.",
                    "source": "s3://mna-docs/cims/acme.pdf",
                }
            ],
        )

        stdin = io.StringIO("Acme Logistics grew revenue 12% in 2023.\n")
        exit_code = main(
            [
                "evaluate",
                "--citations-file",
                str(citations_file),
            ],
            stdin=stdin,
        )

        assert exit_code == 0
        assert "Citation check: PASS" in capsys.readouterr().out

    def test_json_mode_emits_evaluation_result_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response_file = tmp_path / "response.txt"
        response_file.write_text("Revenue grew 12% in 2023.", encoding="utf-8")
        citations_file = self._write_citations(
            tmp_path,
            [
                {
                    "text": "Revenue grew 12% in 2023 across all service lines.",
                    "source": "s3://mna-docs/cims/acme.pdf",
                }
            ],
        )

        exit_code = main(
            [
                "--json",
                "evaluate",
                "--response-file",
                str(response_file),
                "--citations-file",
                str(citations_file),
            ]
        )

        assert exit_code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert payload["total_claims"] == 1
        assert payload["unsupported_claims"] == []

    def test_rejects_empty_response_with_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        citations_file = self._write_citations(tmp_path, [])
        stdin = io.StringIO("   \n")
        exit_code = main(
            [
                "evaluate",
                "--citations-file",
                str(citations_file),
            ],
            stdin=stdin,
        )

        assert exit_code == 1
        assert "Response text is empty" in capsys.readouterr().err

    def test_rejects_non_list_citations_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        citations_file = tmp_path / "citations.json"
        citations_file.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        response_file = tmp_path / "response.txt"
        response_file.write_text("Revenue grew 12%.", encoding="utf-8")

        exit_code = main(
            [
                "evaluate",
                "--response-file",
                str(response_file),
                "--citations-file",
                str(citations_file),
            ]
        )

        assert exit_code == 1
        assert "JSON array" in capsys.readouterr().err

    def test_rejects_missing_citations_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        response_file = tmp_path / "response.txt"
        response_file.write_text("Revenue grew 12%.", encoding="utf-8")

        exit_code = main(
            [
                "evaluate",
                "--response-file",
                str(response_file),
                "--citations-file",
                str(tmp_path / "does_not_exist.json"),
            ]
        )

        assert exit_code == 1
        assert "Citations file not found" in capsys.readouterr().err

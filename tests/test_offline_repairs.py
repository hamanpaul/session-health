"""Regression coverage for the independent offline repair findings."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lib.bundle import BundleError, BundleLimits, SessionBundle, build_session_bundle
from lib.html_report import render_html
from lib.metrics.process_v2 import analyze_process_v2, join_external_outcome
from lib.parser_base import Session, SessionInputLimits, ToolCall, Turn
from lib.parser_codex import parse_codex_session
from lib.parser_copilot import parse_copilot_session
from lib.radar import render_table
from lib.report_types import SessionReport
from lib.scorer import score_session


ROOT = Path(__file__).resolve().parents[1]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def _codex_duplicate_records() -> list[dict]:
    return [
        {"type": "session_meta", "payload": {"id": "duplicate-codex", "cwd": "/private/project"}},
        {"type": "response_item", "timestamp": "2026-09-20T00:00:01Z", "payload": {"role": "user", "content": [{"text": "run"}]}},
        {"type": "response_item", "timestamp": "2026-09-20T00:00:02Z", "payload": {"type": "function_call", "name": "bash", "call_id": "same", "arguments": {"cmd": "echo one"}}},
        {"type": "response_item", "timestamp": "2026-09-20T00:00:03Z", "payload": {"type": "function_call_output", "call_id": "same", "output": "output_1", "exit_code": 0}},
        {"type": "response_item", "timestamp": "2026-09-20T00:00:04Z", "payload": {"type": "function_call", "name": "bash", "call_id": "same", "arguments": {"cmd": "echo two"}}},
        {"type": "response_item", "timestamp": "2026-09-20T00:00:05Z", "payload": {"type": "function_call_output", "call_id": "same", "output": "output_2", "exit_code": 0}},
    ]


def _copilot_duplicate_records() -> list[dict]:
    return [
        {"type": "session.start", "timestamp": "2026-09-20T00:00:00Z", "data": {"sessionId": "duplicate-copilot"}},
        {"type": "user.message", "timestamp": "2026-09-20T00:00:01Z", "data": {"content": "run"}},
        {"type": "tool.execution_start", "timestamp": "2026-09-20T00:00:02Z", "data": {"toolName": "bash", "toolCallId": "same", "arguments": "{\"cmd\": \"echo one\"}"}},
        {"type": "tool.execution_complete", "timestamp": "2026-09-20T00:00:03Z", "data": {"toolCallId": "same", "result": {"content": "output_1"}, "exitCode": 0}},
        {"type": "tool.execution_start", "timestamp": "2026-09-20T00:00:04Z", "data": {"toolName": "bash", "toolCallId": "same", "arguments": "{\"cmd\": \"echo two\"}"}},
        {"type": "tool.execution_complete", "timestamp": "2026-09-20T00:00:05Z", "data": {"toolCallId": "same", "result": {"content": "output_2"}, "exitCode": 0}},
    ]


class OfflineRepairTest(unittest.TestCase):
    def test_snr_noise_facts_survive_bounded_bundle_replay(self) -> None:
        for output in (
            "repeat\n" * 1200,
            "\x1b[31mred\x1b[0m\n" * 900,
            "\n".join(f"record #{index} value={index * 17}" for index in range(900)),
        ):
            session = Session(
                id="snr-replay",
                source="codex",
                turns=[Turn(index=1, tool_calls=[ToolCall(name="bash", call_id="c1", output=output)])],
            )
            bundle = build_session_bundle(session)
            replayed = SessionBundle.from_json(bundle.to_json()).to_session()
            original = analyze_process_v2(session).axes["SNR"].metric.to_dict()
            restored = analyze_process_v2(replayed).axes["SNR"].metric.to_dict()
            for field in ("numerator", "denominator", "value", "status"):
                self.assertEqual(original[field], restored[field])

    def test_raw_input_budget_is_bounded_but_configurable_and_partial(self) -> None:
        records = [
            {"type": "session_meta", "payload": {"id": "budgeted"}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"text": "run"}]}},
            {"type": "response_item", "payload": {"role": "assistant", "content": [{"text": "done"}]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "budgeted.jsonl"
            _write_jsonl(path, records)
            full = parse_codex_session(path, input_limits=SessionInputLimits(max_bytes=100_000))
            self.assertEqual(full.id, "budgeted")
            lines = path.read_bytes().splitlines(keepends=True)
            partial_limit = len(lines[0]) + len(lines[1])
            partial = parse_codex_session(path, input_limits=SessionInputLimits(max_bytes=partial_limit))
            self.assertTrue(any(item.get("kind") == "input_byte_limit_exceeded" for item in partial.diagnostics))
            self.assertTrue(partial.turns)

    def test_duplicate_ids_never_overwrite_and_result_line_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_path = root / "codex.jsonl"
            copilot_path = root / "copilot.jsonl"
            _write_jsonl(codex_path, _codex_duplicate_records())
            _write_jsonl(copilot_path, _copilot_duplicate_records())
            for parser, path in ((parse_codex_session, codex_path), (parse_copilot_session, copilot_path)):
                session = parser(path)
                calls = [call for turn in session.turns for call in turn.tool_calls]
                self.assertEqual([call.output for call in calls], ["output_1", ""])
                self.assertIn("ambiguous_call_result", " ".join(calls[1].diagnostics))
                self.assertTrue(any(event["kind"] == "tool_result" and event["source_ref"].endswith("#L6") for event in session.event_log))

    def test_bundle_is_ordered_redacted_and_round_trip_faithful(self) -> None:
        records = [
            {"type": "session_meta", "timestamp": "2026-09-20T00:00:00Z", "payload": {"id": "ordered", "cwd": "/tmp/private", "task_id": "task-1"}},
            {"type": "response_item", "timestamp": "2026-09-20T00:00:01Z", "payload": {"role": "user", "content": [{"text": "run"}]}},
            {"type": "response_item", "timestamp": "2026-09-20T00:00:02Z", "payload": {"role": "assistant", "content": [{"text": "The test is complete."}]}},
            {"type": "response_item", "timestamp": "2026-09-20T00:00:03Z", "payload": {"type": "function_call", "name": "exec_command", "call_id": "c1", "arguments": {"cmd": "pytest -q", "api_key": "SYNTHETIC_SENTINEL", "path": "/synthetic/private/project"}}},
            {"type": "response_item", "timestamp": "2026-09-20T00:00:04Z", "payload": {"type": "function_call_output", "call_id": "c1", "output": "5000 chars", "exit_code": 0}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ordered.jsonl"
            _write_jsonl(path, records)
            session = parse_codex_session(path)
            session.turns[0].tool_calls[0].output = "A" * 5000
            session.diagnostics.append({"kind": "unknown", "raw_system": "RAW_SYSTEM_SENTINEL"})
            session.source_capabilities["raw_payload"] = "RAW_CAPABILITY_SENTINEL"
            bundle = build_session_bundle(session)
            self.assertNotIn("SYNTHETIC_SENTINEL", bundle.to_json())
            self.assertNotIn("/synthetic/private/project", bundle.to_json())
            self.assertNotIn("RAW_SYSTEM_SENTINEL", bundle.to_json())
            self.assertNotIn("RAW_CAPABILITY_SENTINEL", bundle.to_json())
            self.assertEqual([event["kind"] for event in bundle.events], ["user_message", "assistant_message", "tool_call", "tool_result"])
            self.assertEqual([event["timestamp"] for event in bundle.events], [row["timestamp"] for row in records[1:]])
            case = bundle.cases[0]
            self.assertEqual(case["relations"]["claim_to_verification"], "insufficient")
            self.assertNotIn(bundle.events[-1]["event_id"], case["event_refs"])
            replayed = SessionBundle.from_json(bundle.to_json()).to_session()
            self.assertEqual(replayed.metadata.get("task_id"), "task-1")
            self.assertTrue(replayed.cwd)
            original_result = analyze_process_v2(session).axes["SNR"].metric.denominator
            replayed_result = analyze_process_v2(replayed).axes["SNR"].metric.denominator
            self.assertEqual(original_result, replayed_result)

        missing_time = Session(
            id="missing-time",
            source="codex",
            timestamp_end="2026-09-20T23:59:59Z",
            turns=[Turn(index=1, user_input="claim", assistant_output="done")],
        )
        missing_time_bundle = build_session_bundle(missing_time)
        self.assertIsNone(missing_time_bundle.cases[0]["observation_cutoff"])

    def test_process_axes_use_related_observations_and_structured_fields(self) -> None:
        calls = [ToolCall(name="bash", arguments={"cmd": f"false {index}"}, exit_code=1) for index in range(9)]
        calls.append(ToolCall(name="bash", arguments={"cmd": "pwd"}, exit_code=0))
        session = Session(id="axes", source="codex", turns=[Turn(index=1, tool_calls=calls)])
        result = analyze_process_v2(session)
        self.assertEqual(result.axes["REACT"].metric.value, 0.1)
        self.assertEqual(result.axes["REACT"].observed_facts["recovered_failures"], 0)

        verified = Session(id="verified", source="codex", turns=[Turn(index=1, tool_calls=[ToolCall(name="exec_command", arguments={"cmd": "python -m pytest -q"}, output="passed", exit_code=0)])])
        verified_result = analyze_process_v2(verified)
        self.assertEqual(verified_result.axes["DEPTH"].observed_facts["verification_signals"], 1)
        self.assertEqual(verified_result.axes["STATE"].metric.value, 1.0)

        clean = Session(id="clean", source="codex", turns=[Turn(index=1), Turn(index=2)])
        compacted = Session(id="compacted", source="codex", turns=[Turn(index=1), Turn(index=2, events=[{"type": "context_compacted"}])])
        self.assertIsNone(analyze_process_v2(clean).axes["CTX"].metric.value)
        self.assertIsNone(analyze_process_v2(compacted).axes["CTX"].metric.value)

    def test_lifecycle_and_external_join_do_not_overclaim(self) -> None:
        lifecycle = Session(id="lifecycle", source="codex", task_started_count=10, task_complete_count=1)
        lifecycle_result = analyze_process_v2(lifecycle).axes["CONV"].metric
        self.assertIsNone(lifecycle_result.value)
        self.assertEqual(lifecycle_result.status, "unknown")
        explicit = Session(
            id="explicit-lifecycle",
            source="codex",
            task_started_count=1,
            task_complete_count=1,
            turns=[
                Turn(
                    index=1,
                    events=[
                        {"type": "task_started", "task_id": "task-1"},
                        {"type": "task_complete", "task_id": "task-1"},
                    ],
                )
            ],
        )
        explicit_result = analyze_process_v2(explicit, build_session_bundle(explicit)).axes["CONV"].metric
        self.assertEqual(explicit_result.value, 1.0)
        session = Session(id="sess-1", source="codex", metadata={"task_id": "task-A"})
        rejected = join_external_outcome(session, {"session_id": "sess-2", "task_id": "task-A", "verdict": "FAIL"})
        self.assertEqual(rejected["status"], "not_joined")
        alias_rejected = join_external_outcome(
            session,
            {"session_id": "sess-1", "sessionId": "sess-2", "verdict": "PASS"},
        )
        self.assertEqual(alias_rejected["status"], "not_joined")
        session.source_ref = "session.jsonl"
        rejected_ref = join_external_outcome(
            session,
            {"session_id": "sess-1", "task_id": "task-A", "source_ref": "other.jsonl", "verdict": "PASS"},
        )
        self.assertEqual(rejected_ref["status"], "not_joined")

    def test_bundle_import_validates_ids_references_finite_values_and_limits(self) -> None:
        bundle = build_session_bundle(Session(id="bounded", source="codex", turns=[Turn(index=1, user_input="run")]))
        duplicate = bundle.to_dict()
        duplicate["events"].append(dict(duplicate["events"][0]))
        with self.assertRaises(BundleError):
            SessionBundle.from_dict(duplicate)
        too_many_cases = bundle.to_dict()
        too_many_cases["cases"] = too_many_cases["cases"] * 2
        with self.assertRaises(BundleError):
            SessionBundle.from_dict(too_many_cases, limits=BundleLimits(max_cases=1))
        invalid_ref = bundle.to_dict()
        invalid_ref["cases"][0]["event_refs"] = ["missing-event"]
        with self.assertRaises(BundleError):
            SessionBundle.from_dict(invalid_ref)
        non_finite = bundle.to_dict()
        non_finite["facts"]["bad"] = float("nan")
        with self.assertRaises(BundleError):
            SessionBundle.from_dict(non_finite)

    def test_cli_batch_keeps_statuses_formats_and_returns_nonzero_for_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_jsonl(root / "valid.jsonl", _codex_duplicate_records())
            (root / "invalid.jsonl").write_text("not-json\n", encoding="utf-8")
            command = [sys.executable, str(ROOT / "eval_session.py"), "--dir", str(root), "--offline"]
            json_run = subprocess.run(command + ["--format", "json"], capture_output=True, text=True)
            self.assertNotEqual(json_run.returncode, 0)
            payload = json.loads(json_run.stdout)
            self.assertEqual(payload["processing_status"], "failed")
            self.assertEqual(sorted(item["processing_status"] for item in payload["sessions"]), ["failed", "partial"])

            table_run = subprocess.run(command + ["--format", "table"], capture_output=True, text=True)
            self.assertNotEqual(table_run.returncode, 0)
            self.assertIn("Status", table_run.stdout)
            self.assertIn("SNR", table_run.stdout)

            html_path = root / "report.html"
            html_run = subprocess.run(command + ["--format", "html", "--output", str(html_path)], capture_output=True, text=True)
            self.assertNotEqual(html_run.returncode, 0)
            html = html_path.read_text(encoding="utf-8")
            self.assertIn("processing_status", html)
            self.assertIn("process-v2 axes", html)
            self.assertIn("Diagnostics", html)
            export_dir = root / "exports"
            export_run = subprocess.run(
                command + ["--format", "json", "--export-bundle", str(export_dir)],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(export_run.returncode, 0)
            export_manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(item["processing_status"] for item in export_manifest["sessions"]),
                ["failed", "partial"],
            )
            self.assertEqual(len(list(export_dir.glob("*.bundle.json"))), 2)

    def test_positional_default_does_not_invoke_external_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid.jsonl"
            _write_jsonl(path, [
                {"type": "session_meta", "payload": {"id": "plain"}},
                {"type": "response_item", "payload": {"role": "user", "content": [{"text": "report"}]}},
                {"type": "response_item", "payload": {"role": "assistant", "content": [{"text": "done"}]}},
            ])
            run = subprocess.run([sys.executable, str(ROOT / "eval_session.py"), str(path)], capture_output=True, text=True, cwd=directory)
            self.assertEqual(run.returncode, 0)
            self.assertIn("Process-v2 report", run.stdout)
            self.assertFalse((Path(directory) / "session-health-batch.html").exists())

    def test_single_process_html_has_no_legacy_composite_surface(self) -> None:
        session = Session(id="html-process", source="codex", turns=[Turn(index=1, user_input="run")])
        report = SessionReport(
            session=session,
            score=score_session(session),
            profile="process-v2",
            process_v2=analyze_process_v2(session),
        )
        rendered = render_html(report)
        self.assertIn("Process-v2 observable profile", rendered)
        self.assertNotIn("Overall Score", rendered)
        self.assertNotIn("radar", rendered.lower())
        self.assertNotIn("grade", rendered.lower())


if __name__ == "__main__":
    unittest.main()

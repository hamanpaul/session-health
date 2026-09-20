"""Regression coverage for the independent offline repair findings."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import unittest

from lib.bundle import BundleError, BundleLimits, SessionBundle, build_session_bundle
from lib.html_report import render_html
from lib.metrics.process_v2 import analyze_process_v2, join_external_outcome
from lib.parser_base import Session, SessionInputLimits, ToolCall, Turn, read_jsonl_records
from lib.parser_codex import parse_codex_session
from lib.parser_copilot import parse_copilot_session
from lib.radar import render_table
from lib.report_types import BatchReport, SessionReport
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
    def test_oversize_record_keeps_memory_bounded_and_recovers_next_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversize.jsonl"
            path.write_text(
                '{"first": 1}\n{"blob": "' + "x" * (1024 * 1024) + '"}\n{"last": 2}\n',
                encoding="utf-8",
            )
            tracemalloc.start()
            try:
                records, diagnostics = read_jsonl_records(
                    path, SessionInputLimits(max_bytes=2 * 1024 * 1024, max_record_chars=1024),
                )
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

        self.assertLess(peak, 256 * 1024, "discarded 1 MiB record must not be buffered in full")
        self.assertEqual([item["__source_line__"] for item in records], [1, 3])
        self.assertEqual(records[1]["last"], 2)
        self.assertEqual(diagnostics, [{
            "kind": "oversize_record", "line": 2, "status": "partial", "max_record_chars": 1024,
        }])

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
            byte_diagnostic = next(item for item in partial.diagnostics if item.get("kind") == "input_byte_limit_exceeded")
            self.assertEqual(byte_diagnostic["status"], "partial")
            self.assertTrue(partial.turns)

            first_record_chars = len(lines[0].decode("utf-8").rstrip("\r\n"))
            oversized = parse_codex_session(path, input_limits=SessionInputLimits(max_record_chars=first_record_chars))
            oversized_diagnostic = next(item for item in oversized.diagnostics if item.get("kind") == "oversize_record")
            self.assertEqual(oversized_diagnostic["status"], "partial")

            no_usable_records = parse_codex_session(path, input_limits=SessionInputLimits(max_record_chars=10))
            no_usable_diagnostic = next(item for item in no_usable_records.diagnostics if item.get("kind") == "oversize_record")
            self.assertEqual(no_usable_diagnostic["status"], "failed")

    def test_missing_result_diagnostics_keep_numeric_line_and_source_ref(self) -> None:
        cases = (
            (
                parse_codex_session,
                [
                    {"type": "session_meta", "payload": {"id": "missing-codex"}},
                    {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "run"}]}},
                    {"type": "response_item", "payload": {"type": "function_call", "name": "bash", "arguments": {}, "call_id": "call-1"}},
                ],
            ),
            (
                parse_copilot_session,
                [
                    {"type": "session.start", "data": {"sessionId": "missing-copilot"}},
                    {"type": "user.message", "data": {"content": "run"}},
                    {"type": "tool.execution_start", "data": {"toolCallId": "tool-1", "toolName": "bash", "arguments": {}}},
                ],
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (parser, records) in enumerate(cases):
                path = Path(directory) / f"missing-{index}.jsonl"
                _write_jsonl(path, records)
                diagnostic = next(item for item in parser(path).diagnostics if item.get("kind") == "missing_call_result")
                self.assertIs(type(diagnostic["line"]), int)
                self.assertEqual(diagnostic["line"], 3)
                self.assertEqual(diagnostic["source_ref"], f"{path.name}#L3")

    def test_structured_boolean_exit_codes_remain_unknown(self) -> None:
        cases = (
            (
                parse_codex_session,
                [
                    {"type": "session_meta", "payload": {"id": "structured-codex"}},
                    {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "run"}]}},
                    {"type": "response_item", "payload": {"type": "function_call", "name": "bash", "call_id": "call-1", "arguments": {}}},
                    {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "ok", "exitCode": None}},
                ],
            ),
            (
                parse_copilot_session,
                [
                    {"type": "session.start", "data": {"sessionId": "structured-copilot"}},
                    {"type": "user.message", "data": {"content": "run"}},
                    {"type": "tool.execution_start", "data": {"toolCallId": "tool-1", "toolName": "bash", "arguments": {}}},
                    {"type": "tool.execution_complete", "data": {"toolCallId": "tool-1", "result": {"content": "ok"}, "exitCode": None}},
                ],
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, (parser, records) in enumerate(cases):
                for exit_code in (True, False, 0, 1):
                    if parser is parse_codex_session:
                        records[-1]["payload"]["exitCode"] = exit_code
                    else:
                        records[-1]["data"]["exitCode"] = exit_code
                    path = Path(directory) / f"structured-{index}-{str(exit_code).lower()}.jsonl"
                    _write_jsonl(path, records)
                    session = parser(path)
                    turn = session.turns[0]
                    expected = isinstance(exit_code, int) and not isinstance(exit_code, bool)
                    self.assertEqual(turn.context_meta.get("exit_code_present", False), expected)
                    self.assertEqual(turn.tool_calls[0].exit_code, exit_code if expected else None)

    def test_batch_table_keeps_status_column_aligned_for_long_ids(self) -> None:
        session = Session(
            id="s" * 24,
            source="codex",
            turns=[Turn(index=1, user_input="run")],
        )
        report = SessionReport(session=session, score=score_session(session))
        lines = render_table(
            BatchReport(sessions=[report], profile="process-v2"),
            use_color=False,
        ).splitlines()
        header = next(line for line in lines if line.startswith("Session"))
        row = next(line for line in lines if line.startswith("s" * 20))
        self.assertEqual(row.index("complete"), header.index("Status"))
        self.assertNotIn("s" * 21, row)

    def test_r3_source_coverage_is_separate_from_observed_fact_replay(self) -> None:
        records = [
            {"type": "session_meta", "payload": {"id": "r3-budgeted"}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"text": "run"}]}},
            {"type": "response_item", "payload": {"role": "assistant", "content": [{"text": "done"}]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "r3-budgeted.jsonl"
            _write_jsonl(path, records)
            lines = path.read_bytes().splitlines(keepends=True)
            session = parse_codex_session(
                path,
                input_limits=SessionInputLimits(max_records=2),
            )
            record_diagnostic = next(item for item in session.diagnostics if item.get("kind") == "record_limit_exceeded")
            self.assertEqual(record_diagnostic["status"], "partial")
            bundle = build_session_bundle(session)
            self.assertEqual(bundle.coverage["input_status"], "partial")
            self.assertFalse(bundle.coverage["input_complete"])
            self.assertEqual(bundle.coverage["evidence_status"], "complete")
            self.assertTrue(bundle.facts["metric_facts"]["complete"])
            self.assertEqual(bundle.facts["metric_facts"]["input_status"], "partial")
            replayed = SessionBundle.from_json(bundle.to_json()).to_session()
            self.assertEqual(analyze_process_v2(session).status, "partial")
            self.assertEqual(analyze_process_v2(session, bundle).status, "partial")
            self.assertEqual(analyze_process_v2(replayed, bundle).status, "partial")

            byte_limited = parse_codex_session(
                path,
                input_limits=SessionInputLimits(max_bytes=len(lines[0]) + len(lines[1])),
            )
            byte_bundle = build_session_bundle(byte_limited)
            self.assertEqual(byte_bundle.coverage["input_status"], "partial")
            self.assertTrue(byte_bundle.facts["metric_facts"]["complete"])

        failed = Session(
            id="r3-failed",
            source="codex",
            diagnostics=[{"kind": "parse_failure", "status": "failed"}],
        )
        self.assertEqual(analyze_process_v2(failed).status, "failed")

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

        metadata_cwd = Session(
            id="metadata-cwd",
            source="codex",
            turns=[Turn(index=1, tool_calls=[ToolCall(name="bash", result_metadata={"cwd": "/synthetic"})])],
        )
        metadata_axis = analyze_process_v2(metadata_cwd).axes["STATE"]
        self.assertEqual(metadata_axis.metric.value, 1.0)
        self.assertEqual(metadata_axis.observed_facts["observed_turns"], 1)

        explicit_absent = Session(
            id="explicit-absent",
            source="codex",
            cwd="/inherited",
            turns=[Turn(index=1, context_meta={"cwd_present": False}, tool_calls=[ToolCall(name="bash")])],
        )
        absent_axis = analyze_process_v2(explicit_absent).axes["STATE"]
        self.assertEqual(absent_axis.metric.value, 0.0)

        known_then_unknown = Session(
            id="known-then-unknown",
            source="codex",
            turns=[
                Turn(index=1, tool_calls=[ToolCall(name="bash", exit_code=0, result_metadata={"cwd": "/synthetic"})]),
                Turn(index=2, tool_calls=[ToolCall(name="bash")]),
            ],
        )
        mixed_axis = analyze_process_v2(known_then_unknown).axes["STATE"]
        self.assertEqual(mixed_axis.observed_facts["observed_turns"], 1)
        self.assertEqual(mixed_axis.metric.coverage, 0.5)

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
        for source_ref in ("C:/Users/synthetic/session.jsonl", r"C:\Users\synthetic\session.jsonl"):
            invalid_windows_ref = bundle.to_dict()
            invalid_windows_ref["manifest"]["source_ref"] = source_ref
            with self.assertRaises(BundleError):
                SessionBundle.from_dict(invalid_windows_ref)
            invalid_session_ref = bundle.to_dict()
            invalid_session_ref["session"]["source_ref"] = source_ref
            with self.assertRaises(BundleError):
                SessionBundle.from_dict(invalid_session_ref)

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

    def test_single_process_table_has_no_legacy_diagnosis_surface(self) -> None:
        session = Session(id="table-process", source="codex", turns=[Turn(index=1, user_input="run")])
        report = SessionReport(
            session=session,
            score=score_session(session),
            profile="process-v2",
            process_v2=analyze_process_v2(session),
        )
        rendered = render_table(report, use_color=False)
        self.assertNotIn("量化面總分", rendered)
        self.assertIn("Process-v2 observable axes:", rendered)

    def test_r2_bundle_projection_is_bounded_and_keeps_typed_facts(self) -> None:
        session = Session(
            id="many-turns",
            source="codex",
            turns=[
                Turn(index=index, user_input="run test", assistant_output="complete")
                for index in range(1, 1_201)
            ],
        )
        bundle = build_session_bundle(session)
        self.assertLessEqual(len(bundle.to_json().encode("utf-8")), BundleLimits().max_bytes)
        self.assertEqual(bundle.coverage["status"], "truncated")
        self.assertEqual(len(bundle.facts["metric_facts"]["turns"]), 1_200)

        capped_source = Session(
            id="event-cap",
            source="codex",
            turns=[
                Turn(
                    index=index,
                    user_input=f"user-{index}",
                    tool_calls=[ToolCall(name="bash", call_id=f"c{index}", output="ok", exit_code=0)],
                )
                for index in range(1, 11)
            ],
        )
        capped = build_session_bundle(
            capped_source,
            limits=BundleLimits(max_events=1),
        )
        self.assertLessEqual(len(capped.session["turns"]), 1)
        self.assertEqual(sum(len(turn["tool_calls"]) for turn in capped.session["turns"]), 0)
        self.assertEqual(len(capped.facts["metric_facts"]["calls"]), 10)
        restored = SessionBundle.from_json(capped.to_json()).to_session()
        self.assertEqual(len(restored.turns), 10)
        self.assertEqual(sum(len(turn.tool_calls) for turn in restored.turns), 10)
        self.assertEqual(
            analyze_process_v2(capped_source).axes["SNR"].metric.denominator,
            analyze_process_v2(restored, capped).axes["SNR"].metric.denominator,
        )

    def test_bounded_replay_preserves_exact_argument_identity(self) -> None:
        prefix = "pytest " + ("synthetic_prefix_" * 80)
        suffix = " " + ("synthetic_suffix_" * 80)
        commands = (
            prefix + " --case=red" + suffix,
            prefix + " --case=green" + suffix,
            prefix + " --case=green" + suffix,
        )
        session = Session(
            id="argument-identity",
            source="codex",
            turns=[
                Turn(
                    index=index,
                    tool_calls=[
                        ToolCall(
                            name="exec_command",
                            arguments={"cmd": command},
                            success=success,
                            exit_code=exit_code,
                        )
                    ],
                )
                for index, (command, success, exit_code) in enumerate(
                    zip(commands, (False, True, True), (1, 0, 0)),
                    1,
                )
            ],
        )
        bundle = build_session_bundle(session, BundleLimits(max_events=1))
        restored = SessionBundle.from_json(bundle.to_json()).to_session()
        original = analyze_process_v2(session)
        replayed = analyze_process_v2(restored)
        self.assertEqual(
            original.axes["REACT"].metric.to_dict(),
            replayed.axes["REACT"].metric.to_dict(),
        )
        self.assertEqual(
            original.axes["TOOL"].observed_facts["redundant_calls"],
            replayed.axes["TOOL"].observed_facts["redundant_calls"],
        )
        self.assertEqual(
            session.turns[0].tool_calls[0].argument_fingerprint,
            restored.turns[0].tool_calls[0].argument_fingerprint,
        )

    def test_bounded_replay_does_not_relate_redacted_absolute_executables(self) -> None:
        session = Session(
            id="redacted-executables",
            source="codex",
            turns=[
                Turn(
                    index=1,
                    tool_calls=[
                        ToolCall(
                            name="exec_command",
                            call_id="a",
                            arguments={"cmd": "/usr/bin/false --attempt=red"},
                            output="failed",
                            success=False,
                            exit_code=1,
                        )
                    ],
                ),
                Turn(
                    index=2,
                    tool_calls=[
                        ToolCall(
                            name="exec_command",
                            call_id="b",
                            arguments={"cmd": "/usr/bin/true --attempt=green"},
                            output="succeeded",
                            success=True,
                            exit_code=0,
                        )
                    ],
                ),
            ],
        )
        bundle = build_session_bundle(session, BundleLimits(max_events=1))
        restored = SessionBundle.from_json(bundle.to_json()).to_session()
        original = analyze_process_v2(session).axes["REACT"]
        replayed = analyze_process_v2(restored).axes["REACT"]
        self.assertEqual(original.metric.to_dict(), replayed.metric.to_dict())
        self.assertEqual(original.observed_facts, replayed.observed_facts)
        self.assertEqual(replayed.observed_facts["recovered_failures"], 0)

    def test_r2_projected_order_is_explicitly_unknown_and_ids_are_replay_stable(self) -> None:
        session = Session(
            id="unordered",
            source="codex",
            source_ref="/synthetic/path/session.jsonl",
            timestamp_end="2026-09-20T23:59:59Z",
            turns=[
                Turn(
                    index=1,
                    user_input="run test",
                    assistant_output="All tests passed",
                    timestamp="2026-09-20T00:00:00Z",
                    tool_calls=[ToolCall(name="bash", arguments={"cmd": "pytest -q"}, output="1 passed", exit_code=0)],
                )
            ],
        )
        bundle = build_session_bundle(session)
        self.assertEqual(bundle.coverage["ordering"], "projected")
        self.assertFalse(bundle.coverage["temporal_support"])
        self.assertEqual(bundle.cases[0]["relations"]["claim_to_verification"], "unknown")
        self.assertTrue(all(event["timestamp"] is None for event in bundle.events))
        replayed = build_session_bundle(SessionBundle.from_json(bundle.to_json()).to_session())
        self.assertEqual(replayed.coverage["ordering"], "projected")
        self.assertEqual(
            [case["case_id"] for case in bundle.cases],
            [case["case_id"] for case in replayed.cases],
        )
        self.assertEqual(
            [item["ref_id"] for item in bundle.evidence_refs],
            [item["ref_id"] for item in replayed.evidence_refs],
        )

    def test_r2_direct_lifecycle_uses_event_log_identity_pairs(self) -> None:
        session = Session(
            id="direct-lifecycle",
            source="codex",
            task_started_count=2,
            task_complete_count=1,
            event_log=[
                {"sequence": 1, "kind": "session_event", "turn_index": 1, "payload": {"type": "task_started", "task_id": "a"}},
                {"sequence": 2, "kind": "session_event", "turn_index": 1, "payload": {"type": "task_started", "task_id": "b"}},
                {"sequence": 3, "kind": "session_event", "turn_index": 1, "payload": {"type": "task_complete", "task_id": "a"}},
            ],
            turns=[Turn(index=1)],
        )
        direct = analyze_process_v2(session).axes["CONV"].metric
        replayed_bundle = build_session_bundle(session)
        restored = SessionBundle.from_json(replayed_bundle.to_json()).to_session()
        replayed = analyze_process_v2(restored).axes["CONV"].metric
        replayed_with_bundle = analyze_process_v2(restored, replayed_bundle).axes["CONV"].metric
        self.assertEqual((direct.value, direct.status), (0.5, "observed"))
        self.assertEqual((replayed.value, replayed.status), (0.5, "observed"))
        self.assertEqual((replayed_with_bundle.value, replayed_with_bundle.status), (0.5, "observed"))


if __name__ == "__main__":
    unittest.main()

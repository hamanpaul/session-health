"""Focused checks for saved JSON report visualizations."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from lib.html_report import render_saved_html
from lib.report_visualization import load_saved_report, report_from_saved_payload


ROOT = Path(__file__).resolve().parents[1]
AXES = ("SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL")


def _legacy_session(
    session_id: str,
    offset: float = 0.0,
    *,
    axes: dict[str, float] | None = None,
    processing_status: str = "complete",
) -> dict:
    dimensions = dict(axes) if axes is not None else {axis_id: 80.0 + offset for axis_id in AXES}
    return {
        "session_id": session_id,
        "source": "fixture",
        "model": "fixture-model",
        "turn_count": 3,
        "events": {"compactions": 0, "aborts": 0},
        "composite": 82.0 + offset,
        "grade": "B",
        "dimensions": dimensions,
        "stats": {"min": 80.0, "max": 90.0, "stddev": 2.0},
        "processing_status": processing_status,
    }


def _process_session(
    session_id: str,
    *,
    missing: str | None = None,
    values: dict[str, float | None] | None = None,
    statuses: dict[str, str] | None = None,
    denominators: dict[str, int] | None = None,
) -> dict:
    values = {
        axis_id: round(index / 10, 3)
        for index, axis_id in enumerate(AXES, 1)
    } | (values or {})
    if missing is not None:
        values[missing] = None
    statuses = statuses or {}
    denominators = denominators or {}
    axes = {}
    for axis_id in AXES:
        value = values[axis_id]
        status = statuses.get(axis_id, "unknown" if value is None else "observed")
        denominator = denominators.get(axis_id, 10)
        axes[axis_id] = {
            "axis_id": axis_id,
            "version": "process-v2.1",
            "metric": {
                "numerator": round(value * denominator, 3) if value is not None else None,
                "excluded_count": 0,
                "denominator": denominator if value is not None else None,
                "value": value,
                "coverage": 1.0 if value is not None else None,
                "applicability": "applicable" if status == "observed" else status,
                "status": status,
                "reason": "fixture",
            },
            "observed_facts": {},
            "inference": {"quality_judgment": None},
            "evidence_refs": [],
            "notes": [],
        }
    observed_axis_count = sum(
        status == "observed" and values[axis_id] is not None
        for axis_id, status in ((axis_id, statuses.get(axis_id, "unknown" if values[axis_id] is None else "observed")) for axis_id in AXES)
    )
    return {
        "session_id": session_id,
        "source": "fixture",
        "model": "fixture-model",
        "turn_count": 3,
        "profile": "process-v2",
        "processing_status": "complete",
        "process_v2": {
            "profile": "process-v2",
            "version": "process-v2.1",
            "status": "complete",
            "axes": axes,
            "observed_facts": {},
            "inference": {"correctness_judgment": None},
            "external_outcome": {"status": "not_requested"},
            "coverage": {"axis_count": 7, "observed_axis_count": observed_axis_count},
            "processing": {"mode": "offline"},
        },
    }


def _visible_svg_text(rendered: str, class_name: str) -> str:
    start = rendered.index(f'class="{class_name}"')
    end = rendered.index("</svg>", start)
    return " ".join(re.sub(r"<[^>]+>", " ", rendered[start:end]).split())


def _assert_axis_summary(
    testcase: unittest.TestCase,
    svg_text: str,
    axis_id: str,
    value: str,
    coverage: str,
) -> None:
    testcase.assertRegex(
        svg_text,
        rf"\b{re.escape(axis_id)}\b.*?{re.escape(value)}.*?{re.escape(coverage)}",
    )


class SavedReportHtmlTest(unittest.TestCase):
    def test_failed_analyzer_projection_preserves_error_and_diagnostics(self) -> None:
        failure = {
            "agent_name": "fixture-analyzer",
            "success": False,
            "error": "fixture timeout",
            "raw_response": "partial output",
            "requested_model": "fixture-model",
            "actual_model": None,
            "diagnostics": [{"kind": "timeout", "status": "failed"}],
            "attempts": [{"status": "failed", "error_kind": "timeout"}],
            "native_usage": {"input_tokens": None, "output_tokens": None},
        }
        single = {**_legacy_session("failed-analysis"), "agent_analysis": failure}
        batch = {"report_kind": "batch", "sessions": [single], "agent_analysis": failure}
        for payload in (single, batch):
            with self.subTest(kind=payload.get("report_kind", "single")):
                report = report_from_saved_payload(payload)
                self.assertIsNotNone(report.agent_analysis)
                restored = report.agent_analysis.to_dict()
                for field, value in failure.items():
                    self.assertEqual(restored[field], value)

    def test_legacy_batch_has_one_mean_radar_and_per_session_comparison(self) -> None:
        first = _legacy_session(
            "legacy-a",
            axes={
                "SNR": 20.0,
                "STATE": 40.0,
                "CTX": 60.0,
                "REACT": 80.0,
                "DEPTH": 10.0,
                "CONV": 30.0,
                "TOOL": 50.0,
            },
        )
        second = _legacy_session(
            "legacy-b",
            axes={
                "SNR": 40.0,
                "STATE": 60.0,
                "CTX": 80.0,
                "REACT": 20.0,
                "DEPTH": 70.0,
                "CONV": 50.0,
                "TOOL": 90.0,
            },
        )
        failed = _legacy_session(
            "legacy-failed",
            axes={axis_id: 100.0 for axis_id in AXES},
            processing_status="failed",
        )
        payload = {
            "report_kind": "batch",
            "target_kind": "sessions_dir",
            "profile": "legacy",
            "sessions": [first, second, failed],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "legacy.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            rendered = render_saved_html(str(source), input_kind="batch")

        self.assertEqual(rendered.count('class="legacy-batch-radar"'), 1)
        self.assertEqual(rendered.count('class="legacy-mini-radar"'), 0)
        self.assertNotIn('class="legacy-radar-grid"', rendered)
        self.assertIn('class="batch-comparison"', rendered)
        self.assertEqual(rendered.count('class="legacy-bar"'), 2)
        aggregate = _visible_svg_text(rendered, "legacy-batch-radar")
        for axis_id, mean in {
            "SNR": 30,
            "STATE": 50,
            "CTX": 70,
            "REACT": 50,
            "DEPTH": 40,
            "CONV": 40,
            "TOOL": 70,
        }.items():
            _assert_axis_summary(self, aggregate, axis_id, str(mean), "2/2")
        self.assertIn("legacy heuristic composite", rendered)

    def test_process_single_uses_observed_ratio_polar_view_only_when_complete(self) -> None:
        complete = _process_session("process-complete")
        missing = _process_session("process-missing", missing="CTX")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete_path = root / "complete.json"
            missing_path = root / "missing.json"
            complete_path.write_text(json.dumps(complete), encoding="utf-8")
            missing_path.write_text(json.dumps(missing), encoding="utf-8")
            complete_html = render_saved_html(str(complete_path), input_kind="single")
            missing_html = render_saved_html(str(missing_path), input_kind="single")

        self.assertIn('class="process-polar"', complete_html)
        self.assertEqual(complete_html.count('class="process-polar"'), 1)
        self.assertIn("SNR 10.0%", complete_html)
        self.assertIn("TOOL 70.0%", complete_html)
        self.assertIn("not a calibrated quality score", complete_html)
        self.assertIn('class="process-bars"', complete_html)
        self.assertNotIn('class="process-polar"', missing_html)
        self.assertIn('data-axis="CTX" data-status="unknown"', missing_html)
        self.assertIn(">null<", missing_html)
        self.assertNotIn("width=\"0.0\"", missing_html)

    def test_process_batch_has_one_mean_radar_with_observed_coverage(self) -> None:
        first = _process_session(
            "process-mean-a",
            values={
                "SNR": 0.2,
                "STATE": 0.4,
                "CTX": 0.6,
                "REACT": 0.8,
                "DEPTH": 0.1,
                "CONV": 0.3,
                "TOOL": 0.5,
            },
            denominators={axis_id: 10 for axis_id in AXES},
        )
        second = _process_session(
            "process-mean-b",
            values={
                "SNR": 0.8,
                "STATE": 0.6,
                "CTX": None,
                "REACT": 0.2,
                "DEPTH": 0.9,
                "CONV": None,
                "TOOL": 0.7,
            },
            statuses={"CTX": "unknown", "CONV": "not_applicable"},
            denominators={axis_id: 20 for axis_id in AXES},
        )
        third = _process_session(
            "process-mean-c",
            values={
                "SNR": None,
                "STATE": 0.8,
                "CTX": 0.4,
                "REACT": 0.4,
                "DEPTH": 0.6,
                "CONV": 0.6,
                "TOOL": 0.8,
            },
            statuses={"SNR": "unknown"},
            denominators={axis_id: 5 for axis_id in AXES},
        )
        payload = {
            "report_kind": "batch",
            "profile": "process-v2",
            "sessions": [first, second, third],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "process-mean.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            rendered = render_saved_html(str(source), input_kind="batch")

        self.assertEqual(rendered.count('class="process-batch-radar"'), 1)
        self.assertEqual(rendered.count('class="batch-heatmap"'), 1)
        self.assertEqual(rendered.count('class="process-coverage"'), 1)
        aggregate = _visible_svg_text(rendered, "process-batch-radar")
        for axis_id, mean, coverage in (
            ("SNR", "50.0%", "2/3"),
            ("STATE", "60.0%", "3/3"),
            ("CTX", "50.0%", "2/3"),
            ("REACT", "46.7%", "3/3"),
            ("DEPTH", "53.3%", "3/3"),
            ("CONV", "45.0%", "2/3"),
            ("TOOL", "66.7%", "3/3"),
        ):
            _assert_axis_summary(self, aggregate, axis_id, mean, coverage)
        self.assertIn("process-mean-a SNR: 0.200 observed ratio", rendered)
        self.assertIn("Process-v2 observed axis coverage across sessions", rendered)

    def test_process_batch_omits_polygon_when_axis_has_no_observation(self) -> None:
        sessions = [
            _process_session(
                "process-gap-a",
                values={"CTX": None},
                statuses={"CTX": "unknown"},
            ),
            _process_session(
                "process-gap-b",
                values={"CTX": None},
                statuses={"CTX": "not_applicable"},
            ),
        ]
        payload = {"report_kind": "batch", "profile": "process-v2", "sessions": sessions}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "process-gap.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            rendered = render_saved_html(str(source), input_kind="batch")

        self.assertEqual(rendered.count('class="process-batch-radar"'), 1)
        aggregate = _visible_svg_text(rendered, "process-batch-radar")
        self.assertNotIn('class="batch-radar-data"', rendered)
        _assert_axis_summary(self, aggregate, "CTX", "—", "0/2")
        self.assertIn("No aggregate polygon is shown", rendered)
        self.assertIn('class="batch-heatmap"', rendered)
        coverage = _visible_svg_text(rendered, "process-coverage")
        self.assertRegex(coverage, r"\bCTX\b.*?0/2")
        self.assertIn("data-status=\"unknown\"", rendered)
        self.assertIn("data-status=\"not_applicable\"", rendered)

    def test_process_batch_and_directory_cli_render_saved_data(self) -> None:
        first = _process_session("process-a")
        second = _process_session("process-b", missing="DEPTH")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "saved-sessions"
            input_dir.mkdir()
            (input_dir / "a.json").write_text(json.dumps(first), encoding="utf-8")
            (input_dir / "b.json").write_text(json.dumps(second), encoding="utf-8")
            output = root / "rendered.html"
            run = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "render_saved_report.py"),
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output),
                    "--input-kind",
                    "directory",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(output.is_file(), run.stdout)
            rendered = output.read_text(encoding="utf-8")
            restored = load_saved_report(input_dir, input_kind="directory")
            self.assertEqual(restored.artifact_sources["saved_input"], "saved-sessions")
            self.assertNotIn(str(root), rendered)

        self.assertIn("HTML report saved to", run.stdout)
        self.assertIn('class="batch-heatmap"', rendered)
        self.assertIn('class="process-coverage"', rendered)
        self.assertEqual(rendered.count('class="process-batch-radar"'), 1)
        self.assertIn('data-axis="DEPTH"', rendered)
        self.assertIn('data-status="unknown"', rendered)


if __name__ == "__main__":
    unittest.main()

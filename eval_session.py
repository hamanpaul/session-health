#!/usr/bin/env python3
"""session-health: Agent CLI Session 動態 Prompt 品質量化評估工具.

Usage:
    eval_session.py <session-id-or-path>         # Session ID / JSONL / session dir / sessions dir
    eval_session.py --dir <dir>                  # Legacy-compatible batch mode
    eval_session.py --latest N                   # Evaluate the N most recent sessions
    eval_session.py --latest N --source codex    # Only Codex sessions
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import sys
from pathlib import Path
from typing import Any, List, Tuple

# Add parent dir to path for relative imports
sys.path.insert(0, str(Path(__file__).parent))

from lib.parser_base import (
    MAX_SESSION_INPUT_BYTES,
    MAX_SESSION_RECORD_CHARS,
    MAX_SESSION_RECORDS,
    Session,
    SessionInputLimits,
)
from lib.parser_codex import parse_codex_session
from lib.parser_copilot import parse_copilot_session
from lib.bundle import BundleError, BundleLimits, SessionBundle, build_session_bundle, export_bundle, import_bundle
from lib.metrics.process_v2 import analyze_process_v2
from lib.scorer import score_session, SessionScore
from lib.report_types import BatchReport, SessionReport
from lib.problemmap import (
    build_batch_diagnosis_summary,
    build_diagnosis_summary,
    build_evidence_summary,
    diagnose_problemmap,
)
from lib.radar import render_report_terminal, render_table, render_json
from lib.html_report import render_html
from lib.agent_analysis import (
    AGENT_CHAIN,
    build_repair_callback,
    build_stage2_context,
    discover_agent_catalog,
    catalog_payload,
    operator_catalog,
    prepare_analysis_prompt,
    prepare_batch_analysis_prompt,
    call_agent,
    postcheck_analysis,
)
from lib.jev_analysis import SemanticEvaluation, evaluate_session_semantic
from lib.postcheck import freeze_evidence
from lib.semantic_backend import SemanticBudget, build_default_backend


def detect_source(path: Path) -> str:
    """Auto-detect whether a JSONL file is from Codex or Copilot CLI."""
    try:
        if path.suffix == ".json" or path.name.endswith(".bundle.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return "unknown"
            if payload.get("schema") == "session-health.session-bundle":
                return str(payload.get("manifest", {}).get("source", "unknown"))
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
            if not first_line:
                return "unknown"
            rec = json.loads(first_line)
            if not isinstance(rec, dict):
                return "unknown"

            # Copilot CLI uses top-level "type" like "session.start"
            if rec.get("type", "").startswith("session."):
                return "copilot"
            # Codex CLI uses "type" in top-level but values like "session_meta"
            if rec.get("type") in ("session_meta",):
                return "codex"
            # Codex also has "payload" wrapping
            if "payload" in rec:
                return "codex"
            # Copilot has "data" wrapping
            if "data" in rec and "type" in rec:
                return "copilot"
    except (json.JSONDecodeError, OSError):
        pass
    return "unknown"


def is_bundle_path(path: Path) -> bool:
    """Return whether a JSON artifact is a SessionBundle."""
    if path.name.endswith(".bundle.json") or path.suffix == ".bundle":
        return True
    if path.suffix != ".json":
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return payload.get("schema") == "session-health.session-bundle"


def parse_session(
    path: Path,
    source: str = "auto",
    *,
    input_limits: SessionInputLimits | None = None,
) -> Session:
    """Parse a session file with auto-detection or explicit source."""
    input_limits = input_limits or SessionInputLimits()
    if is_bundle_path(path):
        return import_bundle(path).to_session()
    if source == "auto":
        source = detect_source(path)

    if source == "codex":
        return parse_codex_session(path, input_limits=input_limits)
    elif source == "copilot":
        return parse_copilot_session(path, input_limits=input_limits)
    else:
        # Try both, prefer whichever produces more turns
        try:
            s1 = parse_codex_session(path, input_limits=input_limits)
        except Exception:
            s1 = Session(id="", source="codex")
        try:
            s2 = parse_copilot_session(path, input_limits=input_limits)
        except Exception:
            s2 = Session(id="", source="copilot")
        return s1 if len(s1.turns) >= len(s2.turns) else s2


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def find_sessions_in_dir(dir_path: Path, source: str = "auto") -> List[Path]:
    """Recursively find all .jsonl session files in a directory."""
    files = sorted(list(dir_path.rglob("*.jsonl")) + list(dir_path.rglob("*.bundle.json")))
    if source != "auto":
        return [f for f in files if f.name.endswith(".bundle.json") or detect_source(f) == source]
    return files


def _resolve_copilot_session_path(path: Path) -> Path:
    """Resolve a copilot session path: if directory, return events.jsonl inside it."""
    if path.is_dir():
        events = path / "events.jsonl"
        if events.exists():
            return events
    return path


def find_session_by_id(
    session_id: str, source: str = "auto"
) -> List[Tuple[Path, str]]:
    """Find session files matching a (partial) session ID."""
    results: List[Tuple[Path, str]] = []

    # Codex CLI: ID is embedded in the JSONL filename or payload
    codex_dir = Path.home() / ".codex" / "sessions"
    if codex_dir.exists() and source in ("auto", "codex"):
        for f in codex_dir.rglob("*.jsonl"):
            if session_id in f.stem:
                results.append((f, "codex"))
                continue
            # Check payload session_meta id
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    first = fh.readline().strip()
                    if first:
                        rec = json.loads(first)
                        sid = rec.get("payload", {}).get("id", "")
                        if sid and session_id in sid:
                            results.append((f, "codex"))
            except (json.JSONDecodeError, OSError):
                pass

    # Copilot CLI: {uuid}.jsonl files or {uuid}/ directories with events.jsonl
    copilot_dir = Path.home() / ".copilot" / "session-state"
    if copilot_dir.exists() and source in ("auto", "copilot"):
        for f in copilot_dir.glob("*.jsonl"):
            if session_id in f.stem:
                results.append((f, "copilot"))
        for d in copilot_dir.iterdir():
            if d.is_dir() and session_id in d.name:
                events = d / "events.jsonl"
                if events.exists():
                    results.append((events, "copilot"))

    return results


def find_latest_sessions(
    n: int, source: str = "auto"
) -> List[Tuple[Path, str]]:
    """Find the N most recent session files across known locations."""
    candidates: List[Tuple[Path, str, float]] = []

    # Codex CLI sessions
    codex_dir = Path.home() / ".codex" / "sessions"
    if codex_dir.exists():
        for f in codex_dir.rglob("*.jsonl"):
            if source in ("auto", "codex"):
                candidates.append((f, "codex", f.stat().st_mtime))

    # Copilot CLI sessions (flat .jsonl files and directory-format sessions)
    copilot_dir = Path.home() / ".copilot" / "session-state"
    if copilot_dir.exists():
        for f in copilot_dir.glob("*.jsonl"):
            if source in ("auto", "copilot"):
                candidates.append((f, "copilot", f.stat().st_mtime))
        for d in copilot_dir.iterdir():
            if d.is_dir() and source in ("auto", "copilot"):
                events = d / "events.jsonl"
                if events.exists():
                    candidates.append((events, "copilot", events.stat().st_mtime))

    # Sort by mtime descending, take top N
    candidates.sort(key=lambda x: x[2], reverse=True)
    return [(c[0], c[1]) for c in candidates[:n]]


def _load_operator_catalog(path: str) -> Any:
    """Read explicit JSON model cards; never interpret shell/config templates."""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid model catalog JSON: {exc}") from exc
    if isinstance(payload, dict) and isinstance(payload.get("candidates"), list):
        payload = payload["candidates"]
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        payload = payload["models"]
    if not isinstance(payload, (list, dict)):
        raise ValueError("model catalog must be a JSON array or object map")
    return payload


def _stage2_evidence(report: SessionReport) -> dict:
    """Return original bounded layers used for both generation and postcheck."""

    bundle = report.portable_bundle
    bundle_payload = bundle.to_dict() if bundle is not None and hasattr(bundle, "to_dict") else {}
    semantic_payload = (
        report.semantic.to_dict()
        if report.semantic is not None and hasattr(report.semantic, "to_dict")
        else {}
    )
    return {
        "bundle": bundle_payload,
        "process_v2": report.process_v2.to_dict() if report.process_v2 is not None else {},
        "semantic": semantic_payload,
        "coverage": {
            "processing_status": report.processing_status,
            "analysis_layers": list(report.analysis_layers),
            "bundle_manifest": dict(report.bundle_manifest),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="eval_session",
        description="Agent CLI Session 動態 Prompt 品質量化評估",
    )

    # Input modes (mutually exclusive)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument(
        "session_target",
        nargs="?",
        metavar="SESSION_OR_PATH",
        help="Session ID, session JSONL file, session dir, or sessions dir",
    )
    group.add_argument(
        "--dir", "-d",
        metavar="DIR",
        help="Evaluate all sessions in directory (recursive)",
    )
    group.add_argument(
        "--latest", "-l",
        type=int,
        metavar="N",
        help="Evaluate the N most recent sessions",
    )
    group.add_argument(
        "--import-bundle",
        metavar="FILE",
        help="Import one portable SessionBundle JSON artifact",
    )

    # Options
    parser.add_argument(
        "--source", "-s",
        choices=["auto", "codex", "copilot"],
        default="auto",
        help="Session source format (default: auto-detect)",
    )
    parser.add_argument(
        "--max-input-bytes",
        type=_positive_int,
        default=MAX_SESSION_INPUT_BYTES,
        metavar="N",
        help="Raw JSONL read budget in bytes (default: 128 MiB; over-budget input is partial)",
    )
    parser.add_argument(
        "--max-input-records",
        type=_positive_int,
        default=MAX_SESSION_RECORDS,
        metavar="N",
        help="Maximum raw JSONL records to construct (default: 50000)",
    )
    parser.add_argument(
        "--max-input-record-chars",
        type=_positive_int,
        default=MAX_SESSION_RECORD_CHARS,
        metavar="N",
        help="Maximum characters in one raw JSONL record (default: 1000000)",
    )
    parser.add_argument(
        "--max-bundle-bytes",
        type=_positive_int,
        default=BundleLimits().max_bytes,
        metavar="N",
        help="Portable bundle byte budget (default: 2000000; evidence is truncated to fit)",
    )
    parser.add_argument(
        "--max-bundle-events",
        type=_positive_int,
        default=BundleLimits().max_events,
        metavar="N",
        help="Portable bundle event budget (default: 10000)",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["radar", "table", "json", "html"],
        default="radar",
        help="Output format (default: radar)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Write output to file (default: stdout; auto-named for html)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show per-turn breakdown",
    )
    parser.add_argument(
        "--analyze", "-a",
        action="store_true",
        help="Run bounded AI agent analysis on the selected session or batch",
    )
    parser.add_argument(
        "--jev",
        action="store_true",
        help="Run bounded optional Jev semantic judgments; does not enable generative analysis",
    )
    parser.add_argument(
        "--jev-model",
        default="",
        metavar="MODEL",
        help="Requested Jev evaluator model identity (recorded, never inferred)",
    )
    parser.add_argument(
        "--jev-endpoint",
        default=None,
        metavar="URL",
        help="Jev endpoint override (default: Typesafe systemone endpoint)",
    )
    parser.add_argument(
        "--jev-max-requests",
        type=_positive_int,
        default=8,
        metavar="N",
        help="Maximum Jev requests for this run (default: 8)",
    )
    parser.add_argument(
        "--jev-max-attempts",
        type=_positive_int,
        default=12,
        metavar="N",
        help="Maximum Jev HTTP attempts for this run (default: 12)",
    )
    parser.add_argument(
        "--jev-max-questions",
        type=_positive_int,
        default=64,
        metavar="N",
        help="Maximum questions in one Jev request (default: 64)",
    )
    parser.add_argument(
        "--jev-max-cases",
        type=_positive_int,
        default=32,
        metavar="N",
        help="Maximum semantic cases (default: 32)",
    )
    parser.add_argument(
        "--jev-timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="Jev request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--test-agent",
        action="store_true",
        help="Use test agent (copilot/gpt-5-mini) instead of production chain",
    )
    parser.add_argument(
        "--analyze-model", "--model",
        dest="analyze_model",
        default="",
        metavar="MODEL",
        help="Explicit analyzer candidate/model override; never silently replaced",
    )
    parser.add_argument(
        "--analyze-max-output-bytes",
        type=_positive_int,
        default=128_000,
        metavar="N",
        help="Maximum analyzer stdout bytes retained (default: 128000)",
    )
    parser.add_argument(
        "--list-models", "--model-catalog",
        dest="list_models",
        action="store_true",
        help="Read-only JSON catalog of concrete analyzer candidates and availability provenance",
    )
    parser.add_argument(
        "--model-catalog-file", "--catalog-file",
        dest="model_catalog_file",
        metavar="FILE",
        help="JSON operator catalog with explicit executor/provider/route/model/settings cards",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Disable model/network analysis and produce only local deterministic results",
    )
    parser.add_argument(
        "--profile",
        choices=["legacy", "process-v2"],
        default="process-v2",
        help="Report profile (default: process-v2; legacy keeps the historical heuristic fields)",
    )
    parser.add_argument(
        "--export-bundle",
        metavar="FILE_OR_DIR",
        help="Export a portable SessionBundle (a directory is used for batch input)",
    )
    parser.add_argument(
        "--outcome-file",
        metavar="FILE",
        help="Optional local JSON outcome fixture joined only on exact session/task identity",
    )

    args = parser.parse_args()

    configured_agent_chain = None
    if args.model_catalog_file:
        try:
            configured_agent_chain = operator_catalog(
                _load_operator_catalog(args.model_catalog_file),
                candidates=AGENT_CHAIN,
            )
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    if args.list_models:
        catalog = configured_agent_chain or discover_agent_catalog(AGENT_CHAIN)
        print(json.dumps(catalog_payload(catalog), ensure_ascii=False, indent=2))
        return 0
    if not any((args.session_target, args.dir, args.latest, args.import_bundle)):
        parser.error("one of SESSION_OR_PATH, --dir, --latest, or --import-bundle is required")

    # Auto-detect format from output filename
    if args.output and args.format == "radar":
        if args.output.endswith(".html") or args.output.endswith(".htm"):
            args.format = "html"
        elif args.output.endswith(".json"):
            args.format = "json"

    # Positional input is deterministic by default.  Model analysis is an
    # explicit opt-in via --analyze; a plain path must never launch an agent.

    use_color = not args.no_color and sys.stdout.isatty()

    # Collect session files to evaluate
    sessions_to_eval: List[Tuple[Path, str, str]] = []

    if args.import_bundle:
        p = Path(args.import_bundle)
        if not p.is_file():
            print(f"Error: not a bundle file: {p}", file=sys.stderr)
            sys.exit(1)
        sessions_to_eval.append((p, "auto", "bundle"))
    elif args.session_target:
        p = Path(args.session_target)
        if p.is_dir():
            # Session dir: use events.jsonl when present; otherwise treat as sessions dir.
            events = p / "events.jsonl"
            if events.exists():
                sessions_to_eval.append((events, args.source, "session_dir"))
            else:
                files = find_sessions_in_dir(p, args.source)
                sessions_to_eval.extend((f, args.source, "sessions_dir") for f in files)
        elif p.exists():
            sessions_to_eval.append((p, args.source, "session_file"))
        else:
            # Treat as session ID and search for it
            found = find_session_by_id(args.session_target, args.source)
            if found:
                sessions_to_eval.extend((path, source, "session_id") for path, source in found)
            else:
                print(f"Error: no file or session ID matching: {args.session_target}", file=sys.stderr)
                sys.exit(1)

    elif args.dir:
        d = Path(args.dir)
        if not d.is_dir():
            print(f"Error: not a directory: {d}", file=sys.stderr)
            sys.exit(1)
        files = find_sessions_in_dir(d, args.source)
        sessions_to_eval.extend((f, args.source, "sessions_dir") for f in files)

    elif args.latest:
        latest_target_kind = "session_file" if args.latest == 1 else "sessions_dir"
        sessions_to_eval.extend(
            (path, source, latest_target_kind)
            for path, source in find_latest_sessions(args.latest, args.source)
        )

    if not sessions_to_eval:
        print("No session files found.", file=sys.stderr)
        sys.exit(1)

    input_limits = SessionInputLimits(
        max_bytes=args.max_input_bytes,
        max_records=args.max_input_records,
        max_record_chars=args.max_input_record_chars,
    )
    bundle_limits = BundleLimits(
        max_bytes=args.max_bundle_bytes,
        max_events=args.max_bundle_events,
    )

    outcome_fixture = None
    if args.outcome_file:
        try:
            outcome_fixture = json.loads(Path(args.outcome_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: invalid outcome fixture: {exc}", file=sys.stderr)
            sys.exit(1)

    # Evaluate each selected session.  Parse failures stay in a batch report so
    # that a partial result cannot be mistaken for complete coverage.
    reports: List[SessionReport] = []
    for path, source, target_kind in sessions_to_eval:
        bundle = None
        processing_diagnostics = []
        try:
            if is_bundle_path(path):
                bundle = import_bundle(path, limits=bundle_limits)
                session = bundle.to_session()
            else:
                session = parse_session(path, source, input_limits=input_limits)
            if args.profile == "process-v2" or args.export_bundle or args.offline or args.jev or args.analyze:
                if bundle is None:
                    bundle = build_session_bundle(session, limits=bundle_limits)
            sc = score_session(session)
            evidence_summary = build_evidence_summary(session, sc)
            problemmap = diagnose_problemmap(session, sc, evidence_summary=evidence_summary)
            diagnosis_summary = build_diagnosis_summary(
                session,
                sc,
                evidence_summary=evidence_summary,
                problemmap=problemmap,
            )
            process_result = (
                analyze_process_v2(session, bundle=bundle, external_outcome=outcome_fixture)
                if args.profile == "process-v2"
                else None
            )
            status = "complete" if session.turns else "failed"
            if status != "failed" and process_result is not None and process_result.status == "failed":
                status = "failed"
            elif status != "failed" and bundle is not None and bundle.coverage.get("input_status") == "failed":
                status = "failed"
            elif status != "failed" and process_result is not None and process_result.status == "partial":
                status = "partial"
            elif status != "failed" and bundle is not None and bundle.coverage.get("input_status") == "partial":
                status = "partial"
            elif session.diagnostics and status == "complete":
                status = "partial"
            processing_diagnostics.extend(session.diagnostics)
            reports.append(
                SessionReport(
                    session=session,
                    score=sc,
                    target_kind=target_kind,
                    problemmap=problemmap,
                    diagnosis_summary=diagnosis_summary,
                    evidence_summary=evidence_summary,
                    artifact_sources={
                        "session_input": path.name,
                        "source_ref": bundle.manifest.get("source_ref", path.name) if bundle else path.name,
                    },
                    sync_status="session-only",
                    profile=args.profile,
                    process_v2=process_result,
                    portable_bundle=bundle,
                    bundle_manifest=bundle.manifest if bundle else {},
                    processing_status=status,
                    processing_diagnostics=processing_diagnostics,
                )
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            print(f"Warning: failed to parse {path}: {message}", file=sys.stderr)
            failed_session = Session(
                id=path.stem,
                source=source if source in ("codex", "copilot") else "unknown",
                source_ref=path.name,
                diagnostics=[{"kind": "parse_failure", "status": "failed", "message": message}],
            )
            failed_score = score_session(failed_session)
            failed_process = analyze_process_v2(failed_session, external_outcome=outcome_fixture) if args.profile == "process-v2" else None
            reports.append(
                SessionReport(
                    session=failed_session,
                    score=failed_score,
                    target_kind=target_kind,
                    evidence_summary={},
                    artifact_sources={"session_input": path.name},
                    sync_status="session-only",
                    profile=args.profile,
                    process_v2=failed_process,
                    processing_status="failed",
                    processing_diagnostics=failed_session.diagnostics,
                )
            )

    if not reports:
        print("No valid sessions found.", file=sys.stderr)
        sys.exit(1)

    batch_target_kinds = {report.target_kind for report in reports}
    batch_report = BatchReport(
        sessions=reports,
        target_kind=batch_target_kinds.pop() if len(batch_target_kinds) == 1 else "mixed",
        profile=args.profile,
        processing_status=(
            "failed" if any(report.processing_status == "failed" for report in reports)
            else "partial" if any(report.processing_status == "partial" for report in reports)
            else "complete"
        ),
        processing_diagnostics=[
            {"session_id": report.score.session_id, "status": report.processing_status, "diagnostics": report.processing_diagnostics}
            for report in reports
            if report.processing_status != "complete"
        ],
        diagnosis_summary=build_batch_diagnosis_summary(reports),
        evidence_summary={
            "session_count": len(reports),
            "evaluated_session_count": sum(report.processing_status != "failed" for report in reports),
            "average_score": _average_evaluated_score(reports),
            "min_score": _extreme_evaluated_score(reports, minimum=True),
            "max_score": _extreme_evaluated_score(reports, minimum=False),
            "primary_families": [
                report.problemmap.atlas.get("primary_family_zh", report.problemmap.atlas.get("primary_family", "未解析"))
                for report in reports
                if report.problemmap is not None
            ],
            "failure_signals": sorted(
                {
                    signal
                    for report in reports
                    for signal in report.evidence_summary.get("candidate_failure_signals", [])
                }
            ),
        },
        artifact_sources={
            "input": (
                args.session_target
                if args.session_target
                else args.dir
                if args.dir
                else args.import_bundle
                if args.import_bundle
                else f"latest:{args.latest}"
            )
        },
        sync_status="session-only",
    )

    semantic_backend = None
    if args.jev:
        semantic_budget = SemanticBudget(
            max_requests=args.jev_max_requests,
            max_attempts=max(args.jev_max_attempts, args.jev_max_requests),
            max_questions=args.jev_max_questions,
            max_cases=args.jev_max_cases,
            timeout_seconds=args.jev_timeout,
        )
        semantic_backend = build_default_backend(
            offline=args.offline,
            endpoint=args.jev_endpoint,
            model=args.jev_model,
        )
        for report in reports:
            try:
                semantic = evaluate_session_semantic(
                    report.session,
                    backend=semantic_backend,
                    budget=semantic_budget,
                    offline=args.offline,
                )
                report.semantic = semantic
                if "semantic" not in report.analysis_layers:
                    report.analysis_layers.append("semantic")
                if semantic.status in {"partial", "failed", "unknown"} and report.processing_status == "complete":
                    report.processing_status = "partial"
                    report.processing_diagnostics.append({
                        "kind": "semantic_processing",
                        "status": semantic.status,
                        "message": "offline facts retained; semantic coverage is incomplete",
                    })
            except Exception as exc:
                # A remote semantic failure must never discard the deterministic
                # report.  Keep the failure bounded and machine-readable.
                report.semantic = SemanticEvaluation(
                    status="failed",
                    live_status="failed",
                    backend=type(semantic_backend).__name__,
                    diagnostics=[{
                        "kind": "semantic_exception",
                        "status": "failed",
                        "message": f"{type(exc).__name__}: {exc}"[:300],
                    }],
                )
                report.processing_status = "partial" if report.processing_status == "complete" else report.processing_status
                report.processing_diagnostics.append({
                    "kind": "semantic_exception",
                    "status": "failed",
                    "message": f"{type(exc).__name__}: {exc}",
                })
        batch_report.analysis_layers = sorted({layer for report in reports for layer in report.analysis_layers} | {"quantitative"})
        batch_report.processing_status = (
            "failed" if any(report.processing_status == "failed" for report in reports)
            else "partial" if any(report.processing_status == "partial" for report in reports)
            else "complete"
        )
        batch_report.processing_diagnostics = [
            {"session_id": report.score.session_id, "status": report.processing_status, "diagnostics": report.processing_diagnostics}
            for report in reports
            if report.processing_status != "complete"
        ]

    if args.analyze and args.offline:
        # Explicit offline mode wins over positional auto-analysis and over an
        # explicit --analyze request.  Keep the reason in the report instead of
        # invoking any external CLI or model transport.
        for report in reports:
            report.analysis_status = "disabled_offline"
            report.processing_diagnostics.append({
                "kind": "analysis_disabled_offline",
                "status": "not_requested",
            })
        batch_report.analysis_status = "disabled_offline"
        batch_report.processing_diagnostics.append({
            "kind": "analysis_disabled_offline",
            "status": "not_requested",
        })
    elif args.analyze:
        if len(reports) == 1:
            report = reports[0]
            problemmap_payload = None
            if report.problemmap is not None:
                problemmap_payload = {
                    "pm1_candidates": report.problemmap.pm1_candidates,
                    "atlas": report.problemmap.atlas,
                    "global_fix_route": report.problemmap.global_fix_route,
                }
            stage2_context = build_stage2_context(
                process_v2=report.process_v2,
                bundle=report.portable_bundle,
                semantic=report.semantic,
            )
            # Freeze before the analyzer runs.  The exact same bounded object is
            # passed to postcheck, including any single repair round.
            frozen_stage2_evidence = freeze_evidence(_stage2_evidence(report))
            prompt = prepare_analysis_prompt(
                report.score,
                report.session,
                diagnosis_summary=asdict(report.diagnosis_summary) if report.diagnosis_summary is not None else None,
                problemmap=problemmap_payload,
                evidence_summary=report.evidence_summary,
                process_v2=report.process_v2,
                bundle=report.portable_bundle,
                semantic=report.semantic,
                stage2_context=stage2_context,
            )
            analysis = call_agent(
                prompt,
                agent_chain=configured_agent_chain,
                test_mode=args.test_agent,
                routing_backend=semantic_backend if args.jev else None,
                routing_budget=semantic_budget if args.jev else None,
                model_override=args.analyze_model,
                use_jev=args.jev,
                max_output_bytes=args.analyze_max_output_bytes,
            )
            report.agent_analysis = analysis
            report.analysis_coverage = {
                "source_session_count": 1,
                "selected_session_count": 1,
                "excluded_session_count": 0,
                "status": "complete" if analysis.success else "failed",
            }
            analysis.coverage = dict(report.analysis_coverage)
            report.routing = analysis.routing
            report.analysis_status = "completed" if analysis.success else "failed"
            if analysis.success and "agent" not in report.analysis_layers:
                report.analysis_layers.append("agent")
            if report.routing is not None and "routing" not in report.analysis_layers:
                report.analysis_layers.append("routing")
            if args.jev and analysis.success:
                report.postcheck = postcheck_analysis(
                    frozen_stage2_evidence,
                    analysis,
                    backend=semantic_backend,
                    budget=semantic_budget,
                    repair=build_repair_callback(analysis),
                )
                analysis.postcheck = report.postcheck
                if "postcheck" not in report.analysis_layers:
                    report.analysis_layers.append("postcheck")
                if report.postcheck.status in {"failed", "partial", "unknown", "deferred"} and report.processing_status == "complete":
                    report.processing_status = "partial"
                    report.processing_diagnostics.append({
                        "kind": "postcheck_processing",
                        "status": report.postcheck.status,
                        "message": "deterministic facts retained; generated claim verification is incomplete",
                    })
            if not analysis.success and report.processing_status == "complete":
                report.processing_status = "partial"
                report.processing_diagnostics.append({
                    "kind": "analysis_processing",
                    "status": "failed",
                    "message": analysis.error,
                })
        else:
            session_summaries = []
            for report in reports:
                session_summaries.append(
                    {
                        "session_id": report.score.session_id or "unknown",
                        "score": round(report.score.composite, 1),
                        "grade": report.score.grade,
                        "primary_family": (
                            report.problemmap.atlas.get("primary_family_zh", report.problemmap.atlas.get("primary_family", "未解析"))
                            if report.problemmap is not None
                            else "未解析"
                        ),
                        "weak_dimensions": list(report.evidence_summary.get("weak_dimensions", {}).keys()),
                        "route": (
                            report.problemmap.global_fix_route.get("minimal_fix_zh", report.problemmap.global_fix_route.get("minimal_fix", "無"))
                            if report.problemmap is not None
                            else "無"
                        ),
                    }
                )
            stage2_contexts = [
                build_stage2_context(
                    process_v2=report.process_v2,
                    bundle=report.portable_bundle,
                    semantic=report.semantic,
                )
                for report in reports
            ]
            frozen_batch_evidence = freeze_evidence(
                {
                    "sessions": [_stage2_evidence(report) for report in reports],
                    "coverage": {
                        "source_session_count": len(reports),
                        "selected_session_count": len(reports),
                        "excluded_session_count": 0,
                    },
                }
            )
            prompt = prepare_batch_analysis_prompt(
                batch_report.evidence_summary,
                session_summaries,
                diagnosis_summary=asdict(batch_report.diagnosis_summary) if batch_report.diagnosis_summary is not None else None,
                stage2_contexts=stage2_contexts,
            )
            batch_report.agent_analysis = call_agent(
                prompt,
                agent_chain=configured_agent_chain,
                test_mode=args.test_agent,
                routing_backend=semantic_backend if args.jev else None,
                routing_budget=semantic_budget if args.jev else None,
                model_override=args.analyze_model,
                use_jev=args.jev,
                max_output_bytes=args.analyze_max_output_bytes,
            )
            batch_report.analysis_status = "completed" if batch_report.agent_analysis.success else "failed"
            batch_report.analysis_coverage = {
                "source_session_count": len(reports),
                "selected_session_count": len(reports),
                "excluded_session_count": 0,
                "status": "complete" if batch_report.agent_analysis.success else "failed",
            }
            batch_report.agent_analysis.coverage = dict(batch_report.analysis_coverage)
            batch_report.routing = batch_report.agent_analysis.routing
            if args.jev and batch_report.agent_analysis.success:
                batch_report.postcheck = postcheck_analysis(
                    frozen_batch_evidence,
                    batch_report.agent_analysis,
                    backend=semantic_backend,
                    budget=semantic_budget,
                    repair=build_repair_callback(batch_report.agent_analysis),
                )
                batch_report.agent_analysis.postcheck = batch_report.postcheck
                if batch_report.postcheck.status in {"failed", "partial", "unknown", "deferred"} and batch_report.processing_status == "complete":
                    batch_report.processing_status = "partial"
                    batch_report.processing_diagnostics.append({
                        "kind": "postcheck_processing",
                        "status": batch_report.postcheck.status,
                        "message": "deterministic facts retained; generated batch claims are not fully verified",
                    })
            if not batch_report.agent_analysis.success and batch_report.processing_status == "complete":
                batch_report.processing_status = "partial"
                batch_report.processing_diagnostics.append({
                    "kind": "analysis_processing",
                    "status": "failed",
                    "message": batch_report.agent_analysis.error,
                })

        layers = set(layer for item in reports for layer in item.analysis_layers)
        if batch_report.agent_analysis is not None and batch_report.agent_analysis.success:
            layers.add("agent")
        if batch_report.routing is not None:
            layers.add("routing")
        if batch_report.postcheck is not None:
            layers.add("postcheck")
        batch_report.analysis_layers = sorted(layers)

    if args.export_bundle:
        export_target = Path(args.export_bundle)
        if len(reports) == 1 and (export_target.suffix or not export_target.exists()):
            export_bundle(reports[0].session, export_target, limits=bundle_limits)
            print(f"✓ SessionBundle exported to: {export_target}", file=sys.stderr)
        else:
            export_target.mkdir(parents=True, exist_ok=True)
            used_names: set[str] = set()
            export_manifest: List[dict] = []
            for index, report in enumerate(reports, 1):
                source = (report.session.source or "unknown").replace("/", "_")
                safe_id = (report.score.session_id or "session").replace("/", "_")
                artifact_id = str(report.bundle_manifest.get("artifact_id", ""))[:12]
                stem = "-".join(part for part in (source, safe_id, artifact_id) if part) or f"session-{index}"
                filename = f"{stem}.bundle.json"
                suffix = 2
                while filename in used_names or (export_target / filename).exists():
                    filename = f"{stem}-{suffix}.bundle.json"
                    suffix += 1
                used_names.add(filename)
                export_bundle(report.session, export_target / filename, limits=bundle_limits)
                export_manifest.append(
                    {
                        "input": report.artifact_sources.get("session_input", "unknown"),
                        "session_id": report.score.session_id or None,
                        "source": report.session.source,
                        "processing_status": report.processing_status,
                        "artifact": filename,
                    }
                )
            (export_target / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "session-health.bundle-export-manifest",
                        "version": "1.0",
                        "processing_status": batch_report.processing_status,
                        "sessions": export_manifest,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            print(f"✓ SessionBundles exported to: {export_target}", file=sys.stderr)

    # Output
    if args.format == "html":
        for index, report in enumerate(reports):
            print(render_report_terminal(report, use_color))
            if index < len(reports) - 1:
                print()

        html_content = render_html(batch_report if len(reports) > 1 else reports[0])
        out_path = args.output
        if not out_path:
            if len(reports) == 1:
                safe_id = (reports[0].score.session_id or "session")[:16].replace("/", "_")
                out_path = f"session-health-{safe_id}.html"
            else:
                out_path = "session-health-batch.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"\n✓ HTML report saved to: {out_path}", file=sys.stderr)
    elif args.format == "radar":
        for index, report in enumerate(reports):
            print(render_report_terminal(report, use_color))
            if index < len(reports) - 1:
                print()
    elif args.format == "table":
        print(render_table(batch_report if len(reports) > 1 else reports[0], use_color))
    elif args.format == "json":
        print(render_json(batch_report if len(reports) > 1 else reports[0]))

    if args.verbose and args.format not in ("json",):
        for index, report in enumerate(reports):
            _print_turn_breakdown(report.score, use_color)
            if index < len(reports) - 1:
                print()

    # Batch summary
    if len(reports) > 1 and args.format != "json":
        _print_batch_summary(reports, use_color)

    if any(report.processing_status == "failed" for report in reports):
        return 1
    if any(report.processing_status == "partial" for report in reports):
        return 2
    return 0


def _print_turn_breakdown(sc: SessionScore, use_color: bool) -> None:
    """Print per-turn score breakdown."""
    print(f"\n  Turn breakdown ({sc.turn_count} turns):")
    print(f"  {'#':>4s} {'SNR':>6s} {'STATE':>6s} {'CTX':>6s} {'REACT':>6s} {'DEPTH':>6s} {'TOOL':>6s} {'COMP':>6s}")
    print(f"  {'─'*4} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6}")
    for ts in sc.turn_scores:
        print(
            f"  {ts.index:4d} "
            f"{ts.snr:6.1f} {ts.state:6.1f} {ts.context:6.1f} "
            f"{ts.reaction:6.1f} {ts.depth:6.1f} {ts.tool_efficiency:6.1f} {ts.composite:6.1f}"
        )


def _average_evaluated_score(reports: List[SessionReport]) -> float | None:
    scores = [report.score.composite for report in reports if report.processing_status != "failed"]
    return round(sum(scores) / len(scores), 1) if scores else None


def _extreme_evaluated_score(reports: List[SessionReport], *, minimum: bool) -> float | None:
    scores = [report.score.composite for report in reports if report.processing_status != "failed"]
    if not scores:
        return None
    value = min(scores) if minimum else max(scores)
    return round(value, 1)


def _print_batch_summary(reports: List[SessionReport], use_color: bool) -> None:
    """Print summary for batch evaluation."""
    complete = sum(report.processing_status == "complete" for report in reports)
    partial = sum(report.processing_status == "partial" for report in reports)
    failed = sum(report.processing_status == "failed" for report in reports)

    print("=" * 52)
    print(f"Batch Summary: {len(reports)} selected; {complete} complete, {partial} partial, {failed} failed")
    if reports and all(report.profile != "legacy" for report in reports):
        print("  Process-v2 axis observations:")
        for axis_id in ("SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL"):
            present = 0
            observed = 0
            for report in reports:
                axis = report.process_v2.axes.get(axis_id) if report.process_v2 is not None else None
                if axis is not None:
                    present += 1
                    if axis.metric.status == "observed":
                        observed += 1
            print(f"    {axis_id}: {observed}/{present} observed")
        return

    import statistics
    evaluated = [report.score.composite for report in reports if report.processing_status != "failed"]
    avg = statistics.mean(evaluated) if evaluated else None
    med = statistics.median(evaluated) if evaluated else None
    print(f"  Evaluated: {len(evaluated)}")
    print(f"  Mean:   {'unknown' if avg is None else f'{avg:.1f}'}")
    print(f"  Median: {'unknown' if med is None else f'{med:.1f}'}")
    print(f"  Min:    {'unknown' if not evaluated else f'{min(evaluated):.1f}'}")
    print(f"  Max:    {'unknown' if not evaluated else f'{max(evaluated):.1f}'}")
    if len(evaluated) > 1:
        print(f"  StdDev: {statistics.stdev(evaluated):.1f}")

    if reports and all(report.profile == "legacy" for report in reports):
        grades = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for report in reports:
            if report.processing_status != "failed":
                grades[report.score.grade] += 1
        print(f"\n  Grade distribution:")
        for g in ["A", "B", "C", "D", "F"]:
            if grades[g] > 0:
                bar = "█" * grades[g]
                print(f"    {g}: {grades[g]:3d} {bar}")


if __name__ == "__main__":
    raise SystemExit(main())

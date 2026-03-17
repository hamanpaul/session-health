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
from typing import List, Tuple

# Add parent dir to path for relative imports
sys.path.insert(0, str(Path(__file__).parent))

from lib.parser_base import Session
from lib.parser_codex import parse_codex_session
from lib.parser_copilot import parse_copilot_session
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
    prepare_analysis_prompt,
    prepare_batch_analysis_prompt,
    call_agent,
)


def detect_source(path: Path) -> str:
    """Auto-detect whether a JSONL file is from Codex or Copilot CLI."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
            if not first_line:
                return "unknown"
            rec = json.loads(first_line)

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


def parse_session(path: Path, source: str = "auto") -> Session:
    """Parse a session file with auto-detection or explicit source."""
    if source == "auto":
        source = detect_source(path)

    if source == "codex":
        return parse_codex_session(path)
    elif source == "copilot":
        return parse_copilot_session(path)
    else:
        # Try both, prefer whichever produces more turns
        try:
            s1 = parse_codex_session(path)
        except Exception:
            s1 = Session(id="", source="codex")
        try:
            s2 = parse_copilot_session(path)
        except Exception:
            s2 = Session(id="", source="copilot")
        return s1 if len(s1.turns) >= len(s2.turns) else s2


def find_sessions_in_dir(dir_path: Path, source: str = "auto") -> List[Path]:
    """Recursively find all .jsonl session files in a directory."""
    files = sorted(dir_path.rglob("*.jsonl"))
    if source != "auto":
        return [f for f in files if detect_source(f) == source]
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


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="eval_session",
        description="Agent CLI Session 動態 Prompt 品質量化評估",
    )

    # Input modes (mutually exclusive)
    group = parser.add_mutually_exclusive_group(required=True)
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

    # Options
    parser.add_argument(
        "--source", "-s",
        choices=["auto", "codex", "copilot"],
        default="auto",
        help="Session source format (default: auto-detect)",
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
        help="Run AI agent analysis on the session (single session only)",
    )
    parser.add_argument(
        "--test-agent",
        action="store_true",
        help="Use test agent (copilot/gpt-5-mini) instead of production chain",
    )

    args = parser.parse_args()
    explicit_format = any(flag in sys.argv[1:] for flag in ("--format", "-f"))
    explicit_analyze = any(flag in sys.argv[1:] for flag in ("--analyze", "-a", "--test-agent"))

    # Auto-detect format from output filename
    if args.output and args.format == "radar":
        if args.output.endswith(".html") or args.output.endswith(".htm"):
            args.format = "html"
        elif args.output.endswith(".json"):
            args.format = "json"

    # One-command flow: session ID / path alone produces terminal summary + HTML bundle.
    if args.session_target and not explicit_format and not explicit_analyze and not args.output:
        args.format = "html"
        args.analyze = True

    use_color = not args.no_color and sys.stdout.isatty()

    # Collect session files to evaluate
    sessions_to_eval: List[Tuple[Path, str, str]] = []

    if args.session_target:
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

    # Evaluate each session
    reports: List[SessionReport] = []
    for path, source, target_kind in sessions_to_eval:
        try:
            session = parse_session(path, source)
            if not session.turns:
                continue
            sc = score_session(session)
            evidence_summary = build_evidence_summary(session, sc)
            problemmap = diagnose_problemmap(session, sc, evidence_summary=evidence_summary)
            diagnosis_summary = build_diagnosis_summary(
                session,
                sc,
                evidence_summary=evidence_summary,
                problemmap=problemmap,
            )
            reports.append(
                SessionReport(
                    session=session,
                    score=sc,
                    target_kind=target_kind,
                    problemmap=problemmap,
                    diagnosis_summary=diagnosis_summary,
                    evidence_summary=evidence_summary,
                    artifact_sources={"session_input": str(path)},
                    sync_status="session-only",
                )
            )
        except Exception as e:
            print(f"Warning: failed to parse {path}: {e}", file=sys.stderr)
            continue

    if not reports:
        print("No valid sessions found.", file=sys.stderr)
        sys.exit(1)

    batch_target_kinds = {report.target_kind for report in reports}
    batch_report = BatchReport(
        sessions=reports,
        target_kind=batch_target_kinds.pop() if len(batch_target_kinds) == 1 else "mixed",
        diagnosis_summary=build_batch_diagnosis_summary(reports),
        evidence_summary={
            "session_count": len(reports),
            "average_score": round(sum(report.score.composite for report in reports) / len(reports), 1),
            "min_score": round(min(report.score.composite for report in reports), 1),
            "max_score": round(max(report.score.composite for report in reports), 1),
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
                else f"latest:{args.latest}"
            )
        },
        sync_status="session-only",
    )

    if args.analyze:
        if len(reports) == 1:
            report = reports[0]
            problemmap_payload = None
            if report.problemmap is not None:
                problemmap_payload = {
                    "pm1_candidates": report.problemmap.pm1_candidates,
                    "atlas": report.problemmap.atlas,
                    "global_fix_route": report.problemmap.global_fix_route,
                }
            prompt = prepare_analysis_prompt(
                report.score,
                report.session,
                diagnosis_summary=asdict(report.diagnosis_summary) if report.diagnosis_summary is not None else None,
                problemmap=problemmap_payload,
                evidence_summary=report.evidence_summary,
            )
            analysis = call_agent(prompt, test_mode=args.test_agent)
            report.agent_analysis = analysis
            if analysis.success and "agent" not in report.analysis_layers:
                report.analysis_layers.append("agent")
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
            prompt = prepare_batch_analysis_prompt(
                batch_report.evidence_summary,
                session_summaries,
                diagnosis_summary=asdict(batch_report.diagnosis_summary) if batch_report.diagnosis_summary is not None else None,
            )
            batch_report.agent_analysis = call_agent(prompt, test_mode=args.test_agent)

        layers = set(layer for item in reports for layer in item.analysis_layers)
        if batch_report.agent_analysis is not None and batch_report.agent_analysis.success:
            layers.add("agent")
        batch_report.analysis_layers = sorted(layers)

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
        _print_batch_summary([report.score for report in reports], use_color)


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


def _print_batch_summary(scores: List[SessionScore], use_color: bool) -> None:
    """Print summary for batch evaluation."""
    import statistics
    composites = [s.composite for s in scores]
    avg = statistics.mean(composites)
    med = statistics.median(composites)

    print("=" * 52)
    print(f"Batch Summary: {len(scores)} sessions evaluated")
    print(f"  Mean:   {avg:.1f}")
    print(f"  Median: {med:.1f}")
    print(f"  Min:    {min(composites):.1f}")
    print(f"  Max:    {max(composites):.1f}")
    if len(composites) > 1:
        print(f"  StdDev: {statistics.stdev(composites):.1f}")

    # Grade distribution
    grades = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
    for s in scores:
        grades[s.grade] += 1
    print(f"\n  Grade distribution:")
    for g in ["A", "B", "C", "D", "F"]:
        if grades[g] > 0:
            bar = "█" * grades[g]
            print(f"    {g}: {grades[g]:3d} {bar}")


if __name__ == "__main__":
    main()

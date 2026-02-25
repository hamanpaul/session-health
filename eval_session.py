#!/usr/bin/env python3
"""session-health: Agent CLI Session 動態 Prompt 品質量化評估工具.

Usage:
    eval_session.py <session.jsonl>              # Evaluate a single session
    eval_session.py --dir <dir>                  # Batch evaluate all sessions in dir
    eval_session.py --latest N                   # Evaluate the N most recent sessions
    eval_session.py --latest N --source codex    # Only Codex sessions
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import List, Tuple

# Add parent dir to path for relative imports
sys.path.insert(0, str(Path(__file__).parent))

from lib.parser_base import Session
from lib.parser_codex import parse_codex_session
from lib.parser_copilot import parse_copilot_session
from lib.scorer import score_session, SessionScore
from lib.radar import render_radar, render_table, render_json


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

    # Copilot CLI: filename is {uuid}.jsonl
    copilot_dir = Path.home() / ".copilot" / "session-state"
    if copilot_dir.exists() and source in ("auto", "copilot"):
        for f in copilot_dir.glob("*.jsonl"):
            if session_id in f.stem:
                results.append((f, "copilot"))

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

    # Copilot CLI sessions
    copilot_dir = Path.home() / ".copilot" / "session-state"
    if copilot_dir.exists():
        for f in copilot_dir.glob("*.jsonl"):
            if source in ("auto", "copilot"):
                candidates.append((f, "copilot", f.stat().st_mtime))

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
        "session_file",
        nargs="?",
        help="Path to a single session JSONL file",
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
        choices=["radar", "table", "json"],
        default="radar",
        help="Output format (default: radar)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show per-turn breakdown",
    )

    args = parser.parse_args()
    use_color = not args.no_color and sys.stdout.isatty()

    # Collect session files to evaluate
    sessions_to_eval: List[Tuple[Path, str]] = []

    if args.session_file:
        p = Path(args.session_file)
        if p.exists():
            sessions_to_eval.append((p, args.source))
        else:
            # Treat as session ID and search for it
            found = find_session_by_id(args.session_file, args.source)
            if found:
                sessions_to_eval.extend(found)
            else:
                print(f"Error: no file or session ID matching: {args.session_file}", file=sys.stderr)
                sys.exit(1)

    elif args.dir:
        d = Path(args.dir)
        if not d.is_dir():
            print(f"Error: not a directory: {d}", file=sys.stderr)
            sys.exit(1)
        files = find_sessions_in_dir(d, args.source)
        sessions_to_eval.extend((f, args.source) for f in files)

    elif args.latest:
        sessions_to_eval.extend(find_latest_sessions(args.latest, args.source))

    if not sessions_to_eval:
        print("No session files found.", file=sys.stderr)
        sys.exit(1)

    # Evaluate each session
    scores: List[SessionScore] = []
    for path, source in sessions_to_eval:
        try:
            session = parse_session(path, source)
            if not session.turns:
                continue
            sc = score_session(session)
            scores.append(sc)
        except Exception as e:
            print(f"Warning: failed to parse {path}: {e}", file=sys.stderr)
            continue

    if not scores:
        print("No valid sessions found.", file=sys.stderr)
        sys.exit(1)

    # Output
    for sc in scores:
        if args.format == "radar":
            print(render_radar(sc, use_color))
        elif args.format == "table":
            print(render_table(sc, use_color))
        elif args.format == "json":
            print(render_json(sc))

        if args.verbose and args.format != "json":
            _print_turn_breakdown(sc, use_color)

        if len(scores) > 1:
            print()  # separator between sessions

    # Batch summary
    if len(scores) > 1:
        _print_batch_summary(scores, use_color)


def _print_turn_breakdown(sc: SessionScore, use_color: bool) -> None:
    """Print per-turn score breakdown."""
    print(f"\n  Turn breakdown ({sc.turn_count} turns):")
    print(f"  {'#':>4s} {'SNR':>6s} {'STATE':>6s} {'CTX':>6s} {'REACT':>6s} {'DEPTH':>6s} {'COMP':>6s}")
    print(f"  {'─'*4} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*6}")
    for ts in sc.turn_scores:
        print(
            f"  {ts.index:4d} "
            f"{ts.snr:6.1f} {ts.state:6.1f} {ts.context:6.1f} "
            f"{ts.reaction:6.1f} {ts.depth:6.1f} {ts.composite:6.1f}"
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

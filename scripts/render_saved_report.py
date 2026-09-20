#!/usr/bin/env python3
"""Render an already-saved session-health JSON projection as standalone HTML."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib.html_report import render_saved_html  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="render_saved_report.py",
        description=(
            "Render saved session-health JSON without parsing sessions or invoking metrics/models. "
            "INPUT may be a single/batch JSON file or a directory of per-session JSON files."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        metavar="FILE_OR_DIR",
        help="saved single/batch report JSON, or directory of per-session JSON reports",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        metavar="HTML",
        help="new standalone HTML output path",
    )
    parser.add_argument(
        "--input-kind",
        choices=("auto", "single", "batch", "directory"),
        default="auto",
        help="input contract to enforce (default: auto-detect file or directory)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow replacing an existing output file",
    )
    args = parser.parse_args(argv)

    if args.output.exists() and not args.force:
        parser.error(f"output already exists; choose a new path or pass --force: {args.output}")
    try:
        rendered = render_saved_html(str(args.input), input_kind=args.input_kind)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    print(f"HTML report saved to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


"""Signal-to-Noise Ratio (SNR) metric.

Measures the proportion of "noise" in tool outputs within a turn:
- ANSI escape sequences
- Progress bar patterns (█▓░ #= etc.)
- Repeated similar lines (duplicate detection)
- Blank/whitespace-only lines

Score: 0–100 where 100 = perfectly clean (no noise).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from ..parser_base import Turn

# ANSI escape sequence pattern
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?[A-Za-z]")

# Progress bar character classes
_PROGRESS_RE = re.compile(r"[█▓▒░■□●○◆◇#=\-]{5,}")

# Spinner / carriage-return overwrite patterns
_CR_RE = re.compile(r"\r[^\n]")

# npm/pip/apt progress patterns
_PKG_PROGRESS_RE = re.compile(
    r"(added \d+ packages|"
    r"\d+%\s*\||\d+/\d+\s*\[|"
    r"Downloading\s+.*\d+(\.\d+)?\s*(kB|MB|GB)|"
    r"Installing\s+collected\s+packages|"
    r"(GET|Fetched)\s+https?://)",
    re.IGNORECASE,
)


@dataclass
class SNRResult:
    """Result of SNR analysis for a single turn."""
    total_chars: int = 0
    noise_chars: int = 0
    ansi_chars: int = 0
    progress_chars: int = 0
    duplicate_chars: int = 0
    score: float = 100.0  # 0–100

    @property
    def noise_ratio(self) -> float:
        if self.total_chars == 0:
            return 0.0
        return self.noise_chars / self.total_chars


def analyze_snr(turn: Turn) -> SNRResult:
    """Analyze signal-to-noise ratio for a turn's tool outputs."""
    result = SNRResult()

    # Collect all tool output text
    outputs: List[str] = []
    for tc in turn.tool_calls:
        if tc.output:
            outputs.append(tc.output)

    if not outputs:
        return result  # No tool output → perfect score

    full_text = "\n".join(outputs)
    result.total_chars = len(full_text)

    if result.total_chars == 0:
        return result

    # 1. Count ANSI escape sequence chars
    for m in _ANSI_RE.finditer(full_text):
        result.ansi_chars += len(m.group())

    # 2. Count progress bar chars
    clean_text = _ANSI_RE.sub("", full_text)
    for m in _PROGRESS_RE.finditer(clean_text):
        result.progress_chars += len(m.group())

    # 3. Count package manager progress lines
    for m in _PKG_PROGRESS_RE.finditer(clean_text):
        result.progress_chars += len(m.group())

    # 4. Count carriage-return overwrite noise
    cr_count = len(_CR_RE.findall(full_text))
    result.progress_chars += cr_count * 20  # estimate per CR-overwrite

    # 5. Detect duplicate lines
    lines = clean_text.split("\n")
    result.duplicate_chars = _count_duplicate_chars(lines)

    # Total noise
    result.noise_chars = result.ansi_chars + result.progress_chars + result.duplicate_chars
    result.noise_chars = min(result.noise_chars, result.total_chars)

    # Score: 100 = clean, 0 = all noise
    ratio = result.noise_chars / result.total_chars
    result.score = max(0.0, (1.0 - ratio) * 100)

    return result


def _count_duplicate_chars(lines: List[str], threshold: int = 3) -> int:
    """Count chars from runs of >= threshold identical or near-identical lines."""
    dup_chars = 0
    if len(lines) < threshold:
        return 0

    run_start = 0
    for i in range(1, len(lines)):
        if _is_similar(lines[i], lines[run_start]):
            if i - run_start + 1 >= threshold:
                # All lines beyond the first in this run are duplicates
                dup_chars += len(lines[i])
        else:
            run_start = i

    return dup_chars


def _is_similar(a: str, b: str) -> bool:
    """Quick similarity check: identical after stripping, or differ by <= 20%."""
    a_s = a.strip()
    b_s = b.strip()
    if not a_s or not b_s:
        return a_s == b_s
    if a_s == b_s:
        return True
    # Quick length check
    if abs(len(a_s) - len(b_s)) > max(len(a_s), len(b_s)) * 0.2:
        return False
    # Character-level diff (cheap approximation)
    common = sum(1 for ca, cb in zip(a_s, b_s) if ca == cb)
    return common / max(len(a_s), len(b_s)) > 0.8

"""Terminal styling: ANSI helpers, agent color assignment, and powerline-segment
rendering for the status bar. Zero dependencies.

Color activates only on a TTY (and honors NO_COLOR); set HARNESS_FORCE_COLOR=1
to force it. Set HARNESS_NO_POWERLINE=1 if your font lacks the powerline glyph
() — segments fall back to plain separators.
"""

from __future__ import annotations

import os
import sys

ENABLED = os.environ.get("HARNESS_FORCE_COLOR") == "1" or (
    sys.stderr.isatty() and not os.environ.get("NO_COLOR")
)
# Chevron transitions need a powerline-patched font AND a terminal that renders
# the glyph cleanly - opt in with HARNESS_POWERLINE=1. Default is spaced colored
# pills, which look right in any font.
_POWERLINE_SEP = "" if os.environ.get("HARNESS_POWERLINE") == "1" else ""


def _sgr(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if ENABLED else text


def dim(t: str) -> str:
    return _sgr("2", t)


def bold(t: str) -> str:
    return _sgr("1", t)


def fg(n: int, t: str) -> str:
    return _sgr(f"38;5;{n}", t)


def bold_fg(n: int, t: str) -> str:
    return _sgr(f"1;38;5;{n}", t)


# Stable, distinct colors per agent so concurrent subagent lines are scannable.
_AGENT_COLORS = [114, 176, 110, 179, 140, 73, 167, 109]


def agent_color(name: str) -> int:
    return _AGENT_COLORS[sum(name.encode()) % len(_AGENT_COLORS)]


def fmt_tok(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        s = f"{n / 1000:.1f}k"
        return s.replace(".0k", "k")
    return str(n)


def display_tool(name: str) -> str:
    return name.removeprefix("mcp__harness__")


def short_model(name: str | None) -> str:
    """claude-haiku-4-5-20251001 -> haiku-4-5; claude-fable-5[1m] -> fable-5[1m]"""
    import re

    if not name:
        return "?"
    n = name.removeprefix("claude-")
    return re.sub(r"-\d{8,}$", "", n)


def context_window(model: str | None) -> int:
    """Approximate usable context window for the % gauge. [1m] variants get 1M;
    everything else uses the claude CLI's standard 200K window."""
    if model and "[1m]" in model:
        return 1_000_000
    return 200_000


# -- powerline status segments ------------------------------------------------------

# segment: (text, bg_256, fg_256)
Segment = tuple[str, int, int]


def render_powerline(segments: list[Segment], max_width: int) -> str:
    if not segments:
        return ""
    # Drop trailing segments (lowest priority last) until the visible width fits.
    def visible(segs: list[Segment]) -> int:
        return sum(len(t) + 3 for t, _, _ in segs)

    segs = list(segments)
    while len(segs) > 1 and visible(segs) > max_width:
        segs.pop()

    if not ENABLED:
        return " | ".join(t for t, _, _ in segs)[:max_width]

    parts = []
    for i, (text, bg, fgc) in enumerate(segs):
        parts.append(f"\x1b[48;5;{bg}m\x1b[38;5;{fgc}m {text} ")
        if not _POWERLINE_SEP:
            parts.append("\x1b[0m ")
            continue
        if i + 1 < len(segs):
            nxt_bg = segs[i + 1][1]
            parts.append(f"\x1b[48;5;{nxt_bg}m\x1b[38;5;{bg}m{_POWERLINE_SEP}")
        else:
            parts.append(f"\x1b[0m\x1b[38;5;{bg}m{_POWERLINE_SEP}\x1b[0m")
    return "".join(parts)

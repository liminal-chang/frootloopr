"""Terminal styling: ANSI helpers, agent color assignment, and status-segment
rendering for the status bar. Zero dependencies.

Color activates only on a TTY (and honors NO_COLOR); set HARNESS_FORCE_COLOR=1
to force it. Sections are distinguished by text color (no backgrounds), so the
bar stays flush with the terminal.
"""

from __future__ import annotations

import os
import re
import sys

ENABLED = os.environ.get("HARNESS_FORCE_COLOR") == "1" or (
    sys.stderr.isatty() and not os.environ.get("NO_COLOR")
)


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


# context-occupancy meter: green when low, amber mid, red near the limit
_BAR_GREEN, _BAR_AMBER, _BAR_RED = 78, 214, 196


def _bar_color(pct: float) -> int:
    if pct < 50:
        return _BAR_GREEN
    if pct < 80:
        return _BAR_AMBER
    return _BAR_RED


def context_bar(pct: float, width: int = 6) -> str:
    """A `width`-cell occupancy meter colored by fullness. Block elements are
    broadly-rendered, so they're the safe default; falls back to ASCII when
    color is disabled (NO_COLOR / non-TTY). Always render it last in a segment:
    it emits a bare fg escape and relies on render_powerline's trailing reset."""
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    if not ENABLED:
        return "#" * filled + "-" * (width - filled)
    bar = "█" * filled + "░" * (width - filled)   # █ / ░
    return f"\x1b[38;5;{_bar_color(pct)}m{bar}"


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


# -- status segments ----------------------------------------------------------------

# segment: (text, fg_256). No backgrounds — sections are told apart by text color.
Segment = tuple[str, int]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_SEP = " \x1b[38;5;240m·\x1b[0m " if ENABLED else "  "  # dim middot between sections


def render_powerline(segments: list[Segment], max_width: int) -> str:
    if not segments:
        return ""
    # Drop trailing segments (lowest priority last) until the visible width fits.
    # Strip embedded escapes (e.g. context_bar's color) so the math counts only
    # what actually shows on screen.
    def visible(segs: list[Segment]) -> int:
        return sum(len(_ANSI_RE.sub("", t)) + 3 for t, _ in segs)

    segs = list(segments)
    while len(segs) > 1 and visible(segs) > max_width:
        segs.pop()

    if not ENABLED:
        return "  ".join(_ANSI_RE.sub("", t) for t, _ in segs)[:max_width]

    # Each section opens its own fg color and resets at its end, so a section
    # may embed its own escapes (e.g. context_bar) before the trailing reset.
    return _SEP.join(f"\x1b[38;5;{c}m{t}\x1b[0m" for t, c in segs)

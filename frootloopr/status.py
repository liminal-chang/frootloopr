"""Sticky status line for the frootloopr CLI — powerline-style colored segments
pinned to the bottom of the terminal, scrolling log above:

  ⠸ RUN  ctx 24k  subagent-1 haiku 38s  iter 2/5  ✎ 3  Σ 41k  idle 12s

All stderr output must route through StatusConsole.log() so the sticky line can
be cleared and redrawn around it. Auto-disables when stderr isn't a TTY (cron,
pipes) — automation gets plain scrolling logs and the on-disk artifacts instead.
"""

from __future__ import annotations

import itertools
import shutil
import sys
import time
from pathlib import Path

from .ui import (
    Segment,
    agent_color,
    context_bar,
    context_window,
    fmt_tok,
    render_powerline,
    short_model,
)

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# 256-color text per segment kind — distinguishable, readable on a dark bar.
# Subagent segments use ui.agent_color() so concurrent agents stay distinct.
_C_MODE = 39       # bright blue
_C_CTX = 147       # lavender — lead context
_C_ITER = 80       # teal
_C_NOTES = 245     # gray
_C_TOK = 215       # amber
_C_IDLE = 244      # gray
_C_IDLE_HOT = 203  # red — long silence, look at the log


class StatusConsole:
    def __init__(self, notes_dir: Path, enabled: bool = True):
        self.enabled = enabled and sys.stderr.isatty()
        self.notes_dir = notes_dir
        self.mode = "RUN"  # RUN | PLAN | LOOP — set by the driver per phase
        self.lead_ctx = 0
        self.lead_model: str | None = None
        # agent -> {"model": str, "started": float, "ctx": int}
        self.active: dict[str, dict] = {}
        self.iter_text = ""
        self.tokens = 0
        self._last_event = time.monotonic()
        self._spin = itertools.cycle(_SPINNER)
        self._shown = False
        self.paused = False

    # -- output -------------------------------------------------------------------

    def log(self, text: str) -> None:
        self._clear()
        print(text, file=sys.stderr, flush=True)
        self.draw()

    def finish(self) -> None:
        self._clear()

    def pause(self) -> None:
        """Stop drawing (e.g. while waiting on user input), clearing the line."""
        self._clear()
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    # -- state updates ------------------------------------------------------------

    def touch(self) -> None:
        self._last_event = time.monotonic()

    def spawn_started(self, agent: str, label: str) -> None:
        self.active[agent] = {"model": label, "started": time.monotonic(), "ctx": 0,
                              "activity": ""}
        self.touch()

    def spawn_model(self, agent: str, model: str) -> None:
        if agent in self.active:
            self.active[agent]["model"] = model

    def spawn_ctx(self, agent: str, tokens: int) -> None:
        if agent in self.active:
            self.active[agent]["ctx"] = tokens
        self.touch()

    def spawn_activity(self, agent: str, summary: str) -> None:
        if agent in self.active:
            self.active[agent]["activity"] = summary
        self.touch()

    def spawn_ended(self, agent: str, usage: dict | None) -> None:
        self.active.pop(agent, None)
        if usage:
            self.tokens += (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0)
        self.touch()

    def set_lead(self, model: str) -> None:
        self.lead_model = model

    def lead_turn(self, context: int, usage: dict | None) -> None:
        self.lead_ctx = context
        if usage:
            self.tokens += (usage.get("output_tokens") or 0) + (usage.get("input_tokens") or 0)
        self.touch()

    # -- rendering ----------------------------------------------------------------

    def _clear(self) -> None:
        if self.enabled and self._shown:
            sys.stderr.write("\x1b[2K\r")
            sys.stderr.flush()
            self._shown = False

    def draw(self) -> None:
        if not self.enabled or self.paused:
            return
        now = time.monotonic()
        # Ordered by priority — render_powerline drops from the tail when narrow.
        segments: list[Segment] = [(f"{next(self._spin)} {self.mode}", _C_MODE)]
        if self.lead_ctx or self.lead_model:
            pct = self.lead_ctx / context_window(self.lead_model) * 100
            segments.append(
                (f"lead {short_model(self.lead_model)} {fmt_tok(self.lead_ctx)} {context_bar(pct)}",
                 _C_CTX)
            )
        for agent, info in self.active.items():
            name = agent.replace("subagent-", "sub-")
            seg = f"{name} {short_model(info['model'])}"
            if info["activity"]:
                seg += f" → {info['activity'][:24]}"
            seg += f" {now - info['started']:.0f}s"
            if info["ctx"]:
                pct = info["ctx"] / context_window(info["model"]) * 100
                seg += f" {fmt_tok(info['ctx'])} {context_bar(pct)}"
            segments.append((seg, agent_color(agent)))
        if self.iter_text:
            segments.append((self.iter_text, _C_ITER))
        idle = now - self._last_event
        if idle >= 5:
            segments.append((f"idle {idle:.0f}s", _C_IDLE_HOT if idle > 30 else _C_IDLE))
        if self.tokens:
            segments.append((f"tok {fmt_tok(self.tokens)}", _C_TOK))
        try:
            notes = sum(1 for p in self.notes_dir.iterdir() if p.is_file())
        except OSError:
            notes = 0
        if notes:
            segments.append((f"notes {notes}", _C_NOTES))

        width = shutil.get_terminal_size().columns
        line = render_powerline(segments, max_width=max(20, width - 1))
        sys.stderr.write("\x1b[2K\r" + line)
        sys.stderr.flush()
        self._shown = True

    async def ticker(self, stop) -> None:
        """Redraw ~1/s so spinners, elapsed, and idle ages stay live."""
        import asyncio

        while not stop.is_set():
            self.draw()
            await asyncio.sleep(1.0)
        self._clear()

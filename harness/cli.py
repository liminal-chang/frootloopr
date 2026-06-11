"""CLI entry point.

  python -m harness run  "task" [--workdir DIR] [--mcp-config extra.json] ...
  python -m harness loop "task" --until "pytest -q" --max-iterations 5 ...

Live activity — lead-agent text, tool calls, subagent spawns/tools (tailed from
the run's events.jsonl), models, per-turn context occupancy — renders to stderr
above a sticky status line (TTY only; automation gets plain logs). stdout carries
only the final answer, so output stays pipeable. A per-agent usage table prints
at the end and persists to runs/<id>/usage.json; if --workdir is a git repo, a
what-changed summary prints too.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from .config import load_mcp_servers
from .events import context_tokens, normalize_claude_event, read_events
from .loop import run_loop
from .runner import REFLECTION_PROMPT, RunConfig, Runner
from .status import StatusConsole
from .ui import agent_color, bold, bold_fg, dim, display_tool, fg, fmt_tok


# -- lead-agent live rendering (raw stream-json records) ---------------------------


class LeadRenderer:
    def __init__(self, console: StatusConsole):
        self.console = console
        self.model: str | None = None
        self.context: int = 0

    def __call__(self, record: dict) -> None:
        for ev in normalize_claude_event(record):
            if ev["type"] == "init" and ev.get("model"):
                if ev["model"] != self.model:
                    self.model = ev["model"]
                    self.console.set_lead(self.model)
                    self.console.log(dim(f"· lead model {self.model}"))
            elif ev["type"] == "text":
                self.console.log("")
                lines = ev["text"].splitlines()
                for i, line in enumerate(lines):
                    prefix = f"{fg(67, '⏺')} " if i == 0 else "  "
                    self.console.log(f"{prefix}{line}")
            elif ev["type"] == "tool":
                name = display_tool(ev["tool"])
                self.console.log(f"{fg(34, '⏺')} {bold(name)}{dim('(' + ev['summary'] + ')')}")
            elif ev["type"] == "turn_usage":
                self.context = context_tokens(ev["usage"])
                self.console.lead_turn(self.context, ev["usage"])


# -- subagent live rendering (tailed events.jsonl records) -------------------------


def _render_sub_event(console: StatusConsole, e: dict) -> None:
    t, agent = e.get("type"), e.get("agent", "?")
    c = agent_color(agent)
    console.touch()
    if t == "spawn_start":
        label = (e.get("backend") or "?") + (f":{e['model']}" if e.get("model") else "")
        console.spawn_started(agent, (e.get("model") or e.get("backend") or "?"))
        console.log("")
        console.log(f"{fg(c, '╭─')} {bold_fg(c, agent)} {dim('· ' + label)}")
        console.log(f"{fg(c, '│')}  {dim(e.get('task', ''))}")
    elif t == "model":
        console.spawn_model(agent, str(e.get("model")))
        console.log(f"{fg(c, '│')}  {dim('model ' + str(e.get('model')))}")
    elif t == "ctx":
        console.spawn_ctx(agent, e.get("tokens") or 0)  # status bar only, no log line
    elif t == "tool":
        name = display_tool(e.get("tool", "?"))
        console.log(f"{fg(c, '│')}  {bold(name)}{dim('(' + e.get('summary', '') + ')')}")
    elif t == "text":
        first = (e.get("text") or "").splitlines()[0][:120] if e.get("text") else ""
        if first:
            console.log(f"{fg(c, '│')}  {dim(first)}")
    elif t == "spawn_end":
        console.spawn_ended(agent, e.get("usage"))
        console.spawn_activity(agent, f"{name} {e.get('summary', '')}".strip())
        if e.get("error"):
            console.log(f"{fg(c, '╰─')} {fg(160, 'failed')} {dim(str(e['error'])[:200])}")
        else:
            u = e.get("usage") or {}
            console.log(
                f"{fg(c, '╰─')} "
                + dim(
                    f"done · {e.get('duration_s')}s · "
                    f"in {fmt_tok(u.get('input_tokens') or 0)} · "
                    f"out {fmt_tok(u.get('output_tokens') or 0)}"
                )
            )
    elif t == "memory":
        detail = f"{e.get('op')} {e.get('name')}"
        console.log(f"{fg(176, '⏺')} {bold('memory')} {dim(detail)}")


async def _tail_events(console: StatusConsole, path: Path, stop: asyncio.Event) -> None:
    pos = 0
    while True:
        if path.exists():
            with path.open() as f:
                f.seek(pos)
                for line in f:
                    try:
                        _render_sub_event(console, json.loads(line))
                    except ValueError:
                        pass
                pos = f.tell()
        if stop.is_set():
            return  # one final drain happened above
        await asyncio.sleep(0.3)


# -- end-of-run reporting -----------------------------------------------------------


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _sum_usage(usages: list[dict | None]) -> dict:
    total: dict[str, int] = {}
    for u in usages:
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            total[k] = total.get(k, 0) + ((u or {}).get(k) or 0)
    return total


def _report_usage(runner: Runner, lead: LeadRenderer) -> None:
    rows = []
    lead_total = _sum_usage([t.usage for t in runner.lead_turns])
    rows.append(("lead", lead.model or runner.config.lead_model or "claude default", lead_total))
    sub_records = [e for e in read_events(runner.events_path) if e.get("type") == "spawn_end"]
    for e in sub_records:
        label = e.get("model") or e.get("backend") or "?"
        rows.append((e.get("agent", "?"), label, e.get("usage") or {}))

    _err(dim("\n" + "─" * 78))
    _err(bold(f"{'agent':<14} {'model':<28} {'input':>9} {'output':>8} {'cache_read':>11}"))
    for name, model, u in rows:
        _err(
            f"{name:<14} {str(model)[:28]:<28} {(u.get('input_tokens') or 0):>9,} "
            f"{(u.get('output_tokens') or 0):>8,} {(u.get('cache_read_input_tokens') or 0):>11,}"
        )
    if lead.context:
        _err(f"lead context occupancy (last turn): ~{lead.context:,} tokens")

    summary = {
        "run_id": runner.run_id,
        "lead": {"model": lead.model, "usage": lead_total, "turns": len(runner.lead_turns),
                 "context_tokens_last_turn": lead.context},
        "subagents": sub_records,
    }
    (runner.run_dir / "usage.json").write_text(json.dumps(summary, indent=2))


def _git_status_set(workdir: Path) -> set[str] | None:
    if not (workdir / ".git").exists():
        return None
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(workdir), capture_output=True, text=True
    )
    if proc.returncode != 0:
        return None
    return set(proc.stdout.splitlines())


def _report_changes(workdir: Path, baseline: set[str] | None) -> None:
    after = _git_status_set(workdir)
    if after is None or baseline is None:
        _err(dim(f"\n(no what-changed summary — {workdir} is not a git repo; `git init` it to enable)"))
        return
    new = sorted(after - baseline)
    _err("\nworkdir changes (git):")
    if not new:
        _err("  (no new changes since run start)")
        return
    for line in new[:50]:
        _err(f"  {line}")
    stat = subprocess.run(
        ["git", "diff", "--stat"], cwd=str(workdir), capture_output=True, text=True
    )
    if stat.returncode == 0 and stat.stdout.strip():
        _err("  --- diff --stat (all uncommitted) ---")
        for line in stat.stdout.strip().splitlines()[-15:]:
            _err(f"  {line}")


def _make_loop_printer(console: StatusConsole, max_iterations: int):
    def _print_loop_event(event: dict) -> None:
        t = event.get("type")
        if t == "iteration_start":
            console.iter_text = f"iter {event['iteration']}/{max_iterations}"
            console.log(bold_fg(30, f"\n── iteration {event['iteration']}/{max_iterations} ──"))
        elif t == "check":
            passed = event["passed"]
            console.iter_text = f"iter {event['iteration']}/{max_iterations} {'✓' if passed else '✗'}"
            if passed:
                console.log(f"{fg(34, '✓')} {bold('check passed')}")
            else:
                exit_note = f"(exit {event['exit_code']})"
                console.log(f"{fg(160, '✗')} {bold('check failed')} {dim(exit_note)}")
        elif t == "backend_retry":
            console.log(f"{fg(178, '⚠')} backend error, retrying in {event['wait_s']}s: {dim(event['error'])}")
        elif t == "reflection_start":
            console.log(f"{fg(176, '⏺')} {bold('memory')} {dim('end-of-run reflection')}")

    return _print_loop_event


# -- plan-first phase -----------------------------------------------------------------


async def _plan_phase(runner: Runner, console: StatusConsole, task: str) -> bool:
    """Read-only plan turn + deterministic gate. Returns False if the user aborts.

    Attended (stdin is a TTY): show the plan, accept approve / revision feedback /
    quit. Unattended (cron, pipes): auto-approve — the plan still pays for itself
    as a persisted artifact and a forced up-front spec.
    """
    console.mode = "PLAN"
    console.log(bold_fg(25, "\n── plan phase (read-only) ──"))
    result = await runner.plan(task)

    while True:
        runner.plan_path.write_text(result.text + "\n")
        console.log(dim(f"· plan saved: {runner.plan_path}"))

        if not sys.stdin.isatty():
            console.log(dim("· non-interactive: auto-approving plan"))
            return True

        console.pause()
        rule = dim("─" * 40)
        print(f"\n{rule}\n{bold('PLAN')}\n{rule}\n{result.text}\n{rule}", file=sys.stderr)
        try:
            answer = (
                await asyncio.to_thread(
                    input, "approve plan? [Enter=yes / type feedback to revise / q=quit] "
                )
            ).strip()
        except EOFError:
            answer = ""
        console.resume()

        if answer.lower() in ("q", "quit"):
            return False
        if not answer or answer.lower() in ("y", "yes"):
            return True
        console.log(bold_fg(25, "── revising plan ──"))
        result = await runner.replan(answer)


# -- commands -------------------------------------------------------------------------


def _build_config(args: argparse.Namespace) -> RunConfig:
    return RunConfig(
        workdir=Path(args.workdir),
        runs_dir=Path(args.runs_dir),
        memory_dir=Path(args.memory_dir),
        lead_model=args.model,
        subagent_model=args.subagent_model,
        spawn_timeout_s=args.spawn_timeout,
        reflect=not args.no_reflect,
        extra_mcp_servers=load_mcp_servers(args.mcp_config) if args.mcp_config else {},
    )


async def _execute(args: argparse.Namespace) -> int:
    config = _build_config(args)
    runner = Runner(config)
    console = StatusConsole(runner.notes_dir, enabled=not args.no_status)
    console.mode = "LOOP" if args.command == "loop" else "RUN"
    lead = LeadRenderer(console)
    runner.on_lead_event = lead
    console.log(f"{bold(runner.run_id)}  {dim('notes: ' + str(runner.notes_dir))}")
    baseline = _git_status_set(runner.workdir)

    stop = asyncio.Event()
    tail_task = asyncio.create_task(_tail_events(console, runner.events_path, stop))
    tick_task = asyncio.create_task(console.ticker(stop))
    try:
        first_prompt = None
        if args.plan_first:
            if not await _plan_phase(runner, console, args.task):
                console.log("aborted at plan gate")
                return 130
            console.mode = "LOOP" if args.command == "loop" else "RUN"
            first_prompt = runner.implement_prompt()

        if args.command == "run":
            result = await runner.send(
                first_prompt if first_prompt is not None else runner.first_prompt(args.task)
            )
            if runner.config.reflect:
                console.log(f"\n{fg(176, '⏺')} {bold('memory')} {dim('end-of-run reflection')}")
                await runner.send(REFLECTION_PROMPT)
            final_text, exit_code = result.text, 0
        else:
            result = await run_loop(
                runner,
                args.task,
                until=args.until,
                max_iterations=args.max_iterations,
                retry_wait_s=args.retry_wait,
                on_event=_make_loop_printer(console, args.max_iterations),
                first_prompt=first_prompt,
            )
            final_text = result.last_text
            exit_code = 0 if result.done else 1
            console.log(
                f"\n{'✓ done' if result.done else '✗ not done'} after "
                f"{result.iterations} iteration(s)"
            )
    finally:
        stop.set()
        await tail_task
        await tick_task
        console.finish()

    _report_usage(runner, lead)
    _report_changes(runner.workdir, baseline)
    # stdout carries the final answer for pipes/automation. When both streams are
    # a TTY, the answer was already streamed live above — reprinting would just
    # duplicate it on the same screen.
    if not (sys.stdout.isatty() and sys.stderr.isatty()):
        print(final_text)
    return exit_code


# Artifacts anchor to the harness install, not the launch cwd — so the CLI works
# from any directory and memory stays one store instead of one per project.
_PKG_ROOT = Path(__file__).resolve().parent.parent


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("task", help="The task for the lead agent")
    p.add_argument("--workdir", default=".", help="Where agents execute (default: cwd)")
    p.add_argument("--runs-dir", default=str(_PKG_ROOT / "runs"),
                   help=f"Run artifacts dir (default: {_PKG_ROOT / 'runs'})")
    p.add_argument("--memory-dir", default=str(_PKG_ROOT / "memory"),
                   help=f"Persistent memory dir (default: {_PKG_ROOT / 'memory'})")
    p.add_argument("--model", default=None, help="Lead agent model (default: claude CLI default)")
    p.add_argument("--subagent-model", default=None, help="Default model for claude subagents")
    p.add_argument("--spawn-timeout", type=int, default=3600, help="Per-subagent timeout (s)")
    p.add_argument("--mcp-config", help="Extra MCP servers JSON to mount on the lead agent")
    p.add_argument("--no-reflect", action="store_true", help="Skip the end-of-run memory reflection step")
    p.add_argument("--no-status", action="store_true", help="Disable the sticky status line")
    p.add_argument("--plan-first", action="store_true",
                   help="Read-only plan phase first (saved to notes/plan.md); attended runs gate "
                        "on your approval, unattended runs auto-approve")


_EXAMPLES = """\
targeting a project, directory, or file:
  --workdir picks the project (agents run there; default: your current dir).
  Name files/dirs in the task text, relative to that workdir.

  harness run "audit the error handling in this repo"                      # project = current dir
  harness run "fix the race condition in src/auth.py" --workdir ~/code/api # one file, by path
  harness run "add docstrings to everything under harness/backends/" \\
      --workdir ~/dev/harness                                              # one directory
  harness run "create dashboard.html showing the data in stats.csv" \\
      --workdir ~/Desktop/new-project                                      # fresh/empty dir works too

more examples:
  harness run  "refactor the auth module" --plan-first
  harness run  "deep refactor of the parser" --model opus --subagent-model haiku
  harness loop "fix the failing tests" --until "pytest -q" --max-iterations 5 --workdir ~/code/proj
  harness run  "pull the spec from Figma and draft the component" --mcp-config figma.json
"""

_TOP_EPILOG = """\
common flags (both subcommands — full list: harness run --help):
  --workdir DIR           where agents execute (default: current directory)
  --model NAME            lead agent's model, e.g. opus | sonnet (default: your claude CLI default)
  --subagent-model NAME   default model for claude subagents, e.g. haiku
  --plan-first            read-only plan phase -> approval gate -> implement
  --mcp-config FILE       mount your own MCP servers (figma.json, ...) on the lead agent
  --no-reflect            skip the end-of-run memory step

loop-only flags:
  --until "CMD"           success check: loop ends when CMD exits 0 (run in --workdir)
  --max-iterations N      hard cap (default 5)

models:
  The lead runs your claude CLI default unless --model says otherwise. Claude
  subagents: the lead routes per spawn (haiku for scans, sonnet for routine
  work, opus for hard reasoning); --subagent-model sets their default.
  codex/gemini subagents use their own CLI's configured default.

""" + _EXAMPLES


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Multi-agent CLI orchestrator",
        epilog=_TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser(
        "run", help="Single orchestrated run",
        epilog=_EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_run)

    p_loop = sub.add_parser(
        "loop", help="Iterate until a success check passes",
        epilog=_EXAMPLES, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common(p_loop)
    p_loop.add_argument("--until", help="Shell command; exit 0 = done (run in --workdir)")
    p_loop.add_argument("--max-iterations", type=int, default=5)
    p_loop.add_argument("--retry-wait", type=int, default=300,
                        help="Seconds to wait before retrying after a backend error (e.g. usage limits)")

    args = parser.parse_args()
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())

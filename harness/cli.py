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
import time
from datetime import datetime
from pathlib import Path

from .config import load_mcp_servers
from .events import EventLog, context_tokens, normalize_claude_event, read_events
from .loop import run_loop
from .runner import REFLECTION_PROMPT, RunConfig, Runner
from .status import StatusConsole
from .ui import agent_color, bell, bold, bold_fg, dim, display_tool, fg, fmt_tok, notify


# -- lead-agent live rendering (raw stream-json records) ---------------------------


def _log_lead_text(console: StatusConsole, text: str) -> None:
    console.log("")
    for i, line in enumerate(text.splitlines()):
        prefix = f"{fg(67, '⏺')} " if i == 0 else "  "
        console.log(f"{prefix}{line}")


def _log_lead_tool(console: StatusConsole, tool: str, summary: str) -> None:
    name = display_tool(tool)
    console.log(f"{fg(34, '⏺')} {bold(name)}{dim('(' + summary + ')')}")


class LeadRenderer:
    def __init__(self, console: StatusConsole, events: EventLog | None = None):
        self.console = console
        # Mirror lead milestones into events.jsonl (flagged mirror=True) so
        # `harness watch` can replay a faithful run; the live tail skips them.
        self.events = events
        self.model: str | None = None
        self.context: int = 0
        self.rate_limits: dict[str, dict] = {}  # rateLimitType -> latest info seen

    def _mirror(self, type: str, **fields) -> None:
        if self.events:
            self.events.write("lead", type, mirror=True, **fields)

    def __call__(self, record: dict) -> None:
        for ev in normalize_claude_event(record):
            if ev["type"] == "rate_limit":
                info = ev.get("info") or {}
                if info.get("rateLimitType"):
                    self.rate_limits[info["rateLimitType"]] = info
            elif ev["type"] == "init" and ev.get("model"):
                if ev["model"] != self.model:
                    self.model = ev["model"]
                    self.console.set_lead(self.model)
                    self.console.log(dim(f"· lead model {self.model}"))
                    self._mirror("model", model=self.model)
            elif ev["type"] == "text":
                _log_lead_text(self.console, ev["text"])
                self._mirror("text", text=ev["text"][:500])
            elif ev["type"] == "tool":
                _log_lead_tool(self.console, ev["tool"], ev["summary"])
                self._mirror("tool", tool=ev["tool"], summary=ev["summary"])
            elif ev["type"] == "turn_usage":
                self.context = context_tokens(ev["usage"])
                self.console.lead_turn(self.context, ev["usage"])
                self._mirror("ctx", tokens=self.context)


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
        console.spawn_activity(agent, f"{name} {e.get('summary', '')}".strip())
        console.log(f"{fg(c, '│')}  {bold(name)}{dim('(' + e.get('summary', '') + ')')}")
    elif t == "text":
        first = (e.get("text") or "").splitlines()[0][:120] if e.get("text") else ""
        if first:
            console.log(f"{fg(c, '│')}  {dim(first)}")
    elif t == "spawn_end":
        console.spawn_ended(agent, e.get("usage"))
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


def _render_event(console: StatusConsole, e: dict, bell_enabled: bool = True) -> None:
    """Replay one events.jsonl record for `harness watch` — the unified path that
    renders lead, subagent, AND loop/run milestones. The live run renders the
    non-subagent kinds in-process, so they reach the file only as mirror=True
    records (skipped by the live tail); here we render them too."""
    if not e.get("mirror"):
        _render_sub_event(console, e)  # subagent + memory events, exactly as live
        if e.get("type") == "spawn_end" and e.get("error"):
            bell(bell_enabled)
        return

    t = e.get("type")
    if e.get("agent") == "lead":
        if t == "model":
            console.set_lead(e.get("model"))
            console.log(dim(f"· lead model {e.get('model')}"))
        elif t == "text":
            _log_lead_text(console, e.get("text", ""))
        elif t == "tool":
            _log_lead_tool(console, e.get("tool", "?"), e.get("summary", ""))
        elif t == "ctx":
            console.lead_turn(e.get("tokens") or 0, None)
        elif t == "reflection_start":
            console.log(f"{fg(176, '⏺')} {bold('memory')} {dim('end-of-run reflection')}")
        console.touch()
        return
    if t == "iteration_start":
        n, m = e.get("iteration"), e.get("max_iterations", "?")
        console.iter_text = f"iter {n}/{m}"
        console.log(bold_fg(30, f"\n── iteration {n}/{m} ──"))
    elif t == "check":
        n, m = e.get("iteration"), e.get("max_iterations", "?")
        passed = e.get("passed")
        console.iter_text = f"iter {n}/{m} {'✓' if passed else '✗'}"
        if passed:
            console.log(f"{fg(34, '✓')} {bold('check passed')}")
        else:
            console.log(f"{fg(160, '✗')} {bold('check failed')} {dim('(exit ' + str(e.get('exit_code')) + ')')}")
        bell(bell_enabled)
    elif t == "backend_retry":
        console.log(f"{fg(178, '⚠')} backend error, retrying in {e.get('wait_s')}s: {dim(str(e.get('error', '')))}")
    elif t == "run_start":
        console.log(f"{bold('● ' + str(e.get('run_id', '')))} {dim('started · ' + str(e.get('mode', '')))}")
    elif t == "run_end":
        status = e.get("status", "")
        console.log(f"\n{fg(34 if e.get('ok') else 160, '●')} {bold('run ' + status)}")
        bell(bell_enabled)
        notify("harness", f"run {status}: {e.get('run_id', '')}")
    console.touch()


async def _tail_events(console: StatusConsole, path: Path, stop: asyncio.Event) -> None:
    pos = 0
    while True:
        if path.exists():
            with path.open() as f:
                f.seek(pos)
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if e.get("mirror"):
                        continue  # lead/loop/run milestones: rendered in-process already
                    _render_sub_event(console, e)
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


# claude `modelUsage` fields: token/cost counters to sum vs. per-model constants to keep.
_MU_SUM = ("inputTokens", "outputTokens", "cacheReadInputTokens",
           "cacheCreationInputTokens", "costUSD", "webSearchRequests")
_MU_KEEP = ("contextWindow", "maxOutputTokens")


def _merge_model_usage(dest: dict[str, dict], src: dict | None) -> None:
    for model, mu in (src or {}).items():
        acc = dest.setdefault(model, {})
        for k in _MU_SUM:
            if mu.get(k) is not None:
                acc[k] = acc.get(k, 0) + mu[k]
        for k in _MU_KEEP:
            if mu.get(k) is not None:
                acc[k] = mu[k]


def _fmt_reset(ts: int | None) -> str:
    """'14:30 (in 2h11m)' for a unix reset timestamp."""
    if not ts:
        return "?"
    when = datetime.fromtimestamp(ts).strftime("%a %H:%M")
    secs = int(ts - time.time())
    if secs <= 0:
        return f"{when} (now)"
    mins = secs // 60
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    rel = f"{d}d{h}h" if d else f"{h}h{m}m" if h else f"{m}m"
    return f"{when} (in {rel})"


_LIMIT_LABELS = {"five_hour": "5h window", "seven_day": "weekly", "seven_day_opus": "weekly (opus)"}


def _report_limits(lead: LeadRenderer) -> None:
    if not lead.rate_limits:
        return
    _err(dim("\nsubscription limits (status + reset only — the CLI doesn't expose % used):"))
    for rtype, info in lead.rate_limits.items():
        label = _LIMIT_LABELS.get(rtype, rtype)
        status = info.get("status", "?")
        color = 34 if status == "allowed" else 178 if status == "warning" else 160
        _err(f"  {label:<14} {fg(color, status)}  ·  resets {_fmt_reset(info.get('resetsAt'))}")


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

    # By-model rollup (lead turns + every subagent), with cost — the /usage-style view.
    model_usage: dict[str, dict] = {}
    for t in runner.lead_turns:
        _merge_model_usage(model_usage, t.model_usage)
    for e in sub_records:
        _merge_model_usage(model_usage, e.get("model_usage"))
    if model_usage:
        _err(dim("\nby model:"))
        _err(bold(f"{'model':<28} {'input':>9} {'output':>8} {'cache_read':>11} {'cost':>9}"))
        total_cost = 0.0
        for model, mu in sorted(model_usage.items()):
            total_cost += mu.get("costUSD") or 0.0
            _err(
                f"{str(model)[:28]:<28} {(mu.get('inputTokens') or 0):>9,} "
                f"{(mu.get('outputTokens') or 0):>8,} {(mu.get('cacheReadInputTokens') or 0):>11,} "
                f"${mu.get('costUSD') or 0.0:>8.2f}"
            )
        _err(dim(f"{'total':<28} {'':>9} {'':>8} {'':>11} ${total_cost:>8.2f}"))

    _report_limits(lead)

    summary = {
        "run_id": runner.run_id,
        "lead": {"model": lead.model, "usage": lead_total, "turns": len(runner.lead_turns),
                 "context_tokens_last_turn": lead.context},
        "subagents": sub_records,
        "by_model": model_usage,
        "rate_limits": lead.rate_limits,
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


def _git(workdir: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(["git", *args], cwd=str(workdir), capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def _git_head(workdir: Path) -> str | None:
    """Current HEAD sha, or None if not a repo / no commits yet."""
    if not (workdir / ".git").exists():
        return None
    rc, out = _git(workdir, "rev-parse", "HEAD")
    return out if rc == 0 else None


def _git_commits_since(workdir: Path, base: str | None) -> tuple[str | None, list[str]]:
    """(branch, ["<sha> <subject>", ...]) for commits the run made since `base` HEAD.
    Captures committed work that `git status` no longer shows — the whole point of
    the summary on a --commit run."""
    if base is None or not (workdir / ".git").exists():
        return None, []
    _, branch = _git(workdir, "rev-parse", "--abbrev-ref", "HEAD")
    rc, out = _git(workdir, "log", f"{base}..HEAD", "--format=%h %s")
    commits = out.splitlines() if rc == 0 and out else []
    return (branch or None), commits


def _ensure_commit_branch(workdir: Path, run_id: str, console: StatusConsole) -> None:
    """For --commit runs: if the repo is on its default branch, switch to a fresh
    harness/<run_id> branch BEFORE the run — so it physically cannot commit to
    main/master, regardless of whether the lead remembers to branch. Deterministic
    enforcement beats prose for a hard rule. No-op if already on a feature branch."""
    if not (workdir / ".git").exists():
        return
    rc, branch = _git(workdir, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or branch not in ("main", "master"):
        return  # already on a feature branch, or detached/unborn — leave it
    if _git_head(workdir) is None:
        return  # no commits yet to branch from; the lead will init + branch itself
    new = "harness/" + run_id.removeprefix("run_")
    rc, _out = _git(workdir, "switch", "-c", new)
    if rc != 0:
        rc, _out = _git(workdir, "checkout", "-b", new)  # older git without `switch`
    console.log(dim(f"· --commit: branched to {new} (was on {branch})" if rc == 0
                    else f"· --commit: could not create a branch off {branch}"))


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


def _summary_md(m: dict) -> str:
    """Render the per-run session report (what / why / how) as Markdown."""
    L = [
        f"# {m['run_id']}",
        "",
        f"- **Project:** {m['project']}  (`{m['workdir']}`)",
        f"- **When:** {m['date']}",
        f"- **Mode:** {m['mode']}",
        f"- **Outcome:** {m['outcome']}",
    ]
    if m.get("models"):
        L.append(f"- **Models:** {', '.join(m['models'])}")
    if m.get("cost"):
        L.append(f"- **Cost:** ${m['cost']:.2f}")
    L += ["", "## Task", "", m["task"].strip() or "_(none)_"]
    if m.get("plan"):
        L += ["", "## Plan", "", m["plan"].strip()]
    if m.get("commits"):
        L += ["", "## Commits", "", f"On branch `{m.get('branch') or '?'}`:", "",
              *(f"- `{c}`" for c in m["commits"])]
    L += ["", "## What changed (uncommitted)", ""]
    L += (["```", *m["changed"], "```"] if m.get("changed")
          else ["_Nothing uncommitted in the working tree._"])
    if m.get("subagents"):
        L += ["", "## Subagents", "", *(f"- {s}" for s in m["subagents"])]
    L += ["", "## Summary", "", m.get("final_text", "").strip() or "_(none)_"]
    L += ["", "## Artifacts", "",
          "- `events.jsonl` — full event log",
          "- `usage.json` — token/cost rollup"]
    if m.get("notes"):
        L.append("- `notes/`: " + ", ".join(m["notes"]))
    return "\n".join(L) + "\n"


def _append_index(runs_dir: Path, *, run_id: str, project: str, task: str,
                  outcome: str, date: str) -> None:
    """One grep-able row per run in runs/INDEX.md — the cross-project discovery layer."""
    idx = runs_dir / "INDEX.md"
    try:
        if not idx.exists():
            idx.write_text("# Harness runs\n\n| date | project | task | outcome | run |\n"
                           "|---|---|---|---|---|\n")
        task1 = " ".join(task.split())[:60].replace("|", "/")
        with idx.open("a") as f:
            f.write(f"| {date} | {project} | {task1} | {outcome} | {run_id} |\n")
    except OSError:
        pass


def _write_summary(runner: Runner, args: argparse.Namespace, final_text: str,
                   exit_code: int, iterations: int, baseline: set[str] | None,
                   baseline_head: str | None) -> None:
    """Persist a per-run summary.md (what/why/how) and append to runs/INDEX.md."""
    models, cost, subagents = [], 0.0, []
    try:  # models + cost + subagents from the usage.json _report_usage just wrote
        usage = json.loads((runner.run_dir / "usage.json").read_text())
        models = sorted(usage.get("by_model", {}))
        cost = sum((mu.get("costUSD") or 0.0) for mu in usage.get("by_model", {}).values())
        subagents = [f"{e.get('agent', '?')} ({e.get('model') or e.get('backend') or '?'})"
                     for e in usage.get("subagents", [])]
    except (OSError, ValueError):
        pass

    changed: list[str] = []
    after = _git_status_set(runner.workdir)
    if after is not None and baseline is not None:
        changed = sorted(after - baseline)
    branch, commits = _git_commits_since(runner.workdir, baseline_head)

    plan = runner.plan_path.read_text() if runner.plan_path.exists() else ""
    try:
        notes = sorted(p.name for p in runner.notes_dir.iterdir() if p.is_file())
    except OSError:
        notes = []

    mode = ("loop" if args.command == "loop" else "run") + (" · plan-first" if args.plan_first else "")
    outcome = ("done" if exit_code == 0 else "not done") + (
        f" ({iterations} iter)" if args.command == "loop" else "")
    date = datetime.now().strftime("%Y-%m-%d %H:%M")

    md = _summary_md({
        "run_id": runner.run_id, "project": runner.workdir.name, "workdir": str(runner.workdir),
        "date": date, "mode": mode, "outcome": outcome, "models": models, "cost": cost,
        "task": args.task, "plan": plan, "changed": changed, "subagents": subagents,
        "branch": branch, "commits": commits, "final_text": final_text, "notes": notes,
    })
    try:
        (runner.run_dir / "summary.md").write_text(md)
        _err(dim(f"\n· summary: {runner.run_dir / 'summary.md'}"))
    except OSError:
        pass
    _append_index(runner.run_dir.parent, run_id=runner.run_id, project=runner.workdir.name,
                  task=args.task, outcome=outcome, date=date)


def _make_loop_printer(console: StatusConsole, max_iterations: int, events: EventLog | None = None):
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
        # Mirror for `harness watch` (skipped by the live tail via mirror=True).
        if events:
            events.write("loop", t, mirror=True, max_iterations=max_iterations,
                         **{k: v for k, v in event.items() if k != "type"})

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

        bell()  # nudge: an attended plan is waiting for your approval
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


def _default_mcp_servers(no_default: bool) -> dict:
    """Servers mounted on every run unless --no-default-mcp. Ships Context7 (live
    library docs, read-only) so agents stop recalling stale APIs; edit
    mcp/default.json to change the set. Fails open: a missing file means none."""
    if no_default:
        return {}
    path = _PKG_ROOT / "mcp" / "default.json"
    return load_mcp_servers(path) if path.exists() else {}


def _build_config(args: argparse.Namespace) -> RunConfig:
    # Defaults first, then explicit --mcp-config wins on any name collision.
    servers = _default_mcp_servers(args.no_default_mcp)
    if args.mcp_config:
        servers.update(load_mcp_servers(args.mcp_config))
    return RunConfig(
        workdir=Path(args.workdir),
        runs_dir=Path(args.runs_dir),
        memory_dir=Path(args.memory_dir),
        lead_model=args.model,
        subagent_model=args.subagent_model,
        spawn_timeout_s=args.spawn_timeout,
        reflect=not args.no_reflect,
        commit=args.commit,
        extra_mcp_servers=servers,
    )


async def _execute(args: argparse.Namespace) -> int:
    config = _build_config(args)
    runner = Runner(config, task=args.task)
    console = StatusConsole(runner.notes_dir, enabled=not args.no_status)
    console.mode = "LOOP" if args.command == "loop" else "RUN"
    # The same events.jsonl the MCP server appends subagent activity to; the lead
    # mirrors its own milestones here too so `harness watch` sees the whole run.
    events_log = EventLog(runner.events_path)
    lead = LeadRenderer(console, events=events_log)
    runner.on_lead_event = lead
    console.log(f"{bold(runner.run_id)}  {dim('notes: ' + str(runner.notes_dir))}")
    baseline = _git_status_set(runner.workdir)
    baseline_head = _git_head(runner.workdir)  # to report commits the run makes (--commit)
    if config.commit:
        _ensure_commit_branch(runner.workdir, runner.run_id, console)
    events_log.write("run", "run_start", mirror=True, run_id=runner.run_id, mode=console.mode)

    stop = asyncio.Event()
    tail_task = asyncio.create_task(_tail_events(console, runner.events_path, stop))
    tick_task = asyncio.create_task(console.ticker(stop))
    iterations = 1
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
                events_log.write("lead", "reflection_start", mirror=True)
                await runner.send(REFLECTION_PROMPT)
            final_text, exit_code = result.text, 0
        else:
            result = await run_loop(
                runner,
                args.task,
                until=args.until,
                max_iterations=args.max_iterations,
                retry_wait_s=args.retry_wait,
                on_event=_make_loop_printer(console, args.max_iterations, events=events_log),
                first_prompt=first_prompt,
            )
            final_text = result.last_text
            exit_code = 0 if result.done else 1
            iterations = result.iterations
            console.log(
                f"\n{'✓ done' if result.done else '✗ not done'} after "
                f"{result.iterations} iteration(s)"
            )
        status = "done" if exit_code == 0 else "not done"
        events_log.write("run", "run_end", mirror=True, run_id=runner.run_id,
                         status=status, ok=(exit_code == 0))
        bell()  # audible nudge that the run finished (TTY only)
    finally:
        stop.set()
        await tail_task
        await tick_task
        console.finish()

    _report_usage(runner, lead)
    _report_changes(runner.workdir, baseline)
    _write_summary(runner, args, final_text, exit_code, iterations, baseline, baseline_head)
    # stdout carries the final answer for pipes/automation. When both streams are
    # a TTY, the answer was already streamed live above — reprinting would just
    # duplicate it on the same screen.
    if not (sys.stdout.isatty() and sys.stderr.isatty()):
        print(final_text)
    return exit_code


# -- watch (live-render a run from a second terminal) --------------------------------


def _resolve_run_dir(runs_dir: Path, run_id: str | None) -> Path | None:
    """Pick the run to watch: an explicit id, a prefix (e.g. just the timestamp
    run_<ts>, which resolves a slugged dir), or the newest run. Run dirs lead with
    the timestamp, so a reverse lexical sort is chronological."""
    if run_id:
        for cand in (runs_dir / run_id, runs_dir / f"run_{run_id}"):
            if cand.is_dir():
                return cand
        for pat in (f"{run_id}*", f"run_{run_id}*"):  # prefix match
            matches = sorted((p for p in runs_dir.glob(pat) if p.is_dir()), reverse=True)
            if matches:
                return matches[0]
        return None
    runs = sorted((p for p in runs_dir.glob("run_*") if p.is_dir()), reverse=True)
    return runs[0] if runs else None


async def _watch(args: argparse.Namespace) -> int:
    """Tail a run's events.jsonl and render it through the status UI. Runs in its
    own process (a second terminal/pane) so a live view costs the orchestrating
    session nothing — the run writes the file; this only reads it."""
    runs_dir = Path(args.runs_dir)
    run_dir = _resolve_run_dir(runs_dir, args.run_id)
    if run_dir is None:
        where = f" matching '{args.run_id}'" if args.run_id else ""
        _err(f"no run found in {runs_dir}{where} (looked for run_* directories)")
        return 1

    events_path = run_dir / "events.jsonl"
    console = StatusConsole(run_dir / "notes", enabled=not args.no_status)
    console.log(f"{bold('watching ' + run_dir.name)}  {dim(str(events_path))}")
    # A replay (--once) of a finished run shouldn't ring a flurry of stale bells.
    bell_enabled = not args.no_bell and not args.once

    stop = asyncio.Event()
    tick = asyncio.create_task(console.ticker(stop))
    pos, finished = 0, False
    try:
        while True:
            if events_path.exists():
                with events_path.open() as f:
                    f.seek(pos)
                    for line in f:
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        _render_event(console, e, bell_enabled)
                        if e.get("type") == "run_end":
                            finished = True
                    pos = f.tell()
            if args.once or finished:
                break
            await asyncio.sleep(0.3)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        await tick
        console.finish()
    if finished and not args.once:
        console.log(dim("· run finished — watch exiting"))
    return 0


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
    p.add_argument("--no-default-mcp", action="store_true",
                   help="Skip auto-mounting mcp/default.json (Context7) for this run")
    p.add_argument("--no-reflect", action="store_true", help="Skip the end-of-run memory reflection step")
    p.add_argument("--commit", action="store_true",
                   help="Let the lead commit completed work (branch-first, by concern); off by default")
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

    p_watch = sub.add_parser(
        "watch",
        help="Live-render a run in a second terminal (zero token cost — just reads events.jsonl)",
        epilog="  harness watch                 # follow the newest run\n"
               "  harness watch run_20260611_134642\n"
               "  harness watch --once          # snapshot current state and exit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_watch.add_argument("run_id", nargs="?", help="Run id to watch (default: the newest run)")
    p_watch.add_argument("--runs-dir", default=str(_PKG_ROOT / "runs"),
                         help=f"Run artifacts dir (default: {_PKG_ROOT / 'runs'})")
    p_watch.add_argument("--once", action="store_true",
                         help="Render the run's current state and exit (no follow)")
    p_watch.add_argument("--no-bell", action="store_true",
                         help="Suppress the terminal bell on milestones")
    p_watch.add_argument("--no-status", action="store_true",
                         help="Disable the sticky status line")

    args = parser.parse_args()
    if args.command == "watch":
        return asyncio.run(_watch(args))
    return asyncio.run(_execute(args))


if __name__ == "__main__":
    raise SystemExit(main())

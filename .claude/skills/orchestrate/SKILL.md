---
name: orchestrate
description: Run the multi-model agent orchestrator (frootloopr) — a lead Claude agent that spawns claude/codex/gemini subagents with shared notes, persistent memory, optional plan-first gating, and loop-until-check. Use when the user says /orchestrate, asks to "use the orchestrator", or wants a task delegated to the multi-agent frootloopr.
---

# Orchestrate

Drive the `frootloopr` CLI that ships in this repo.

**Resolve the repo root from THIS skill's own location** (do not hardcode a path —
this skill ships inside the frootloopr repo at `<repo>/.claude/skills/orchestrate/`).
You are given this skill's base directory at invocation ("Base directory for this
skill"); the repo root is three levels up:

```sh
FROOTLOOPR_DIR="$(cd "<this skill's base directory>/../../.." && pwd)"   # or: git -C "<base dir>" rev-parse --show-toplevel
BIN="$FROOTLOOPR_DIR/.venv/bin/fl"                                        # `fl` (short) — `frootloopr` also works
```

**Prerequisites** — if `$BIN` doesn't exist, the repo isn't set up. Tell the user to
do the README → Setup first: `python3 -m venv .venv && .venv/bin/pip install -e .`,
and ensure the `claude` CLI (and optionally `codex` / `gemini`) are logged in. The
orchestrator can't run without those.

## How to run a task

1. **Parse the request** (`$ARGUMENTS` / the user's message):
   - The task description (becomes the quoted prompt).
   - Target directory → `--workdir` (default: the current project directory; if
     the task implies a fresh project, create a new dir and `git init` it so the
     what-changed summary works).
   - A verifiable success criterion (tests pass, file exists)? → use `loop`
     with `--until "<shell cmd>"` and `--max-iterations 5`. Otherwise → `run`.
2. **Pick flags:**
   - `--plan-first` for multi-step or code-changing tasks; skip for trivial ones.
   - `--commit` if the user wants the work committed (the lead commits completed
     units branch-first, by concern, per frootloopr's built-in pattern); off by
     default, so changes otherwise just land in the workdir for review.
   - `--model` / `--subagent-model` only if the user named models; otherwise let
     frootloopr route (lead picks haiku/sonnet/opus per spawn).
   - MCP servers (mounted on the lead AND claude subagents; codex/gemini
     subagents don't get them):
     - **Context7** (live library docs, read-only) is mounted by **default** on
       every run via `$FROOTLOOPR_DIR/mcp/default.json` — no flag needed, safe with
       `--plan-first`. Pass `--no-default-mcp` to skip it (e.g. offline runs).
     - Web/UI task needing a real browser (e2e verification, front-end work) →
       opt in with `--mcp-config "$FROOTLOOPR_DIR/mcp/playwright.json"`. Heavy and
       stateful: don't add it for non-web tasks, and note that subagents run
       with bypassPermissions — only point it at trusted local/dev URLs. For
       verification, have the agent author a Playwright spec and gate with
       `loop --until "npx playwright test"` (the shell check stays the gate).
     - `--mcp-config` merges over the defaults; the CLI takes ONE such file, so
       to add several servers write one temp file merging their `mcpServers`.
3. **Run it in the foreground** with a long Bash timeout (10 min+), always from
   the frootloopr dir so `runs/` and `memory/` artifacts stay together:

   ```sh
   cd "$FROOTLOOPR_DIR" && "$BIN" run "<task>" --plan-first --workdir <dir>
   ```

   Inside Claude Code, prefer launching it in the **background** (runs outlast a
   10-min foreground cap, and frootloopr notifies on completion).

   **Always, right after launching, give the user the watch command** so they can
   follow it live in a second terminal/pane at zero token cost — don't poll the
   run yourself:

   ```sh
   cd "$FROOTLOOPR_DIR" && "$BIN" watch        # follows the newest run
   ```

4. **Report back**: the final answer, which models ran (usage table on stderr),
   what changed in the workdir, and where artifacts live
   (`$FROOTLOOPR_DIR/runs/<run_id>/` — `notes/`, `events.jsonl`, `usage.json`).

## Caveats when invoked from inside Claude Code

- stdin is not a TTY here, so `--plan-first`'s gate **auto-approves**. After the
  run, show the user the saved plan (`runs/<id>/notes/plan.md`) alongside the
  result. If the user explicitly wants to approve the plan interactively, give
  them the exact command to paste into their own terminal instead of running it.
- The sticky status line auto-disables (non-TTY); plain event logs still stream.
- **Live visibility without token burn**: don't poll the run's output/events from
  the conversation (anything read in is re-billed every turn). Instead tell the
  user they can watch it live in a second terminal/pane — `"$BIN" watch` follows
  the newest run (or `watch <run_id>`), rendering the status bar + event stream
  and ringing a bell on milestones, at zero cost to this session. frootloopr also
  auto-notifies on background completion, so prefer that over polling.
- Spawned agents run with `--permission-mode bypassPermissions` inside the
  workdir — confirm with the user before pointing `--workdir` at a directory
  with anything destructive at stake.

## Quick reference

```sh
"$BIN" run  "task"                                   # single orchestrated run
"$BIN" run  "task" --plan-first                      # plan (read-only) → implement
"$BIN" loop "task" --until "pytest -q" --max-iterations 5
"$BIN" run  "task" --mcp-config figma.json           # mount user MCP servers on the lead
"$BIN" watch                                         # live-watch newest run (2nd terminal, $0)
"$BIN" run --help                                    # all flags + examples
```

# frootloopr

A multi-agent orchestrator built on the agent CLIs you already pay for — Claude
Code (Max), Codex CLI (ChatGPT Pro), Gemini CLI — no API keys. A lead agent
wrangles a crew of model-subagents that run in loops, share a workspace, and
persist memory across runs.

## How it works

Each vendor CLI is already a full agentic harness (loop, tools, context
management). frootloopr orchestrates them at the process level:

```
loop driver (Python, deterministic)         ← cron-able; success check + iteration cap
  └─ iteration N:
       lead agent = claude -p --resume …    ← your Max plan; one conversation across iterations
         │  mounts the frootloopr MCP server:
         ├─ spawn_claude  → claude -p       (subprocess, own context)
         ├─ spawn_codex   → codex exec      (ChatGPT Pro)
         ├─ spawn_gemini  → gemini -p       (Google account)
         └─ memory_read/write/delete        (persistent, cross-run)
       shared: runs/<id>/notes/  +  memory/
```

Context isolation is enforced by process boundaries: subagents do tool-heavy work
in their own contexts and return only a distilled summary, so the lead agent's
context grows by one summary per subtask — compaction never becomes the
bottleneck. Findings pass between agents through `notes/`; durable learnings
persist in `memory/` (file-per-fact + `INDEX.md`, injected at run start, updated
by an end-of-run reflection turn).

## Setup

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
# requires: claude CLI logged in; optionally codex / gemini CLIs
```

This installs two identical console scripts at `.venv/bin/`: **`frootloopr`** and
the short alias **`fl`**.

**Where to run it from:** anywhere — but `.venv/bin/fl` is a *relative* path that
only resolves from this folder. From any other directory, use the absolute path:

```sh
"/path/to/dev/harness/.venv/bin/fl" run "task" --workdir .
```

or (recommended) add an alias to `~/.zshrc` once, then `fl` works everywhere:

```sh
alias fl='"/path/to/dev/harness/.venv/bin/fl"'
```

The alias takes effect in new terminals (or `source ~/.zshrc` in the current
one). **All examples below assume `fl` is on your PATH** — substitute the absolute
path if not.

Launch directory doesn't matter for artifacts: `runs/` and `memory/` always
anchor to this folder (override with `--runs-dir` / `--memory-dir`), and agents
execute wherever `--workdir` points (default: your current directory).

**From Claude Code — the `/orchestrate` skill.** This repo ships a project skill at
`.claude/skills/orchestrate/`, so any Claude Code session working in the clone can
drive frootloopr — `/orchestrate build a snake game in ~/Desktop/demo`. The session
picks the subcommand and flags, runs it, and reports back. The skill resolves the
repo root from its own location, so it works in any clone with no path editing —
it just needs the Setup above done (the `.venv` + a logged-in `claude` CLI).
(Invoked this way the plan gate auto-approves since there's no TTY; ask for the
command instead if you want to gate the plan yourself.)

## Run

```sh
# Single orchestrated run
fl run "Compare module A and B and write a recommendation"

# Loop until a check passes (the model never decides when to stop — the check does)
fl loop "Fix the failing tests in this repo" \
  --until "pytest -q" --max-iterations 5 --workdir ~/code/myproject

# Plan first (explore→plan→code), then implement
fl run "Refactor the auth module" --plan-first

# Let the lead commit its work (branch-first, one commit per concern)
fl run "Add the feature with tests" --commit
```

**Targeting a project, directory, or file:** `--workdir` picks the project —
agents execute there (default: wherever you launched from). Files and directories
are named *in the task text*, relative to that workdir:

```sh
fl run "fix the race condition in src/auth.py" --workdir ~/code/api     # one file
fl run "add docstrings under frootloopr/backends/" --workdir ~/dev/harness  # one dir
fl run "create dashboard.html from stats.csv" --workdir ~/new-project   # empty dir is fine
```

`--plan-first` runs the lead's first turn in read-only plan mode
(`--permission-mode plan` — it can explore but not modify), saves the plan to
`runs/<id>/notes/plan.md`, then gates: attended runs show the plan and wait for
approve / revision feedback / quit; unattended runs (cron, pipes) auto-approve.
After approval the *same session* resumes in full agent mode and implements its
own plan.

`--commit` (off by default) tells the lead to commit completed work. Enforced
deterministically: if the workdir is on `main`/`master`, frootloopr switches to a
fresh `frootloopr/<run-id>` branch *before* the run, so it physically cannot
commit to the default branch. The lead commits one logical unit per concern with
a `Co-Authored-By` trailer, runs tests before committing, and never pushes unless
asked. Subagents never commit (parallel commits would race the workdir).

Useful flags: `--workdir` (where agents execute; default cwd), `--mcp-config
extra.json` (mount your own MCP servers on the lead + claude subagents),
`--no-default-mcp` (skip the default Context7 mount), `--commit`, `--no-reflect`,
`--retry-wait 300` (backoff after usage-limit errors — loops on subscription
plans hit rolling windows; the driver waits and retries instead of aborting).

`fl --help`, `fl run --help`, `fl loop --help`, and `fl watch --help` list every
flag with examples.

## Choosing models

Three layers, from broadest to finest:

| Agent | Default | How to set |
|---|---|---|
| Lead | your claude CLI default (`/model` in Claude Code) | `--model opus` (or `sonnet`, or a full model name) |
| Claude subagents | claude CLI default | `--subagent-model haiku` sets the default; the **lead overrides per spawn** (below) |
| Codex / Gemini subagents | that CLI's own configured default | configure in `~/.codex/config.toml` / gemini settings |

**Per-spawn routing is the interesting layer:** the lead agent's `spawn_claude`
tool takes a `model` argument, and its guidance tells it how to route —
**haiku** for mechanical scans and simple lookups, **sonnet** for routine
multi-step work, **opus** (or the default) for hard reasoning, **codex/gemini**
for cross-model second opinions or spreading usage across providers. So you
mostly don't pick subagent models; the orchestrator does, per subtask.

```sh
# Everything default: lead = your claude default; lead routes subagents itself
fl run "audit the error handling in this repo"

# Pin the lead to opus, make haiku the subagent default (lead can still override)
fl run "deep refactor of the parser" --model opus --subagent-model haiku
```

**Verify what actually ran:** the `lead model:` line at run start, each
`╭─ subagent-N · claude:haiku` block, the model column in the end-of-run usage
table, and `runs/<id>/usage.json`.

## MCP servers

**Context7 is mounted by default.** Every run gets the Context7 server (live,
read-only library/framework docs) so agents reference real APIs instead of
recalling stale ones — no flag needed (it lives in `mcp/default.json`). Pass
`--no-default-mcp` to skip it (e.g. offline runs).

**Add your own** with `--mcp-config` (Figma, Linear, filesystem, …). The file
uses the same `{"mcpServers": {...}}` shape as Claude Desktop / Claude Code, so
configs you already have port over directly. A bundled `mcp/playwright.json`
opt-in mounts a real browser for web/UI tasks.

```json
{
  "mcpServers": {
    "figma": { "url": "https://mcp.figma.com/mcp" },
    "fs-tmp": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
```

```sh
fl run "Pull the spec from Figma and draft the component" --mcp-config figma.json
```

**Lead + claude subagents both get the mounted servers.** The lead receives the
full config; claude subagents inherit a *frootloopr-server-free* copy (so they
have your tools but can't recursively `spawn_*`). Codex/gemini subagents don't
inherit per-run MCP — their CLIs take MCP only via their own global config.
`--mcp-config` merges *over* the defaults; the CLI takes one such file, so to add
several servers, write one temp file merging their `mcpServers` maps.

Doing it in the spirit of the orchestrator:

1. **Mount per run, not everything always.** Every mounted server's tool schemas
   sit in the agents' context for the whole run. Keep a config file per
   integration and pass only what the task needs.
2. **Keep bulky MCP results out of the lead's context.** For big pulls, tell the
   lead — in your task text — to save results to the run's notes directory and
   spawn a subagent to process them. The distilled summary comes back; the
   payload stays on disk.
3. **Auth happens once, interactively.** Token servers take credentials via `env`
   in the config. OAuth servers (Figma's hosted MCP, etc.) need a one-time grant
   (`claude mcp add --transport http figma https://mcp.figma.com/mcp`, then
   `/mcp` in an interactive session); after that, headless runs reuse the stored
   credential.
4. **Smoke-test the server before a long run.** A cheap probe ("List 2 items from
   <server> and stop") confirms connectivity and auth for a few hundred tokens.

## Observability

Live activity renders to **stderr**. stdout carries only the final answer and only
when piped/redirected (`fl run ... > out.md`, cron). Claude-CLI-style log: colored
bullets, bold tool names, dim metadata, each subagent in its own color block:

```
⏺ I'll spawn a subagent to survey the files.
⏺ spawn_claude(survey the project files…)

╭─ subagent-1 · claude:haiku
│  Bash(find . -type f | sort)
│  Scanning the directory now.
╰─ done · 29.8s · in 42 · out 2.6k
```

Below it, a status bar of colored segments (drops trailing segments on narrow
terminals):

```
 ⠸ RUN   lead fable-5[1m] 24.3k (2%)   sub-1 haiku-4-5 38s 12k (6%)   iter 2/5   idle 12s   tok 41k   notes 3
```

| Segment | Meaning |
|---|---|
| `RUN` / `PLAN` / `LOOP` | current phase (spinner = process alive) |
| `lead fable-5[1m] 24.3k (2%)` | lead agent: model, context used, **% of its context window** |
| `sub-1 haiku-4-5 38s 12k (6%)` | live subagent: model, elapsed, its own context and window % |
| `iter 2/5` | loop progress (`✓`/`✗` = last check result) |
| `idle 12s` | seconds since last event — turns **red past 30s**: go look at the log |
| `tok 41k` | total tokens spent this run |
| `notes 3` | files in the shared notes dir |

Set `FROOTLOOPR_POWERLINE=1` for powerline chevrons (needs a patched font);
`NO_COLOR=1` disables color.

### Watching from a second terminal — `fl watch`

A run launched in the background (or driven from Claude Code) renders nothing in
your current view. **`fl watch`** (newest run) or **`fl watch <run_id>`** tails
the run's `events.jsonl` and re-renders the full status bar + event stream in a
separate terminal — **zero cost to the launching session**, since it only reads a
file. It rings a terminal bell on milestones (check pass/fail, run done, subagent
failure, an attended plan gate); set `FROOTLOOPR_NOTIFY=1` for macOS desktop
notifications. `fl watch --once` snapshots current state and exits.

The lead streams via `claude -p --output-format stream-json`; subagent activity
escapes the MCP-server process through `runs/<id>/events.jsonl`, which the CLI
tails (and which you can `tail -f` yourself).

### Per-run summary

Every run writes a Markdown session report to **`runs/<id>/summary.md`** — task,
plan (if any), commits made / what changed (git), outcome, models + cost,
subagents, and the lead's final answer — and appends a row to **`runs/INDEX.md`**
for cross-project discovery. Run folders are self-identifying:
`run_<timestamp>_<project>_<task-slug>`. Also at run end: a per-agent usage table
(persisted to `runs/<id>/usage.json`) and, when `--workdir` is a git repo, a
what-changed summary.

### Interactive mode (free byproduct)

The same MCP server mounts in a normal interactive Claude Code session, giving it
the `spawn_claude` / `spawn_codex` / `spawn_gemini` + `memory_read/write/delete`
tools directly. Register it once (run from the repo root so `$PWD` resolves):

```sh
claude mcp add frootloopr \
  -e FROOTLOOPR_MEMORY_DIR="$PWD/memory" \
  -- "$PWD/.venv/bin/python" -m frootloopr.mcp_server
```

`-e KEY=value` sets env vars (`--` separates the launch command). All `FROOTLOOPR_*`
vars have sensible defaults (see `frootloopr/mcp_server.py`); `MEMORY_DIR` is the
one worth setting so the interactive session shares the same persistent memory as
your CLI runs. Add `-s user` to make the server available in every project instead
of just this repo. Then `/mcp` in a session lists the `frootloopr` tools.

## Layout

```
frootloopr/
  backends/        CLI adapters: claude (resume support), codex, gemini
  mcp_server.py    spawn_* + memory tools (FastMCP, stdio)
  runner.py        single run: lead agent + MCP config + reflection
  loop.py          deterministic loop driver (success check, retries, state file)
  memory.py        persistent cross-run memory (file-per-fact + INDEX.md)
  cli.py           `fl run` / `fl loop` / `fl watch` + summary/usage reporting
  ui.py status.py events.py   rendering, status bar, event plumbing
  # library-only API path (needs ANTHROPIC_API_KEY; kept for future use):
  agent.py  orchestrator.py  providers/  mcp_manager.py  offload.py  tools.py
mcp/                bundled MCP configs (default.json = Context7; playwright.json)
tests/smoke_test.py   offline tests (no auth needed)
```

## Troubleshooting

- **`zsh: no such file or directory: .venv/bin/fl`** — you're not in the project
  folder; use the absolute path or the alias (see Setup → Where to run it from).
- **`ModuleNotFoundError: No module named 'frootloopr'`** (from the entry point) —
  historical: when this project lived in iCloud-synced `~/Desktop`, iCloud kept
  re-applying the macOS `hidden` flag across the venv, and Python 3.13 silently
  skips hidden `.pth` files. Moving to `~/dev/harness` (outside iCloud) removed
  the root cause; `site-packages/sitecustomize.py` remains as a safety net. If
  this appears: check `sitecustomize.py` exists, and run `ls -lO
  .venv/lib/python3.13/site-packages/*.pth` for `hidden` flags.
- **Status bar separators look broken** — only relevant with
  `FROOTLOOPR_POWERLINE=1` (chevron mode needs a powerline-patched font); unset it
  to return to the default spaced-pill rendering.
- **A run seems stuck** — check the status bar's `idle Ns` segment (red past 30s),
  then `tail -f` the run's `events.jsonl`, or `fl watch` it from another terminal.

## Notes

- Every orchestrated agent gets a distilled set of coding norms (assumptions
  stated, minimal code, surgical changes, verify before done) baked into its
  guidance — all providers. Claude agents additionally honor the **target
  project's** `CLAUDE.md` (loaded from `--workdir`) and your global
  `~/.claude/CLAUDE.md`.
- Subagents default to `--permission-mode bypassPermissions` (full autonomy in
  the workdir). Point `--workdir` at directories you trust the agents to modify.
- `codex` / `gemini` backends are best-effort until those CLIs are installed and
  their flags verified; `spawn_*` returns a clean error if a CLI is missing, so
  the lead routes around it.
- Offline tests: `.venv/bin/python tests/smoke_test.py`

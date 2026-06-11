# harness

A multi-agent orchestrator built on the agent CLIs you already pay for — Claude
Code (Max), Codex CLI (ChatGPT Pro), Gemini CLI — no API keys. Agents run in
loops, share a workspace, and persist memory across runs.

## How it works

Each vendor CLI is already a full harness (agentic loop, tools, context
management). This project orchestrates them at the process level:

```
loop driver (Python, deterministic)         ← cron-able; success check + iteration cap
  └─ iteration N:
       lead agent = claude -p --resume …    ← your Max plan; one conversation across iterations
         │  mounts the harness MCP server:
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

This installs a `harness` entry point at `.venv/bin/harness`.

**Where to run it from:** anywhere — but `.venv/bin/harness` is a *relative*
path that only resolves from this folder. From any other directory, either use
the absolute path:

```sh
"/path/to/dev/harness/.venv/bin/harness" run "task" --workdir .
```

or (recommended) add the alias to `~/.zshrc` once, then `harness` works
everywhere:

```sh
alias harness='"/path/to/dev/harness/.venv/bin/harness"'
```

The alias takes effect in new terminals (or `source ~/.zshrc` in the current
one). **All examples in this README assume the alias** — substitute the
absolute path if you haven't added it.

Launch directory doesn't matter for artifacts: `runs/` and `memory/` always
anchor to this folder (override with `--runs-dir` / `--memory-dir`), and agents
execute wherever `--workdir` points (default: your current directory).

**From Claude Code:** the `/orchestrate` skill (`~/.claude/skills/orchestrate/`)
lets any interactive session drive the harness — `/orchestrate build a snake
game in ~/Desktop/harness-demo`. The session picks the right subcommand and
flags, runs it, and reports back. (Note: invoked that way, the plan gate
auto-approves since there's no TTY; ask for the command instead if you want to
gate the plan yourself.)

## Run

```sh
# Single orchestrated run
harness run "Compare module A and B and write a recommendation"

# Loop until a check passes (the model never decides when to stop — the check does)
harness loop "Fix the failing tests in this repo" \
  --until "pytest -q" --max-iterations 5 --workdir ~/code/myproject

# Plan first (Anthropic's documented explore→plan→code practice), then implement
harness run "Refactor the auth module" --plan-first
```

**Targeting a project, directory, or file:** `--workdir` picks the project —
agents execute there (default: wherever you launched from). Files and
directories are named *in the task text*, relative to that workdir:

```sh
harness run "fix the race condition in src/auth.py" --workdir ~/code/api   # one file
harness run "add docstrings under harness/backends/" --workdir ~/dev/harness  # one dir
harness run "create dashboard.html from stats.csv" --workdir ~/new-project    # empty dir is fine
```

`--plan-first` runs the lead's first turn in read-only plan mode (`--permission-mode
plan` — it can explore but not modify), saves the plan to `runs/<id>/notes/plan.md`,
then gates: attended runs show the plan and wait for approve / revision feedback /
quit; unattended runs (cron, pipes) auto-approve — the plan still pays for itself
as a persisted artifact subagents and later loop iterations reference. After
approval the *same session* resumes in full agent mode and implements its own plan.

Useful flags: `--workdir` (where agents execute; default cwd), `--mcp-config
extra.json` (mount your own MCP servers on the lead agent), `--no-reflect`,
`--retry-wait 300` (backoff after usage-limit errors — loops on subscription
plans hit rolling windows; the driver waits and retries instead of aborting).

`harness --help`, `harness run --help`, and `harness loop --help` list every
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
harness run "audit the error handling in this repo"

# Pin the lead to opus, make haiku the subagent default (lead can still override)
harness run "deep refactor of the parser" \
  --model opus --subagent-model haiku

# Force a model for the whole run's claude subagents (e.g. everything cheap)
harness run "inventory all TODO comments" --subagent-model haiku
```

**Verify what actually ran:** the `lead model:` line at run start, each
`→ spawn subagent-N [claude:haiku]` line, the model column in the end-of-run
usage table, and `runs/<id>/usage.json`.

## Adding your own MCP servers (Figma, Linear, filesystem, …)

The harness mounts extra MCP servers on the **lead agent** via `--mcp-config`.
The file uses the same `{"mcpServers": {...}}` shape as Claude Desktop / Claude
Code, so configs you already have port over directly:

```json
{
  "mcpServers": {
    "figma": {
      "url": "https://mcp.figma.com/mcp"
    },
    "fs-tmp": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "env": { "AN_API_TOKEN": "..." }
    }
  }
}
```

```sh
harness run "Pull the spec from Figma and draft the component" \
  --mcp-config figma.json
```

**Doing it in the spirit of the orchestrator:**

1. **Mount per run, not everything always.** Every mounted server's tool schemas
   sit in the lead agent's context for the whole run. Keep a config file per
   integration (`figma.json`, `linear.json`) and pass only what the task needs —
   that's why `--mcp-config` is a per-run flag rather than a global setting.
2. **Keep bulky MCP results out of the lead's context.** The lead's job is
   narrative, not payload. For big pulls (a full Figma file, a large export),
   tell the lead — in your task text — to save results to the run's notes
   directory and spawn a subagent to process them. The distilled summary comes
   back; the payload stays on disk.
3. **Lead-only for now.** Subagents are spawned without the extra servers, so
   the lead makes the MCP calls itself and delegates the *processing*. (Passing
   MCP servers through to spawns is a planned extension — ask for it when a
   real task needs a subagent to drive an MCP server directly.)
4. **Auth happens once, interactively.** Token-based servers take credentials
   via `env` in the config. OAuth servers (Figma's hosted MCP, etc.) need a
   one-time interactive grant first: `claude mcp add --transport http figma
   https://mcp.figma.com/mcp`, then `/mcp` in an interactive session to complete
   login — after that, headless runs reuse the stored credential.
5. **Smoke-test the server before trusting a long run.** A cheap probe run
   ("List 2 items from <server> and stop") confirms connectivity and auth for a
   few hundred tokens, instead of discovering a dead server 10 minutes into a
   loop.

## Observability

Live activity renders to **stderr**. stdout carries only the final answer and
only when piped/redirected (`harness run ... > out.md`, cron) — at a TTY you
already watched it stream, so it isn't reprinted. Claude-CLI-style log: colored bullets, bold tool names,
dim metadata, and each subagent grouped in its own color-coded block:

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
| `lead fable-5[1m] 24.3k (2%)` | lead agent: model, context used, **% of its context window** — should stay low if delegation is working |
| `sub-1 haiku-4-5 38s 12k (6%)` | live subagent: model, elapsed, its own context and window % |
| `iter 2/5` | loop progress (`✓`/`✗` = last check result) |
| `idle 12s` | seconds since last event — turns **red past 30s**: go look at the log |
| `tok 41k` | total tokens spent this run (your usage-limit proxy) |
| `notes 3` | files in the shared notes dir (handoffs happening) |

Set `HARNESS_POWERLINE=1` for powerline chevron transitions (needs a patched
font); `NO_COLOR=1` disables color entirely.

The lead agent streams via `claude -p --output-format stream-json`; subagent
activity escapes the MCP-server process through `runs/<id>/events.jsonl`, which
the CLI tails (and which you can `tail -f` yourself, including from automation).
At the end: a per-agent usage table with models and the lead's context occupancy
(persisted to `runs/<id>/usage.json`), plus — when `--workdir` is a git repo — a
what-changed summary (new dirty files since run start + `git diff --stat`).

Model selection and routing is covered in **Choosing models** above.

### Interactive mode (free byproduct)

The same MCP server mounts in a normal interactive Claude Code session, giving it
the spawn + memory tools directly. Set the `HARNESS_*` env vars (see
`harness/mcp_server.py`) in the server entry:

```sh
claude mcp add harness -- .venv/bin/python -m harness.mcp_server
```

## Layout

```
harness/
  backends/        CLI adapters: claude (resume support), codex, gemini
  mcp_server.py    spawn_* + memory tools (FastMCP, stdio)
  runner.py        single run: lead agent + MCP config + reflection
  loop.py          deterministic loop driver (success check, retries, state file)
  memory.py        persistent cross-run memory (file-per-fact + INDEX.md)
  cli.py           `harness run` / `harness loop`
  # library-only API path (needs ANTHROPIC_API_KEY; kept for future use):
  agent.py  orchestrator.py  providers/  mcp_manager.py  offload.py  tools.py
tests/smoke_test.py   offline tests (no auth needed)
```

## Troubleshooting

- **`zsh: no such file or directory: .venv/bin/harness`** — you're not in the
  harness folder; use the absolute path or the alias (see Setup → Where to run
  it from).
- **`ModuleNotFoundError: No module named 'harness'`** (from the `harness`
  entry point) — historical: when this project lived in iCloud-synced
  `~/Desktop`, iCloud kept re-applying the macOS `hidden` flag across the venv,
  and Python 3.13 silently skips hidden `.pth` files (it also renamed a `.venv`
  symlink to `.venv 2`). Moving to `~/dev/harness` (outside iCloud) removed the
  root cause; `site-packages/sitecustomize.py` remains as a safety net — it
  injects the project path via normal module import, which ignores hidden
  flags. If this error appears: check `sitecustomize.py` exists, and run
  `ls -lO .venv/lib/python3.13/site-packages/*.pth` to look for `hidden` flags.
- **Status bar separators look broken** — only relevant with
  `HARNESS_POWERLINE=1` (chevron mode needs a powerline-patched font); unset it
  to return to the default spaced-pill rendering, which works in any font.
- **A run seems stuck** — check the status bar's `idle Ns` segment (red past
  30s), then `tail -f` the run's `events.jsonl` under `runs/<id>/`.

## Notes

- Every orchestrated agent gets a distilled set of coding norms (assumptions
  stated, minimal code, surgical changes, verify before done) baked into its
  guidance — all providers. Claude agents additionally honor the **target
  project's** `CLAUDE.md` (loaded from `--workdir`) and your global
  `~/.claude/CLAUDE.md`, so project-specific rules travel with the project.
- Subagents default to `--permission-mode bypassPermissions` (full autonomy in
  the workdir). Point `--workdir` at directories you trust the agents to modify.
- `codex` / `gemini` backends are best-effort until those CLIs are installed and
  their flags verified (`codex exec --help`, `gemini --help`); `spawn_*` returns
  a clean error if a CLI is missing, so the lead agent routes around it.
- Offline tests: `.venv/bin/python tests/smoke_test.py`

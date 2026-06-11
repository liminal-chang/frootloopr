# CLAUDE.md — frootloopr

Behavioral coding norms live in the global `~/.claude/CLAUDE.md` (Karpathy
guidelines) — do not duplicate them here. This file is frootloopr-specific.

## What this is

A multi-agent orchestrator built on subscription-authenticated agent CLIs
(`claude -p`, `codex exec`, `gemini -p`). **There are no API keys** — never wire
raw SDK calls into the active path. Architecture and usage are in README.md.

The project is `frootloopr` (command: `frootloopr`, or the short alias `fl`). The
on-disk repo folder is still `~/dev/harness` — the folder was intentionally not
renamed (keeps the venv + the `/orchestrate` skill's hardcoded path working).

## Module map (active path)

- `cli.py` — entry point; rendering, status bar feed, usage/git reports,
  `fl watch`, per-run `summary.md` + `runs/INDEX.md`, `--commit` branch-first
- `runner.py` — lead-agent session (resume), prompts/guidance, plan-first turns,
  MCP config (lead + subagent), self-identifying run ids
- `loop.py` — deterministic iterate-until-check driver
- `mcp_server.py` — spawn_* + memory tools; launched BY the claude CLI as
  `python -m frootloopr.mcp_server` (stdio)
- `backends/` — CLI adapters (claude has stream-json + resume; codex/gemini are
  flag-unverified until those CLIs are installed)
- `events.py`, `status.py`, `ui.py` — observability plumbing and rendering
- `memory.py`, `workspace.py` — persistent memory store, per-run notes dir
- `mcp/` — bundled MCP configs (`default.json` = Context7, auto-mounted;
  `playwright.json` = opt-in)

`agent.py`, `providers/`, `orchestrator.py`, `mcp_manager.py`, `offload.py`,
`tools.py` are the **dormant raw-API path** (needs ANTHROPIC_API_KEY). Don't
revive or "clean up" without being asked; `smoke_test.py` still covers parts.

## Invariants (violating these breaks things subtly)

- **Headless rules**: spawned claude runs pass `--disallowedTools
  AskUserQuestion` (plus `ExitPlanMode`/`EnterPlanMode` on plan turns) and the
  guidance says "you are HEADLESS". Interactive-only tools deadlock `-p` runs.
- **stdout/stderr contract**: stdout = final answer, printed only when piped.
  All live output goes through `StatusConsole.log()` (never bare prints in
  renderers) so the sticky status line can clear/redraw around it.
- **The MCP server must never print to stdout** — that's the MCP protocol
  channel. Subagent observability goes through `events.jsonl` (`EventLog`).
- **System prompts must stay byte-stable** within a session (prompt caching);
  volatile context goes in user messages. `CODE_NORMS` is single-sourced in
  `runner.py` and imported by `mcp_server.py`.
- **MCP server name ↔ tool names ↔ guidance** are coupled: `FastMCP("frootloopr")`
  makes the tools `mcp__frootloopr__spawn_*`, which `LEAD_GUIDANCE` (runner.py)
  references and `ui.py` `display_tool` strips. Change one, change all three.
- **Env prefix `FROOTLOOPR_*`** is written in `runner.py` (`_write_mcp_config`)
  and read in `mcp_server.py` / `ui.py` — keep writer and readers in sync.
- **Subagent MCP config excludes the frootloopr server**: subagents get only the
  third-party servers (no `spawn_*`), so they can't recursively spawn. The
  smoke test guards this.
- **`--commit` branches before the run**: `_ensure_commit_branch` switches off
  main/master deterministically; only the lead commits, never subagents.
- **Status bar**: plain labels, no font-dependent glyphs by default (powerline
  chevrons are opt-in via `FROOTLOOPR_POWERLINE=1`).

## Verify changes

```sh
.venv/bin/python tests/smoke_test.py          # offline, no auth needed
.venv/bin/fl run --help                       # CLI parses, examples render
```

For a live check (uses Max quota, tiny): a haiku "Reply with exactly: ok" call
through `backends.ClaudeBackend`.

Gotcha history (the iCloud hidden-flag saga, why the venv setup looks the way
it does) is in README.md → Troubleshooting.

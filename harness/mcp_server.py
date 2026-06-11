"""The harness MCP server (stdio).

Exposes spawn_claude / spawn_codex / spawn_gemini + persistent-memory tools.
Mounted by the headless lead agent on every run — and mountable in an interactive
Claude Code session for hands-on orchestration (same tools, same memory).

This process cannot print to the terminal (its stdout IS the MCP protocol), so
all subagent activity — spawn start/end, each tool call, usage, models — is
appended to the run's events.jsonl (HARNESS_EVENTS_FILE), which the harness CLI
tails and renders live.

Launched by the agent CLI itself; run context arrives via environment variables:
  HARNESS_WORKDIR     where spawned agents execute (the project dir)
  HARNESS_NOTES_DIR   shared notes directory for this run (absolute)
  HARNESS_MEMORY_DIR  persistent memory store (absolute)
  HARNESS_RUN_ID      current run id (memory provenance)
  HARNESS_EVENTS_FILE events.jsonl path for this run (absolute)
  HARNESS_SUBAGENT_MODEL  default model for claude subagents (optional)
  HARNESS_SPAWN_TIMEOUT_S per-spawn timeout (default 3600)
"""

from __future__ import annotations

import os
import time
from itertools import count
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .backends import BackendError, ClaudeBackend, CodexBackend, GeminiBackend
from .events import EventLog, context_tokens, normalize_claude_event
from .memory import MemoryStore
from .runner import CODE_NORMS

mcp = FastMCP("harness")

WORKDIR = Path(os.environ.get("HARNESS_WORKDIR", ".")).resolve()
NOTES_DIR = Path(os.environ.get("HARNESS_NOTES_DIR", str(WORKDIR / "notes"))).resolve()
RUN_ID = os.environ.get("HARNESS_RUN_ID", "run_unknown")
TIMEOUT_S = int(os.environ.get("HARNESS_SPAWN_TIMEOUT_S", "3600"))

_memory = MemoryStore(
    Path(os.environ.get("HARNESS_MEMORY_DIR", str(WORKDIR / "memory"))).resolve(),
    RUN_ID,
)
_events = EventLog(
    Path(os.environ.get("HARNESS_EVENTS_FILE", str(WORKDIR / "events.jsonl"))).resolve()
)
_spawn_seq = count(1)

SUBAGENT_GUIDANCE = (
    "You are a focused subagent in a multi-agent harness, working on one assigned "
    "task. Work only on that task; do not expand scope. Write durable findings that "
    "other agents may need to {notes_dir}/<topic>.md. You are running HEADLESS — "
    "never ask the user questions; make reasonable assumptions and note them. Your "
    "final message is returned verbatim to the orchestrator and your context is "
    "then discarded: make it a complete, distilled result (facts, paths, numbers, "
    "conclusions) — not a narrative of what you did."
) + CODE_NORMS


def _subagent_prompt(task: str, context_hint: str) -> str:
    # Working directory comes first so phrases like "this directory" in the task
    # resolve to the project dir, not the notes dir (the guidance already names
    # the notes dir as the write target for findings).
    parts = [f"Your working directory (the project you operate on): {WORKDIR}", task]
    if context_hint:
        parts.append(f"Start by reading: {context_hint}")
    return "\n\n".join(parts)


async def _spawn(backend, task: str, context_hint: str) -> str:
    if not backend.available():
        return (
            f"Error: the '{backend.name}' CLI is not installed on this machine. "
            f"Use a different spawn tool."
        )
    agent = f"subagent-{next(_spawn_seq)}"
    _events.write(
        agent, "spawn_start", backend=backend.name, model=backend.model, task=task[:200]
    )

    def on_raw(record: dict) -> None:
        for ev in normalize_claude_event(record):
            if ev["type"] == "tool":
                _events.write(agent, "tool", tool=ev["tool"], summary=ev["summary"])
            elif ev["type"] == "text":
                _events.write(agent, "text", text=ev["text"][:300])
            elif ev["type"] == "init" and ev.get("model"):
                _events.write(agent, "model", model=ev["model"])
            elif ev["type"] == "turn_usage":
                _events.write(agent, "ctx", tokens=context_tokens(ev["usage"]))

    started = time.monotonic()
    guidance = SUBAGENT_GUIDANCE.format(notes_dir=NOTES_DIR)
    try:
        result = await backend.run(
            _subagent_prompt(task, context_hint),
            cwd=WORKDIR,
            system_append=guidance,
            timeout_s=TIMEOUT_S,
            on_event=on_raw,
            disallowed_tools=["AskUserQuestion"],  # headless: nobody to answer
        )
    except BackendError as e:
        _events.write(
            agent, "spawn_end", backend=backend.name, error=str(e)[:500],
            duration_s=round(time.monotonic() - started, 1),
        )
        return f"Error: {backend.name} subagent failed: {e}"

    _events.write(
        agent, "spawn_end", backend=backend.name, model=result.model or backend.model,
        usage=result.usage, model_usage=result.model_usage,
        duration_s=round(time.monotonic() - started, 1),
        result_chars=len(result.text),
    )
    return result.text or "(subagent returned no text)"


@mcp.tool()
async def spawn_claude(task: str, context_hint: str = "", model: str = "") -> str:
    """Spawn a focused Claude subagent for a subtask. Call this when a step needs
    heavy tool use, exploration, or large-output processing — the subagent works in
    its own context and returns only a distilled summary, keeping your context
    small. Give it a self-contained task (it cannot see this conversation),
    including what the result should contain. Use context_hint to point it at
    notes files from earlier subagents. model picks the subagent's model: "haiku"
    for mechanical scans and simple lookups, "sonnet" for routine multi-step work,
    "opus" (or leave empty for the default) for hard reasoning."""
    chosen = model or os.environ.get("HARNESS_SUBAGENT_MODEL") or None
    return await _spawn(ClaudeBackend(model=chosen), task, context_hint)


@mcp.tool()
async def spawn_codex(task: str, context_hint: str = "") -> str:
    """Spawn an OpenAI Codex subagent for a subtask. Call this for an independent
    second implementation or review from a different model family, or to spread
    load when Claude usage limits are a concern. Same rules as spawn_claude: give a
    self-contained task; only a distilled summary returns."""
    return await _spawn(CodexBackend(), task, context_hint)


@mcp.tool()
async def spawn_gemini(task: str, context_hint: str = "") -> str:
    """Spawn a Google Gemini subagent for a subtask. Call this for an independent
    perspective from a different model family, very large-context reading tasks, or
    to spread load across providers. Same rules as spawn_claude: give a
    self-contained task; only a distilled summary returns."""
    return await _spawn(GeminiBackend(), task, context_hint)


@mcp.tool()
def memory_read(name: str) -> str:
    """Read a persistent memory entry by name. Call this when the memory index
    lists something relevant to the current task. Treat memories as hints — verify
    facts against reality before relying on them."""
    try:
        return _memory.read(name)
    except FileNotFoundError as e:
        return f"Error: {e}"


@mcp.tool()
def memory_write(name: str, description: str, type: str, content: str) -> str:
    """Create or update a persistent memory (survives across runs). Call this for
    durable facts: user preferences, environment gotchas, project facts. Reuse an
    existing name to update rather than duplicate. type must be one of: preference,
    learning, project-fact, reference. Use absolute dates, not relative ones."""
    try:
        result = _memory.write(name, description, type, content)
        _events.write("lead", "memory", op="write", name=name)
        return result
    except (ValueError, OSError) as e:
        return f"Error: {e}"


@mcp.tool()
def memory_delete(name: str) -> str:
    """Delete a persistent memory that turned out to be wrong or obsolete."""
    try:
        result = _memory.delete(name)
        _events.write("lead", "memory", op="delete", name=name)
        return result
    except FileNotFoundError as e:
        return f"Error: {e}"


if __name__ == "__main__":
    mcp.run()

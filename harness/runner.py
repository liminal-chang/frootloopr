"""Single-run orchestration over CLI backends.

The lead agent is a headless Claude Code session that mounts the harness MCP
server (spawn_* + memory tools). Subagents do tool-heavy work in their own
contexts; only distilled summaries return — that's the anti-compaction mechanism,
now enforced by process boundaries. Session resume keeps one lead conversation
across loop iterations and the reflection turn.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .backends import BackendResult, ClaudeBackend
from .memory import MemoryStore

# Distilled from the project's Karpathy-style CLAUDE.md guidelines — the core
# that earns its tokens on every orchestrated agent, in every workdir, on every
# provider. Deliberately short: current models follow instructions literally,
# and heavy "don't do X" prose makes them under-act.
CODE_NORMS = """\

When changing code:
- State your assumptions; if multiple interpretations exist, pick the most \
reasonable one and say so in your summary.
- Write the minimum code that solves the task — no speculative features, \
abstractions, or configurability beyond what was asked.
- Touch only what the task requires; match the existing style; clean up only \
orphans your own changes created.
- Verify before declaring done: run the relevant check or test, and if something \
was not verified, say so plainly.
"""

LEAD_GUIDANCE = """\
You are the lead agent (orchestrator) in a multi-agent harness.
- Delegate tool-heavy or exploratory subtasks via the spawn_claude / spawn_codex \
/ spawn_gemini tools (mcp__harness__*); keep your own context lean. Do small, \
single-step lookups yourself.
- Subagents cannot see this conversation: give them self-contained tasks that say \
what the returned result must contain. They return distilled summaries; their \
working files and findings land in the shared notes directory named in your first \
message.
- Route models deliberately: spawn_claude with model "haiku" for mechanical scans \
and simple lookups, "sonnet" for routine multi-step work, default/"opus" only for \
hard reasoning. Use spawn_codex / spawn_gemini for independent second opinions, \
cross-model review, or to spread usage across providers.
- A persistent memory index may appear in your first message. Treat memories as \
hints — verify before relying on them; memory_read for full entries.
- If third-party MCP tools are mounted (e.g. Context7 for live library docs, \
Playwright for browser automation), prefer them over recalling library/framework \
APIs from memory; claude subagents have the same tools.
- You are running HEADLESS — there is no user to answer questions mid-run. Never \
wait for approval; make reasonable assumptions and note them in your final summary.
""" + CODE_NORMS

# Appended to the lead guidance only when --commit is set. The pattern lives here
# (not in a skill) so it reaches the lead on every run, including terminal use —
# and only the lead commits, never subagents (parallel commits race the workdir).
COMMIT_GUIDANCE = """

Committing your work (you have --commit; do this yourself — subagents never \
commit):
- Commit onto the CURRENT branch — the harness has already moved a --commit run \
onto a dedicated branch off main/master. Never switch to, or commit on, \
main/master.
- Commit in logical units, NOT one catch-all dump: when the work spans separable \
concerns — implementation vs tests vs docs, or two independent changes — make a \
SEPARATE commit for each, in a sensible order, each message traceable to its one \
concern. A single commit is right only when the change is genuinely one cohesive unit.
- Message: imperative subject line; a short body explaining WHY when it isn't \
obvious. End every message with the trailer line: \
Co-Authored-By: Claude (harness lead) <noreply@anthropic.com>
- Run the project's checks/tests before committing; don't commit a red tree — fix \
it or report it instead.
- Do NOT push or open PRs unless the task explicitly asks."""

# Interactive-only tools that deadlock or thrash a headless run: nobody is there
# to answer a question or approve a native plan-mode exit (the harness driver is
# the gate, not the claude CLI's interactive machinery).
HEADLESS_DISALLOWED = ["AskUserQuestion"]
PLAN_DISALLOWED = ["AskUserQuestion", "ExitPlanMode", "EnterPlanMode"]

PLAN_PROMPT_SUFFIX = """\


Before any implementation: you are currently in READ-ONLY plan mode. Explore the \
project as needed, then output a complete implementation plan as your final \
message: the goal, ordered steps, files to touch, which subtasks you will \
delegate to subagents (and which backend/model for each), and verifiable success \
criteria. Do not implement anything yet. You are running headless: do NOT call \
ExitPlanMode and do NOT ask the user anything — the harness handles plan \
approval outside this session. Just end your turn with the full plan as text."""

REPLAN_PROMPT = """\
Revise the plan based on this feedback, and output the full revised plan as your \
final message. Stay in planning — do not implement yet.

Feedback: {feedback}"""

IMPLEMENT_PROMPT = """\
The plan is approved — implement it now. The approved plan is saved at \
{plan_path}; point subagents at it via context_hint. Follow it, and if reality \
forces a deviation, note the deviation and why in your final summary."""

REFLECTION_PROMPT = """\
The task above is complete. Before finishing, review this run for durable \
learnings worth persisting across future runs: user preferences, environment \
gotchas, project facts, hard-won discoveries.
- Check the memory index from the start of this conversation; UPDATE an existing \
memory (same name) rather than creating a near-duplicate.
- Skip anything derivable from the project files or this run's notes.
- Use absolute dates, not relative ones.
- Use memory_write / memory_delete as needed. If nothing is durable, reply \
"No memory updates." and stop.
"""


@dataclass
class RunConfig:
    workdir: Path = Path(".")
    runs_dir: Path = Path("runs")
    memory_dir: Path = Path("memory")
    lead_model: str | None = None
    subagent_model: str | None = None
    spawn_timeout_s: int = 3600
    lead_timeout_s: int = 7200
    reflect: bool = True
    commit: bool = False  # --commit: lead commits completed work, branch-first
    extra_mcp_servers: dict[str, Any] = field(default_factory=dict)


def _run_slug(text: str, max_len: int) -> str:
    """Filesystem-safe, human-readable slug for run-dir names."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len].rstrip("-")


def make_run_id(workdir: Path, task: str) -> str:
    """run_<timestamp>_<project>_<task> — self-identifying so a flat runs/ dir and
    its INDEX stay scannable across projects. Timestamp leads, so reverse-lexical
    sort is still chronological and prefix lookups (run_<ts>) still resolve."""
    parts = ["run", datetime.now().strftime("%Y%m%d_%H%M%S")]
    project = _run_slug(workdir.name, 20)
    if project:
        parts.append(project)
    task_slug = _run_slug(" ".join(task.split()[:6]), 30)
    if task_slug:
        parts.append(task_slug)
    return "_".join(parts)


class Runner:
    def __init__(self, config: RunConfig, on_lead_event=None, task: str = ""):
        self.config = config
        self.workdir = config.workdir.resolve()
        self.run_id = make_run_id(self.workdir, task)
        self.run_dir = (config.runs_dir / self.run_id).resolve()
        self.notes_dir = self.run_dir / "notes"
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.memory = MemoryStore(config.memory_dir.resolve(), self.run_id)
        self.lead = ClaudeBackend(model=config.lead_model)
        self.session_id: str | None = None
        self.on_lead_event = on_lead_event  # receives raw stream-json records
        self.lead_turns: list[BackendResult] = []
        self._mcp_config_path = self.run_dir / "mcp_config.json"
        self._subagent_mcp_config_path: Path | None = None
        self._write_mcp_config()

    def _write_mcp_config(self) -> None:
        pkg_root = Path(__file__).resolve().parent.parent
        # Subagents get the third-party servers only — never the harness server,
        # which would let them call spawn_* and recursively spawn agent fleets.
        if self.config.extra_mcp_servers:
            self._subagent_mcp_config_path = self.run_dir / "subagent_mcp_config.json"
            self._subagent_mcp_config_path.write_text(
                json.dumps({"mcpServers": self.config.extra_mcp_servers}, indent=2)
            )
        servers: dict[str, Any] = {
            "harness": {
                "command": sys.executable,
                "args": ["-m", "harness.mcp_server"],
                "env": {
                    "PYTHONPATH": str(pkg_root),
                    "HARNESS_WORKDIR": str(self.workdir),
                    "HARNESS_NOTES_DIR": str(self.notes_dir),
                    "HARNESS_MEMORY_DIR": str(self.config.memory_dir.resolve()),
                    "HARNESS_RUN_ID": self.run_id,
                    "HARNESS_EVENTS_FILE": str(self.events_path),
                    "HARNESS_SPAWN_TIMEOUT_S": str(self.config.spawn_timeout_s),
                    **(
                        {"HARNESS_SUBAGENT_MODEL": self.config.subagent_model}
                        if self.config.subagent_model
                        else {}
                    ),
                    **(
                        {"HARNESS_SUBAGENT_MCP_CONFIG": str(self._subagent_mcp_config_path)}
                        if self._subagent_mcp_config_path
                        else {}
                    ),
                },
            },
            **self.config.extra_mcp_servers,
        }
        self._mcp_config_path.write_text(json.dumps({"mcpServers": servers}, indent=2))

    def first_prompt(self, task: str) -> str:
        parts = []
        index = self.memory.load_index()
        if index:
            parts.append(f"<memory-index>\n{index}\n</memory-index>")
        parts.append(
            f"Project working directory: {self.workdir}\n"
            f"Shared notes directory (subagent findings land here): {self.notes_dir}"
        )
        parts.append(task)
        return "\n\n".join(parts)

    def _lead_system_prompt(self) -> str:
        """Lead guidance, plus the commit pattern when --commit is set. Byte-stable
        within a run (config is fixed), preserving prompt caching."""
        return LEAD_GUIDANCE + (COMMIT_GUIDANCE if self.config.commit else "")

    async def send(self, prompt: str, permission_mode: str | None = None) -> BackendResult:
        """Send one turn to the lead agent, continuing its session if one exists.

        permission_mode overrides the backend default for this turn only —
        "plan" makes the turn read-only (the plan-first phase)."""
        result = await self.lead.run(
            prompt,
            cwd=self.workdir,
            system_append=self._lead_system_prompt(),
            mcp_config=self._mcp_config_path,
            resume=self.session_id,
            timeout_s=self.config.lead_timeout_s,
            on_event=self.on_lead_event,
            permission_mode=permission_mode,
            disallowed_tools=PLAN_DISALLOWED if permission_mode == "plan" else HEADLESS_DISALLOWED,
        )
        self.session_id = result.session_id or self.session_id
        self.lead_turns.append(result)
        return result

    async def run(self, task: str) -> BackendResult:
        result = await self.send(self.first_prompt(task))
        if self.config.reflect:
            await self.send(REFLECTION_PROMPT)
        return result

    # -- plan-first phase ---------------------------------------------------------

    @property
    def plan_path(self) -> Path:
        return self.notes_dir / "plan.md"

    async def plan(self, task: str) -> BackendResult:
        """Read-only planning turn: explore + produce a plan, implement nothing.
        The driver persists the returned text to notes/plan.md."""
        return await self.send(self.first_prompt(task) + PLAN_PROMPT_SUFFIX, permission_mode="plan")

    async def replan(self, feedback: str) -> BackendResult:
        return await self.send(REPLAN_PROMPT.format(feedback=feedback), permission_mode="plan")

    def implement_prompt(self) -> str:
        return IMPLEMENT_PROMPT.format(plan_path=self.plan_path)

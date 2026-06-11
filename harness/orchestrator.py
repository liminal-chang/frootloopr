"""Lead agent + spawn_agent tool.

The anti-compaction mechanism lives here: subagents do tool-heavy work in their
own transcripts (discarded afterwards) and only a distilled result string returns
to the orchestrator, whose context grows by one summary per subtask.

System prompts are byte-stable constants — volatile context (memory index, task)
goes into messages so the cached tools+system prefix survives across runs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from .agent import Agent
from .config import HarnessConfig
from .mcp_manager import MCPManager
from .memory import MemoryStore
from .offload import Offloader
from .providers.anthropic import AnthropicProvider
from .tools import ToolRegistry, register_file_tools, register_memory_tools
from .types import AgentResult, ToolDef, Usage
from .workspace import Workspace

ORCHESTRATOR_SYSTEM = """\
You are the lead agent (orchestrator) in a multi-agent harness.

Working style:
- Delegate tool-heavy or exploratory work to subagents via spawn_agent; keep your \
own context lean. Spawn a subagent when a step will need many tool calls or large \
results; do small, single-step lookups yourself.
- Subagents return distilled summaries. Shared files live in the workspace: \
notes/ for findings, tool_results/ for offloaded large tool outputs.
- Tool results over the size threshold are auto-saved to tool_results/ with a \
preview; read_file/grep_file them selectively instead of asking for them again.
- A persistent memory index may appear in your first message. Treat memories as \
hints — verify facts against reality before relying on them. Use memory_read for \
full entries.
- Your final message should directly answer the user's task.
"""

SUBAGENT_SYSTEM = """\
You are a focused subagent in a multi-agent harness, working on one assigned task.

Working style:
- Work only on the assigned task; do not expand scope.
- Tool results over the size threshold are auto-saved to tool_results/ with a \
preview; read_file/grep_file them selectively.
- Write durable findings other agents may need to notes/<topic>.md in the workspace.
- Your final message is returned verbatim to the orchestrator and your transcript \
is discarded: make it a complete, distilled result (facts, paths, numbers, \
conclusions) — not a narrative of what you did.
"""

REFLECTION_PROMPT = """\
The task above is complete. Before finishing, review this run for durable \
learnings worth persisting across future runs: user preferences, environment \
gotchas, project facts, hard-won discoveries.

- Check the memory index from the start of this conversation; UPDATE an existing \
memory (same name) rather than creating a near-duplicate.
- Skip anything derivable from the workspace, the code, or this run's notes.
- Use absolute dates, not relative ones.
- Use memory_write / memory_delete as needed. If nothing is durable, reply \
"No memory updates." and stop.
"""


class Orchestrator:
    def __init__(
        self,
        config: HarnessConfig,
        mcp: MCPManager | None = None,
        stream_cb: Callable[[str], None] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.config = config
        self.on_event = on_event or (lambda e: None)
        self.run_id = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        self.workspace = Workspace(config.runs_dir / self.run_id)
        self.memory = MemoryStore(config.memory_dir, self.run_id)
        self.usage_by_agent: dict[str, Usage] = {}
        self._spawn_count = 0

        offloader = Offloader(
            self.workspace,
            threshold_tokens=config.offload_threshold_tokens,
            on_event=self.on_event,
        )

        # Shared registry: file tools + memory tools + MCP tools.
        base_registry = ToolRegistry()
        register_file_tools(base_registry, self.workspace)
        register_memory_tools(base_registry, self.memory)
        if mcp:
            mcp.register_into(base_registry)

        # Subagents get everything except spawn_agent (one level of delegation).
        self._sub_registry = base_registry
        self._offloader = offloader

        orch_registry = base_registry.copy_without()  # shallow copy
        orch_registry.register(self._spawn_tool_def(), self._spawn_agent)

        self.lead = Agent(
            name="orchestrator",
            provider=AnthropicProvider(config.model, config.max_tokens),
            system=ORCHESTRATOR_SYSTEM,
            registry=orch_registry,
            offloader=offloader,
            max_turns=config.max_turns,
            stream_cb=stream_cb,
            on_event=self.on_event,
        )

    async def run(self, task: str) -> AgentResult:
        index = self.memory.load_index()
        first_message = task
        if index:
            first_message = f"<memory-index>\n{index}\n</memory-index>\n\n{task}"

        result = await self.lead.run(first_message)

        if self.config.reflect:
            self.on_event({"type": "reflection_start"})
            await self.lead.run(REFLECTION_PROMPT)

        self.usage_by_agent["orchestrator"] = self.lead.usage
        return result

    # -- spawn_agent ----------------------------------------------------------------

    def _spawn_tool_def(self) -> ToolDef:
        return ToolDef(
            name="spawn_agent",
            description=(
                "Spawn a focused subagent for a subtask. Call this when a step needs "
                "heavy tool use, exploration, or large-result processing — the "
                "subagent works in its own context and returns only a distilled "
                "summary, keeping your context small. Give it a self-contained task: "
                "it cannot see this conversation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Self-contained task description, including what the result should contain",
                    },
                    "context_hint": {
                        "type": "string",
                        "description": "Optional: workspace files (notes/, tool_results/) the subagent should read first",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override for this subagent",
                    },
                },
                "required": ["task"],
            },
        )

    async def _spawn_agent(self, inp: dict) -> str:
        self._spawn_count += 1
        name = f"subagent-{self._spawn_count}"
        model = inp.get("model") or self.config.subagent_model or self.config.model
        self.on_event({"type": "spawn", "agent": name, "task": inp["task"][:200], "model": model})

        task = inp["task"]
        if inp.get("context_hint"):
            task += f"\n\nStart by reading these workspace files: {inp['context_hint']}"

        sub = Agent(
            name=name,
            provider=AnthropicProvider(model, self.config.max_tokens),
            system=SUBAGENT_SYSTEM,
            registry=self._sub_registry,
            offloader=self._offloader,
            max_turns=self.config.subagent_max_turns,
            on_event=self.on_event,
        )
        result = await sub.run(task)
        self.usage_by_agent[name] = result.usage
        self.on_event({"type": "spawn_done", "agent": name, "turns": result.turns})
        # Transcript is discarded here — only the distilled text returns.
        return result.text

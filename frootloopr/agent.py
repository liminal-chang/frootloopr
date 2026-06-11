"""The agentic loop. One agent = one provider + one transcript + a tool registry.

The loop is manual (not an SDK tool runner) because the offload interceptor must
sit between tool dispatch and the transcript — that hook is the point of the
frootloopr. Agents keep their transcript across run() calls, so the orchestrator can
continue a conversation (e.g. the end-of-run reflection turn).
"""

from __future__ import annotations

from typing import Callable

from .offload import Offloader
from .providers.base import ModelProvider
from .tools import ToolRegistry
from .types import AgentResult, Message, ToolResult, Usage


class Agent:
    def __init__(
        self,
        name: str,
        provider: ModelProvider,
        system: str,
        registry: ToolRegistry,
        offloader: Offloader | None = None,
        max_turns: int = 40,
        stream_cb: Callable[[str], None] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.name = name
        self.provider = provider
        self.system = system
        self.registry = registry
        self.offloader = offloader
        self.max_turns = max_turns
        self.stream_cb = stream_cb
        self.on_event = on_event
        self.messages: list[Message] = []
        self.usage = Usage()

    async def run(self, user_text: str) -> AgentResult:
        self.messages.append(Message(role="user", text=user_text))
        tools = self.registry.defs()
        last_text = ""

        for turn_i in range(self.max_turns):
            turn = await self.provider.complete(
                self.system, self.messages, tools, stream_cb=self.stream_cb
            )
            self.usage.add(turn.usage)
            self.messages.append(turn.message)
            last_text = turn.message.text or last_text

            if turn.stop_reason == "pause_turn":
                # Server-side tool paused mid-turn; re-send to let it resume.
                continue
            if turn.stop_reason == "refusal":
                return AgentResult(text=last_text or "[model refused]", usage=self.usage, turns=turn_i + 1)
            if turn.stop_reason == "max_tokens" and not turn.message.tool_calls:
                return AgentResult(
                    text=last_text + "\n[response truncated: hit max_tokens]",
                    usage=self.usage,
                    turns=turn_i + 1,
                )
            if not turn.message.tool_calls:
                return AgentResult(text=last_text, usage=self.usage, turns=turn_i + 1)

            results: list[ToolResult] = []
            for call in turn.message.tool_calls:
                if self.on_event:
                    self.on_event({"type": "tool_call", "agent": self.name, "tool": call.name})
                try:
                    raw = await self.registry.dispatch(call.name, call.input)
                    content = self.offloader.process(call.name, raw) if self.offloader else raw
                    results.append(ToolResult(call_id=call.id, content=content))
                except Exception as e:  # tool failures go back to the model as is_error
                    results.append(
                        ToolResult(call_id=call.id, content=f"Error: {e}", is_error=True)
                    )
            self.messages.append(Message(role="user", tool_results=results))

        return AgentResult(
            text=last_text + "\n[stopped: reached max turns]",
            usage=self.usage,
            turns=self.max_turns,
        )

"""Provider adapter interface. One adapter per model provider; the agent loop only
ever sees this protocol and the neutral types."""

from __future__ import annotations

from typing import Callable, Protocol

from ..types import Message, ToolDef, Turn


class ModelProvider(Protocol):
    model: str

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolDef],
        stream_cb: Callable[[str], None] | None = None,
    ) -> Turn:
        """Run one model turn. Implementations must:
        - keep the rendered (system, tools) prefix byte-stable across calls for caching
        - preserve provider-native assistant content on Message.raw for round-tripping
        - normalize stop reasons to: end_turn | tool_use | max_tokens | pause_turn | refusal
        """
        ...

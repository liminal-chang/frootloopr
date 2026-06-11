"""Provider-neutral internal types. Everything inside the frootloopr speaks these;
only provider adapters speak provider wire formats."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: str  # "user" | "assistant"
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    # Provider-native assistant content, kept opaque and round-tripped verbatim.
    # An agent stays on one provider for its lifetime, so this is always valid —
    # and it preserves provider artifacts (e.g. thinking-block signatures) that a
    # lossy neutral rendering would destroy.
    raw: Any = None


@dataclass
class Turn:
    message: Message
    stop_reason: str
    usage: Usage


@dataclass
class AgentResult:
    text: str
    usage: Usage
    turns: int


class ToolError(Exception):
    """A tool failed in a way the model should see and adapt to."""

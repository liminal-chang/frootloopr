"""Anthropic adapter (v1's only provider). Streams every request, uses adaptive
thinking, and places a cache breakpoint on the system block so the byte-stable
tools+system prefix is cached across turns and runs."""

from __future__ import annotations

from typing import Callable

import anthropic

from ..types import Message, ToolCall, ToolDef, Turn, Usage


class AnthropicProvider:
    def __init__(self, model: str = "claude-opus-4-8", max_tokens: int = 16000):
        self.client = anthropic.AsyncAnthropic()
        self.model = model
        self.max_tokens = max_tokens

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolDef],
        stream_cb: Callable[[str], None] | None = None,
    ) -> Turn:
        params: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "thinking": {"type": "adaptive"},
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [self._render(m) for m in messages],
        }
        if tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]

        async with self.client.messages.stream(**params) as stream:
            async for text in stream.text_stream:
                if stream_cb:
                    stream_cb(text)
            response = await stream.get_final_message()

        text = "".join(b.text for b in response.content if b.type == "text")
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        usage = Usage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_input_tokens=response.usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=response.usage.cache_creation_input_tokens or 0,
        )
        message = Message(
            role="assistant",
            text=text,
            tool_calls=tool_calls,
            raw=response.content,  # round-trip verbatim: preserves thinking signatures
        )
        return Turn(message=message, stop_reason=response.stop_reason or "end_turn", usage=usage)

    def _render(self, m: Message) -> dict:
        if m.role == "assistant":
            if m.raw is not None:
                return {"role": "assistant", "content": m.raw}
            return {"role": "assistant", "content": m.text}
        if m.tool_results:
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in m.tool_results
                ],
            }
        return {"role": "user", "content": m.text}

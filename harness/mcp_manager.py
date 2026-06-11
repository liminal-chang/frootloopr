"""MCP client lifecycle and tool bridging.

The harness owns the MCP client and presents MCP tools as plain JSON-schema tools
to whatever model is running — that's what makes MCP provider-neutral here, and it
gives the offload interceptor a place to sit. Tool names are prefixed with the
server name (server__tool) to avoid collisions.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .tools import ToolRegistry
from .types import ToolDef, ToolError


class MCPManager:
    def __init__(self, servers: dict[str, dict[str, Any]]):
        self.servers = servers
        self.tool_defs: dict[str, ToolDef] = {}
        self._routes: dict[str, tuple[ClientSession, str]] = {}
        self._stack: AsyncExitStack | None = None

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        for name, spec in self.servers.items():
            if "url" in spec:
                from mcp.client.streamable_http import streamablehttp_client

                read, write, _ = await self._stack.enter_async_context(
                    streamablehttp_client(spec["url"])
                )
            else:
                params = StdioServerParameters(
                    command=spec["command"],
                    args=spec.get("args", []),
                    env=spec.get("env"),
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            listing = await session.list_tools()
            for t in listing.tools:
                full = f"{name}__{t.name}"
                self.tool_defs[full] = ToolDef(
                    name=full,
                    description=t.description or "",
                    input_schema=t.inputSchema,
                )
                self._routes[full] = (session, t.name)

    async def stop(self) -> None:
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    async def call(self, full_name: str, tool_input: dict) -> str:
        session, tool_name = self._routes[full_name]
        result = await session.call_tool(tool_name, tool_input)
        parts: list[str] = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(f"[{getattr(block, 'type', 'non-text')} content omitted]")
        text = "\n".join(parts)
        if result.isError:
            raise ToolError(text or "MCP tool returned an error")
        return text

    def register_into(self, registry: ToolRegistry) -> None:
        for full_name, tool_def in self.tool_defs.items():
            registry.register(
                tool_def,
                lambda inp, n=full_name: self.call(n, inp),
            )

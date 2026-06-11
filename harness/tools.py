"""Tool registry + built-in tools (workspace files, persistent memory).

Handlers may be sync or async; dispatch normalizes. MCP tools and spawn_agent are
registered into the same registry by mcp_manager.py / orchestrator.py.
"""

from __future__ import annotations

import inspect
import re
from typing import Any, Callable

from .memory import MemoryStore, VALID_TYPES
from .types import ToolDef
from .workspace import Workspace

MAX_READ_CHARS = 40_000
MAX_GREP_MATCHES = 200
MAX_LIST_ENTRIES = 500


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, tuple[ToolDef, Callable[[dict], Any]]] = {}

    def register(self, tool_def: ToolDef, handler: Callable[[dict], Any]) -> None:
        self._tools[tool_def.name] = (tool_def, handler)

    def defs(self) -> list[ToolDef]:
        # Deterministic order so the rendered tool list is byte-stable (cache-friendly).
        return [d for d, _ in (self._tools[k] for k in sorted(self._tools))]

    def copy_without(self, *names: str) -> "ToolRegistry":
        reg = ToolRegistry()
        for name, (tool_def, handler) in self._tools.items():
            if name not in names:
                reg._tools[name] = (tool_def, handler)
        return reg

    async def dispatch(self, name: str, tool_input: dict) -> str:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        _, handler = self._tools[name]
        result = handler(tool_input)
        if inspect.isawaitable(result):
            result = await result
        return str(result)


# -- built-in workspace file tools ------------------------------------------------


def register_file_tools(registry: ToolRegistry, workspace: Workspace) -> None:
    def read_file(inp: dict) -> str:
        path = workspace.resolve(inp["path"])
        if not path.is_file():
            raise FileNotFoundError(f"Not a file: {inp['path']}")
        lines = path.read_text(errors="replace").splitlines()
        offset = int(inp.get("offset", 0))
        limit = int(inp.get("limit", 2000))
        chunk = lines[offset : offset + limit]
        text = "\n".join(chunk)
        suffix = ""
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS]
            suffix = "\n[truncated at 40,000 chars — re-read with offset/limit to see more]"
        elif offset + limit < len(lines):
            suffix = f"\n[showing lines {offset}-{offset + len(chunk)} of {len(lines)} — use offset to continue]"
        return text + suffix

    def write_file(inp: dict) -> str:
        path = workspace.resolve(inp["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp["content"])
        return f"Wrote {len(inp['content'])} chars to {workspace.relative(path)}"

    def grep_file(inp: dict) -> str:
        target = workspace.resolve(inp["path"])
        pattern = re.compile(inp["pattern"])
        files = [target] if target.is_file() else [p for p in sorted(target.rglob("*")) if p.is_file()]
        matches: list[str] = []
        for f in files:
            try:
                for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        matches.append(f"{workspace.relative(f)}:{i}: {line.strip()[:300]}")
                        if len(matches) >= MAX_GREP_MATCHES:
                            matches.append(f"[stopped at {MAX_GREP_MATCHES} matches]")
                            return "\n".join(matches)
            except OSError:
                continue
        return "\n".join(matches) if matches else "No matches."

    def list_files(inp: dict) -> str:
        target = workspace.resolve(inp.get("path", "."))
        entries = []
        for p in sorted(target.rglob("*")):
            if p.is_file():
                entries.append(f"{workspace.relative(p)} ({p.stat().st_size:,} bytes)")
                if len(entries) >= MAX_LIST_ENTRIES:
                    entries.append(f"[stopped at {MAX_LIST_ENTRIES} entries]")
                    break
        return "\n".join(entries) if entries else "(empty)"

    registry.register(
        ToolDef(
            "read_file",
            "Read a file in the shared workspace. Call this to inspect offloaded tool "
            "results under tool_results/ or notes left by other agents under notes/.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "offset": {"type": "integer", "description": "Start line (0-based)"},
                    "limit": {"type": "integer", "description": "Max lines to return (default 2000)"},
                },
                "required": ["path"],
            },
        ),
        read_file,
    )
    registry.register(
        ToolDef(
            "write_file",
            "Write a file in the shared workspace. Use notes/<topic>.md for findings "
            "that other agents or the orchestrator may need later.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        ),
        write_file,
    )
    registry.register(
        ToolDef(
            "grep_file",
            "Regex-search a workspace file (or all files under a directory). Call this "
            "to find the relevant part of a large offloaded tool result instead of "
            "reading the whole file.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file or directory"},
                    "pattern": {"type": "string", "description": "Python regex"},
                },
                "required": ["path", "pattern"],
            },
        ),
        grep_file,
    )
    registry.register(
        ToolDef(
            "list_files",
            "List files in the shared workspace (recursive, with sizes).",
            {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Workspace-relative directory (default '.')"}},
            },
        ),
        list_files,
    )


# -- built-in persistent-memory tools ----------------------------------------------


def register_memory_tools(registry: ToolRegistry, memory: MemoryStore) -> None:
    registry.register(
        ToolDef(
            "memory_read",
            "Read a persistent memory entry by name. Call this when the memory index "
            "lists something relevant to the current task. Treat memories as hints — "
            "verify facts against reality before relying on them.",
            {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Memory name from the index"}},
                "required": ["name"],
            },
        ),
        lambda inp: memory.read(inp["name"]),
    )
    registry.register(
        ToolDef(
            "memory_write",
            "Create or update a persistent memory (survives across runs). Call this for "
            "durable facts: user preferences, environment gotchas, project facts. Check "
            "the index first and reuse an existing name to update rather than duplicate. "
            "Don't store anything derivable from the workspace or code.",
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "kebab-case slug; reuse to update"},
                    "description": {"type": "string", "description": "One-line summary for the index"},
                    "type": {"type": "string", "enum": list(VALID_TYPES)},
                    "content": {"type": "string", "description": "The fact. Use absolute dates, not relative ones."},
                },
                "required": ["name", "description", "type", "content"],
            },
        ),
        lambda inp: memory.write(inp["name"], inp["description"], inp["type"], inp["content"]),
    )
    registry.register(
        ToolDef(
            "memory_delete",
            "Delete a persistent memory that turned out to be wrong or obsolete.",
            {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ),
        lambda inp: memory.delete(inp["name"]),
    )

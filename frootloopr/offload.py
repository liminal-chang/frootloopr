"""Tool-result offload interceptor.

Any tool result over the token threshold is written to the workspace and replaced
in the transcript by a short notice + preview + path. The agent then reads/greps
the file selectively. Provider-agnostic — sits between tool dispatch and the
transcript, so it applies to MCP and built-in tools alike.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from .workspace import Workspace

# Pull-based tools are already bounded/selective; re-offloading their output
# would just copy files around.
EXEMPT_TOOLS = {"read_file", "grep_file", "list_files", "memory_read", "spawn_agent"}


class Offloader:
    def __init__(
        self,
        workspace: Workspace,
        threshold_tokens: int = 2000,
        preview_chars: int = 2000,
        on_event: Callable[[dict], None] | None = None,
    ):
        self.workspace = workspace
        self.threshold_tokens = threshold_tokens
        self.preview_chars = preview_chars
        self.on_event = on_event
        self._counts: dict[str, int] = {}

    def process(self, tool_name: str, content: str) -> str:
        if tool_name in EXEMPT_TOOLS:
            return content
        est_tokens = len(content) // 4
        if est_tokens <= self.threshold_tokens:
            return content

        n = self._counts.get(tool_name, 0) + 1
        self._counts[tool_name] = n
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool_name)
        ext = ".json" if _looks_like_json(content) else ".txt"
        path = self.workspace.tool_results_dir / f"{safe}_{n}{ext}"
        path.write_text(content)
        rel = self.workspace.relative(path)

        if self.on_event:
            self.on_event({"type": "offload", "tool": tool_name, "tokens": est_tokens, "path": rel})

        preview = content[: self.preview_chars]
        return (
            f"[Large result: ~{est_tokens:,} tokens. Full content saved to {rel} — "
            f"use read_file/grep_file on that path to inspect it selectively.]\n"
            f"--- preview (first {len(preview)} chars) ---\n{preview}"
        )


def _looks_like_json(content: str) -> bool:
    s = content.lstrip()
    if not s or s[0] not in "{[":
        return False
    try:
        json.loads(content)
        return True
    except (ValueError, RecursionError):
        return False

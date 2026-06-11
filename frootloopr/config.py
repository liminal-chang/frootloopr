"""Run configuration and MCP server config loading."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class FrootlooprConfig:
    model: str = "claude-opus-4-8"
    subagent_model: str | None = None  # None -> same as model
    max_tokens: int = 16000
    max_turns: int = 40
    subagent_max_turns: int = 30
    offload_threshold_tokens: int = 2000
    runs_dir: Path = Path("runs")
    memory_dir: Path = Path("memory")
    reflect: bool = True
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_mcp_servers(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load an MCP server config file.

    Accepts the Claude-Desktop-style shape {"mcpServers": {name: spec}} or a bare
    {name: spec} mapping. Each spec is either stdio ({"command", "args"?, "env"?})
    or HTTP ({"url"}).
    """
    data = json.loads(Path(path).read_text())
    return data.get("mcpServers", data)

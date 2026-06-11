"""Multi-agent frootloopr.

Primary path (subscription auth): CLI backends + frootloopr MCP server + loop driver.
    Runner, RunConfig, run_loop, backends.*, mcp_server (stdio entry point)

Library-only API path (needs an ANTHROPIC_API_KEY; kept for future use):
    Orchestrator, Agent, AnthropicProvider, MCPManager, Offloader, ToolRegistry
"""

from .backends import BACKENDS, BackendError, BackendResult, ClaudeBackend, CodexBackend, GeminiBackend
from .config import FrootlooprConfig, load_mcp_servers
from .loop import LoopResult, run_loop
from .memory import MemoryStore
from .runner import RunConfig, Runner
from .workspace import Workspace

__all__ = [
    "BACKENDS",
    "BackendError",
    "BackendResult",
    "ClaudeBackend",
    "CodexBackend",
    "GeminiBackend",
    "FrootlooprConfig",
    "LoopResult",
    "MemoryStore",
    "RunConfig",
    "Runner",
    "Workspace",
    "load_mcp_servers",
    "run_loop",
]

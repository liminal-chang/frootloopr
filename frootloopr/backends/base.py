"""CLI agent backends. Each backend wraps a vendor agent CLI invoked headlessly —
the CLI brings its own agentic loop, tools, and context management; the frootloopr
orchestrates at the process level. Auth is whatever the CLI is logged into
(subscription plans), no API keys involved."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


class BackendError(Exception):
    """The CLI invocation failed (nonzero exit, timeout, unparseable output)."""


@dataclass
class BackendResult:
    text: str
    session_id: str | None = None  # only backends with resume support set this
    usage: dict | None = None
    model: str | None = None  # the model that actually served the run, if reported
    model_usage: dict | None = None  # per-model tokens/cost (claude `modelUsage`)


class CLIBackend(Protocol):
    name: str

    def available(self) -> bool: ...

    async def run(
        self,
        prompt: str,
        cwd: Path,
        system_append: str | None = None,
        mcp_config: Path | None = None,
        resume: str | None = None,
        timeout_s: int = 3600,
        on_event: Callable[[dict], None] | None = None,
    ) -> BackendResult: ...

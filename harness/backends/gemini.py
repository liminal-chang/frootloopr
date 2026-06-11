"""Gemini CLI backend: headless `gemini -p`. Uses the logged-in Google account.
No session resume in v1; each invocation is self-contained.

Note: flag set verified against gemini-cli docs, but the CLI isn't installed on
this machine yet — re-check `gemini --help` on first use if a flag errors.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .base import BackendError, BackendResult


class GeminiBackend:
    name = "gemini"

    def __init__(self, model: str | None = None):
        self.model = model

    def available(self) -> bool:
        return shutil.which("gemini") is not None

    async def run(
        self,
        prompt: str,
        cwd: Path,
        system_append: str | None = None,
        mcp_config: Path | None = None,
        resume: str | None = None,
        timeout_s: int = 3600,
        on_event=None,  # no structured event stream wired for gemini in v1
        disallowed_tools=None,  # claude-specific; ignored here
    ) -> BackendResult:
        if system_append:
            prompt = f"{system_append}\n\n---\n\n{prompt}"
        cmd = ["gemini", "-p", prompt, "--yolo"]
        if self.model:
            cmd += ["--model", self.model]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise BackendError(f"gemini timed out after {timeout_s}s")
        if proc.returncode != 0:
            raise BackendError(
                f"gemini exited {proc.returncode}: {stderr.decode(errors='replace')[-2000:]}"
            )
        return BackendResult(text=stdout.decode(errors="replace").strip())

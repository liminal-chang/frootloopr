"""OpenAI Codex CLI backend: headless `codex exec`. Uses the logged-in ChatGPT
Pro plan. No session resume in v1; each invocation is self-contained.

Note: flag set verified against codex-cli docs, but the CLI isn't installed on
this machine yet — re-check `codex exec --help` on first use if a flag errors.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from .base import BackendError, BackendResult


class CodexBackend:
    name = "codex"

    def __init__(self, model: str | None = None):
        self.model = model

    def available(self) -> bool:
        return shutil.which("codex") is not None

    async def run(
        self,
        prompt: str,
        cwd: Path,
        system_append: str | None = None,
        mcp_config: Path | None = None,
        resume: str | None = None,
        timeout_s: int = 3600,
        on_event=None,  # no structured event stream wired for codex in v1
        disallowed_tools=None,  # claude-specific; ignored here
    ) -> BackendResult:
        if system_append:
            # codex exec has no system-prompt flag; fold guidance into the prompt.
            prompt = f"{system_append}\n\n---\n\n{prompt}"
        with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False) as f:
            last_msg_path = Path(f.name)
        try:
            cmd = [
                "codex",
                "exec",
                "--full-auto",
                "--skip-git-repo-check",
                "--output-last-message",
                str(last_msg_path),
            ]
            if self.model:
                cmd += ["--model", self.model]
            cmd.append(prompt)
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
                raise BackendError(f"codex timed out after {timeout_s}s")
            if proc.returncode != 0:
                raise BackendError(
                    f"codex exited {proc.returncode}: {stderr.decode(errors='replace')[-2000:]}"
                )
            text = last_msg_path.read_text().strip() if last_msg_path.exists() else ""
            if not text:
                text = stdout.decode(errors="replace").strip()
            return BackendResult(text=text)
        finally:
            last_msg_path.unlink(missing_ok=True)

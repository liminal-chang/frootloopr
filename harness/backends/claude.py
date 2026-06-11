"""Claude Code backend: headless `claude -p` with stream-json output. Uses the
logged-in Max/Pro subscription. Supports session resume (loop iterations continue
one conversation), mounting MCP servers (the lead agent mounts the harness MCP
server), and live event callbacks — every NDJSON record is surfaced as it arrives
instead of waiting for process exit."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Callable

from .base import BackendError, BackendResult

_STREAM_LIMIT = 32 * 1024 * 1024  # tool inputs/results can make single lines huge


class ClaudeBackend:
    name = "claude"

    def __init__(self, model: str | None = None, permission_mode: str = "bypassPermissions"):
        self.model = model
        self.permission_mode = permission_mode

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def build_cmd(
        self,
        prompt: str,
        system_append: str | None = None,
        mcp_config: Path | None = None,
        resume: str | None = None,
        permission_mode: str | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> list[str]:
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",  # required by the CLI for stream-json in print mode
            "--permission-mode",
            permission_mode or self.permission_mode,
        ]
        if self.model:
            cmd += ["--model", self.model]
        if system_append:
            cmd += ["--append-system-prompt", system_append]
        if mcp_config:
            cmd += ["--mcp-config", str(mcp_config)]
        if resume:
            cmd += ["--resume", resume]
        if disallowed_tools:
            cmd += ["--disallowedTools", ",".join(disallowed_tools)]
        return cmd

    async def run(
        self,
        prompt: str,
        cwd: Path,
        system_append: str | None = None,
        mcp_config: Path | None = None,
        resume: str | None = None,
        timeout_s: int = 3600,
        on_event: Callable[[dict], None] | None = None,
        permission_mode: str | None = None,
        disallowed_tools: list[str] | None = None,
    ) -> BackendResult:
        cmd = self.build_cmd(
            prompt, system_append, mcp_config, resume, permission_mode, disallowed_tools
        )
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # allow nested runs
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STREAM_LIMIT,
        )

        result_record: dict | None = None
        model: str | None = None

        async def consume() -> None:
            nonlocal result_record, model
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # non-JSON noise on stdout
                if record.get("type") == "system" and record.get("subtype") == "init":
                    model = record.get("model") or model
                if record.get("type") == "result":
                    result_record = record
                if on_event:
                    try:
                        on_event(record)
                    except Exception:
                        pass  # observers must never take down the run

        try:
            await asyncio.wait_for(consume(), timeout=timeout_s)
            stderr = (await proc.stderr.read()) if proc.stderr else b""
            await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            raise BackendError(f"claude timed out after {timeout_s}s")

        if proc.returncode != 0 and result_record is None:
            raise BackendError(
                f"claude exited {proc.returncode}: {stderr.decode(errors='replace')[-2000:]}"
            )
        if result_record is None:
            raise BackendError("claude produced no result event")
        if result_record.get("is_error"):
            raise BackendError(f"claude reported error: {str(result_record.get('result'))[:2000]}")
        return BackendResult(
            text=result_record.get("result", "") or "",
            session_id=result_record.get("session_id"),
            usage=result_record.get("usage"),
            model=model,
            model_usage=result_record.get("modelUsage"),
        )

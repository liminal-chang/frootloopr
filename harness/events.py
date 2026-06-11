"""Event plumbing shared by the CLI (lead-agent rendering) and the MCP server
(subagent logging).

Two layers:
- normalize_claude_event(): turns raw `claude -p --output-format stream-json`
  NDJSON records into small normalized events (init/text/tool/turn_usage/result).
- EventLog: append-only JSONL file under runs/<id>/ — the side-channel that gets
  subagent activity out of the MCP-server process (which cannot print to the
  terminal: its stdout is the MCP protocol). The CLI tails this file. Written with
  O_APPEND one-line writes, so the CLI process and the MCP server process can
  share it safely.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def summarize_tool_input(name: str, tool_input: dict) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "task", "name", "query", "url"):
        if key in tool_input and isinstance(tool_input[key], str):
            val = " ".join(tool_input[key].split())
            return val[:100] + ("…" if len(val) > 100 else "")
    try:
        s = json.dumps(tool_input)
    except (TypeError, ValueError):
        return ""
    return s[:100] + ("…" if len(s) > 100 else "")


def normalize_claude_event(raw: dict) -> Iterator[dict]:
    """Yield zero or more normalized events from one raw stream-json record."""
    rtype = raw.get("type")
    if rtype == "system" and raw.get("subtype") == "init":
        yield {"type": "init", "model": raw.get("model"), "session_id": raw.get("session_id")}
    elif rtype == "assistant":
        message = raw.get("message") or {}
        for block in message.get("content") or []:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                yield {"type": "text", "text": block["text"]}
            elif btype == "tool_use":
                yield {
                    "type": "tool",
                    "tool": block.get("name", "?"),
                    "summary": summarize_tool_input(block.get("name", ""), block.get("input") or {}),
                }
        usage = message.get("usage")
        if usage:
            yield {"type": "turn_usage", "usage": usage}
    elif rtype == "result":
        yield {
            "type": "result",
            "usage": raw.get("usage"),
            "num_turns": raw.get("num_turns"),
            "duration_ms": raw.get("duration_ms"),
        }


def context_tokens(usage: dict | None) -> int:
    """Approximate context occupancy from a turn's usage: prompt tokens seen by the
    model this turn (fresh + served-from-cache + written-to-cache)."""
    if not usage:
        return 0
    return (
        (usage.get("input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
        + (usage.get("cache_creation_input_tokens") or 0)
    )


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, agent: str, type: str, **fields: Any) -> None:
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "agent": agent, "type": type, **fields}
        try:
            with self.path.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass  # observability must never take down the run


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events

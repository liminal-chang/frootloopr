"""Offline smoke tests (no API key needed): offloader + memory store + workspace
sandbox. Run with: python tests/smoke_test.py
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.memory import MemoryStore
from harness.offload import Offloader
from harness.workspace import Workspace


def test_offload_large_result():
    with tempfile.TemporaryDirectory() as d:
        ws = Workspace(Path(d) / "run")
        events = []
        off = Offloader(ws, threshold_tokens=2000, on_event=events.append)

        big = '{"items": [' + ",".join(f'"row-{i}"' for i in range(5000)) + "]}"
        assert len(big) // 4 > 2000
        out = off.process("search__query", big)

        assert "Large result" in out and "tool_results/search__query_1.json" in out
        saved = ws.tool_results_dir / "search__query_1.json"
        assert saved.exists() and saved.read_text() == big
        assert len(out) < len(big)
        assert events and events[0]["type"] == "offload"
        print("ok: large result offloaded to file with preview")


def test_offload_small_passthrough_and_exempt():
    with tempfile.TemporaryDirectory() as d:
        ws = Workspace(Path(d) / "run")
        off = Offloader(ws, threshold_tokens=2000)
        small = "just a small result"
        assert off.process("search__query", small) == small
        big = "x" * 100_000
        assert off.process("read_file", big) == big  # exempt: already pull-based
        assert not list(ws.tool_results_dir.iterdir())
        print("ok: small results pass through; exempt tools never offload")


def test_memory_write_update_index():
    with tempfile.TemporaryDirectory() as d:
        mem = MemoryStore(Path(d) / "memory", run_id="run_test")
        assert mem.load_index() == ""

        msg = mem.write("staging-db", "Staging DB is read-only", "project-fact", "The staging DB rejects writes.")
        assert "created" in msg
        index = mem.load_index()
        assert "staging-db" in index and "read-only" in index

        msg = mem.write("staging-db", "Staging DB is read-only (confirmed)", "project-fact", "Still true as of 2026-06-10.")
        assert "updated" in msg
        files = [f.name for f in (Path(d) / "memory").glob("*.md") if f.name != "INDEX.md"]
        assert files == ["staging-db.md"], f"expected single file, got {files}"
        body = mem.read("staging-db")
        assert "updated_by: run_test" in body and "created_by:" in body

        mem.delete("staging-db")
        assert mem.load_index() == ""
        print("ok: memory create/update-not-duplicate/delete + index regeneration")


def test_claude_backend_cmd():
    from harness.backends import ClaudeBackend

    b = ClaudeBackend(model="haiku")
    cmd = b.build_cmd("do it", system_append="sys", mcp_config=Path("/tmp/m.json"), resume="sess-1")
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert "--verbose" in cmd  # required by the CLI for stream-json in print mode
    for flag, val in [("--output-format", "stream-json"), ("--model", "haiku"),
                      ("--append-system-prompt", "sys"), ("--mcp-config", "/tmp/m.json"),
                      ("--resume", "sess-1"), ("--permission-mode", "bypassPermissions")]:
        i = cmd.index(flag)
        assert cmd[i + 1] == val, f"{flag} -> {cmd[i+1]}"
    plan_cmd = b.build_cmd("plan it", permission_mode="plan",
                           disallowed_tools=["AskUserQuestion", "ExitPlanMode"])
    i = plan_cmd.index("--permission-mode")
    assert plan_cmd[i + 1] == "plan", plan_cmd
    i = plan_cmd.index("--disallowedTools")
    assert plan_cmd[i + 1] == "AskUserQuestion,ExitPlanMode", plan_cmd
    print("ok: claude backend builds the expected headless streaming command (+ plan/disallow overrides)")


def test_event_normalization_and_log():
    import json
    from harness.events import EventLog, context_tokens, normalize_claude_event, read_events

    init = {"type": "system", "subtype": "init", "model": "claude-opus-4-8", "session_id": "s1"}
    assistant = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Working on it."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la /tmp"}},
        {"type": "tool_use", "id": "t2", "name": "mcp__harness__spawn_claude",
         "input": {"task": "survey files", "model": "haiku"}},
    ], "usage": {"input_tokens": 100, "cache_read_input_tokens": 5000,
                 "cache_creation_input_tokens": 200, "output_tokens": 50}}}
    result = {"type": "result", "result": "done", "usage": {"output_tokens": 50}, "num_turns": 3}

    evs = [e for raw in (init, assistant, result) for e in normalize_claude_event(raw)]
    types = [e["type"] for e in evs]
    assert types == ["init", "text", "tool", "tool", "turn_usage", "result"], types
    assert evs[0]["model"] == "claude-opus-4-8"
    assert evs[2]["tool"] == "Bash" and "ls -la" in evs[2]["summary"]
    assert evs[3]["summary"].startswith("survey files")
    assert context_tokens(evs[4]["usage"]) == 5300

    with tempfile.TemporaryDirectory() as d:
        log = EventLog(Path(d) / "events.jsonl")
        log.write("subagent-1", "spawn_start", backend="claude", model="haiku", task="t")
        log.write("subagent-1", "spawn_end", usage={"input_tokens": 9},
                  model="claude-haiku-4-5", duration_s=4.2)
        records = read_events(Path(d) / "events.jsonl")
        assert len(records) == 2
        assert records[1]["model"] == "claude-haiku-4-5" and records[1]["usage"]["input_tokens"] == 9
        assert all("ts" in r and r["agent"] == "subagent-1" for r in records)
    print("ok: stream-json normalization, context occupancy, and event log roundtrip")


def test_rate_limit_and_model_usage():
    from harness.cli import _fmt_reset, _merge_model_usage
    from harness.events import normalize_claude_event

    rl = {"type": "rate_limit_event",
          "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 123}}
    (ev,) = list(normalize_claude_event(rl))
    assert ev["type"] == "rate_limit" and ev["info"]["rateLimitType"] == "five_hour"

    result = {"type": "result", "result": "ok", "usage": {"output_tokens": 1},
              "modelUsage": {"m": {"inputTokens": 10, "costUSD": 0.5, "contextWindow": 200000}}}
    (rev,) = list(normalize_claude_event(result))
    assert rev["type"] == "result" and rev["model_usage"]["m"]["inputTokens"] == 10

    # counters sum across turns; per-model constants (contextWindow) are kept, not summed
    acc: dict = {}
    _merge_model_usage(acc, {"m": {"inputTokens": 10, "costUSD": 0.5, "contextWindow": 200000}})
    _merge_model_usage(acc, {"m": {"inputTokens": 5, "costUSD": 0.25, "contextWindow": 200000}})
    assert acc["m"]["inputTokens"] == 15 and abs(acc["m"]["costUSD"] - 0.75) < 1e-9
    assert acc["m"]["contextWindow"] == 200000

    assert _fmt_reset(None) == "?"
    assert "(in " in _fmt_reset(int(time.time()) + 3600)
    print("ok: rate-limit event + per-model usage rollup")


def test_runner_writes_mcp_config():
    import json
    from harness.runner import RunConfig, Runner

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        cfg = RunConfig(
            workdir=base,
            runs_dir=base / "runs",
            memory_dir=base / "memory",
            extra_mcp_servers={"fs": {"command": "npx", "args": ["x"]}},
        )
        r = Runner(cfg)
        data = json.loads((r.run_dir / "mcp_config.json").read_text())
        servers = data["mcpServers"]
        assert "harness" in servers and "fs" in servers
        env = servers["harness"]["env"]
        assert env["HARNESS_WORKDIR"] == str(base.resolve())
        assert env["HARNESS_RUN_ID"] == r.run_id
        assert env["HARNESS_EVENTS_FILE"] == str(r.events_path)
        assert r.notes_dir.is_dir()
        prompt = r.first_prompt("do the thing")
        assert "do the thing" in prompt and str(r.notes_dir) in prompt
        print("ok: runner writes merged MCP config and builds the first prompt")


def test_context_bar():
    import re

    from harness.ui import _BAR_AMBER, _BAR_GREEN, _BAR_RED, _bar_color, context_bar

    strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
    filled = lambda s: sum(ch in "█#" for ch in strip(s))
    for pct in (0, 50, 100):
        assert len(strip(context_bar(pct))) == 6, pct
    assert filled(context_bar(0)) == 0
    assert filled(context_bar(50)) == 3
    assert filled(context_bar(100)) == 6
    assert _bar_color(0) == _BAR_GREEN and _bar_color(49) == _BAR_GREEN
    assert _bar_color(50) == _BAR_AMBER and _bar_color(79) == _BAR_AMBER
    assert _bar_color(80) == _BAR_RED and _bar_color(100) == _BAR_RED
    print("ok: context bar renders fixed width and picks color tier by fullness")


def test_workspace_sandbox():
    with tempfile.TemporaryDirectory() as d:
        ws = Workspace(Path(d) / "run")
        try:
            ws.resolve("../../etc/passwd")
            raise AssertionError("sandbox escape not caught")
        except ValueError:
            print("ok: workspace path escape rejected")


if __name__ == "__main__":
    test_offload_large_result()
    test_offload_small_passthrough_and_exempt()
    test_memory_write_update_index()
    test_claude_backend_cmd()
    test_event_normalization_and_log()
    test_rate_limit_and_model_usage()
    test_runner_writes_mcp_config()
    test_context_bar()
    test_workspace_sandbox()
    print("\nAll smoke tests passed.")

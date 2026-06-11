"""Deterministic loop driver: iterate the lead agent until a success check passes
or the iteration cap is hit. The controller is plain code — the model never
decides whether to loop again.

Loop state (session id, iteration) persists to the run dir, so a killed loop can
be inspected; backend failures (e.g. usage-limit exhaustion on subscription
plans) trigger a fixed wait-and-retry rather than aborting the run.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass

from .backends import BackendError
from .runner import REFLECTION_PROMPT, Runner

CHECK_OUTPUT_TAIL = 4000


@dataclass
class LoopResult:
    done: bool
    iterations: int
    last_text: str
    last_check_output: str = ""


def run_check(cmd: str, cwd) -> tuple[int, str]:
    proc = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True)
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode, output


async def run_loop(
    runner: Runner,
    task: str,
    until: str | None = None,
    max_iterations: int = 5,
    retry_wait_s: int = 300,
    max_retries: int = 3,
    on_event=None,
    first_prompt: str | None = None,
) -> LoopResult:
    emit = on_event or (lambda e: None)
    state_path = runner.run_dir / "loop_state.json"
    last_text = ""
    check_output = ""

    for iteration in range(1, max_iterations + 1):
        if iteration == 1:
            # first_prompt overrides when the session already holds context —
            # e.g. plan-first ran and the kickoff is "implement the approved plan"
            prompt = first_prompt if first_prompt is not None else runner.first_prompt(task)
            if until:
                prompt += (
                    f"\n\nWhen you believe you are done, the frootloopr will verify by "
                    f"running: `{until}` (success = exit 0). Make that check pass."
                )
        else:
            prompt = (
                f"Iteration {iteration}: the success check `{until}` still fails.\n"
                f"<check-output>\n{check_output[-CHECK_OUTPUT_TAIL:]}\n</check-output>\n"
                f"Diagnose what's still wrong, fix it, and finish."
            )

        emit({"type": "iteration_start", "iteration": iteration})
        for attempt in range(max_retries + 1):
            try:
                result = await runner.send(prompt)
                break
            except BackendError as e:
                if attempt == max_retries:
                    raise
                emit({"type": "backend_retry", "error": str(e)[:300], "wait_s": retry_wait_s})
                await asyncio.sleep(retry_wait_s)
        last_text = result.text

        state_path.write_text(
            json.dumps({"session_id": runner.session_id, "iteration": iteration, "task": task})
        )

        if not until:
            done = True
        else:
            rc, check_output = run_check(until, runner.workdir)
            done = rc == 0
            emit({"type": "check", "iteration": iteration, "exit_code": rc, "passed": done})

        if done:
            if runner.config.reflect:
                emit({"type": "reflection_start"})
                await runner.send(REFLECTION_PROMPT)
            return LoopResult(done=True, iterations=iteration, last_text=last_text,
                              last_check_output=check_output)

    if runner.config.reflect:
        emit({"type": "reflection_start"})
        await runner.send(REFLECTION_PROMPT)
    return LoopResult(done=False, iterations=max_iterations, last_text=last_text,
                      last_check_output=check_output)

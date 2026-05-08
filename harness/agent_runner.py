"""Agent runner — interactive (subscription) and headless (API) entry points.

In interactive mode the runner stages everything in the workdir and prints
the next-step instructions; the operator drives Claude Code in that
directory. Once the session ends, `gjb claim <run_id>` records telemetry
and triggers the validator suite.

In headless mode (TODO) the harness invokes the Anthropic SDK directly with
a tool loop; MCP servers from the system config are spawned per-run.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from .model_adapter import build_driver, AgentSession


def stage_interactive(prompt: str, project_dir: pathlib.Path, run_artifacts: pathlib.Path,
                      system: dict[str, Any]) -> dict[str, str]:
    """Write the prompt + system config + Claude Code launch hint into the run dir."""
    (run_artifacts / "prompt.md").write_text(prompt, encoding="utf-8")
    (run_artifacts / "system.json").write_text(json.dumps(system, indent=2), encoding="utf-8")

    next_steps = (
        "GameJamBench — interactive run staged.\n"
        f"\nProject:  {project_dir}\n"
        f"Prompt:   {run_artifacts / 'prompt.md'}\n"
        f"System:   {run_artifacts / 'system.json'}\n\n"
        "Next steps:\n"
        f"  1. cd \"{project_dir}\"\n"
        "  2. claude       # open Claude Code in the workdir\n"
        f"  3. Paste the prompt from {run_artifacts / 'prompt.md'} and let the agent work.\n"
        f"  4. When done:  gjb claim {run_artifacts.name}\n"
        "     (records msgs/tokens/tool-calls and runs the validator suite)\n"
    )
    (run_artifacts / "NEXT_STEPS.txt").write_text(next_steps, encoding="utf-8")
    return {
        "prompt": str(run_artifacts / "prompt.md"),
        "next_steps": str(run_artifacts / "NEXT_STEPS.txt"),
    }


def claim_interactive(run_artifacts: pathlib.Path, telemetry: dict[str, Any]) -> AgentSession:
    """Back-fill an interactive run with telemetry the operator pasted in.

    Telemetry shape:
        {model, msgs, tokens_in, tokens_out, tool_calls, transcript_path}
    """
    sess = AgentSession(
        model=telemetry.get("model", "claude-opus-4-7"),
        msgs=int(telemetry.get("msgs", 0)),
        tokens_in=int(telemetry.get("tokens_in", 0)),
        tokens_out=int(telemetry.get("tokens_out", 0)),
        tool_calls=int(telemetry.get("tool_calls", 0)),
    )
    out = run_artifacts / "session.json"
    out.write_text(json.dumps({
        "model": sess.model, "msgs": sess.msgs,
        "tokens_in": sess.tokens_in, "tokens_out": sess.tokens_out,
        "tool_calls": sess.tool_calls,
        "transcript": telemetry.get("transcript_path"),
    }, indent=2), encoding="utf-8")
    return sess


def run_headless(prompt: str, project_dir: pathlib.Path, system: dict[str, Any]) -> AgentSession:
    driver = build_driver("headless", system.get("model", "claude-opus-4-7"))
    return driver.run(prompt, str(project_dir), system.get("mcps", []))

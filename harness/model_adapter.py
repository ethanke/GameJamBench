"""Model adapter — abstracts over the Claude family.

We support exactly three models for now (locked decision 2026-05-08):
    - claude-opus-4-7      (Opus 4.7)
    - claude-sonnet-4-6    (Sonnet 4.6)
    - claude-haiku-4-5-20251001 (Haiku 4.5)

Two run modes:
    - interactive: agent is driven by a human Claude Code session in the
      run's workdir. The harness preps the prompt + MCP config and waits
      for `gjb claim <run_id>` to record telemetry.
    - headless: harness invokes the Anthropic SDK directly with prompt-as-
      system + tool-loop. Used for production scale.

This v0 ships the interactive path. Headless is a stub interface so the
runner code already speaks the right shape when we plug the SDK in.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol


SUPPORTED_MODELS = {
    "claude-opus-4-7":            {"family": "opus",   "version": "4.7"},
    "claude-sonnet-4-6":          {"family": "sonnet", "version": "4.6"},
    "claude-haiku-4-5-20251001":  {"family": "haiku",  "version": "4.5"},
}


@dataclasses.dataclass
class AgentSession:
    """Captured telemetry for a single agent run."""
    model: str
    msgs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: int = 0
    notes: list[str] = dataclasses.field(default_factory=list)


class AgentDriver(Protocol):
    def run(self, prompt: str, project_dir: str, mcps: list[str]) -> AgentSession: ...


class InteractiveDriver:
    """Subscription-mode driver.

    Preps the run's workdir with prompt.md and instructs the operator (you)
    to drive Claude Code in that directory. Telemetry is back-filled via
    `gjb claim <run_id>` once the session ends.
    """

    def __init__(self, model: str = "claude-opus-4-7"):
        if model not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported model: {model}")
        self.model = model

    def run(self, prompt: str, project_dir: str, mcps: list[str]) -> AgentSession:
        # The runner has already written prompt.txt to the artifacts dir.
        # In interactive mode we return an empty session and let `claim`
        # populate it.
        return AgentSession(
            model=self.model,
            notes=["interactive_pending: run `gjb claim <run_id>` after Claude Code session"],
        )


class HeadlessDriver:
    """API-mode driver — invokes the Anthropic SDK with tool-loop.

    Stub for v0. Will use the `anthropic` Python SDK plus an MCP server
    spawned per run when this is implemented.
    """

    def __init__(self, model: str = "claude-opus-4-7"):
        if model not in SUPPORTED_MODELS:
            raise ValueError(f"unsupported model: {model}")
        self.model = model

    def run(self, prompt: str, project_dir: str, mcps: list[str]) -> AgentSession:
        raise NotImplementedError("headless mode lands when we wire the Anthropic SDK")


def build_driver(mode: str, model: str) -> AgentDriver:
    if mode == "interactive":
        return InteractiveDriver(model)
    if mode == "headless":
        return HeadlessDriver(model)
    raise ValueError(f"unknown driver mode: {mode}")

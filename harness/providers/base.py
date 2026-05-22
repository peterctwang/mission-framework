from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Usage:
    """Token accounting for one call. Fields that the provider doesn't
    report (most CLI subprocesses) stay at 0 — count those calls as
    invocations, not tokens, in budget tracking."""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    invocations: int = 1  # always 1 per call; lets you budget by call-count
    turns: int = 0  # agent-loop turn count (for trajectory cap enforcement)


@dataclass
class CompletionResult:
    text: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    raw: dict | None = None


class QuotaExhausted(RuntimeError):
    """Provider has hit a hard quota cap and won't recover this run.
    Runner catches this and fails over to the next provider in the chain."""

    def __init__(self, provider: str, model: str, reason: str):
        super().__init__(f"{provider}/{model} quota exhausted: {reason}")
        self.provider = provider
        self.model = model
        self.reason = reason


class TransientProviderError(RuntimeError):
    """Network / 5xx / unknown failure. Runner retries, doesn't fail over."""


class Provider(ABC):
    name: str = "base"
    model: str = "unknown"

    @abstractmethod
    def generate(self, system: str, user: str, *, max_tokens: int = 8192,
                 cwd: str | None = None) -> CompletionResult:
        """Run a single completion.

        cwd: if provided, CLI-backed providers run the underlying subprocess
        in that directory, so when the LLM uses tools like Write / Edit /
        Bash the files land in the project dir, not the harness install dir.
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} model={self.model}>"

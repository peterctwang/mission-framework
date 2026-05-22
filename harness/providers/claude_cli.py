from __future__ import annotations

import json
import shutil
import subprocess

from .base import CompletionResult, Provider, QuotaExhausted, TransientProviderError, Usage


def _resolve(binary: str) -> str:
    """Resolve a CLI name to its absolute path (handles Windows .cmd shims)."""
    path = shutil.which(binary)
    if not path:
        raise RuntimeError(f"`{binary}` not found on PATH — run `npm i -g` for the relevant CLI")
    return path


class ClaudeCLI(Provider):
    name = "claude-cli"

    def __init__(self, model: str = "claude-opus-4-7", binary: str = "claude"):
        self.model = model
        self.binary = _resolve(binary)

    def generate(self, system: str, user: str, *, max_tokens: int = 8192,
                 cwd: str | None = None) -> CompletionResult:
        # Documented headless invocation per code.claude.com/docs/en/headless
        # and /cli-reference. Two earlier mistakes are now corrected:
        #
        # 1. --system-prompt REPLACES Claude Code's default agent-loop
        #    instructions. Removing those is what caused "I'll wait for
        #    your request" — the model lost the directive to ACT.
        #    Correct flag: --append-system-prompt (preserves agent loop).
        #
        # 2. Long structured prompts in argv -p can exceed Windows' 8191
        #    char limit (silently truncated). Pipe via stdin instead by
        #    using bare `-p` (no arg) and subprocess input=user.
        cmd = [
            self.binary,
            "-p",                       # headless / print mode, prompt via stdin
            "--model", self.model,
            "--output-format", "json",
        ]
        if cwd is not None:
            # Worker mode — agentic, tool-use enabled.
            cmd.extend(["--permission-mode", "acceptEdits"])
            cmd.extend(["--allowedTools", "Bash,Read,Edit,Write,Glob,Grep,LS"])
            if system:
                cmd.extend(["--append-system-prompt", system])
        else:
            # Validator mode — chat only, no tools.
            if system:
                cmd.extend(["--append-system-prompt", system])

        proc = subprocess.run(
            cmd,
            input=user,                  # user prompt via stdin (no argv truncation)
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=1800,
            cwd=cwd,
        )
        if proc.returncode != 0:
            stderr_lower = (proc.stderr or "").lower()
            if any(s in stderr_lower for s in ("rate limit", "usage limit", "quota", "exceeded your")):
                raise QuotaExhausted(self.name, self.model, proc.stderr[:300])
            raise TransientProviderError(f"claude CLI exit {proc.returncode}: {proc.stderr[:300]}")

        try:
            payload = json.loads(proc.stdout)
            text = payload.get("result") or payload.get("text") or proc.stdout
        except json.JSONDecodeError:
            payload = None
            text = proc.stdout

        usage = self._extract_usage(payload)
        return CompletionResult(
            text=text.strip(),
            provider=self.name,
            model=self.model,
            usage=usage,
            raw=payload,
        )

    @staticmethod
    def _extract_usage(payload: dict | None) -> Usage:
        if not payload:
            return Usage()
        u = payload.get("usage") or {}
        return Usage(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cached_input_tokens=u.get("cache_read_input_tokens", 0),
        )

"""Gemini via the official `gemini` CLI (Google OAuth login)."""
from __future__ import annotations

import json
import subprocess

from .base import CompletionResult, Provider, QuotaExhausted, TransientProviderError, Usage
from .claude_cli import _resolve


class GeminiOAuth(Provider):
    name = "gemini-cli"

    def __init__(self, model: str = "gemini-2.5-pro", binary: str = "gemini"):
        self.model = model
        self.binary = _resolve(binary)

    def generate(self, system: str, user: str, *, max_tokens: int = 8192,
                 cwd: str | None = None) -> CompletionResult:
        # Gemini has no --system-prompt flag and reads [SYSTEM]/[USER] tags
        # as plain text, getting confused. Smoke test showed it follows
        # directive prompts only when they're framed as a single user
        # message with a leading "do this only" preamble. We collapse the
        # system+user into one continuous prompt.
        directive = (
            "INSTRUCTIONS — read carefully and follow EXACTLY:\n"
            "1. This is a single-shot request, not a conversation.\n"
            "2. Do NOT explore the filesystem unless an explicit tool call "
            "is requested in the role definition below.\n"
            "3. Do NOT write a project summary, research overview, or "
            "self-introduction.\n"
            "4. Do NOT ask clarifying questions; respond with what's asked.\n"
            "5. For Validator role, END your output with exactly: '判決:通過' "
            "or '判決:打回' on its own line.\n\n"
            "=== ROLE & TASK ===\n"
        )
        prompt = f"{directive}{system}\n\n=== REQUEST ===\n{user}" if system else f"{directive}{user}"
        # Gemini needs -p to enter non-interactive (headless) mode.
        # Without -p it sits waiting for interactive input even if stdin is
        # piped. Pass the full prompt as the -p value.
        cmd = [
            self.binary,
            "-p", prompt,
            "-m", self.model,
            "-o", "json",
            "--skip-trust",
        ]
        # cwd present = Worker (needs to write files); else Validator (read-only).
        if cwd is not None:
            cmd.append("--yolo")
        else:
            cmd.extend(["--approval-mode", "plan"])
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=1800,
            cwd=cwd,
        )
        if proc.returncode != 0:
            blob = (proc.stderr or "").lower()
            for marker in ("quota exceeded", "resource_exhausted", "rate limit",
                           "usage limit", "daily limit"):
                if marker in blob:
                    raise QuotaExhausted(self.name, self.model, marker)
            raise TransientProviderError(f"gemini CLI exit {proc.returncode}: {proc.stderr[:300]}")

        text, usage = self._parse(proc.stdout)
        if not text:
            text = proc.stdout.strip()
        return CompletionResult(text=text, provider=self.name, model=self.model, usage=usage, raw=None)

    @staticmethod
    def _parse(stdout: str) -> tuple[str, Usage]:
        stripped = stdout.strip()
        if not stripped:
            return "", Usage()
        try:
            obj = json.loads(stripped)
            text = obj.get("response") or obj.get("text") or obj.get("output") or ""
            u = obj.get("usage") or obj.get("usageMetadata") or {}
            usage = Usage(
                input_tokens=u.get("promptTokenCount", u.get("input_tokens", 0)),
                output_tokens=u.get("candidatesTokenCount", u.get("output_tokens", 0)),
                cached_input_tokens=u.get("cachedContentTokenCount", 0),
            )
            return text, usage
        except json.JSONDecodeError:
            pass
        last_text = ""
        for line in stripped.splitlines():
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            candidate = evt.get("response") or evt.get("text") or evt.get("output")
            if isinstance(candidate, str) and candidate:
                last_text = candidate
        return last_text, Usage()

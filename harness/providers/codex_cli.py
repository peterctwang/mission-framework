from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from .base import CompletionResult, Provider, QuotaExhausted, TransientProviderError, Usage
from .claude_cli import _resolve


class CodexCLI(Provider):
    name = "codex-cli"

    def __init__(self, model: str | None = None, binary: str = "codex"):
        # ChatGPT-subscription accounts reject explicit -m; leave None for default.
        self.model = model
        self.binary = _resolve(binary)

    def generate(self, system: str, user: str, *, max_tokens: int = 8192,
                 cwd: str | None = None) -> CompletionResult:
        prompt = f"[SYSTEM]\n{system}\n\n[USER]\n{user}" if system else user
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(prompt)
            prompt_path = f.name
        out_path = prompt_path + ".out"
        json_log_path = prompt_path + ".jsonl"

        try:
            model_arg = f"-m {self.model} " if self.model else ""
            cd_arg = f'-C "{cwd}" ' if cwd else ""
            # workspace-write lets Worker create/edit files in cwd
            sandbox_arg = "-s workspace-write " if cwd else ""
            cmd = (
                f'"{self.binary}" exec --json --skip-git-repo-check --ignore-user-config '
                f'--dangerously-bypass-approvals-and-sandbox '
                f'{model_arg}{cd_arg}{sandbox_arg}-o "{out_path}" - < "{prompt_path}" > "{json_log_path}"'
            )
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=1800,
            )
            text = ""
            if os.path.exists(out_path):
                text = open(out_path, encoding="utf-8").read().strip()

            jsonl_content = ""
            if os.path.exists(json_log_path):
                jsonl_content = open(json_log_path, encoding="utf-8").read()

            self._check_for_quota_signal(jsonl_content + (proc.stderr or ""))

            if not text and proc.returncode != 0:
                raise TransientProviderError(
                    f"codex CLI exit {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
                )
            if not text:
                text = (proc.stdout or "").strip()

            usage = self._extract_usage(jsonl_content)
            model_used = self.model or "default"
            return CompletionResult(text=text, provider=self.name, model=model_used, usage=usage, raw=None)
        finally:
            for p in (prompt_path, out_path, json_log_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _check_for_quota_signal(self, blob: str) -> None:
        lo = blob.lower()
        # ChatGPT subscription limit signals seen in the wild:
        # "you've reached your usage limit", "rate_limit_exceeded", "weekly limit"
        for marker in ("usage limit", "rate limit exceeded", "weekly limit",
                       "exceeded your", "quota exceeded"):
            if marker in lo:
                raise QuotaExhausted(self.name, self.model or "default", marker)

    @staticmethod
    def _extract_usage(jsonl: str) -> Usage:
        """codex --json emits a `turn.completed` event with usage block."""
        total = Usage(input_tokens=0, output_tokens=0, cached_input_tokens=0)
        for line in jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "turn.completed":
                u = evt.get("usage") or {}
                total.input_tokens += u.get("input_tokens", 0)
                total.output_tokens += u.get("output_tokens", 0)
                total.cached_input_tokens += u.get("cached_input_tokens", 0)
        return total

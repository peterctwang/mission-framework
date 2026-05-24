"""Gemini via the official `gemini` CLI (Google OAuth login).

May 2026 invocation pattern, verified against
https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md.

Key constraints — learned from broken older invocations:
  * `--yolo` is deprecated; use `--approval-mode yolo`.
  * `--approval-mode plan` is broken in headless mode (issue #24814) AND
    extra-broken on Windows (Shift+Tab toggle non-functional, #25584). Never
    use it from this wrapper — readonly validator runs use `default` which
    is safe because they have no tools to approve.
  * There is no `--system-prompt` / `--append-system-prompt` flag. The
    system role must be embedded in the user prompt text.
  * Prompt over stdin works (and is preferred on Windows to dodge the
    8191-char argv limit). `-p ""` is required to force headless even if
    stdin is piped (else the CLI may try to attach to a TTY).
  * Several env vars defend against first-run wizards and auto-updates
    that otherwise cause the CLI to hang silently.
"""
from __future__ import annotations

import json
import os
import subprocess

from .base import CompletionResult, Provider, QuotaExhausted, TransientProviderError, Usage
from .claude_cli import _resolve


_DIRECTIVE = (
    "INSTRUCTIONS — read carefully and follow EXACTLY:\n"
    "1. This is a single-shot request, not a conversation.\n"
    "2. Do NOT explore the filesystem unless an explicit tool call is "
    "requested in the role definition below.\n"
    "3. Do NOT write a project summary, research overview, or "
    "self-introduction.\n"
    "4. Do NOT ask clarifying questions; respond with what's asked.\n"
    "5. For Validator role, END your output with exactly: '判決:通過' "
    "or '判決:打回' on its own line.\n"
    "6. FILE EDITING DISCIPLINE — CRITICAL:\n"
    "   • When MODIFYING an EXISTING file, NEVER overwrite the whole file.\n"
    "   • ALWAYS read the file first (use read_file or shell `cat`).\n"
    "   • Then use a SURGICAL edit: the Edit tool, `sed -i 's/find/replace/'`, "
    "or apply a unified diff via shell `patch`. NEVER call write_file with "
    "<full file content> on an existing file just to add one key.\n"
    "   • Forbidden placeholders: '...existing config...', '...existing code...', "
    "'// rest unchanged', '<!-- previous content -->'. These wipe code the next "
    "subtask depends on — the runner's disk-diff guard will reject your output.\n"
    "   • For ADD operations (e.g. append a key to LAYOUT object), find the EXACT "
    "closing brace + surrounding context (3-4 lines), and replace it with the "
    "same context + your new content. Preserve every other line byte-for-byte.\n\n"
    "=== ROLE & TASK ===\n"
)


def _build_env() -> dict[str, str]:
    """Defensive env that prevents the CLI from hanging on TUIs/updates."""
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    env.setdefault("GEMINI_CLI_DISABLE_TELEMETRY", "1")
    env.setdefault("GEMINI_CLI_DISABLE_AUTO_UPDATE", "1")
    return env


class GeminiOAuth(Provider):
    name = "gemini-cli"

    def __init__(self, model: str = "gemini-2.5-pro", binary: str = "gemini"):
        self.model = model
        self.binary = _resolve(binary)

    def generate(self, system: str, user: str, *, max_tokens: int = 8192,
                 cwd: str | None = None) -> CompletionResult:
        # Collapse system+user — Gemini has no system-prompt flag.
        if system:
            prompt = f"{_DIRECTIVE}{system}\n\n=== REQUEST ===\n{user}"
        else:
            prompt = f"{_DIRECTIVE}{user}"

        # cwd present = Worker (needs to write files) → approve every tool.
        # cwd None    = Validator (read-only)         → default mode is fine.
        approval = "yolo" if cwd is not None else "default"

        cmd = [
            self.binary,
            "-m", self.model,
            "-o", "json",
            "--approval-mode", approval,
            "--skip-trust",
            "-p", "",  # force headless even if stdin is piped
        ]

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=1800,
                cwd=cwd,
                env=_build_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise TransientProviderError(f"gemini CLI timeout after {e.timeout}s")

        if proc.returncode != 0:
            blob = (proc.stderr or "").lower()
            # exit code 53 == turn limit reached (treat as transient retry candidate)
            for marker in ("quota exceeded", "resource_exhausted", "rate limit",
                           "usage limit", "daily limit"):
                if marker in blob:
                    raise QuotaExhausted(self.name, self.model, marker)
            tail = (proc.stderr or proc.stdout or "")[-400:]
            raise TransientProviderError(f"gemini CLI exit {proc.returncode}: {tail}")

        text, usage = self._parse(proc.stdout, model=self.model)
        if not text:
            text = proc.stdout.strip()
        return CompletionResult(text=text, provider=self.name, model=self.model, usage=usage, raw=None)

    @staticmethod
    def _parse(stdout: str, model: str = "") -> tuple[str, Usage]:
        """Parse gemini -o json envelope.

        Schema (per docs/cli/headless.md):
          {
            "response": "<text>",
            "stats": {
              "models": { "<model>": { "tokens": { "prompt": N, "candidates": N,
                                                   "cached": N, "total": N } } },
              "tools": {...}, "files": {...}
            },
            "error": { "type":..., "message":..., "code":... }?
          }
        """
        stripped = stdout.strip()
        if not stripped:
            return "", Usage()
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            # Fallback — older NDJSON / stream-json envelopes.
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

        text = obj.get("response") or obj.get("text") or obj.get("output") or ""
        usage = Usage()
        stats = obj.get("stats") or {}
        models = stats.get("models") or {}
        # Prefer exact model match; fall back to first entry.
        m = models.get(model) or (next(iter(models.values()), {}) if models else {})
        tokens = (m.get("tokens") or {}) if isinstance(m, dict) else {}
        if tokens:
            usage = Usage(
                input_tokens=tokens.get("prompt", 0),
                output_tokens=tokens.get("candidates", tokens.get("output", 0)),
                cached_input_tokens=tokens.get("cached", 0),
            )
        else:
            # Legacy envelope without `stats` block.
            u = obj.get("usage") or obj.get("usageMetadata") or {}
            if u:
                usage = Usage(
                    input_tokens=u.get("promptTokenCount", u.get("input_tokens", 0)),
                    output_tokens=u.get("candidatesTokenCount", u.get("output_tokens", 0)),
                    cached_input_tokens=u.get("cachedContentTokenCount", 0),
                )
        return text, usage

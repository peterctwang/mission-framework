"""Minimax Coding/Token Plan client.

Adapted from patent-ai's MiniMax client (crates/patent-query/src/minimax.rs).

Auth resolution order:
    1. env MINIMAX_API_KEY      (primary — sk-cp-... for Coding Plan)
    2. env MINIMAX_OAUTH_TOKEN
    3. env MINIMAX_CODE_PLAN_KEY
    4. env MINIMAX_CODING_API_KEY
    5. ~/.mmx/config.json       (written by `mmx auth login`)

Quota-exhaustion signals:
- HTTP 429 + body contains "(2056)" or "usage limit exceeded"  → QuotaExhausted
- HTTP 429 + other body → retryable (rate limit)

Other resilience:
- Strip <think>...</think> blocks (M2.5 reasoning model emits these).
- Retry 429/5xx with exponential backoff + jitter.
- On 400 "context window exceeds limit (N)", shrink user content and retry.
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any  # noqa

from .base import CompletionResult, Provider, QuotaExhausted, TransientProviderError, Usage

DEFAULT_URL = "https://api.minimax.io/v1/chat/completions"
DEFAULT_MODEL = "MiniMax-M2.5"
RETRY_DELAYS_MS = (0, 2000, 6000, 15000, 30000)
MAX_SHRINK_ROUNDS = 3

_CTX_LIMIT_RE = re.compile(r"context window exceeds limit\s*\((\d+)\)")


def _strip_think(s: str) -> str:
    out_parts: list[str] = []
    rest = s
    while True:
        i = rest.find("<think>")
        if i < 0:
            out_parts.append(rest)
            break
        out_parts.append(rest[:i])
        j = rest.find("</think>", i)
        if j < 0:
            break
        rest = rest[j + len("</think>"):]
    return "".join(out_parts).strip()


class MinimaxToken(Provider):
    name = "minimax-token"

    ENV_VARS = (
        "MINIMAX_API_KEY",
        "MINIMAX_OAUTH_TOKEN",
        "MINIMAX_CODE_PLAN_KEY",
        "MINIMAX_CODING_API_KEY",
    )

    def __init__(self, model: str | None = None, url: str | None = None):
        self.model = model or os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
        self.url = url or os.environ.get("MINIMAX_API_URL", DEFAULT_URL)
        self._token = self._resolve_token()

    @classmethod
    def _resolve_token(cls) -> str:
        for var in cls.ENV_VARS:
            if val := os.environ.get(var):
                return val
        config_path = Path.home() / ".mmx" / "config.json"
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise RuntimeError(f"corrupt ~/.mmx/config.json: {e}") from e
            for key in ("oauth_token", "code_plan_key", "api_key", "token"):
                if val := cfg.get(key):
                    return val
        raise RuntimeError(
            "No Minimax credential found. Run `mmx auth login` or set "
            "MINIMAX_API_KEY=sk-cp-..."
        )

    # OpenAI-compatible function-calling tool schemas. Per Minimax M2 docs
    # (huggingface.co/MiniMaxAI/MiniMax-M2/blob/main/docs/tool_calling_guide.md),
    # the hosted endpoint api.minimax.io/v1/chat/completions parses the
    # model's raw <minimax:tool_call> XML and returns standard OpenAI
    # message.tool_calls JSON when `tools` is declared in the request.
    TOOLS_SCHEMA = [
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Create or overwrite a file under the working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path relative to cwd"},
                        "content": {"type": "string", "description": "Full file content"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file under the working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path relative to cwd"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List entries in a directory under the working directory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path relative to cwd; '.' for root"},
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": "Run a safe shell command (cp/mv/mkdir/ls/find) under cwd. NOT for arbitrary code execution.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "Shell command. Allowed verbs: cp, mv, mkdir, ls, find, rm. Refused otherwise.",
                        },
                    },
                    "required": ["command"],
                },
            },
        },
    ]
    _ALLOWED_SHELL_VERBS = {"cp", "mv", "mkdir", "ls", "find", "rm", "echo", "cat"}
    MAX_TOOL_ROUNDS = 12

    def generate(self, system: str, user: str, *, max_tokens: int = 1024,
                 cwd: str | None = None) -> CompletionResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        # If cwd is set (Worker mode), enable tool calling so the model can
        # write/read files. Without cwd (Validator/Orchestrator mode), skip
        # tools — those roles produce text only.
        tools = self.TOOLS_SCHEMA if cwd else None
        cwd_path = Path(cwd).resolve() if cwd else None

        last_payload: dict = {}
        total_in = total_out = 0
        turns = 0
        for _round in range(self.MAX_TOOL_ROUNDS):
            turns += 1
            payload = self._post_chat(messages, max_tokens=max_tokens, tools=tools)
            last_payload = payload
            usage_data = payload.get("usage", {}) or {}
            total_in += usage_data.get("prompt_tokens", 0)
            total_out += usage_data.get("completion_tokens", 0)

            choice = payload["choices"][0]
            msg = choice.get("message", {}) or {}
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                # Final answer.
                content = msg.get("content", "") or ""
                return CompletionResult(
                    text=_strip_think(content),
                    provider=self.name, model=self.model,
                    usage=Usage(input_tokens=total_in, output_tokens=total_out, turns=turns),
                    raw=payload,
                )

            # Execute tools and feed results back. Standard OpenAI loop:
            # 1) append the assistant message verbatim (with tool_calls)
            # 2) for each tool_call, append a {role:tool} message with the result
            messages.append({
                "role": "assistant",
                "content": msg.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tc in tool_calls:
                fn = (tc.get("function") or {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                result = self._dispatch_tool(name, args, cwd_path)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": result,
                })

        # Hit the round cap without convergence — return whatever the last
        # assistant message had (better than failing the subtask).
        msg = (last_payload.get("choices", [{}])[0] or {}).get("message", {}) or {}
        return CompletionResult(
            text=_strip_think(msg.get("content", "") or "(tool loop exceeded MAX_TOOL_ROUNDS)"),
            provider=self.name, model=self.model,
            usage=Usage(input_tokens=total_in, output_tokens=total_out),
            raw=last_payload,
        )

    @staticmethod
    def _dispatch_tool(name: str, args: dict, cwd: Path | None) -> str:
        """Execute a tool call against the local filesystem. Returns the
        result as a string (always — the API expects string tool results)."""
        if cwd is None:
            return "ERROR: no cwd set, cannot execute filesystem tool"
        try:
            if name == "write_file":
                rel = args.get("path", "").strip()
                content = args.get("content", "")
                if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
                    return f"ERROR: unsafe path {rel!r}"
                target = cwd / rel
                target.resolve().relative_to(cwd)  # traversal guard
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return f"OK: wrote {len(content)} chars to {rel}"
            elif name == "read_file":
                rel = args.get("path", "").strip()
                if not rel or ".." in Path(rel).parts:
                    return f"ERROR: unsafe path {rel!r}"
                target = cwd / rel
                if not target.exists():
                    return f"ERROR: not found {rel}"
                return target.read_text(encoding="utf-8")[:8000]  # cap
            elif name == "list_dir":
                rel = args.get("path", ".").strip() or "."
                if ".." in Path(rel).parts:
                    return f"ERROR: unsafe path {rel!r}"
                target = cwd / rel if rel != "." else cwd
                if not target.exists():
                    return f"ERROR: not found {rel}"
                entries = sorted(
                    f"{p.name}{'/' if p.is_dir() else ''}"
                    for p in target.iterdir()
                )
                return "\n".join(entries) if entries else "(empty)"
            elif name == "run_shell":
                import shlex, subprocess as _sp
                cmd_str = args.get("command", "").strip()
                if not cmd_str:
                    return "ERROR: empty command"
                try:
                    tokens = shlex.split(cmd_str, posix=False)
                except ValueError as e:
                    return f"ERROR: shell parse: {e}"
                verb = tokens[0].lower().lstrip("./\\")
                if verb not in MinimaxToken._ALLOWED_SHELL_VERBS:
                    return f"ERROR: shell verb {verb!r} not allowed (allowed: {sorted(MinimaxToken._ALLOWED_SHELL_VERBS)})"
                # Reject absolute paths or .. in any arg (best-effort safety)
                for t in tokens[1:]:
                    if t.startswith(("/", "\\")) or t.startswith(("C:", "D:", "c:", "d:")) or ".." in t.split("/"):
                        # exception: -name pattern for find, or git-bash-style /c/...
                        if not (t.startswith("-") or "*" in t or "?" in t):
                            return f"ERROR: unsafe arg {t!r}"
                proc = _sp.run(cmd_str, shell=True, cwd=str(cwd),
                               capture_output=True, text=True, timeout=60)
                out = (proc.stdout or "") + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
                return f"exit={proc.returncode}\n{out[:4000]}"
            else:
                return f"ERROR: unknown tool {name!r}"
        except Exception as e:
            return f"ERROR: {type(e).__name__}: {e}"

    def _post_chat(self, messages: list[dict], *, max_tokens: int,
                   temperature: float = 0.3, tools: list[dict] | None = None) -> dict:
        shrink_rounds = 0
        while True:
            body_dict: dict = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
            if tools:
                body_dict["tools"] = tools
                body_dict["tool_choice"] = "auto"
            body = json.dumps(body_dict).encode("utf-8")

            last_status = 0
            last_body = ""
            for attempt, base_delay in enumerate(RETRY_DELAYS_MS):
                if base_delay > 0:
                    jitter = base_delay * 0.25 * (random.random() * 2 - 1)
                    time.sleep(max(0.0, (base_delay + jitter) / 1000))

                req = urllib.request.Request(
                    self.url,
                    data=body,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=600) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as e:
                    last_status = e.code
                    last_body = e.read().decode("utf-8", errors="replace")
                except urllib.error.URLError as e:
                    last_status = 0
                    last_body = str(e)

                if last_status == 429 and ("usage limit exceeded" in last_body or "(2056)" in last_body):
                    raise QuotaExhausted(self.name, self.model, f"Minimax 2056: {last_body[:200]}")
                if last_status == 400:
                    m = _CTX_LIMIT_RE.search(last_body)
                    if m and shrink_rounds < MAX_SHRINK_ROUNDS:
                        n = int(m.group(1))
                        budget = max(n * 3, 2000)
                        messages[-1]["content"] = messages[-1]["content"][:budget]
                        shrink_rounds += 1
                        break
                    raise TransientProviderError(f"Minimax HTTP 400: {last_body[:300]}")
                if last_status not in (429, 502, 503, 504, 0):
                    raise TransientProviderError(f"Minimax HTTP {last_status}: {last_body[:300]}")
            else:
                raise TransientProviderError(
                    f"Minimax HTTP {last_status} after {len(RETRY_DELAYS_MS)} attempts: {last_body[:300]}"
                )

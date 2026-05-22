"""Probe which models the current ChatGPT subscription allows via codex CLI."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.providers import CodexCLI

CANDIDATES = [
    "gpt-5",
    "gpt-5-codex",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-pro",
    "o3",
    "o3-mini",
    "o3-pro",
    "o4-mini",
    "o1",
    "gpt-4.1",
    "gpt-4o",
    "codex-mini-latest",
]


def probe(model: str | None) -> str:
    try:
        p = CodexCLI(model=model)
        r = p.generate("Reply with exactly: OK", "ping")
        return f"OK :: {r.text!r}"
    except Exception as e:
        msg = str(e)
        if "not supported" in msg:
            return "REJECTED (not supported on this account)"
        if "invalid_request_error" in msg:
            return f"REJECTED ({msg[:200]})"
        return f"ERROR ({msg[:200]})"


print(f"{'(default / None)':<20} -> {probe(None)}")
for m in CANDIDATES:
    print(f"{m:<20} -> {probe(m)}", flush=True)

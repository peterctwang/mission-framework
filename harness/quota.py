"""Quota tracking and failover state.

Per-provider state lives in `<project>/.harness-state.json`. It survives
across runs so a provider exhausted yesterday stays exhausted today until
you reset it (or the cooldown window passes).

Three signals can mark a provider exhausted:
1. Hard signal — provider raised `QuotaExhausted` (e.g. Minimax 2056,
   Claude/Codex "usage limit" in stderr).
2. Soft signal — usage exceeded the configured budget for this provider.
3. Manual — `harness.quota.mark_exhausted("claude-cli/...")`.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ProviderState:
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    invocations: int = 0
    status: str = "ok"             # ok | exhausted | unavailable
    exhausted_at: float | None = None
    last_reason: str = ""

    def add_usage(self, tokens_in: int, tokens_out: int, cached: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.tokens_cached += cached
        self.invocations += 1

    def exceeds(self, budget: dict | None) -> bool:
        """Return True if accumulated usage exceeds a configured soft budget."""
        if not budget:
            return False
        if (cap := budget.get("max_tokens_in")) and self.tokens_in >= cap:
            return True
        if (cap := budget.get("max_tokens_out")) and self.tokens_out >= cap:
            return True
        if (cap := budget.get("max_invocations")) and self.invocations >= cap:
            return True
        return False


@dataclass
class QuotaTracker:
    """In-memory state, persisted to JSON on every mutation.

    Cooldown semantics: once exhausted, a provider becomes available again
    after `cooldown_seconds` (default 6h). Set to 0 to disable auto-recovery
    and require manual reset.
    """
    state_path: Path
    cooldown_seconds: int = 6 * 3600
    providers: dict[str, ProviderState] = field(default_factory=dict)
    budgets: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def load(cls, state_path: Path, budgets: dict[str, dict] | None = None,
             cooldown_seconds: int = 6 * 3600) -> "QuotaTracker":
        tracker = cls(state_path=state_path, cooldown_seconds=cooldown_seconds,
                      budgets=budgets or {})
        if state_path.exists():
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
            for key, raw in (data.get("providers") or {}).items():
                tracker.providers[key] = ProviderState(**raw)
        return tracker

    def _key(self, provider_name: str, model: str | None) -> str:
        return f"{provider_name}/{model or 'default'}"

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "providers": {k: asdict(v) for k, v in self.providers.items()},
            "updated_at": time.time(),
        }
        self.state_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def get(self, provider_name: str, model: str | None) -> ProviderState:
        key = self._key(provider_name, model)
        if key not in self.providers:
            self.providers[key] = ProviderState()
        return self.providers[key]

    def is_available(self, provider_name: str, model: str | None) -> bool:
        st = self.get(provider_name, model)
        if st.status == "ok":
            return not st.exceeds(self.budgets.get(self._key(provider_name, model)))
        if st.status == "exhausted" and st.exhausted_at and self.cooldown_seconds > 0:
            if (time.time() - st.exhausted_at) > self.cooldown_seconds:
                st.status = "ok"
                st.exhausted_at = None
                self._save()
                return True
        return False

    def record_usage(self, provider_name: str, model: str | None,
                     tokens_in: int, tokens_out: int, cached: int) -> None:
        st = self.get(provider_name, model)
        st.add_usage(tokens_in, tokens_out, cached)
        if st.exceeds(self.budgets.get(self._key(provider_name, model))):
            st.status = "exhausted"
            st.exhausted_at = time.time()
            st.last_reason = "soft budget hit"
        self._save()

    def mark_exhausted(self, provider_name: str, model: str | None, reason: str) -> None:
        st = self.get(provider_name, model)
        st.status = "exhausted"
        st.exhausted_at = time.time()
        st.last_reason = reason
        self._save()

    def reset(self, provider_name: str | None = None, model: str | None = None) -> None:
        if provider_name is None:
            for st in self.providers.values():
                st.status = "ok"
                st.exhausted_at = None
            self._save()
            return
        st = self.get(provider_name, model)
        st.status = "ok"
        st.exhausted_at = None
        self._save()

    def summary(self) -> str:
        if not self.providers:
            return "(no usage recorded yet)"
        lines = []
        for key, st in self.providers.items():
            lines.append(
                f"  {key:<40} in={st.tokens_in:>8}  out={st.tokens_out:>6}  "
                f"calls={st.invocations:>3}  status={st.status}"
            )
        return "\n".join(lines)

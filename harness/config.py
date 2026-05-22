"""Capability-profile → provider chain mapping.

A chain is an ordered list of factories: try the first, fall through to the
next when the previous is exhausted (sticky, per quota.json) or fails.

Edit CHAINS to match your subscriptions and preferences.

Cross-model rule (enforced by runner): the validator's resolved provider
must NOT equal the worker's resolved provider for the same subtask.
"""
from __future__ import annotations

from typing import Callable

from .providers import ClaudeCLI, CodexCLI, GeminiOAuth, MinimaxToken, Provider

Factory = Callable[[], Provider]


def _make_claude() -> Provider:
    return ClaudeCLI(model="claude-opus-4-7")


def _make_claude_sonnet() -> Provider:
    return ClaudeCLI(model="claude-sonnet-4-6")


def _make_codex() -> Provider:
    # ChatGPT subscription forces account-default model (probe earlier
    # showed all explicit -m values are rejected).
    return CodexCLI(model=None)


def _make_gemini() -> Provider:
    return GeminiOAuth(model="gemini-2.5-pro")


def _make_minimax() -> Provider:
    return MinimaxToken(model="MiniMax-M2.5")


# =============================================================================
# Routing model — three orthogonal chains, one per role.
#
# - Orchestrator: the "master planner" — reads the mission, decides task
#   difficulty, writes the manifest. We give this to Claude Opus because the
#   quality of decomposition compounds across the whole mission.
#
# - Worker: dispatched by DIFFICULTY (T1/T2/T3), not by capability profile.
#   The Orchestrator's difficulty rating directly drives compute allocation:
#     T1 (routine / boilerplate)  → Minimax  (fast, cheap, no Claude quota burn)
#     T2 (standard)               → Claude Sonnet  (good balance)
#     T3 (hard / architectural)   → Claude Opus  (best reasoning)
#
# - Validator: Codex, always. It treats short directive prompts as directives
#   (no agentic exploration), runs in any cwd, and never collides with Claude/
#   Minimax Workers (cross-model diversity guaranteed).
#
# Each chain still has fallbacks for when the primary is rate-limited or
# the QuotaTracker has marked it exhausted.
# =============================================================================

# Master planner — Opus first, no compromise on quality of decomposition.
ORCHESTRATOR_CHAIN: list[Factory] = [
    _make_claude,         # Opus 4.7 — best decomposition
    _make_codex,          # fallback if Anthropic quota hits
    _make_claude_sonnet,
    _make_minimax,
]

# Difficulty → Worker chain. First entry is the primary; rest are failover.
WORKER_TIERS: dict[str, list[Factory]] = {
    "T1": [_make_minimax,         # easy → cheap & fast
           _make_claude_sonnet,   # if Minimax exhausted
           _make_codex,
           _make_claude],
    "T2": [_make_claude_sonnet,   # standard → Sonnet sweet spot
           _make_claude,          # bump up to Opus if Sonnet exhausted
           _make_codex,
           _make_minimax],
    "T3": [_make_claude,          # hard → Opus
           _make_claude_sonnet,   # downgrade if Opus exhausted
           _make_codex,
           _make_minimax],
}

# Validator — Codex always (treats directives as directives, no agentic drift).
VALIDATOR_CHAIN: list[Factory] = [
    _make_codex,          # primary
    _make_minimax,        # also reliable for structured verdict output
    _make_claude_sonnet,  # last resort — leave Opus reserved for Orchestrator
    _make_claude,
]


def worker_chain(subtask: dict) -> list[Factory]:
    """Pick the Worker chain based on subtask difficulty.

    Honors `escalation_profile` overrides if the manifest explicitly names
    one (for tasks the Orchestrator wants to force into a specific tier
    regardless of nominal difficulty).
    """
    diff = (subtask.get("difficulty") or "T2").upper()
    return WORKER_TIERS.get(diff, WORKER_TIERS["T2"])


# Legacy / fallback for the old PROFILE_MAP-style routing. Kept so explicit
# `default_profile: P-CODE` etc. in old manifests still resolves to something.
CHAINS: dict[str, list[Factory]] = {
    "P-CODE":   WORKER_TIERS["T2"],
    "P-REASON": ORCHESTRATOR_CHAIN,
    "P-JUDGE":  VALIDATOR_CHAIN,
}


# Soft budgets per provider key — set caps that, when exceeded in the current
# state file, trigger preemptive failover. Leave empty `{}` to disable for a
# provider (only hard signals will exhaust it).
#
# Practical defaults: very generous, so only hard signals trigger failover
# unless you customize. Tighten these once you know your typical run-cost.
BUDGETS: dict[str, dict] = {
    # "claude-cli/claude-opus-4-7": {"max_tokens_in": 2_000_000, "max_tokens_out": 500_000},
    # "codex-cli/default":          {"max_invocations": 200},
    # "gemini-cli/gemini-2.5-pro":  {"max_tokens_in": 5_000_000},
    # "minimax-token/MiniMax-M2.5": {"max_tokens_in": 10_000_000},
}


def chain_for(profile: str) -> list[Factory]:
    if profile not in CHAINS:
        raise KeyError(f"unknown capability profile: {profile!r}")
    return CHAINS[profile]

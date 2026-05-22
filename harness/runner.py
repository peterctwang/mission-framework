"""Manifest-driven runner with quota-aware failover.

For each subtask:
    1. Resolve a Worker from the first AVAILABLE factory in CHAINS[default_profile].
    2. Run with [SOUL] + [TEMPLATE] + [CONTRACT excerpt] + [DYNAMIC].
    3. If a provider raises QuotaExhausted: mark it exhausted in quota.json
       and try the next factory in the chain. Repeat until success or chain
       empty.
    4. If needs_validator: same logic for the Validator chain. Must resolve
       to a different provider than the worker — chains automatically skip
       to keep diversity.
    5. On Validator reject: re-run worker. After 2 rejects, escalate to
       escalation_profile. After 3 rejects, mark "rework" and stop.
    6. Cache identical (provider+model+system+user) responses to skip re-runs.

Usage:
    python -m harness.runner path/to/manifest.json
    python -m harness.runner path/to/manifest.json --contract path/to/contract.md

Inspecting / resetting quota state:
    python -m harness.runner --status                 # show all providers
    python -m harness.runner --reset                  # reset all to ok
    python -m harness.runner --reset claude-cli/claude-opus-4-7
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import config
from . import skills as _skills
from .providers import Provider, QuotaExhausted, TransientProviderError
from .quota import QuotaTracker

PROMPTS_DIR = Path(__file__).parent / "prompts"
SOULS_DIR = Path(__file__).parent / "souls"

TOKEN_BUDGETS = {
    "worker": 8192,
    "validator": 2048,
    "orchestrator": 4096,
}

# Factory's reference run shows median 51 turns / impl, 30 / validation.
# We set a generous cap as a runaway-loop guardrail; trips on stuck workers.
TURN_CAPS = {
    "worker": 80,
    "validator": 50,
    "orchestrator": 30,
}

HANDOFF_WORD_CAP = 200


def _find_dotenv() -> Path | None:
    """Look for .env in (1) cwd, (2) cwd parents up to git root, (3) framework root.
    This lets the harness behave as a framework installed in any project."""
    candidates: list[Path] = []
    cwd = Path.cwd()
    candidates.append(cwd / ".env")
    for parent in cwd.parents:
        candidates.append(parent / ".env")
        if (parent / ".git").exists():
            break
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    for c in candidates:
        if c.exists():
            return c
    return None


def _load_dotenv(path: Path | None) -> None:
    if not path or not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(_find_dotenv())


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_soul(role: str, *, mode: str = "default") -> str:
    """Validators come in two flavors: scrutiny (code review) and
    functional (black-box). Default mode = scrutiny."""
    if role == "validator" and mode == "functional":
        return _read(SOULS_DIR / "validator-functional.md")
    return _read(SOULS_DIR / f"{role}.md")


def _load_template(name: str) -> str:
    return _read(PROMPTS_DIR / name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _save_artifact(out_dir: Path, subtask_id: str, label: str, content: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{subtask_id}.{label}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _build_system(role: str, *, mode: str = "default") -> str:
    """Compose [SOUL] + [TEMPLATE] for a role.

    Modes:
      - orchestrator/default — initial planning prompt
      - orchestrator/resolve — escalation handler
      - worker/default       — implementation prompt
      - validator/default    — scrutiny (code review)
      - validator/functional — black-box (actually runs the system)
    """
    role_to_template = {
        ("orchestrator", "default"): "1-orchestrator.md",
        ("orchestrator", "resolve"): "5-orchestrator-resolve.md",
        ("worker", "default"): "2-worker.md",
        ("validator", "default"): "3-validator.md",
        ("validator", "functional"): "6-validator-functional.md",
    }
    tmpl = role_to_template[(role, mode)]
    return f"{_load_soul(role, mode=mode)}\n\n---\n\n{_load_template(tmpl)}"


# Pattern to detect Worker's escalation request in its output.
# We match `## ESCALATE_TO_ORCHESTRATOR` followed by a fenced or unfenced
# JSON block until the next `##` or end-of-text.
_ESCALATE_RE = re.compile(
    r"##\s*ESCALATE_TO_ORCHESTRATOR\s*\n+(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_ORCH_DECISION_RE = re.compile(
    r"##\s*ORCHESTRATOR_DECISION\s*\n+(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _extract_json_block(text: str) -> dict | None:
    """Pull the first {...} JSON object from a text chunk. Tolerates fenced
    ```json blocks and leading prose."""
    text = text.strip()
    # Strip a leading ```json fence if present
    if text.startswith("```"):
        # remove first line + trailing ```
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    # Find the outermost {...}
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _detect_escalation(worker_output: str) -> dict | None:
    """If Worker asked to escalate, return the parsed payload; else None."""
    m = _ESCALATE_RE.search(worker_output)
    if not m:
        return None
    return _extract_json_block(m.group(1))


# Captures the body of a `## FILES_TO_WRITE` section until the next H2 or EOF.
_FILES_BLOCK_RE = re.compile(
    r"##\s*FILES_TO_WRITE\s*\n+(.*?)(?=\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# Within the body, each file = "### <path>" + a fenced code block.
_FILE_ENTRY_RE = re.compile(
    r"###\s+(?P<path>[^\n]+?)\s*\n+"
    r"```[a-zA-Z0-9_+-]*\n"
    r"(?P<content>.*?)"
    r"\n```",
    re.DOTALL,
)
_DELETE_MARKER_RE = re.compile(r"\(DELETE\)\s*$", re.IGNORECASE)


def _apply_files_to_write(worker_output: str, project_dir: Path,
                           log: Callable[..., None], subtask_id: str) -> int:
    """Parse the `## FILES_TO_WRITE` section of a Worker output and materialize
    every file on disk under project_dir. This is the safety net for providers
    that can't use tools (Minimax) or that lie about using them (Sonnet
    sometimes claims success without writing).

    Returns the count of files materialized. Skips files where the existing
    on-disk content is identical (idempotent — Workers using Write tools
    AND emitting FILES_TO_WRITE won't double-write).

    Rejects path traversal (no `..`, no absolute paths) for safety.
    """
    m = _FILES_BLOCK_RE.search(worker_output)
    if not m:
        return 0
    body = m.group(1)
    written = 0
    for entry in _FILE_ENTRY_RE.finditer(body):
        raw_path = entry.group("path").strip()
        content = entry.group("content")

        is_delete = bool(_DELETE_MARKER_RE.search(raw_path))
        clean_path = _DELETE_MARKER_RE.sub("", raw_path).strip().strip("`")

        # Safety: reject absolute paths and traversal
        if not clean_path or clean_path.startswith(("/", "\\")) or ".." in Path(clean_path).parts:
            log("files-skip-unsafe", id=subtask_id, path=clean_path)
            continue
        # Drop accidental drive letters on Windows
        if len(clean_path) > 1 and clean_path[1] == ":":
            log("files-skip-unsafe", id=subtask_id, path=clean_path)
            continue

        target = project_dir / clean_path
        try:
            target.resolve().relative_to(project_dir.resolve())
        except ValueError:
            log("files-skip-unsafe", id=subtask_id, path=clean_path)
            continue

        if is_delete:
            if target.exists():
                target.unlink()
                log("files-delete", id=subtask_id, path=clean_path)
            continue

        # Idempotent skip: if on-disk content already matches what the Worker
        # emitted, don't rewrite (preserves the tool-use case).
        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
                if existing.strip() == content.strip():
                    log("files-skip-identical", id=subtask_id, path=clean_path)
                    continue
            except (UnicodeDecodeError, OSError):
                pass

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        log("files-write", id=subtask_id, path=clean_path, size=len(content))
        written += 1
    return written


def _resolve_escalation(
    *,
    subtask: dict,
    payload: dict,
    manifest: dict,
    manifest_path: Path,
    contract: str,
    project_dir: Path,
    out_dir: Path,
    cache: "ResponseCache",
    tracker: QuotaTracker,
    log: Callable[..., None],
) -> dict | None:
    """Call the Orchestrator chain to handle a Worker's ESCALATE block.
    Returns the parsed decision dict (with normalized `action`).

    On REPLAN, manifest is patched IN-PLACE and re-saved.
    """
    orch_system = _build_system("orchestrator", mode="resolve")
    orch_user = (
        f"## Mission\n{manifest.get('mission', '')}\n\n"
        f"## Current manifest (compact)\n```json\n"
        f"{json.dumps([{k: s.get(k) for k in ('id', 'desc', 'difficulty', 'status', 'depends_on', 'covers')} for s in manifest['subtasks']], indent=2, ensure_ascii=False)}\n```\n\n"
        f"## Subtask in question\n```json\n{json.dumps(subtask, indent=2, ensure_ascii=False)}\n```\n\n"
        f"## Validation contract items relevant to this subtask\n"
        f"{_relevant_contract(contract, subtask.get('covers') or [])}\n\n"
        f"## Worker's ESCALATE_TO_ORCHESTRATOR payload\n```json\n"
        f"{json.dumps(payload, indent=2, ensure_ascii=False)}\n```\n\n"
        f"Respond with exactly ONE `## ORCHESTRATOR_DECISION` JSON block."
    )
    log("orchestrator-resolve-start", id=subtask["id"])
    text, used, _ = _call_with_failover(
        config.ORCHESTRATOR_CHAIN, orch_system, orch_user,
        role="orchestrator", cache=cache, tracker=tracker,
        forbidden=None, log=log, use_cache=False,
    )
    _save_artifact(out_dir, subtask["id"], "orchestrator-resolve", text)
    log("orchestrator-resolve-done", id=subtask["id"],
        provider=used.name, model=used.model)

    m = _ORCH_DECISION_RE.search(text)
    if not m:
        log("orchestrator-resolve-malformed", id=subtask["id"],
            note="no ORCHESTRATOR_DECISION block found")
        return None
    decision = _extract_json_block(m.group(1))
    if not decision or "action" not in decision:
        log("orchestrator-resolve-malformed", id=subtask["id"],
            note="missing 'action' in decision")
        return None
    decision["action"] = decision["action"].upper()

    if decision["action"] == "REPLAN":
        _apply_replan(subtask, decision, manifest)
        _save_manifest(manifest_path, manifest)
        log("manifest-patched", id=subtask["id"],
            note=f"applied {len(decision.get('subtask_patches', []))} patch(es)")
    return decision


def _orchestrator_decide_fix(
    *,
    subtask: dict,
    worker_output: str,
    validator_feedback: str,
    manifest: dict,
    manifest_path: Path,
    contract: str,
    project_dir: Path,
    out_dir: Path,
    cache: "ResponseCache",
    tracker: QuotaTracker,
    log: Callable[..., None],
) -> dict | None:
    """Factory-style fix-features pattern: after multiple validator rejects,
    instead of letting the same Worker keep failing, ask the Orchestrator
    (Opus) to either issue a precise DIRECTIVE or REPLAN a clean-context
    fix subtask (`split_into` a T-XX-fix child).

    The reasoning is that a Worker that has already failed on a piece is
    poorly positioned to objectively diagnose the gap — a fresh agent with
    only the validator's complaint as input does better.
    """
    orch_system = _build_system("orchestrator", mode="resolve")
    orch_user = (
        f"## Mission\n{manifest.get('mission', '')}\n\n"
        f"## Subtask that keeps failing\n```json\n"
        f"{json.dumps(subtask, indent=2, ensure_ascii=False)}\n```\n\n"
        f"## Relevant validation contract items\n"
        f"{_relevant_contract(contract, subtask.get('covers') or [])}\n\n"
        f"## Worker's latest output (rejected)\n{worker_output[:4000]}\n\n"
        f"## Validator's reject reasoning\n{validator_feedback[:3000]}\n\n"
        f"This subtask has been rejected multiple times. As Orchestrator you "
        f"must decide:\n"
        f"  - **DIRECTIVE**: a one-paragraph directive the next Worker attempt "
        f"will receive. Use if the gap is well-defined and small.\n"
        f"  - **REPLAN with `split_into`**: spawn a clean fix subtask "
        f"(id=`{subtask['id']}-fix`) targeting only the validator's complaints. "
        f"Use this when the Worker is clearly stuck and needs a fresh context.\n\n"
        f"Respond with exactly ONE `## ORCHESTRATOR_DECISION` JSON block."
    )
    log("orchestrator-fix-start", id=subtask["id"])
    text, used, _ = _call_with_failover(
        config.ORCHESTRATOR_CHAIN, orch_system, orch_user,
        role="orchestrator", cache=cache, tracker=tracker,
        forbidden=None, log=log, use_cache=False,
    )
    _save_artifact(out_dir, subtask["id"], "orchestrator-fix", text)
    log("orchestrator-fix-done", id=subtask["id"],
        provider=used.name, model=used.model)

    m = _ORCH_DECISION_RE.search(text)
    if not m:
        log("orchestrator-fix-malformed", id=subtask["id"])
        return None
    decision = _extract_json_block(m.group(1))
    if not decision or "action" not in decision:
        log("orchestrator-fix-malformed", id=subtask["id"])
        return None
    decision["action"] = decision["action"].upper()
    if decision["action"] == "REPLAN":
        _apply_replan(subtask, decision, manifest)
        _save_manifest(manifest_path, manifest)
        log("manifest-patched", id=subtask["id"],
            note=f"fix-feature: {len(decision.get('subtask_patches', []))} patch(es)")
    return decision


def _apply_replan(subtask: dict, decision: dict, manifest: dict) -> None:
    """Patch the manifest based on Orchestrator's REPLAN payload.

    Supports:
      - `desc`, `covers`, `difficulty`, `depends_on` overwrites on the
        current subtask.
      - `split_into`: replaces the subtask with N new ones; original id is
        marked deprecated (kept for log integrity but skipped on next pass).
    """
    patches = decision.get("subtask_patches", [])
    for p in patches:
        target_id = p.get("id")
        if target_id != subtask["id"]:
            # Only the in-flight subtask can be replanned this round.
            continue
        body = p.get("patch", {})
        for field in ("desc", "covers", "difficulty", "depends_on"):
            if field in body:
                subtask[field] = body[field]
        split = body.get("split_into")
        if split:
            idx = next(i for i, s in enumerate(manifest["subtasks"])
                       if s["id"] == subtask["id"])
            for j, new_sub in enumerate(split):
                manifest["subtasks"].insert(idx + 1 + j, {
                    **{
                        "execution": "serial",
                        "depends_on": [subtask["id"]],
                        "default_profile": "P-CODE",
                        "needs_validator": subtask.get("needs_validator", True),
                        "validator_profile": "P-JUDGE",
                        "status": "todo",
                    },
                    **new_sub,
                })
            subtask["status"] = "deprecated-by-split"


def _relevant_contract(contract: str, covers: list[str]) -> str:
    if not contract or not covers:
        return contract
    lines = contract.splitlines()
    keep: list[str] = []
    keeping = False
    for line in lines:
        m = re.match(r"\s*[-*]?\s*\*{0,2}(AC-[A-Za-z0-9_.]+)", line)
        if m:
            keeping = m.group(1) in covers
        if keeping or line.startswith("#") or not line.strip():
            keep.append(line)
    excerpt = "\n".join(keep).strip()
    return excerpt or contract


def _content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


class ResponseCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> str | None:
        p = self.root / f"{key}.txt"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def put(self, key: str, value: str) -> None:
        (self.root / f"{key}.txt").write_text(value, encoding="utf-8")


def _resolve_provider(
    chain: list[Callable[[], Provider]],
    tracker: QuotaTracker,
    *,
    forbidden: Provider | None = None,
    log: Callable[..., None],
) -> Provider:
    """Walk the chain and return the first provider that is:
    - available per QuotaTracker (not exhausted)
    - not the same as `forbidden` (used to enforce worker/validator diversity)
    Returns the constructed Provider. Raises if chain is empty.
    """
    last_error: Exception | None = None
    for factory in chain:
        try:
            provider = factory()
        except Exception as e:
            last_error = e
            log("provider-construct-failed", factory=factory.__name__, error=str(e)[:200])
            continue
        if not tracker.is_available(provider.name, provider.model):
            log("provider-skip-exhausted", provider=provider.name, model=provider.model)
            continue
        if forbidden and provider.name == forbidden.name and provider.model == forbidden.model:
            log("provider-skip-diversity", provider=provider.name, model=provider.model)
            continue
        return provider
    raise RuntimeError(
        f"all providers in chain exhausted or unavailable (last error: {last_error}). "
        f"Run `python -m harness.runner --status` to inspect, `--reset` to clear."
    )


def _call_with_failover(
    chain: list[Callable[[], Provider]],
    system: str,
    user: str,
    *,
    role: str,
    cache: ResponseCache,
    tracker: QuotaTracker,
    forbidden: Provider | None,
    log: Callable[..., None],
    cwd: str | None = None,
    use_cache: bool = True,
) -> tuple[str, Provider, bool]:
    """Try providers in order. On QuotaExhausted, mark + advance. Returns
    (text, provider_used, cache_hit). `cwd` is the project dir — passed to
    CLI providers so Workers can write files where they belong.
    `use_cache=False` is used on retry attempts so we don't get stuck
    returning the same rejected output."""
    remaining = list(chain)
    while remaining:
        provider = _resolve_provider(remaining, tracker, forbidden=forbidden, log=log)
        key = _content_hash(provider.name, provider.model or "", system, user)
        if use_cache:
            if cached := cache.get(key):
                log("cache-hit", role=role, provider=provider.name)
                return cached, provider, True
        try:
            result = provider.generate(
                system=system, user=user,
                max_tokens=TOKEN_BUDGETS[role],
                cwd=cwd,
            )
        except QuotaExhausted as e:
            tracker.mark_exhausted(provider.name, provider.model, e.reason)
            log("provider-exhausted", provider=provider.name, model=provider.model, reason=e.reason)
            remaining = [f for f in remaining if not _factory_matches(f, provider)]
            continue
        except TransientProviderError as e:
            log("provider-transient-error", provider=provider.name, error=str(e)[:200])
            remaining = [f for f in remaining if not _factory_matches(f, provider)]
            continue
        tracker.record_usage(
            provider.name, provider.model,
            result.usage.input_tokens, result.usage.output_tokens,
            result.usage.cached_input_tokens,
        )
        # Trajectory observability — emit a warning when the agent loop
        # ran longer than expected (caps from TURN_CAPS).
        cap = TURN_CAPS.get(role, 100)
        if result.usage.turns and result.usage.turns > cap:
            log("trajectory-cap-exceeded", role=role, provider=provider.name,
                turns=result.usage.turns, cap=cap)
        elif result.usage.turns:
            log("trajectory", role=role, provider=provider.name, turns=result.usage.turns)
        cache.put(key, result.text)
        return result.text, provider, False
    raise RuntimeError("call_with_failover exhausted all providers in chain")


def _factory_matches(factory: Callable[[], Provider], provider: Provider) -> bool:
    """We can't introspect factory output cheaply; the heuristic is to construct
    and compare. Used only on exhaustion (rare), so cost is fine."""
    try:
        p = factory()
    except Exception:
        return False
    return p.name == provider.name and p.model == provider.model


_VERDICT_RE = re.compile(r"判決\s*[:：]\s*(通過|打回)\s*$")


def _parse_verdict(validator_text: str) -> bool:
    tail = validator_text.strip().splitlines()[-3:] if validator_text.strip() else []
    for line in reversed(tail):
        m = _VERDICT_RE.search(line.strip())
        if m:
            return m.group(1) == "通過"
    lower_tail = "\n".join(tail).lower()
    if any(k in lower_tail for k in ("打回", "reject", "fail", "不通過")):
        return False
    if any(k in lower_tail for k in ("通過", "pass", "accept")):
        return True
    return False


def _extract_handoff(worker_text: str) -> str:
    text = worker_text.strip()
    if "## Handoff" in text:
        chunk = text.split("## Handoff", 1)[1].strip()
    else:
        chunk = "\n".join(text.splitlines()[-30:])
    words = chunk.split()
    if len(words) > HANDOFF_WORD_CAP:
        chunk = " ".join(words[:HANDOFF_WORD_CAP]) + " …[truncated]"
    return chunk


def run(manifest_path: Path, contract_path: Path | None) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    contract = (
        contract_path.read_text(encoding="utf-8")
        if contract_path and contract_path.exists()
        else manifest.get("validation_contract", "")
    )
    project_dir = manifest_path.parent
    out_dir = project_dir / "artifacts"
    cache = ResponseCache(project_dir / ".cache")
    tracker = QuotaTracker.load(project_dir / ".harness-state.json", budgets=config.BUDGETS)
    log_fp = (project_dir / "run.log.jsonl").open("a", encoding="utf-8")

    def log(event: str, **kwargs: Any) -> None:
        record = {"ts": _now(), "event": event, **kwargs}
        log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        log_fp.flush()
        print(f"[{record['ts']}] {event} :: {kwargs.get('id', '') or kwargs.get('provider', '')} "
              f"{kwargs.get('note') or kwargs.get('reason') or ''}", file=sys.stderr)

    handoff = ""
    rc = 0
    try:
        for subtask in manifest["subtasks"]:
            sid = subtask["id"]
            if subtask.get("status") in ("done", "deprecated-by-split"):
                log("skip-done", id=sid, note=subtask.get("status"))
                continue

            subtask["status"] = "in-progress"
            _save_manifest(manifest_path, manifest)

            ac_excerpt = _relevant_contract(contract, subtask.get("covers") or [])
            worker_system = _build_system("worker")
            # Optional: surface relevant skills from ~/.mission/skills/ that
            # match this subtask's description. Cheap keyword search.
            skill_ctx = _skills.load_skills_for_mission(
                subtask.get("desc", "") + " " + manifest.get("mission", ""),
            )
            worker_user = (
                f"## Current subtask\n```json\n{json.dumps(subtask, indent=2, ensure_ascii=False)}\n```\n\n"
                f"## Relevant validation contract items\n{ac_excerpt}\n\n"
                + (f"{skill_ctx}\n\n" if skill_ctx else "")
                + f"## Previous handoff\n{handoff or '(none — first subtask)'}\n"
            )

            # Worker chain is now picked by SUBTASK DIFFICULTY (T1/T2/T3),
            # not by default_profile. Easy → Minimax, medium → Sonnet,
            # hard → Opus. See harness/config.py::WORKER_TIERS.
            worker_chain = config.worker_chain(subtask)
            difficulty = subtask.get("difficulty", "T2")
            log("worker-start", id=sid, note=f"difficulty={difficulty}")
            worker_out, worker_used, _ = _call_with_failover(
                worker_chain, worker_system, worker_user,
                role="worker", cache=cache, tracker=tracker, forbidden=None, log=log, cwd=str(project_dir),
            )
            log("worker-done", id=sid, provider=worker_used.name, model=worker_used.model)
            _save_artifact(out_dir, sid, "worker", worker_out)

            # Safety net — materialize any `## FILES_TO_WRITE` content the
            # Worker emitted (handles providers without tool use and
            # providers that claim success without writing).
            n_files = _apply_files_to_write(worker_out, project_dir, log, sid)
            if n_files:
                log("files-applied", id=sid, count=n_files)

            # ── Worker → Orchestrator escalation (max 1 round per subtask) ──
            # If the Worker hit a global / decision boundary it emits an
            # ESCALATE_TO_ORCHESTRATOR block. We send it to Opus (Orchestrator),
            # get a DIRECTIVE / REPLAN / PROCEED_AS_IS decision, apply it, and
            # re-call the Worker with the resolution.
            escalation_payload = _detect_escalation(worker_out)
            if escalation_payload is not None:
                log("worker-escalated", id=sid,
                    reason=str(escalation_payload.get("reason", ""))[:120])
                decision = _resolve_escalation(
                    subtask=subtask, payload=escalation_payload,
                    manifest=manifest, manifest_path=manifest_path,
                    contract=contract, project_dir=project_dir,
                    out_dir=out_dir, cache=cache, tracker=tracker, log=log,
                )
                # On DIRECTIVE: re-call Worker with the directive injected.
                if decision and decision.get("action") == "DIRECTIVE":
                    directive = decision.get("directive", "")
                    resolved_user = (
                        f"{worker_user}\n\n"
                        f"## Orchestrator directive (after escalation)\n{directive}\n\n"
                        f"Do not escalate again — proceed and complete the subtask."
                    )
                    log("worker-resume-after-directive", id=sid)
                    worker_out, worker_used, _ = _call_with_failover(
                        worker_chain, worker_system, resolved_user,
                        role="worker", cache=cache, tracker=tracker,
                        forbidden=None, log=log, cwd=str(project_dir),
                        use_cache=False,
                    )
                    _save_artifact(out_dir, sid, "worker.after-directive", worker_out)
                    n = _apply_files_to_write(worker_out, project_dir, log, sid)
                    if n: log("files-applied", id=sid, count=n, stage="after-directive")

                # On REPLAN: manifest was already patched by _resolve_escalation.
                # Reload subtask from manifest, re-call Worker with the new spec.
                elif decision and decision.get("action") == "REPLAN":
                    subtask = next((s for s in manifest["subtasks"] if s["id"] == sid), subtask)
                    worker_user = (
                        f"## Current subtask (REPLANNED)\n```json\n"
                        f"{json.dumps(subtask, indent=2, ensure_ascii=False)}\n```\n\n"
                        f"## Relevant validation contract items\n"
                        f"{_relevant_contract(contract, subtask.get('covers') or [])}\n\n"
                        f"## Previous handoff\n{handoff or '(none)'}\n"
                    )
                    worker_chain = config.worker_chain(subtask)
                    log("worker-resume-after-replan", id=sid,
                        note=f"new_difficulty={subtask.get('difficulty')}")
                    worker_out, worker_used, _ = _call_with_failover(
                        worker_chain, worker_system, worker_user,
                        role="worker", cache=cache, tracker=tracker,
                        forbidden=None, log=log, cwd=str(project_dir),
                        use_cache=False,
                    )
                    _save_artifact(out_dir, sid, "worker.after-replan", worker_out)
                    n = _apply_files_to_write(worker_out, project_dir, log, sid)
                    if n: log("files-applied", id=sid, count=n, stage="after-replan")

                # PROCEED_AS_IS or unparseable decision → keep original output.

                # If REPLAN included `split_into`, the original subtask is
                # now deprecated and the inserted children will run in the
                # next outer-loop iteration. Skip validation for the deprecated
                # parent — its work is replaced by the children.
                if subtask.get("status") == "deprecated-by-split":
                    _save_manifest(manifest_path, manifest)
                    log("skip-replaced", id=sid, note="children inserted, skipping")
                    continue

            if not subtask.get("needs_validator", False):
                subtask["status"] = "done"
                handoff = _extract_handoff(worker_out)
                _save_manifest(manifest_path, manifest)
                continue

            # Validator chain is fixed (Codex primary). The validation_kind
            # field on the subtask selects between scrutiny (code review,
            # default) and functional (runs the system end-to-end via Bash).
            validator_chain = config.VALIDATOR_CHAIN
            validation_kind = subtask.get("validation_kind", "scrutiny")
            validator_mode = "functional" if validation_kind == "functional" else "default"
            validator_system = _build_system("validator", mode=validator_mode)
            log("validator-config", id=sid, kind=validation_kind)

            attempt = 1
            passed = False
            last_reject_feedback = ""
            while attempt <= 3:
                validator_user = (
                    f"## Validation contract\n{contract}\n\n"
                    f"## Subtask requirement\n```json\n{json.dumps(subtask, indent=2, ensure_ascii=False)}\n```\n\n"
                    f"## Artifact under review\n{worker_out}\n"
                )
                log("validator-start", id=sid, attempt=attempt)
                # Cache-bypass validator on retries — same worker output may be
                # validated fairly differently after fresh evaluation, and we
                # never want to lock into a cached reject.
                v_text, validator_used, _ = _call_with_failover(
                    validator_chain, validator_system, validator_user,
                    role="validator", cache=cache, tracker=tracker, forbidden=worker_used, log=log,
                    cwd=str(project_dir),  # validator reads disk to verify worker output
                    use_cache=(attempt == 1),
                )
                _save_artifact(out_dir, sid, f"validator.attempt{attempt}", v_text)
                passed = _parse_verdict(v_text)
                log("validator-done", id=sid, attempt=attempt,
                    provider=validator_used.name, model=validator_used.model,
                    note=f"verdict={'pass' if passed else 'reject'}")
                if passed:
                    log("validator-pass", id=sid, attempt=attempt)
                    break

                log("validator-reject", id=sid, attempt=attempt)
                last_reject_feedback = v_text

                # Fix-features pattern (Factory Missions §4):
                # On the 2nd reject, hand control to the Orchestrator (Opus).
                # It can either issue a DIRECTIVE for one more retry, or
                # REPLAN with split_into → spawn a clean-context fix subtask.
                # Difficulty escalation is now part of the Orchestrator's
                # toolkit (it can change `difficulty` via REPLAN), not a
                # hardcoded bump.
                if attempt == 2:
                    fix_decision = _orchestrator_decide_fix(
                        subtask=subtask, worker_output=worker_out,
                        validator_feedback=v_text, manifest=manifest,
                        manifest_path=manifest_path, contract=contract,
                        project_dir=project_dir, out_dir=out_dir,
                        cache=cache, tracker=tracker, log=log,
                    )
                    # REPLAN with split_into → deprecated-by-split, outer loop
                    # picks up the new fix child subtask cleanly.
                    if subtask.get("status") == "deprecated-by-split":
                        log("subtask-replaced-by-fix-feature", id=sid)
                        break

                    # DIRECTIVE → take it as the retry guidance
                    if fix_decision and fix_decision.get("action") == "DIRECTIVE":
                        last_reject_feedback = (
                            f"{v_text}\n\n[Orchestrator directive after review]\n"
                            f"{fix_decision.get('directive', '')}"
                        )
                    # If Orchestrator updated difficulty via REPLAN-in-place,
                    # the manifest is already patched; refresh worker_chain.
                    if subtask.get("difficulty"):
                        worker_chain = config.worker_chain(subtask)
                        log("escalate", id=sid,
                            note=f"orchestrator-decided difficulty={subtask['difficulty']}")

                # Build retry user prompt with the validator's reject reasoning
                # interpolated (+ Orchestrator directive if present).
                retry_user = (
                    f"{worker_user}\n\n"
                    f"## Previous attempt was REJECTED — Validator feedback\n{last_reject_feedback}\n\n"
                    f"Address every item above. Do not re-emit the same output."
                )
                worker_out, worker_used, _ = _call_with_failover(
                    worker_chain, worker_system, retry_user,
                    role="worker", cache=cache, tracker=tracker, forbidden=validator_used, log=log,
                    cwd=str(project_dir), use_cache=False,
                )
                _save_artifact(out_dir, sid, f"worker.attempt{attempt + 1}", worker_out)
                _apply_files_to_write(worker_out, project_dir, log, sid)
                log("worker-retry-done", id=sid, attempt=attempt + 1,
                    provider=worker_used.name, model=worker_used.model)
                attempt += 1

            # Don't overwrite status if Orchestrator replaced this subtask
            # with a fix-feature child (status="deprecated-by-split").
            if subtask.get("status") != "deprecated-by-split":
                subtask["status"] = "done" if passed else "rework"
            handoff = _extract_handoff(worker_out)
            _save_manifest(manifest_path, manifest)
            if subtask.get("status") == "rework":
                log("subtask-failed", id=sid, note="left as rework — manual intervention required")
                rc = 2
                break
    finally:
        log_fp.write(json.dumps({"ts": _now(), "event": "run-end", "usage_summary": tracker.summary()},
                                ensure_ascii=False) + "\n")
        log_fp.close()
        print("\n=== usage summary ===\n" + tracker.summary(), file=sys.stderr)

    return rc


def cmd_status(state_dir: Path) -> int:
    tracker = QuotaTracker.load(state_dir / ".harness-state.json", budgets=config.BUDGETS)
    print(tracker.summary())
    return 0


def cmd_reset(state_dir: Path, target: str | None) -> int:
    tracker = QuotaTracker.load(state_dir / ".harness-state.json", budgets=config.BUDGETS)
    if not target:
        tracker.reset()
        print("reset all providers to status=ok")
    else:
        if "/" in target:
            name, model = target.split("/", 1)
        else:
            name, model = target, None
        tracker.reset(name, model)
        print(f"reset {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mission Framework — cross-model harness")
    ap.add_argument("manifest", nargs="?", type=Path, help="path to manifest.json (omit for --status/--reset)")
    ap.add_argument("--contract", type=Path, default=None)
    ap.add_argument("--status", action="store_true", help="show quota usage and exit")
    ap.add_argument("--reset", nargs="?", const="__all__",
                    help="reset all providers, or one (e.g. --reset claude-cli/claude-opus-4-7)")
    ap.add_argument("--state-dir", type=Path, default=Path.cwd(),
                    help="where .harness-state.json lives (default: cwd)")
    args = ap.parse_args(argv)

    if args.status:
        return cmd_status(args.state_dir)
    if args.reset:
        return cmd_reset(args.state_dir, None if args.reset == "__all__" else args.reset)
    if not args.manifest:
        ap.print_help()
        return 1
    return run(args.manifest, args.contract)


if __name__ == "__main__":
    raise SystemExit(main())

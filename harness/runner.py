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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

# Wall-time cap per subtask (including all retries + fix-features). Once
# exceeded the subtask is marked rework and the mission moves on, so a stuck
# loop can't consume the whole token budget.
SUBTASK_WALL_TIME_S = 30 * 60   # 30 minutes

HANDOFF_WORD_CAP = 200

# Long-running Worker calls (Sonnet / Opus writing for 5-10 minutes) leave the
# parent Python silent while subprocess.run blocks. Some harnesses (background
# task monitors, supervisord watchdogs) interpret extended silence as a dead
# process and reap. The heartbeat thread writes a single line to stderr every
# 30s so the parent is visibly alive AND touches the PID lock so other runners
# know we're still here.
HEARTBEAT_INTERVAL_S = 20   # << 60s reaper threshold for margin
LOCK_STALE_AFTER_S = 60     # 3× heartbeat — orphan if lock not touched
PROVIDER_FAIL_LIMIT = 3     # circuit breaker: N consecutive transient errors → disable
DEFAULT_TOKEN_CAP_PER_MISSION = 10_000_000  # hard cap; overridable via --max-tokens
PARALLEL_MAX_WORKERS = 4   # ThreadPoolExecutor size for parallel subtasks
_heartbeat_stop = threading.Event()
_lock_path: Path | None = None


def _heartbeat_loop() -> None:
    while not _heartbeat_stop.wait(HEARTBEAT_INTERVAL_S):
        try:
            print(f"[{_now()}] heartbeat :: runner alive", file=sys.stderr, flush=True)
            if _lock_path:
                try:
                    _atomic_write(_lock_path, f"{os.getpid()}\n{time.time()}\n")
                except OSError:
                    pass
            # Also write a heartbeat event to the project's run.log.jsonl so
            # dashboards / consoles see liveness without sniffing stderr.
            if _log_fp_ref:
                try:
                    _log_fp_ref.write(json.dumps(
                        {"ts": _now(), "event": "heartbeat", "pid": os.getpid()},
                        ensure_ascii=False) + "\n")
                    _log_fp_ref.flush()
                except (OSError, ValueError):
                    pass
        except (OSError, ValueError):
            return


# Shared reference so the heartbeat thread can append to the same log file
# the main loop uses. Set in run() before starting the thread, cleared after.
_log_fp_ref = None


def _start_heartbeat() -> threading.Thread:
    _heartbeat_stop.clear()
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="mission-heartbeat")
    t.start()
    return t


def _stop_heartbeat() -> None:
    _heartbeat_stop.set()


class MissionLockHeld(RuntimeError):
    """Another runner has a fresh lock on this project."""


def _acquire_lock(project_dir: Path) -> None:
    """Refuse to start if another runner is alive on this project. The lock
    file holds the PID + last-heartbeat timestamp; we treat it as stale only
    if it hasn't been touched in LOCK_STALE_AFTER_S.

    On stale lock takeover, we also attempt to clean up the previous PID
    (and our own previously-spawned children if any are still alive).

    Prevents two runners writing to the same manifest.json concurrently
    (which corrupts data — orphan can overwrite the active run's progress).
    """
    global _lock_path
    p = project_dir / ".harness.lock"
    if p.exists():
        try:
            content = p.read_text(encoding="utf-8").strip().splitlines()
            pid = int(content[0]) if content else 0
            last_beat = float(content[1]) if len(content) > 1 else 0
        except (OSError, ValueError, IndexError):
            pid, last_beat = 0, 0
        age = time.time() - last_beat
        if age < LOCK_STALE_AFTER_S:
            raise MissionLockHeld(
                f"another runner (PID {pid}) is active on this project "
                f"(lock {age:.0f}s old, stale threshold {LOCK_STALE_AFTER_S}s). "
                f"If you're sure no runner is alive, run "
                f"`mission resume <project> --clean-lock`"
            )
        # Stale lock — attempt to kill the orphan PID before taking over.
        if pid and pid != os.getpid():
            _kill_pid_silent(pid)
    _atomic_write(p, f"{os.getpid()}\n{time.time()}\n")
    _lock_path = p


def _kill_pid_silent(pid: int) -> None:
    """Best-effort kill of an orphan PID. Silent on failure (process may
    already be dead, or we may lack permission)."""
    try:
        if sys.platform == "win32":
            import subprocess
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        else:
            os.kill(pid, 15)
    except (OSError, ValueError, subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _release_lock() -> None:
    global _lock_path
    if _lock_path and _lock_path.exists():
        try:
            _lock_path.unlink()
        except OSError:
            pass
    _lock_path = None


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


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via tmp + os.replace — atomic on POSIX and Windows
    (NTFS). Prevents corrupt half-written files when the process is killed
    mid-write. Used for manifest, state, cache, lock — anything a reader
    might read concurrently or that the runner re-loads after restart.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _save_manifest(path: Path, manifest: dict) -> None:
    _atomic_write(path, json.dumps(manifest, indent=2, ensure_ascii=False))


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


# ── Worker disk-diff guard ────────────────────────────────────────────────
#
# Background: Minimax & Gemini have both been observed to "rewrite" a file
# when asked to "add a key" — wiping all other exports/constants in the
# process. The synthetic validators sometimes pass these (they only see the
# worker's text artifact, not disk diffs), so we have to catch it in the
# runner before validator wastes its call.
#
# Approach: snapshot the workspace immediately before the worker runs, then
# after FILES_TO_WRITE is applied, diff the snapshot. If any tracked file
# shrunk dramatically or now contains a known "stub placeholder" string,
# treat it as an immediate disk-verify reject — same path as missing files.

# Files we DON'T snapshot — generated, vendored, or noise.
_SNAPSHOT_SKIP_DIRS = {".git", "node_modules", ".cache", "out", "__pycache__",
                       ".harness-cache", ".pytest_cache", "dist", "build"}
# Only files of these extensions get tracked. Conservative — text source only.
_SNAPSHOT_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".md", ".html",
                  ".css", ".yaml", ".yml", ".toml"}
# Files smaller than this are too short to meaningfully regress.
_SNAPSHOT_MIN_SIZE = 200
# Phrases that strongly suggest the worker substituted a placeholder for
# real content — case-insensitive substring match.
_STUB_MARKERS = (
    "...existing config...",
    "...existing code...",
    "// existing code here",
    "# existing code here",
    "<!-- existing content -->",
    "...rest of file...",
    "...rest of code...",
    "...previous content...",
    "/* unchanged */",
    "// (rest unchanged)",
)
# A file that lost more than this fraction of its bytes is suspicious.
_SHRINK_THRESHOLD = 0.40   # kept ≥40% of original bytes is "ok"; below = suspect
# Or that lost more than this many distinct top-level symbols.
_SYMBOL_LOSS_THRESHOLD = 5

_SYMBOL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:function|const|let|var|class|def)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)


def _snapshot_workspace(project_dir: Path) -> dict[str, dict]:
    """Snapshot text source files: {relpath: {size, symbols: set[str], head256}}.

    `head256` is the first 256 chars of the file — used to fingerprint
    identity-preserving rewrites (same file, contents replaced).
    """
    snap: dict[str, dict] = {}
    if not project_dir.exists():
        return snap
    for p in project_dir.rglob("*"):
        if not p.is_file():
            continue
        # Skip files inside opted-out directories
        parts = set(p.relative_to(project_dir).parts)
        if parts & _SNAPSHOT_SKIP_DIRS:
            continue
        if p.suffix.lower() not in _SNAPSHOT_EXTS:
            continue
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(data) < _SNAPSHOT_MIN_SIZE:
            continue
        symbols = set(_SYMBOL_RE.findall(data))
        rel = p.relative_to(project_dir).as_posix()
        snap[rel] = {
            "size": len(data),
            "symbols": symbols,
            "head256": data[:256],
        }
    return snap


def _check_workspace_regression(
    snapshot_before: dict[str, dict],
    project_dir: Path,
    log: Callable[..., None],
    subtask_id: str,
) -> list[str]:
    """Compare current workspace to snapshot. Returns human-readable
    regression descriptions ('' if clean)."""
    issues: list[str] = []
    for relpath, before in snapshot_before.items():
        p = project_dir / relpath
        if not p.exists():
            issues.append(f"{relpath}: file deleted (was {before['size']} bytes)")
            continue
        try:
            data = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Stub placeholder check — fires regardless of size.
        lower = data.lower()
        for marker in _STUB_MARKERS:
            if marker.lower() in lower:
                issues.append(
                    f"{relpath}: contains stub placeholder '{marker}' — "
                    f"worker wrote a skeleton instead of the full file"
                )
                break
        # Shrink check
        ratio = len(data) / max(before["size"], 1)
        if ratio < _SHRINK_THRESHOLD:
            issues.append(
                f"{relpath}: shrunk {before['size']}→{len(data)} bytes "
                f"({ratio:.0%}, threshold {_SHRINK_THRESHOLD:.0%})"
            )
        # Symbol loss check — count named top-level symbols that disappeared.
        after_syms = set(_SYMBOL_RE.findall(data))
        lost = before["symbols"] - after_syms
        if len(lost) >= _SYMBOL_LOSS_THRESHOLD:
            sample = ", ".join(sorted(lost)[:8])
            issues.append(
                f"{relpath}: lost {len(lost)} named symbols (e.g. {sample})"
            )
        # Critical-export check — any UPPER_SNAKE_CASE name lost is suspicious
        # even alone (PROVIDER_ABBR-style constants are typically module
        # exports that the rest of the codebase depends on).
        critical_lost = [s for s in lost
                         if len(s) >= 4 and s.isupper().__class__
                         and s.replace("_", "").isalnum()
                         and s.upper() == s and any(c.isalpha() for c in s)]
        if critical_lost and not any("lost" in i for i in issues):
            issues.append(
                f"{relpath}: lost critical UPPER_CASE export(s): "
                + ", ".join(sorted(critical_lost)[:6])
            )
    if issues:
        log("disk-diff-regression", id=subtask_id, count=len(issues),
            note=issues[0][:160])
    return issues


def _verify_worker_artifacts(worker_output: str, project_dir: Path,
                               log: Callable[..., None], subtask_id: str) -> list[str]:
    """After worker-done, check that files claimed in FILES_TO_WRITE actually
    exist on disk with non-zero size. Returns a list of missing paths (empty
    = all good). Catches Sonnet's path-normalization bug where the model
    claims success but writes to the wrong directory.
    """
    m = _FILES_BLOCK_RE.search(worker_output)
    if not m:
        return []  # no FILES_TO_WRITE block, can't verify
    body = m.group(1)
    missing: list[str] = []
    for entry in _FILE_ENTRY_RE.finditer(body):
        raw_path = entry.group("path").strip()
        is_delete = bool(_DELETE_MARKER_RE.search(raw_path))
        if is_delete:
            continue
        rel = _DELETE_MARKER_RE.sub("", raw_path).strip().strip("`")
        if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
            continue
        target = project_dir / rel
        if not target.exists() or target.stat().st_size == 0:
            missing.append(rel)
    if missing:
        log("disk-verify-fail", id=subtask_id, missing=",".join(missing[:5]))
    return missing


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
    decision = _normalize_decision(_extract_json_block(m.group(1)))
    if not decision:
        log("orchestrator-resolve-malformed", id=subtask["id"],
            note="missing/invalid action in decision")
        return None

    if decision["action"] == "REPLAN":
        _apply_replan(subtask, decision, manifest)
        _save_manifest(manifest_path, manifest)
        log("manifest-patched", id=subtask["id"],
            note=f"applied {len(decision.get('subtask_patches', []))} patch(es)")
    return decision


def _normalize_decision(raw: dict | None) -> dict | None:
    """Tolerate the multiple shapes Orchestrator (LLM) emits in practice:

      Canonical:  {"action":"REPLAN","subtask_patches":[{"id":..,"patch":{...}}],"rationale":"..."}
      Flat:       {"decision":"REPLAN","split_into":[<new subtask>],"rationale":"..."}
      DIRECTIVE:  {"action":"DIRECTIVE","directive":"...","rationale":"..."}

    Returns normalized canonical shape, or None if unrecoverable.
    """
    if not isinstance(raw, dict):
        return None
    action = (raw.get("action") or raw.get("decision") or "").strip().upper()
    if action not in ("DIRECTIVE", "REPLAN", "PROCEED_AS_IS"):
        return None
    out = {
        "action": action,
        "directive": raw.get("directive", ""),
        "subtask_patches": raw.get("subtask_patches") or [],
        "rationale": raw.get("rationale", ""),
    }
    # Flat {split_into:[...]} → synthesize subtask_patches that _apply_replan
    # interprets as "split the current subtask".
    if action == "REPLAN" and not out["subtask_patches"] and raw.get("split_into"):
        out["subtask_patches"] = [{
            "id": "__current__",
            "patch": {"split_into": raw["split_into"]},
        }]
    return out


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
    decision = _normalize_decision(decision)
    if not decision:
        log("orchestrator-fix-malformed", id=subtask["id"])
        return None
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
        # "__current__" is the synthesized id from _normalize_decision for flat
        # {split_into:[...]} shape — always applies to the in-flight subtask.
        if target_id != subtask["id"] and target_id != "__current__":
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
        try:
            return p.read_text(encoding="utf-8") if p.exists() else None
        except OSError:
            return None

    def put(self, key: str, value: str) -> None:
        # Defensive: external cleanup (rm -rf .cache) between __init__ and put
        # can race and remove the dir under us — recreate before write.
        # Atomic: tmp + replace so concurrent readers never see half files.
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / f"{key}.txt"
            _atomic_write(target, value)
        except OSError:
            # Caching is opportunistic. Don't crash the whole mission if the
            # filesystem refuses one write.
            pass


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


_provider_fail_count: dict[str, int] = {}   # in-run circuit breaker counters


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
            # Circuit breaker — N consecutive transient errors → treat
            # this provider as exhausted for the rest of the run.
            pkey = f"{provider.name}/{provider.model or ''}"
            _provider_fail_count[pkey] = _provider_fail_count.get(pkey, 0) + 1
            if _provider_fail_count[pkey] >= PROVIDER_FAIL_LIMIT:
                tracker.mark_exhausted(provider.name, provider.model,
                                       f"circuit-breaker:{_provider_fail_count[pkey]} consecutive transient errors")
                log("provider-circuit-break", provider=provider.name,
                    count=_provider_fail_count[pkey])
            remaining = [f for f in remaining if not _factory_matches(f, provider)]
            continue
        # Reset circuit breaker on success
        pkey = f"{provider.name}/{provider.model or ''}"
        _provider_fail_count[pkey] = 0
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


# ── Mission Ledger — structured cross-subtask memory ──────────────────────
#
# A 200-word free-text handoff is too thin for long missions (50+ subtasks,
# 4+ hours). By subtask #20 the worker has no idea what subtask #5 decided.
#
# The ledger is an append-only JSON record persisted to out_dir/ledger.json
# after every subtask completion. Each entry captures structured fields
# parsed from the worker's `## Handoff` section:
#
#   { id, ts, files_touched: [paths], invariants: [str], decisions: [str],
#     narrative: str }
#
# When building the next worker's prompt, we don't replay all narratives —
# instead we surface:
#   • the FULL last 2 subtasks (recent context)
#   • the deduplicated invariant list (long-term commitments)
#   • a files-touched index (path → which subtask introduced it)
#
# This means subtask #50 sees "subtask #5 established that
# `PROVIDER_KEYS must contain claude-cli/codex-cli/gemini-cli/minimax-token`"
# even though there's no room to replay subtask #5's full output.
#
# Resume-friendly: if a mission gets killed mid-run, ledger.json on disk
# preserves cumulative state, so the next `mission run` rebuilds the
# context as if no interruption happened.

# Section headers the parser recognises inside ## Handoff. Case-insensitive,
# the worker SOUL prompts for these exact names but we tolerate variants.
_HANDOFF_SECTION_RE = re.compile(
    r"^###\s+(Files\s+touched|Invariants|Decisions|Narrative)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
LEDGER_RECENT_NARRATIVE_COUNT = 2
LEDGER_INVARIANT_HARD_CAP = 30  # don't let it grow unbounded across 100+ subtasks
LEDGER_FILES_INDEX_CAP = 40


def _parse_structured_handoff(worker_text: str) -> dict:
    """Parse `## Handoff` body into structured fields.

    Expected (loose) shape:
        ## Handoff
        ### Files touched
        - frontend/game.js
        - backend/app.py
        ### Invariants
        - LAYOUT.providers must contain all 4 keys
        ### Decisions
        - Used Phaser graphics over text for performance
        ### Narrative
        Brief free-text recap (<80 words).

    Missing sections degrade gracefully — narrative falls back to last
    30 lines if no `## Handoff` block at all.
    """
    text = worker_text or ""
    if "## Handoff" in text:
        body = text.split("## Handoff", 1)[1]
    else:
        body = "\n".join(text.strip().splitlines()[-30:])
        return {
            "files_touched": [],
            "invariants": [],
            "decisions": [],
            "narrative": body[:1000],
        }

    sections: dict[str, list[str]] = {"files_touched": [], "invariants": [],
                                       "decisions": [], "narrative": []}
    cur = "narrative"
    for line in body.splitlines():
        m = _HANDOFF_SECTION_RE.match(line)
        if m:
            name = m.group(1).lower().replace(" ", "_")
            cur = {"files_touched": "files_touched", "invariants": "invariants",
                   "decisions": "decisions", "narrative": "narrative"}.get(name, "narrative")
            continue
        # Stop if we hit the next H2 (some other top-level section).
        if line.startswith("## ") and not line.startswith("## Handoff"):
            break
        sections[cur].append(line)

    def _items(lines: list[str]) -> list[str]:
        out = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            # Bullet markers
            if s.startswith(("-", "*", "•")):
                s = s.lstrip("-*• ").strip()
            if s:
                out.append(s[:300])  # individual item cap
        return out

    narrative_text = "\n".join(sections["narrative"]).strip()
    if len(narrative_text) > 800:
        narrative_text = narrative_text[:800].rstrip() + " …[truncated]"

    return {
        "files_touched": _items(sections["files_touched"])[:20],
        "invariants":    _items(sections["invariants"])[:15],
        "decisions":     _items(sections["decisions"])[:10],
        "narrative":     narrative_text,
    }


class MissionLedger:
    """Append-only cross-subtask memory persisted to ledger.json.

    Long-mission survivor: on resume, load_or_new(out_dir) reconstructs
    cumulative state from disk so the worker prompt looks identical
    whether the mission started fresh or resumed after a crash.
    """

    def __init__(self, out_dir: Path):
        self.path = out_dir / "ledger.json"
        self.entries: list[dict] = []

    @classmethod
    def load_or_new(cls, out_dir: Path) -> "MissionLedger":
        led = cls(out_dir)
        if led.path.exists():
            try:
                led.entries = json.loads(led.path.read_text(encoding="utf-8"))
                if not isinstance(led.entries, list):
                    led.entries = []
            except (json.JSONDecodeError, OSError):
                led.entries = []
        return led

    def record(self, subtask_id: str, worker_text: str) -> dict:
        parsed = _parse_structured_handoff(worker_text)
        entry = {
            "id": subtask_id,
            "ts": _now(),
            **parsed,
        }
        self.entries.append(entry)
        self._persist()
        return entry

    def _persist(self) -> None:
        try:
            _atomic_write(self.path, json.dumps(self.entries, indent=2, ensure_ascii=False))
        except OSError:
            # Ledger is observability — never fail the mission over it.
            pass

    def aggregate_invariants(self) -> list[str]:
        """Deduplicated invariants across all subtasks, most-recent first.
        Capped so it doesn't bloat the worker prompt on 100+ subtask runs."""
        seen: set[str] = set()
        out: list[str] = []
        for entry in reversed(self.entries):
            for inv in entry.get("invariants", []):
                key = inv.lower().strip()
                if key in seen:
                    continue
                seen.add(key)
                out.append(inv)
                if len(out) >= LEDGER_INVARIANT_HARD_CAP:
                    return out
        return out

    def files_index(self) -> list[tuple[str, str]]:
        """[(path, last-subtask-that-touched-it)] up to a cap."""
        idx: dict[str, str] = {}
        for entry in self.entries:
            for f in entry.get("files_touched", []):
                idx[f] = entry["id"]   # later overwrites earlier
        items = list(idx.items())
        # Most-recently-touched first
        items.sort(key=lambda kv: next(
            (i for i, e in enumerate(reversed(self.entries))
             if kv[0] in e.get("files_touched", [])),
            10**9,
        ))
        return items[:LEDGER_FILES_INDEX_CAP]

    def recent_narratives(self, n: int = LEDGER_RECENT_NARRATIVE_COUNT) -> list[dict]:
        """Last n entries' (id, narrative) — full text for proximate context."""
        return self.entries[-n:] if self.entries else []

    def as_worker_context(self) -> str:
        """Render the ledger as a section for the worker prompt.

        Returns empty string if there's nothing yet (first subtask).
        """
        if not self.entries:
            return ""
        parts: list[str] = []
        parts.append("## Mission ledger (cumulative cross-subtask memory)")
        invs = self.aggregate_invariants()
        if invs:
            parts.append("\n### Invariants established by prior subtasks (must respect)")
            for inv in invs:
                parts.append(f"- {inv}")
        files = self.files_index()
        if files:
            parts.append("\n### Files touched so far")
            for path, sid in files:
                parts.append(f"- `{path}` (last touched by {sid})")
        narratives = self.recent_narratives()
        if narratives:
            parts.append(f"\n### Recent subtask handoffs (last {len(narratives)})")
            for e in narratives:
                hdr = f"#### {e['id']}"
                parts.append(hdr)
                if e.get("decisions"):
                    parts.append("**Decisions:** " + "; ".join(e["decisions"][:5]))
                if e.get("narrative"):
                    parts.append(e["narrative"])
        return "\n".join(parts).strip()


_log_lock = threading.Lock()


def _schedule_iter(manifest: dict):
    """Dependency-aware iterator over subtasks.

    Yields the next ready subtask (one at a time). Skips finished states.
    A subtask is ready when ALL its `depends_on` are done or deprecated-by-split.
    Re-scans on each yield because runner mutates statuses live.

    Parallel execution (when all ready subtasks share execution=readonly-parallel)
    is a planned extension — see _schedule_next_batch for the multi-yield variant
    that the parallel-aware runner will use.
    """
    finished = {"done", "deprecated-by-split"}
    while True:
        by_id = {s["id"]: s for s in manifest["subtasks"]}
        next_one = None
        for s in manifest["subtasks"]:
            if s.get("status") in finished:
                continue
            deps = s.get("depends_on") or []
            if any(by_id.get(d, {}).get("status") not in finished for d in deps):
                continue
            next_one = s
            break
        if next_one is None:
            return
        yield next_one


def _schedule_next_batch(manifest: dict) -> list[dict]:
    """Return the next group of subtasks ready to run in parallel.

    A batch is multiple subtasks when ALL ready entries declare
    execution=readonly-parallel; otherwise the batch is a single subtask
    (serial). Used by the (future) parallel runner; current code calls
    _schedule_iter for one-at-a-time semantics.
    """
    finished = {"done", "deprecated-by-split"}
    by_id = {s["id"]: s for s in manifest["subtasks"]}
    ready: list[dict] = []
    for s in manifest["subtasks"]:
        if s.get("status") in finished or s.get("status") == "in-progress":
            continue
        deps = s.get("depends_on") or []
        if any(by_id.get(d, {}).get("status") not in finished for d in deps):
            continue
        ready.append(s)
    if not ready:
        return []
    if all(s.get("execution") == "readonly-parallel" for s in ready):
        return ready[:PARALLEL_MAX_WORKERS]
    return [ready[0]]


def _validate_manifest_schema(manifest: dict) -> list[str]:
    """Lightweight schema validation — returns list of human-readable errors,
    empty list = OK. Fails fast on missing required fields before we burn
    LLM tokens on a manifest that won't run to completion.
    """
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    if "mission" not in manifest:
        errors.append("missing top-level 'mission' (str describing the goal)")
    subs = manifest.get("subtasks")
    if not isinstance(subs, list) or not subs:
        errors.append("'subtasks' must be a non-empty array")
        return errors
    ids_seen: set[str] = set()
    for i, s in enumerate(subs):
        if not isinstance(s, dict):
            errors.append(f"subtasks[{i}] is not an object")
            continue
        sid = s.get("id")
        if not sid:
            errors.append(f"subtasks[{i}] missing 'id'")
        elif sid in ids_seen:
            errors.append(f"duplicate subtask id: {sid}")
        else:
            ids_seen.add(sid)
        if not s.get("desc"):
            errors.append(f"subtask {sid!r} missing 'desc'")
        if s.get("difficulty") not in ("T1", "T2", "T3", None):
            errors.append(f"subtask {sid!r} difficulty must be T1/T2/T3 (got {s.get('difficulty')!r})")
        for dep in s.get("depends_on") or []:
            if dep not in ids_seen and dep not in [x.get("id") for x in subs]:
                errors.append(f"subtask {sid!r} depends_on {dep!r} which doesn't exist")
    return errors


def run(manifest_path: Path, contract_path: Path | None,
        max_tokens: int = DEFAULT_TOKEN_CAP_PER_MISSION) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Schema validation — fail fast on malformed manifest.
    errors = _validate_manifest_schema(manifest)
    if errors:
        print("manifest validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 4
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
    # Expose to heartbeat thread so it can emit liveness events.
    global _log_fp_ref
    _log_fp_ref = log_fp

    def log(event: str, **kwargs: Any) -> None:
        record = {"ts": _now(), "event": event, **kwargs}
        log_fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        log_fp.flush()
        print(f"[{record['ts']}] {event} :: {kwargs.get('id', '') or kwargs.get('provider', '')} "
              f"{kwargs.get('note') or kwargs.get('reason') or ''}", file=sys.stderr)

    handoff = ""
    # Mission ledger — cumulative cross-subtask memory survives crashes.
    # If ledger.json exists from a prior interrupted run, we resume with
    # full context (invariants, files touched, recent narratives).
    ledger = MissionLedger.load_or_new(out_dir)
    rc = 0
    _provider_fail_count.clear()
    # PID lock — refuse to run if another mission is alive on this project.
    # Stale locks (heartbeat > LOCK_STALE_AFTER_S old) are auto-claimed.
    _acquire_lock(project_dir)
    _start_heartbeat()
    manifest_lock = threading.Lock()  # serializes manifest mutations across parallel workers
    try:
        for subtask in _schedule_iter(manifest):
            # Hard token cap — abort cleanly if mission has consumed beyond budget.
            total_tokens = sum(
                st.tokens_in + st.tokens_out for st in tracker.providers.values()
            )
            if total_tokens > max_tokens:
                log("mission-token-cap-exceeded", note=f"{total_tokens} > {max_tokens}")
                rc = 3
                break
            sid = subtask["id"]
            if subtask.get("status") in ("done", "deprecated-by-split"):
                log("skip-done", id=sid, note=subtask.get("status"))
                continue

            with manifest_lock:
                subtask["status"] = "in-progress"
            _save_manifest(manifest_path, manifest)
            subtask_start_time = time.time()

            ac_excerpt = _relevant_contract(contract, subtask.get("covers") or [])
            worker_system = _build_system("worker")
            # Optional: surface relevant skills from ~/.mission/skills/ that
            # match this subtask's description. Cheap keyword search.
            skill_ctx = _skills.load_skills_for_mission(
                subtask.get("desc", "") + " " + manifest.get("mission", ""),
            )
            ledger_ctx = ledger.as_worker_context()
            worker_user = (
                f"## Current subtask\n```json\n{json.dumps(subtask, indent=2, ensure_ascii=False)}\n```\n\n"
                f"## Relevant validation contract items\n{ac_excerpt}\n\n"
                + (f"{skill_ctx}\n\n" if skill_ctx else "")
                + (f"{ledger_ctx}\n\n" if ledger_ctx else "")
                + f"## Previous handoff (most recent — full text)\n{handoff or '(none — first subtask)'}\n"
            )

            # Worker chain is now picked by SUBTASK DIFFICULTY (T1/T2/T3),
            # not by default_profile. Easy → Minimax, medium → Sonnet,
            # hard → Opus. See harness/config.py::WORKER_TIERS.
            worker_chain = config.worker_chain(subtask)
            difficulty = subtask.get("difficulty", "T2")
            # Snapshot the workspace BEFORE the worker runs so we can detect
            # regressions (file rewrites that wipe other exports) afterwards.
            # This catches Minimax/Gemini stub-rewrites that pass naive
            # validation but break the rest of the project.
            workspace_before = _snapshot_workspace(project_dir)
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

            # Disk-diff guard — compare snapshot vs current workspace. Any
            # file that shrunk dramatically, lost ≥5 named symbols, or now
            # contains a "...existing config..." stub marker is treated as a
            # regression and fed into the same reject path as missing files.
            regressions = _check_workspace_regression(
                workspace_before, project_dir, log, sid,
            )
            if regressions and subtask.get("needs_validator", False):
                synth_reject = (
                    "DISK DIFF REGRESSION — Worker corrupted existing files:\n"
                    + "\n".join(f"  • {r}" for r in regressions[:8])
                    + "\n\nThis usually means the worker rewrote a whole file when "
                    "asked to add or modify a section. Re-emit ONLY the targeted "
                    "change (use Edit tool / patch syntax, not Write/rewrite). "
                    "The file's other exports, constants, and functions MUST be "
                    "preserved exactly.\n\n判決:打回"
                )
                _save_artifact(out_dir, sid, "disk-diff-reject", synth_reject)
                log("disk-diff-reject", id=sid, note=f"{len(regressions)} regressions")
                worker_out = (worker_out
                              + "\n\n[RUNTIME NOTE — disk diff regressions]\n"
                              + "\n".join(regressions[:8]))

            # Disk verification — fail-fast if Worker claimed files that
            # don't exist on disk. Avoids wasting a Validator call on
            # output that's obviously broken.
            missing = _verify_worker_artifacts(worker_out, project_dir, log, sid)
            if missing and subtask.get("needs_validator", False):
                # Treat as immediate reject — synthesize a feedback message
                # the retry loop will use.
                synth_reject = (
                    f"DISK VERIFICATION FAILED — Worker claimed FILES_TO_WRITE "
                    f"entries that do not exist on disk: {', '.join(missing[:10])}.\n\n"
                    f"Either the Write tool silently wrote to wrong cwd, or the "
                    f"FILES_TO_WRITE block lied. Re-emit with correct paths and "
                    f"verify each file actually persists.\n\n判決:打回"
                )
                _save_artifact(out_dir, sid, "disk-verify-reject", synth_reject)
                log("disk-verify-reject", id=sid, note=f"missing {len(missing)} files")
                # Forge a synthetic validator-reject into the retry loop by
                # pre-seeding worker_out to include the rejection context.
                worker_out = worker_out + "\n\n[RUNTIME NOTE — files missing]\n" + ", ".join(missing[:10])

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
                ledger.record(sid, worker_out)
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
            timed_out = False
            # Strict scope — Validator sees ONLY the AC items in covers,
            # not the full contract. Prevents "I see AC-N exists in contract,
            # let me check it too" scope creep observed in L-02 reject loop.
            covers = subtask.get("covers") or []
            validator_scope = _relevant_contract(contract, covers)
            while attempt <= 3:
                # Wall-time guard — break out if subtask has consumed too much time
                if time.time() - subtask_start_time > SUBTASK_WALL_TIME_S:
                    log("subtask-timeout", id=sid, note=f"exceeded {SUBTASK_WALL_TIME_S}s wall-time")
                    timed_out = True
                    break
                validator_user = (
                    f"## Subtask requirement\n```json\n{json.dumps(subtask, indent=2, ensure_ascii=False)}\n```\n\n"
                    f"## In-scope acceptance criteria (THESE ONLY — do not check others)\n"
                    f"{validator_scope}\n\n"
                    f"Subtask covers exactly: {', '.join(covers) if covers else '(none — verify nothing)'}\n\n"
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
                if passed:
                    subtask["status"] = "done"
                elif timed_out:
                    subtask["status"] = "rework"
                else:
                    subtask["status"] = "rework"
            handoff = _extract_handoff(worker_out)
            # Record into ledger only on success — rework/failed entries
            # would pollute future subtask context with broken state.
            if subtask.get("status") == "done":
                ledger.record(sid, worker_out)
            _save_manifest(manifest_path, manifest)
            if subtask.get("status") == "rework":
                note = "wall-time exceeded" if timed_out else "left as rework — manual intervention required"
                log("subtask-failed", id=sid, note=note)
                rc = 2
                break
    finally:
        _stop_heartbeat()
        _release_lock()
        log_fp.write(json.dumps({"ts": _now(), "event": "run-end", "usage_summary": tracker.summary()},
                                ensure_ascii=False) + "\n")
        log_fp.close()
        print("\n=== usage summary ===\n" + tracker.summary(), file=sys.stderr)

    return rc


def cmd_status(state_dir: Path, detailed: bool = False) -> int:
    tracker = QuotaTracker.load(state_dir / ".harness-state.json", budgets=config.BUDGETS)
    print(tracker.summary())
    if detailed:
        log_path = state_dir / "run.log.jsonl"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            print("\n=== events by type (last run) ===")
            counts: dict[str, int] = {}
            for line in lines:
                try:
                    evt = json.loads(line).get("event", "?")
                    counts[evt] = counts.get(evt, 0) + 1
                except json.JSONDecodeError:
                    pass
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
                print(f"  {k:30s}  {v}")
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
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_TOKEN_CAP_PER_MISSION,
                    help=f"hard token cap per mission (default {DEFAULT_TOKEN_CAP_PER_MISSION:,})")
    args = ap.parse_args(argv)

    if args.status:
        return cmd_status(args.state_dir)
    if args.reset:
        return cmd_reset(args.state_dir, None if args.reset == "__all__" else args.reset)
    if not args.manifest:
        ap.print_help()
        return 1
    return run(args.manifest, args.contract, max_tokens=args.max_tokens)


if __name__ == "__main__":
    raise SystemExit(main())

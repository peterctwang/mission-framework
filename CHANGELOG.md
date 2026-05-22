# Changelog

## 0.2.0 — First fully working mission (2026-05-23)

### Milestone

Framework completed its first real-world mission end-to-end: built a Phaser-based pixel dashboard inspired by Star-Office-UI in `C:\Users\User\Desktop\mission framework 面板\`. All 4 LLM providers participated (T1=Minimax, T2=Sonnet, T3=Opus, Validator=Codex), all 6 subtasks passed validation.

### Added

- **Difficulty-based Worker routing** — T1/T2/T3 from manifest directly drives compute allocation (Minimax / Sonnet / Opus). Orchestrator (Opus) is the master planner; Validator (Codex) is fixed.
- **Worker → Orchestrator escalation protocol** — Worker emits `## ESCALATE_TO_ORCHESTRATOR` JSON when blocked by global decisions; Orchestrator returns `DIRECTIVE` / `REPLAN` (incl. `split_into`) / `PROCEED_AS_IS`. Capped at 1 escalation per subtask.
- **FILES_TO_WRITE safety net** — runner parses Worker output for fenced code blocks under `## FILES_TO_WRITE` and materializes files to disk, with path traversal protection. Compensates for providers that lie about tool use (Sonnet) or lack tool channels (Minimax fallback).
- **Minimax OpenAI-standard tool calling** — `write_file`, `read_file`, `list_dir`, `run_shell` (verb whitelist: cp/mv/mkdir/ls/find/rm/echo/cat). Hosted endpoint parses XML → standard `tool_calls` when `tools=[...]` is declared.
- **Validator response includes validator reject feedback in Worker retry prompt** — breaks identical-cache-hit reject loops.
- **Cache bypass on retries** — `use_cache=False` for any attempt > 1.
- **`SPEC.md`**, **`LESSONS.md`**, expanded **`DESIGN.md`**.

### Fixed

- **Critical**: Claude `--system-prompt` was REPLACING Claude Code's default agent-loop instructions, causing all Workers to return "I'll wait for your request". Switched to `--append-system-prompt` (preserves agent loop).
- **Critical**: User prompt sent via argv could exceed Windows' 8191-char limit and be silently truncated. Now piped via subprocess stdin.
- **Critical**: Validator was running with no cwd, defaulting to framework dir, so it couldn't see Worker's files. Now passes `cwd=str(project_dir)`.
- **Critical**: Worker SOUL was 3,757 chars — too long for Claude to interpret as directive. Trimmed to ~1,200 chars with imperative-bullet style.
- Manifest's `needs_validator` field is required for important tasks — added explicit warning in Orchestrator SOUL.
- Minimax was emitting raw `<minimax:tool_call>` XML; fixed by declaring `tools=[]` in request.
- `.env` UTF-8 BOM (written by PowerShell `Out-File -Encoding utf8`) was breaking first-line env vars; loader now uses `utf-8-sig`.
- Rich console crashed on Windows cp950 with Unicode glyphs; CLI now force-reconfigures stdout to UTF-8.
- Codex stdin handling on Windows hangs with `subprocess.DEVNULL`; uses `shell=True` + `< NUL` redirect.
- Codex on ChatGPT subscription rejects any explicit `-m`; provider uses `model=None`.

### Removed

- **Gemini from default chains** — `gemini -p` headless mode ignores `--approval-mode plan` and crashes with `INVALID_STREAM` on Windows. Provider file kept for when Google fixes upstream bug.

### Verified provider capability matrix

| Provider | Worker | Validator | Tool use | Reliable on Windows |
|---|---|---|---|---|
| Claude Opus | ✅ | ✅ | ✅ | ✅ |
| Claude Sonnet | ⚠️ (path normalization can write to wrong dir) | ✅ | ✅ | ⚠️ |
| Codex | ✅ (heavy lifter) | ✅ (primary) | ✅ | ✅ |
| Minimax | ✅ (tools added) | ✅ | ✅ (we wired it) | ✅ |
| Gemini | ❌ | ❌ | ❌ | ❌ |

### Token usage (first successful mission)

```
minimax-token/MiniMax-M2.5     in= 178,952  out=  7,747   3 calls  (T-01, T-02, T-06)
claude-cli/claude-sonnet-4-6   in=   8,165  out= 30,878   5 calls  (T-03, T-04 + retries)
claude-cli/claude-opus-4-7     in=      93  out= 65,664   4 calls  (T-05 + escalation)
codex-cli/default              in= 999,199  out= 18,480   8 calls  (Validator x 6 subtasks + retries)
```

Mission cost: ~$0 (all subscriptions) + ~50 minutes wall time (heavily debug-iterated; clean run ~15-20 min).

---

## 0.1.0 — Initial framework (early session)

- Manifest-driven runner
- 4 provider stubs (Claude / Codex / Gemini / Minimax)
- SOUL files for Orchestrator / Worker / Validator
- Prompt-cache-friendly layered system prompts
- Quota tracker with sticky exhaustion
- Rich-based dashboard, watch, console (Textual TUI)
- One-shot `setup.ps1` + `.bat` launchers

# mission-framework

Cross-model multi-agent harness for **long-running autonomous development**. Uses your **subscription / OAuth logins**, not API keys. When one provider runs out of quota, it **automatically fails over** to the next so the run doesn't stop.

## What it does

- Treats LLMs as interchangeable workers under three abstract roles:
  - **Worker** (`P-CODE`) — implements one subtask at a time
  - **Validator** (`P-JUDGE`) — adversarially reviews the worker's output, on a **different model** (cross-model diversity)
  - **Orchestrator** (`P-REASON`) — plans the manifest before execution
- Drives every run from a single `manifest.json` — list of subtasks with difficulty, dependencies, and which validators they need.
- Persists per-provider quota state to `.harness-state.json` and **switches providers** on signals like Minimax 2056, Claude/Codex "usage limit", Gemini "resource_exhausted".
- Caches identical calls under `.cache/` so re-runs cost zero tokens.
- Layered prompts (SOUL.md → template → contract excerpt → dynamic) for **prompt-cache hits** on stable prefixes.

## Currently supported providers

| Provider | Auth | Tool use | Quota signal |
|---|---|---|---|
| Claude (`claude` CLI) | `claude login` — Anthropic subscription | ✅ via `--allowedTools` (Worker mode) | stderr "usage limit / rate limit / quota" |
| Codex (`codex` CLI) | `codex login` — ChatGPT subscription | ✅ via `workspace-write` sandbox | event-stream "usage limit / weekly limit" |
| Minimax (HTTPS) | `MINIMAX_API_KEY` — Coding/Token Plan | ✅ via OpenAI-standard `tools=[...]` (`write_file` / `read_file` / `list_dir` / `run_shell`) | HTTP 429 + body `(2056)` |
| Gemini (`gemini` CLI) | first interactive run — Google OAuth | ❌ headless mode broken upstream | (disabled in default chains) |

Adding a provider = one file under `harness/providers/` + an entry in `harness/config.py`.

**Before writing any new provider or modifying invocation**, read [LESSONS.md](LESSONS.md) — every quirk we've learned the hard way.

## Install

### Windows — one-click setup

```powershell
git clone https://github.com/peterctwang/mission-framework.git
cd mission-framework
.\setup.ps1
```

`setup.ps1` is idempotent — it checks Python, pip-installs the framework, wires the `mission` command onto your User PATH, installs the three Node CLIs (claude / codex / gemini), reminds you to log in, and runs a smoke-test render. Re-runnable any time.

After it finishes, **open a new terminal** so the PATH update takes effect, then:

```powershell
claude login      # if not already logged in
codex login
gemini            # interactive; sign in then /quit
```

For Minimax, copy `.env.example` to `.env` and paste your Coding Plan key (`sk-cp-...`).

Two double-click launchers also drop into the repo root:
- `console.bat` — opens the interactive TUI
- `dashboard.bat` — prints a one-shot snapshot (pause-on-exit)

### Manual install (other platforms)

```bash
git clone https://github.com/peterctwang/mission-framework.git
cd mission-framework
pip install -e .
npm install -g @anthropic-ai/claude-code @openai/codex @google/gemini-cli
claude login && codex login && gemini   # browser OAuth
cp .env.example .env                    # paste MINIMAX_API_KEY
```

## Use it on your own project

The framework runs against any directory containing a `manifest.json`:

```bash
cd ~/my-project
mission path/to/manifest.json
# or, if you didn't pip install:
python -m harness.runner path/to/manifest.json
```

Per-project artifacts land **next to the manifest**, not inside the framework:

```
my-project/
├── manifest.json              # you write this (Orchestrator can generate it)
├── contract.md                # validation contract (Step 0)
├── artifacts/                 # ← worker / validator outputs
├── .cache/                    # ← response cache
├── .harness-state.json        # ← quota tracker
└── run.log.jsonl              # ← structured event log
```

The framework directory stays clean — no per-run files written there.

## Authoring a manifest

Use the [Step 0 contract prompt](harness/prompts/0-contract.md) on any model to write `contract.md`, then the [Orchestrator prompt](harness/prompts/1-orchestrator.md) to produce `manifest.json`. Schema:

```json
{
  "mission": "Add --dry-run to deploy.py",
  "validation_contract": "contract.md",
  "subtasks": [
    {
      "id": "T-01",
      "desc": "Parse --dry-run flag in argparse setup",
      "difficulty": "T1",
      "execution": "serial",
      "depends_on": [],
      "default_profile": "P-CODE",
      "escalation_profile": null,
      "needs_validator": false,
      "validator_profile": null,
      "covers": ["AC-1"],
      "status": "todo"
    }
  ]
}
```

See [examples/manifest.example.json](examples/manifest.example.json).

## Console — view & control the framework

Three ways to watch what's happening:

### 1. `mission dashboard` — one-shot snapshot (works in Claude Code)

```bash
mission dashboard           # snapshot of cwd
mission dashboard ~/myapp   # snapshot of a specific project
```

Prints a four-panel layout (mission summary / providers / tasks / events) in any terminal. **Designed so Claude Code can call this via the Bash tool and you can see the whole state at a glance** — no special parsing, the rendered ANSI is what you read.

```
┌────────────────── MISSION ──────────────────┐  ┌──────────────────── PROVIDERS ─────────────────────┐
│ Add --dry-run to deploy.py                  │  │ ● claude-cli/claude-opus-4-7   124,382  ok         │
│ done 1  active 1  rework 0  todo 1  /  3    │  │ ● codex-cli/default             55,410  ok         │
└─────────────────────────────────────────────┘  │ ◐ gemini-cli/gemini-2.5-pro     12,881  exhausted  │
                                                  │ ● minimax-token/MiniMax-M2.5    38,192  ok         │
                                                  └────────────────────────────────────────────────────┘
┌───────────────────── TASKS ─────────────────────────────────────────────────────────────────────────┐
│ T-01  Parse --dry-run flag       T1   P-CODE        todo                                            │
│ T-02  Guard mutation calls       T2   P-CODE   ✓    done                                            │
│ T-03  Integration test           T2   P-CODE   ✓    in-progress                                     │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
┌────────────────── EVENTS (latest) ──────────────────────────────────────────────────────────────────┐
│ 14:32:06  validator-pass          id=T-02                                                           │
│ 14:33:48  provider-exhausted      provider=gemini-cli  reason=quota exceeded                        │
│ 14:34:14  escalate                id=T-03                                                           │
└─────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 2. `mission watch` — live-updating full-screen dashboard

Same layout but refreshes ~4Hz. Best in a side terminal while a long run progresses. `Ctrl+C` exits.

### 3. `mission console` — interactive TUI (textual)

```bash
mission console            # opens textual TUI for cwd
mission console ~/myapp
```

Keyboard:
- `s` — submit / run a manifest (modal: enter path)
- `r` — reset a provider's quota (modal: pick one or "all")
- `e` — force refresh now
- `q` — quit

The TUI spawns `mission run` as a detached background process, then tails the log files. You can close the TUI and the run continues; reopen it and it picks up where it was.

### 4. Other inspection verbs

```bash
mission tail -n 30          # last 30 events from run.log.jsonl
mission tasks               # task table only
mission status              # provider quota state
```

All of these print plain ANSI — usable from Claude Code via Bash, no special integration.

## Quota tracking & failover

The runner streams a usage summary at the end of each run:

```
=== usage summary ===
  claude-cli/claude-opus-4-7              in=  124382  out=  8932  calls= 12  status=ok
  codex-cli/default                       in=   55410  out=  4204  calls=  6  status=ok
  gemini-cli/gemini-2.5-pro               in=   12881  out=  1102  calls=  4  status=ok
  minimax-token/MiniMax-M2.5              in=   38192  out=  2210  calls=  8  status=ok
```

Inspect / reset state at any time:

```bash
mission --status
mission --reset                                  # all providers back to ok
mission --reset claude-cli/claude-opus-4-7       # one provider
```

### How failover decides

Routing is **difficulty-based for Workers** + fixed chains for Orchestrator / Validator:

```python
# Worker: dispatched by subtask difficulty (T1/T2/T3)
WORKER_TIERS = {
    "T1": [Minimax, Sonnet, Codex, Opus],   # routine → cheap & fast
    "T2": [Sonnet,  Opus,   Codex, Minimax],# standard → balanced
    "T3": [Opus,    Sonnet, Codex, Minimax],# hard → max reasoning
}

# Orchestrator (master planner) — Opus first
ORCHESTRATOR_CHAIN = [Opus, Codex, Sonnet, Minimax]

# Validator — Codex always (best at structured verdicts)
VALIDATOR_CHAIN = [Codex, Minimax, Sonnet, Opus]
```

Gemini is **not** in default chains — its headless mode has an upstream bug (see [LESSONS.md #8](LESSONS.md)).

Resolution order at each call:
1. Find the **first chain entry** whose provider is `status=ok` in `.harness-state.json`.
2. Skip any provider already used as the Worker on this subtask (cross-model diversity for Validator).
3. Call it. On `QuotaExhausted`, mark it `exhausted` and walk to the next.
4. Repeat until success or the chain is empty.

Exhaustion is **sticky for 6 hours** (configurable: `QuotaTracker(cooldown_seconds=...)`). After cooldown, the provider auto-recovers. Set cooldown to 0 to require manual `--reset`.

### Soft budgets (preemptive failover)

In addition to hard signals, you can set per-provider token caps in `config.BUDGETS`:

```python
BUDGETS = {
    "claude-cli/claude-opus-4-7": {"max_tokens_in": 2_000_000, "max_tokens_out": 500_000},
    "codex-cli/default":          {"max_invocations": 200},
}
```

Once usage exceeds the cap, the tracker marks that provider `exhausted` even without a server-side signal. Useful for long-running autonomous runs where you want to pace your subscription.

## Long autonomous runs

For continuous "develop project X for hours" workflows:

```bash
# Drive multiple manifests in sequence
for m in tasks/*.json; do
    mission "$m" || echo "stopped on $m"
done

# Or wrap in your own scheduler / cron / systemd timer.
```

The harness is stateless across runs except for `.harness-state.json` and `.cache/` — both per-project, both safe to leave.

## Architecture at a glance

```
manifest.json
     │
     ▼
┌─────────────────┐         ┌──────────────────┐
│  Orchestrator   │ writes  │  Worker chain    │
│  (you author    │  ───►   │  P-CODE          │ ───► artifacts/T-XX.worker.md
│   manifest)     │         │  fails over →    │
└─────────────────┘         │  next provider   │
                            └──────────────────┘
                                     │
                                     ▼
                            ┌──────────────────┐
                            │  Validator chain │
                            │  P-JUDGE         │ ───► artifacts/T-XX.validator.attempt1.md
                            │  must differ     │
                            │  from worker     │
                            └──────────────────┘
                                     │
                          pass ──────┴────── reject ──► retry (max 3) → rework
```

See [DESIGN.md](DESIGN.md) for the token-optimization rationale (prompt layering, AC excerpting, cache strategy, per-role budgets).

## Roles & SOULs

Each role has a **SOUL.md** — a non-changing "constitution" that's injected at the top of every prompt. This is what makes outputs stable across runs and across providers:

- [harness/souls/orchestrator.md](harness/souls/orchestrator.md) — Planner: stop on ambiguous specs, smallest indivisible subtasks
- [harness/souls/worker.md](harness/souls/worker.md) — Implementer: respect subtask boundaries, write tests for each AC
- [harness/souls/validator.md](harness/souls/validator.md) — Reviewer: find what breaks, not what works; verdict must be machine-parseable

## Limitations

- `readonly-parallel` is in the schema but the runner is currently serial.
- Token counts for CLI providers depend on what the CLI emits — some versions of `codex --json` and `gemini -o json` may report 0; the framework still works (it tracks invocations, not just tokens).
- Codex on Windows uses `shell=True` + temp files to dodge a stdin EOF bug — slightly less efficient than spawn-direct, but the bug is in the upstream CLI.

## License

MIT. See [LICENSE](LICENSE).

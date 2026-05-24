# mission-framework — Specification

> 這份文件是經過實戰驗證的規格,涵蓋路由模型、provider 行為、escalation 協議、安全網與已知陷阱。
> 第一次跑這個框架的人讀這份就夠;深入細節讀 [DESIGN.md](DESIGN.md);採坑紀錄讀 [LESSONS.md](LESSONS.md);改版紀錄讀 [CHANGELOG.md](CHANGELOG.md)。
>
> **v0.3.6(本版)新增**:Orchestrator 統一決策的平行 worker 執行(ThreadPoolExecutor + readonly-parallel 標記)、結構化 Mission Ledger(跨 subtask 記憶 + 斷點續跑)、Workspace disk-diff guard(catch 砍檔/塞 stub 的 worker)、Gemini CLI parity(完整對齊 Claude/Codex 能力,進入所有 chain)。
>
> 早期版本里程碑:v0.3.0 Factory Missions alignment;v0.3.1 Wave 1 data safety;v0.3.2 Wave 2+3(resume/orphan/circuit breaker/token cap/heartbeat/schema validation);v0.3.3 Gemini CLI;v0.3.4 disk-diff guard;v0.3.5 Mission Ledger;v0.3.6 平行執行。

---

## 1. 角色與職責

| 角色 | 由誰執行 | 職責 | 不做什麼 |
|---|---|---|---|
| **Orchestrator** | Opus (主) | 寫 manifest,定難度 (T1/T2/T3) + **execution (parallel/serial)** + depends_on,處理 Worker escalate | 不寫程式碼 |
| **Worker** | 視難度路由 | 實作一個 subtask,用工具寫檔 + 輸出結構化 ## Handoff | 不審查、不擴大範圍 |
| **Validator** | Codex (主) | 對照 AC 給判決 通過/打回 | 不修程式碼 |

---

## 2. 路由模型(經實測確認)

### Worker by Difficulty

```python
WORKER_TIERS = {
    "T1": [Minimax, Sonnet, Gemini, Codex, Opus],    # 例行 → 便宜
    "T2": [Sonnet,  Opus,   Gemini, Codex, Minimax], # 標準 → 均衡
    "T3": [Opus,    Sonnet, Gemini, Codex, Minimax], # 困難 → 最強推理
}
```

### Orchestrator(主規劃師)
```python
ORCHESTRATOR_CHAIN = [Opus, Codex, Gemini, Sonnet, Minimax]
```

### Validator(嚴格 directive 服從)
```python
VALIDATOR_CHAIN = [Codex, Minimax, Gemini, Sonnet, Opus]
```

**為什麼 Codex 是 Validator 第一順位**:它對「short directive prompts → structured verdict output」的服從性最高,不會像 Claude `-p` 偶爾退化成 chat 模式。

**Gemini 的位置**:全部 chain 第 3 順位 backup。TDD parity 34/34 證實能寫檔/讀檔/改檔/跑 shell,但 production 中**對「Edit 現有複雜檔案」會誤判成 rewrite**(會砍掉其他 exports)— 不適合當 worker primary,但作為 backup 救火 OK。

### 跨模型多樣性(自動強制)

`_ensure_diverse(worker, validator)` 在每次組對前檢查 —— 同 provider + 同模型一定拋錯。Validator 鏈會自動跳過撞型的選項。

---

## 3. 各 Provider 能力矩陣(實測)

| 能力 | Claude (Opus/Sonnet) | Codex | Minimax M2.5 | Gemini 2.5 Pro |
|---|---|---|---|---|
| 短 directive prompt → structured output | ⚠️ 需 `--append-system-prompt` | ✅ 完美 | ✅ 完美 | ✅(我們的 directive preamble) |
| Worker 寫檔(tool use) | ✅(需正確 invocation) | ✅ | ✅(我們補的 OpenAI tools 通道) | ✅(approval-mode yolo) |
| Shell 操作(cp/mv/mkdir) | ✅ acceptEdits 自動允許 | ✅ workspace-write | ✅(我們補的 `run_shell` 工具) | ✅(yolo mode) |
| 在 cwd 內精確寫檔 | ⚠️ Sonnet 偶爾寫到怪路徑 | ✅ | ✅ | ✅ |
| Edit 現有複雜檔案 | ✅ | ✅ | ⚠️ 易 rewrite | ⚠️ 易 rewrite |
| Long context 處理(>10k tokens) | ✅ | ✅ | ✅(會自動 shrink) | ✅(stdin pipe 14KB tested) |
| 輸出 usage(token 統計) | ✅ | ✅ | ✅ | ✅(stats.models.tokens) |
| 中文 / 多語言 | ✅ | ✅ | ✅ | ✅ |

**結論**:所有 4 個 provider 都可用,但 Worker primary 仍以 Claude/Sonnet/Minimax 為主。Gemini + Codex 主要當 Validator/Orchestrator/Backup。

---

## 4. 正確的 Provider Invocation

### Claude CLI(關鍵!)

```bash
claude -p \
  --model claude-opus-4-7 \
  --output-format json \
  --permission-mode acceptEdits \
  --allowedTools "Bash,Read,Edit,Write,Glob,Grep,LS" \
  --append-system-prompt "<SOUL>"
# user prompt via STDIN (input=user_prompt), NOT positional argv
```

**地雷**:
- ❌ `--system-prompt`(REPLACE)會刪掉 default agent-loop 指令 → Claude 退化成 chat 模式回 "I'll wait"
- ❌ `claude -p "long_prompt" ...` 超過 8191 字會在 Windows argv 上靜默截斷
- ✅ 用 `--append-system-prompt` + stdin pipe
- ✅ Worker 模式才設 cwd;Validator 模式不要(但要讓 Validator 能讀檔 → 在 runner 給 cwd)

### Codex CLI

```bash
codex exec --json --skip-git-repo-check --ignore-user-config \
  --dangerously-bypass-approvals-and-sandbox \
  -C "<project_dir>" \
  -s workspace-write \
  -o "<out_file>" \
  - < "<prompt_file>"
```

**地雷**:
- ❌ ChatGPT 訂閱拒絕 `-m gpt-5` 等任何顯式模型 → 不傳 `-m`,讓 codex 自選
- ✅ stdin pipe + `-o` 取最終訊息

### Minimax(HTTPS Chat Completions)

```python
POST https://api.minimax.io/v1/chat/completions
{
  "model": "MiniMax-M2.5",
  "messages": [...],
  "tools": [...],          # OpenAI 標準 function calling schema
  "tool_choice": "auto"
}
```

**地雷**:
- ❌ 不傳 `tools` → 模型會吐 `<minimax:tool_call>` XML 在 content 裡(我們不解析)
- ✅ 傳 `tools=[...]` → hosted endpoint 自動解析 XML 回標準 `tool_calls`
- ✅ 需 loop:`tool_calls` → 執行 → 把結果以 `role:"tool"` 加進 messages → 再 call
- ✅ 我們提供的 tools:`write_file` / `read_file` / `list_dir` / `run_shell`(verb 白名單)

### Gemini CLI(v0.3.3 新增 — 已可用)

```bash
gemini \
  -m gemini-2.5-pro \
  -o json \
  --approval-mode yolo \         # Worker (--yolo 已 deprecated)
  --skip-trust \
  -p ""                          # 強制 headless,即使 stdin pipe
# user+system prompt via STDIN(無 --system-prompt flag)
```

Env 強化(防 TUI/auto-update hang):
```
NO_COLOR=1  TERM=dumb
GEMINI_CLI_DISABLE_TELEMETRY=1
GEMINI_CLI_DISABLE_AUTO_UPDATE=1
```

**地雷**:
- ❌ `--yolo` 已 deprecated,改用 `--approval-mode yolo`
- ❌ **`--approval-mode plan` 在 Windows headless 是壞的**(issue #24814 + #25584)— Validator 用 `default` 即可
- ❌ 沒有 `--system-prompt` flag → system 與 user 串成一段 prompt
- ✅ JSON envelope: `{response, stats:{models:{<model>:{tokens:{prompt,candidates,cached,total}}}}}`
- ✅ TDD parity 34/34 通過(包含 write_file / read_file / edit / shell / 14KB long prompt / 中文 / multi-step)

---

## 5. Worker 輸出格式(SOUL 規範,v0.3.5 起結構化)

Worker 必須輸出 **3 段**:

````markdown
## Implementation
<簡述做了什麼,fenced code block 標檔名>

## FILES_TO_WRITE
### relative/path/file.py
```python
<完整檔案內容,不是 diff>
```

### nested/dir/file.html
```html
...
```

### oldfile.txt (DELETE)
```
```

## Handoff
### Files touched
- relative/path/file1
- relative/path/file2

### Invariants
- 後續 subtask **必須** respect 的事實
- 通常是新建的 constant / function signature / config schema
- 寫成「X 必須 Y」格式 — 後續 worker 會把這當合約看

### Decisions
- 關鍵實作選擇 + 為什麼(≤ 1 行/條)

### Narrative
做了什麼的一段話(≤ 80 字),給下一條 subtask 看的提醒。
````

**Handoff 結構化是長 mission 的命脈** — runner 把這 4 個 section 解析成 `ledger.json`,後續 subtask 看不到完整 worker 輸出,只看到 Invariants 區 + Files index + 最近 2 個 narrative。Invariants 寫不清楚 = 後面踩坑。

**FILES_TO_WRITE 是安全網**:即使 Worker 用了 Write tool,還是要列出來。Runner 會解析這區塊 + 落檔到 cwd。
- 處理「Sonnet 宣稱寫了實際沒寫」的情況
- 處理 Minimax 不熟悉 tool 直接 emit code 的情況
- 路徑安全檢查:拒絕絕對路徑、`..`、`X:` 開頭

---

## 6. Worker→Orchestrator 升級協議(escalation)

Worker 卡到 subtask 範圍以外 → 輸出:

```
## ESCALATE_TO_ORCHESTRATOR
{
  "reason": "...",
  "blocking_question": "...",
  "options_considered": ["A...","B..."],
  "recommendation": "A"
}
```

Runner 接到 → 呼叫 Orchestrator (Opus) → 回應其一:
- **`DIRECTIVE`** — 直接回答,Worker 帶答案重試
- **`REPLAN`** — 修改 manifest(改欄位 / `split_into` 拆任務)
- **`PROCEED_AS_IS`** — 確認 Worker 自己決定

深度限制:同 subtask 最多 escalate **1 次**。**僅在 serial 路徑啟用** — 平行 batch 內的 escalation 直接標 rework,讓 serial 路徑或 Orchestrator 重 plan。

---

## 7. Validator 驗證契約

判決行**必須**是 `判決:通過` 或 `判決:打回`(逐字,半/全形冒號都接受)。Runner 用 regex 嚴格匹配。

Validator 必須在 **cwd=project_dir** 下執行(從 disk 看 Worker 寫的真實檔案)。

**Strict scope (v0.3.1)**:每次 Validator call 只送該 subtask `covers` 列出的 AC 條目,其他 AC 完全不接觸 — 防止 Validator 跨範圍打回。

### Two-tier Validators

每個 subtask 可選 `validation_kind`:

| `validation_kind` | 行為 | 適用 |
|---|---|---|
| `scrutiny`(預設) | 讀 code、對照 AC、找隱藏假設 / 邊界 case | 純程式邏輯 |
| `functional` | 用 Bash 工具**真的執行**系統(`curl` / `python` / `pytest`)觀察輸出 | UI / API / 服務、契約有可執行驗證的任務 |

SOUL 各自獨立 —— [`validator-functional.md`](harness/souls/validator-functional.md) 明確指示「不要讀 code 下判決,實際跑」。

### Fix-features pattern

Validator reject 2 次後,**不再盲目升難度重試**,改呼 Orchestrator(Opus)決定:
- **DIRECTIVE** —— 一段精準指令給下次 Worker
- **REPLAN with `split_into`** —— 拆出 `T-XX-fix` 乾淨 context 子任務

依據 Factory Missions 原則:「Validator 只指出問題,Orchestrator 排程修法」 —— 同一個失敗 context 的 Worker 不適合自己救自己。

### Trajectory caps

每個 role 的 agent-loop turn 上限:

```python
TURN_CAPS = {
    "worker": 80,
    "validator": 50,
    "orchestrator": 30,
}
```

超過 → 紀錄 `trajectory-cap-exceeded` 事件(早期偵測 stuck workers)。

---

## 8. 平行執行(v0.3.6 新增 — Orchestrator 統一決策)

### Manifest 欄位

每個 subtask **必須**設 `execution`:
- `"readonly-parallel"` — 只讀現有檔、不寫共用檔,**可以跟其他 readonly-parallel 同時跑**
- `"serial"`(預設) — 寫共用檔 / 改 config / 動 schema,**獨佔執行**

### Scheduler 規則(`_schedule_next_batch`)

1. 找出所有 ready(`status` 不在 done/deprecated,deps 都滿足)的 subtasks
2. **First-ready 決定 batch mode**:
   - First-ready 是 `readonly-parallel` → 撈所有 ready 的 readonly-parallel 一起跑(上限 `PARALLEL_MAX_WORKERS = 4`)
   - First-ready 是 serial → 只跑 [first]

### Dispatch 路徑

```
batch = _schedule_next_batch(manifest)

len(batch) == 1  → 走完整 serial flow(worker → escalate → files → disk-diff
                   → verify → validator → fix-features → ledger.record)

len(batch) >  1  → ThreadPoolExecutor(max_workers=4) dispatch
                   每個 worker 跑 _run_subtask_parallel_lite:
                     • 單一 attempt(不 escalate、不 fix-features)
                     • 完整 disk-diff guard 保護
                     • 失敗 → status=rework,讓下輪 serial 接手
                   各自獨立 ledger.record(under ledger_lock)
```

### Locks

```
manifest_lock — manifest 寫入互斥
ledger_lock   — ledger.record() append+persist 互斥
tracker_lock  — QuotaTracker 狀態檔互斥(預留)
_log_lock     — run.log.jsonl 行寫入互斥
```

### Worker prompt(平行模式)

額外注入 `## Parallel execution note` 告知 worker 同時間還有哪些 subtask 在跑,要 stay strictly within named files。disk-diff guard 會 catch 任何越界。

### Orchestrator SOUL 規則 #6

判斷標準:「**兩個 worker 同時跑會不會踩到同一個檔?**」會 → serial。不會 → readonly-parallel。

典型 readonly-parallel: 跑測試、查詢、分析、生報告、靜態檢查。

---

## 9. Mission Ledger(v0.3.5 新增 — 結構化跨 subtask 記憶)

`artifacts/ledger.json` 是 append-only JSON,每完成一個 subtask 寫一筆:

```json
[
  { "id": "T-01", "ts": "...",
    "files_touched": ["frontend/layout.js"],
    "invariants":    ["LAYOUT.providers 必須含 4 個 key"],
    "decisions":     ["depth=1100 高過 desk=1000"],
    "narrative":     "Added LAYOUT.providers config block." },
  ...
]
```

Worker prompt 由 `MissionLedger.as_worker_context()` 注入:
```
## Mission ledger (cumulative cross-subtask memory)
### Invariants established by prior subtasks (must respect)
- ...(dedupe,cap 30)
### Files touched so far
- `frontend/game.js` (last touched by T-12)
### Recent subtask handoffs (last 2)
#### T-12
**Decisions:** ...
narrative ...
```

**長 mission 救命:**
- Subtask #50 知道 #5 定的 invariant
- Mission 被 kill 後 `mission run` 重啟,ledger.json 自動 reload,完整 context 重建
- 只記 done 的 subtask(rework/failed 不污染後續 worker context)

---

## 10. Workspace Disk-diff Guard(v0.3.4 新增)

Worker 跑前 snapshot 整個 workspace(text source files,跳 node_modules/cache),跑完比對:

| 偵測規則 | 觸發條件 |
|---|---|
| 檔案被刪 | 原本存在的檔現在不存在 |
| 嚴重縮水 | 縮到 <40% 原大小 |
| 符號流失 | 失去 ≥5 named symbols(function/const/let/class/def) |
| Critical export 流失 | 失去任一 UPPER_CASE export(PROVIDER_ABBR-style) |
| Stub 標記 | 含 `// ...existing config...` `...rest of file...` 等 placeholder |

觸發 → 合成 `disk-diff-reject` 事件 + 具體 diff 內容塞入 worker_out → 走 fix-features 路徑(serial)或 status=rework(parallel)。

**catch 到的真實案例**:Minimax 把 layout.js 整個改成「LAYOUT = { // ...existing config... newKey: 1 }」;Gemini 把 PROVIDER_KEYS 從 `[claude-cli, codex-cli, gemini-cli, minimax-token]` 改成 `[gemini, openai, anthropic, ...]`。

---

## 11. 失效模式 → 自動處理

| 失效訊號 | 框架行為 |
|---|---|
| Provider `QuotaExhausted` | 標 exhausted,沿 chain 走下一順位 |
| Provider 連續 3 次 `TransientProviderError` | Circuit breaker — 該 provider 本場 mission 停用 |
| Validator reject 2 次 | 進 fix-features:Orchestrator 決定 DIRECTIVE / REPLAN |
| Validator reject 第 3 次 | 標 `rework`,run 中止 |
| Worker output 含 `## ESCALATE_TO_ORCHESTRATOR` | 暫停,呼 Orchestrator 決策後重試 |
| 同 system+user hash 再次出現 | Response cache 命中跳過(retry 時 use_cache=False) |
| Worker 沒用 Write tool 但 emit FILES_TO_WRITE | runner 落檔 |
| Worker 寫檔後 disk-diff 觸發 | 合成 reject,走 fix-features |
| Subtask 超過 30 min wall-time | 標 rework + 跳下一條(防 stuck loop) |
| 整 mission 超過 token cap | run-end 中止 `exit 3` |
| Backend reaper 偵測 Python 沒輸出 | Heartbeat thread 每 20s 寫 stderr + log + 觸 lock 防 reap |
| 另一 runner 已在跑同 project | PID lock 拒絕啟動;stale lock (>60s 沒 heartbeat) 自動接管 |
| 中途被 kill | atomic write 確保 manifest/state/cache/ledger 不毀;`mission resume` 從 disk 重建 |

---

## 12. 觀測

每個 mission run 在 `<project_dir>` 產生:

```
<project>/
├── .harness-state.json       # provider quota 累積(atomic write)
├── .harness.lock             # PID lock + heartbeat timestamp
├── .cache/<hash>.txt         # response cache
├── artifacts/
│   ├── ledger.json           # 結構化 cross-subtask memory(v0.3.5)
│   ├── T-XX.worker.md
│   ├── T-XX.worker.after-directive.md       # escalation 後重試
│   ├── T-XX.validator.attempt1.md
│   ├── T-XX.validator.attempt2.md
│   ├── T-XX.orchestrator-resolve.md         # escalation 處理紀錄
│   ├── T-XX.disk-diff-reject.md             # disk-diff guard 紀錄(v0.3.4)
│   └── T-XX.disk-verify-reject.md
└── run.log.jsonl              # 完整事件流(每行一個 JSON event)
```

事件類型(v0.3.6 完整):
- Worker: `worker-start` / `worker-done` / `worker-retry-done` / `worker-escalated` / `worker-resume-after-directive`
- Validator: `validator-config` / `validator-start` / `validator-done` / `validator-pass` / `validator-reject`
- Files: `files-applied` / `files-write` / `files-skip-identical`
- Disk guard: `disk-verify-reject` / `disk-diff-regression` / `disk-diff-reject`
- Orchestrator: `escalate` / `orchestrator-fix-start` / `orchestrator-fix-done` / `manifest-patched`
- Parallel: `parallel-batch-start` / `parallel-batch-done` / `parallel-thread-crash` / `parallel-exception`
- Mission: `subtask-failed` / `mission-token-cap-exceeded` / `run-end` / `heartbeat` / `trajectory` / `trajectory-cap-exceeded`

CLI 查詢:
```bash
mission dashboard <project>    # 一次性快照
mission watch <project>        # 全螢幕 live
mission console <project>      # 互動式 TUI
mission tail <project> -n 30   # 最近 N 個事件
mission status [--detailed]    # provider 配額狀態
mission reset [target]         # 清 quota
mission resume <project>       # 診斷 + 安全清 stale lock
mission skills list / install-seeds
```

---

## 13. 框架穩定性安全網(長時間運行為設計目標)

長 mission(5+ 小時、50+ subtask)能跑完不爛資料,靠 10 條防線疊起來:

1. **PID lock** — 防多 runner 並發
2. **Atomic write** — manifest / state / cache / ledger 都 tmp+rename
3. **Heartbeat 20s** — 防 background reaper 砍進程
4. **Circuit breaker** — provider 連 3 次 transient fail 自動停用
5. **Subtask wall-time cap (30 min)** — 防 worker 卡死燒 token
6. **Token cap (10M default)** — 防整場 mission 失控,`--max-tokens` 可調
7. **Disk-diff guard** — worker 砍檔/塞 stub 立刻 reject
8. **Mission ledger** — 結構化跨 subtask 記憶 + 斷點續跑
9. **Parallel ThreadPool** — readonly-parallel ≤4 個同時跑,4 個 locks 守護共享狀態
10. **Resume CLI** — 任何時刻被砍都能 `mission resume` 從 disk 重建狀態繼續

### TDD 覆蓋

| 套件 | 測試數 | 涵蓋 |
|---|---|---|
| `test_gemini_parity.py` | 34 | Gemini CLI 對齊 Claude/Codex 能力(parser/env/error/command/integration) |
| `test_mission_ledger.py` | 15 | 結構化 handoff 解析 / 累積 / 持久化 / resume |
| `test_disk_diff_guard.py` | 11 | 砍檔/縮水/符號流失/critical export/stub marker 偵測 |
| `test_parallel_scheduling.py` | 14 | `_schedule_next_batch` 決策(mixed scenarios / dep blocking / cap) |
| `test_files_to_write.py` | — | FILES_TO_WRITE parser 邊界 |
| **合計** | **74+** | 安全網 + provider 能力 + scheduler 都有測試守 |

---

## 14. 已實戰驗證的真實任務範例

`C:\Users\User\Desktop\mission framework 面板\` 是這套框架第一個成功完成的真實任務:仿 Star-Office-UI 的 Phaser 像素 dashboard,後續累積 5 個 mission:

```
mission-living-office   6 subtasks  全 4 個 provider 都上場
mission-blackboard      6 subtasks  Sonnet/Opus/Minimax/Codex,黑板+貓動畫
mission-polish          5 subtasks  HUD + 思考泡 + 煙火 + ticker
mission-gemini          3 subtasks  Gemini smoke(production 暴露 rewrite bug → 觸發 disk-diff guard 補強)
```

Dashboard 跑起來會看到:
- 4 隻 provider 貓在工位上 idle bobbing
- 黑板顯示 todo/in-progress 任務卡
- worker-start → 對應貓 tween 走到黑板拿卡 → 回工位播 working 動畫
- worker-done → 卡飛右上 ✓ 完成堆 + 粒子煙火
- 頂部即時時鐘、底部進度條 + event ticker

---

## 15. Skill Library

`~/.mission/skills/` 內每個 `.md` 檔是一條可重用 pattern。Orchestrator 與 Worker 啟動時自動 keyword-search 相關 skills 注入 prompt。

CLI:
```bash
mission skills list             # 列已安裝
mission skills install-seeds    # 裝 framework 內建的 seed skills
```

內建 seeds(`harness/skills_seed/`):
- `claude-cli-headless.md` — Claude headless 正確 invocation
- `minimax-tool-calling.md` — OpenAI 標準 tool calling
- `flask-stdlib-json-server.md` — dashboard backend pattern

寫新 skill 直接放進 `~/.mission/skills/`,下次 mission 自動受益。

---

## 16. Milestones 階層

長 mission(> 8 subtasks)可用 milestones 分組:

```json
{
  "mission": "...",
  "milestones": [{"id": "M1", "desc": "..."}],
  "subtasks": [{"id": "T-01", "milestone_id": "M1", ...}]
}
```

Dashboard 自動畫 milestone section header。短 mission 不用標,向後相容。

---

## 17. 寫新 mission 的 checklist

1. **建專案資料夾**(任意位置)
2. 用 [0-contract.md](harness/prompts/0-contract.md) prompt 找任一 LLM 寫 `contract.md`(12+ 條二元 AC)
3. 用 [1-orchestrator.md](harness/prompts/1-orchestrator.md) prompt + Opus 寫 `manifest.json`:
   - 每個 subtask 一個動作(不是驗收項)
   - 標難度 `T1`/`T2`/`T3`
   - **標 `execution: "readonly-parallel"` 或 `"serial"`**(v0.3.6 必填)
   - 標 `needs_validator: true`/`false`
   - 標 `covers: ["AC-N", ...]`
   - 標 `depends_on: [...]`
4. `mission run path/to/manifest.json`
5. 另開視窗 `mission console <project>` 看進度
6. 跑完後啟動產出物驗證

---

## 18. 警告:這些事不要做

- ❌ 用 `--system-prompt`(REPLACE)給 Claude Worker — 會刪掉 agent-loop default,模型退化成 chat
- ❌ 把長 prompt 透過 argv 傳給 Claude / Gemini(Windows 8191 字限制)— 用 stdin
- ❌ 對 Gemini 用 `--yolo`(已 deprecated)或 `--approval-mode plan`(Windows 上壞掉)
- ❌ 把需 shell-copy 的任務標成 T1 + 不提供 `run_shell` 工具(Minimax 會生 placeholder)
- ❌ Manifest 漏掉 `needs_validator: true` 對重要任務(會跳過驗證,worker 一回就標 done)
- ❌ Validator 不給 cwd(它看不到 worker 寫的檔,只能基於 worker_output 文本判)
- ❌ 在 worker SOUL 寫超過 2000 字 — Claude 對長 prompt 不友善,會傾向退化成 chat
- ❌ **讓 T1 Minimax 編輯 LAYOUT.js / config.py 等多 key 複雜檔**— 易 rewrite。disk-diff guard 會 catch,但浪費一輪
- ❌ **manifest 全部串行** — 讀檔/查詢/測試類就該標 `readonly-parallel`,不然 50 subtask mission 跑死人
- ❌ **standlone Gemini 當 T2/T3 Worker primary** — 跟 Minimax 同樣有 Edit-rewrite 問題;留 backup 即可

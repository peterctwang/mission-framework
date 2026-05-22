# mission-framework — Specification

> 這份文件是經過實戰驗證的規格,涵蓋路由模型、provider 行為、escalation 協議、安全網與已知陷阱。
> 第一次跑這個框架的人讀這份就夠;深入細節讀 [DESIGN.md](DESIGN.md);採坑紀錄讀 [LESSONS.md](LESSONS.md);改版紀錄讀 [CHANGELOG.md](CHANGELOG.md)。
>
> **v0.3.0(本版)新增**:two-tier Validator(scrutiny + functional)、fix-features 模式、test-first 紀律、trajectory caps、milestones 階層、skill library、Mission Control UI。設計向 Factory Missions 對齊。

---

## 1. 角色與職責

| 角色 | 由誰執行 | 職責 | 不做什麼 |
|---|---|---|---|
| **Orchestrator** | 人類 + Opus 輔助 | 寫 contract.md 與 manifest.json,定難度 (T1/T2/T3),處理 Worker escalate | 不寫程式碼 |
| **Worker** | 視難度路由 | 實作一個 subtask,用工具寫檔 + 輸出 FILES_TO_WRITE | 不審查、不擴大範圍 |
| **Validator** | Codex (固定) | 對照 AC 給判決 通過/打回 | 不修程式碼 |

---

## 2. 路由模型(經實測確認)

### Worker by Difficulty

```python
WORKER_TIERS = {
    "T1": [Minimax, Sonnet, Codex, Opus],   # 例行 → 便宜
    "T2": [Sonnet,  Opus,   Codex, Minimax],# 標準 → 均衡
    "T3": [Opus,    Sonnet, Codex, Minimax],# 困難 → 最強推理
}
```

### Orchestrator(主規劃師)
```python
ORCHESTRATOR_CHAIN = [Opus, Codex, Sonnet, Minimax]
```

### Validator(固定)
```python
VALIDATOR_CHAIN = [Codex, Minimax, Sonnet, Opus]
```

**為什麼 Codex 是 Validator 第一順位**:它對「short directive prompts → structured verdict output」的服從性最高,不會像 Claude `-p` 偶爾退化成 chat 模式。Codex CLI exec 模式專為這種 single-shot 任務設計。

### 跨模型多樣性(自動強制)

`_ensure_diverse(worker, validator)` 在每次組對前檢查 —— 同 provider + 同模型一定拋錯。Validator 鏈會自動跳過撞型的選項。

---

## 3. 各 Provider 能力矩陣(實測)

| 能力 | Claude (Opus/Sonnet) | Codex | Minimax M2.5 | Gemini |
|---|---|---|---|---|
| 短 directive prompt → structured output | ⚠️ 需 `--append-system-prompt` | ✅ 完美 | ✅ 完美 | ❌ 上游 bug |
| Worker 寫檔(tool use) | ✅(需正確 invocation) | ✅ | ✅(我們補的 OpenAI tools 通道) | ❌ |
| Shell 操作(cp/mv/mkdir) | ✅ acceptEdits 自動允許 | ✅ workspace-write | ✅(我們補的 `run_shell` 工具) | ❌ |
| 在 cwd 內精確寫檔 | ⚠️ Sonnet 偶爾寫到怪路徑 | ✅ | ✅ | ❌ |
| Long context 處理(>10k tokens) | ✅ | ✅ | ✅(會自動 shrink) | — |
| 輸出 usage(token 統計) | ✅ | ✅ | ✅ | ❌ 永遠 0/0 |

**結論**:Gemini 暫時不在預設 chains。Claude 完全可用但 invocation 要對。

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

### Gemini CLI(暫時不用)

頭痛問題:`-p` headless 模式即使加 `--approval-mode plan` 仍會跑 tool calls + 回 `INVALID_STREAM`。等 Google 修。

---

## 5. Worker 輸出格式(SOUL 規範)

Worker 必須輸出 **3-4 段**:

```markdown
## Implementation
<簡述做了什麼,fenced code block 標檔名>

## FILES_TO_WRITE
### relative/path/file.py
\`\`\`python
<完整檔案內容,不是 diff>
\`\`\`

### nested/dir/file.html
\`\`\`html
...
\`\`\`

### oldfile.txt (DELETE)
\`\`\`
\`\`\`

## Handoff
1. 完成了什麼(一句話)
2. 改了哪些檔案
3. 給下一個 Worker 的提醒
(≤ 200 字)
```

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

深度限制:同 subtask 最多 escalate **1 次**。

---

## 7. Validator 驗證契約

判決行**必須**是 `判決:通過` 或 `判決:打回`(逐字,半/全形冒號都接受)。Runner 用 regex 嚴格匹配。

Validator 必須在 **cwd=project_dir** 下執行(從 disk 看 Worker 寫的真實檔案)。

### Two-tier Validators(v0.3 新增)

每個 subtask 可選 `validation_kind`:

| `validation_kind` | 行為 | 適用 |
|---|---|---|
| `scrutiny`(預設) | 讀 code、對照 AC、找隱藏假設 / 邊界 case | 純程式邏輯 |
| `functional` | 用 Bash 工具**真的執行**系統(`curl` / `python` / `pytest`)觀察輸出 | UI / API / 服務、契約有可執行驗證的任務 |

SOUL 各自獨立 —— [`validator-functional.md`](harness/souls/validator-functional.md) 明確指示「不要讀 code 下判決,實際跑」。

### Fix-features pattern(v0.3 新增)

Validator reject 2 次後,**不再盲目升難度重試**,改呼 Orchestrator(Opus)決定:
- **DIRECTIVE** —— 一段精準指令給下次 Worker
- **REPLAN with `split_into`** —— 拆出 `T-XX-fix` 乾淨 context 子任務

依據 Factory Missions 原則:「Validator 只指出問題,Orchestrator 排程修法」 —— 同一個失敗 context 的 Worker 不適合自己救自己。

## 7a. Trajectory caps(v0.3 新增)

每個 role 的 agent-loop turn 上限:

```python
TURN_CAPS = {
    "worker": 80,
    "validator": 50,
    "orchestrator": 30,
}
```

超過 → 紀錄 `trajectory-cap-exceeded` 事件(早期偵測 stuck workers,Factory reference 數據:median 51 turns / impl)。

---

## 8. 失效模式 → 自動處理

| 失效訊號 | 框架行為 |
|---|---|
| Provider `QuotaExhausted` 例外 | 標 exhausted(6h cooldown),沿 chain 走下一順位 |
| Validator reject 2 次 | escalate `default → escalation_profile`(或自動升難度 T1→T2→T3) |
| Validator reject 3 次 | 標 `rework`,run 中止 `exit 2` |
| Worker output 含 `## ESCALATE_TO_ORCHESTRATOR` | 暫停,呼叫 Orchestrator 決策後重試 |
| 同 system+user hash 再次出現 | Response cache 命中,跳過 LLM call(retry 時 use_cache=False) |
| Worker 沒用 Write tool 但 emit 了 FILES_TO_WRITE | runner 落檔 |

---

## 9. 觀測

每個 mission run 在 `<project_dir>` 產生:

```
<project>/
├── .harness-state.json       # provider quota 累積
├── .cache/<hash>.txt         # response cache
├── artifacts/
│   ├── T-XX.worker.md
│   ├── T-XX.worker.after-directive.md   # escalation 後重試
│   ├── T-XX.validator.attempt1.md
│   ├── T-XX.validator.attempt2.md
│   └── T-XX.orchestrator-resolve.md     # escalation 處理紀錄
└── run.log.jsonl              # 完整事件流(每行一個 JSON event)
```

事件類型:`worker-start` / `worker-done` / `worker-retry-done` / `worker-escalated` / `validator-start` / `validator-done` / `validator-pass` / `validator-reject` / `escalate` / `manifest-patched` / `files-applied` / `files-write` / `files-skip-identical` / `subtask-failed` / `run-end`。

CLI 查詢:
```bash
mission dashboard <project>    # 一次性快照
mission watch <project>        # 全螢幕 live
mission console <project>      # 互動式 TUI
mission tail <project> -n 30   # 最近 N 個事件
mission status                 # provider 配額狀態
mission reset                  # 清 quota
```

---

## 10. 已實戰驗證的真實任務範例

`C:\Users\User\Desktop\mission framework 面板\` 是這套框架第一個成功完成的真實任務:仿 Star-Office-UI 的 Phaser 像素 dashboard,6 個 subtask,全 4 個 provider 都上場過。

```
T-01 (T1) Minimax  → 用 run_shell 複製 16 個 sprite + font + Phaser vendor
T-02 (T1) Minimax  → 寫 Flask backend(stdlib + flask)
T-03 (T2) Sonnet   → frontend/index.html(Codex 一次過驗證)
T-04 (T2) Sonnet   → frontend/layout.js(LAYOUT 物件 + 4 個 provider 座標)
T-05 (T3) Opus     → frontend/game.js(Phaser scene + polling + sprite 動畫)— 65,664 token 輸出
T-06 (T1) Minimax  → README + start.bat
```

驗證後啟動:`python backend/app.py --port 19002`,瀏覽器開 `http://127.0.0.1:19002`,看到 1280×720 像素辦公室即時 poll 框架自己的狀態。

---

## 10a. Skill Library(v0.3 新增)

`~/.mission/skills/` 內每個 `.md` 檔是一條可重用 pattern。Orchestrator 與 Worker 啟動時自動 keyword-search 相關 skills 注入 prompt。

CLI:
```bash
mission skills list             # 列已安裝
mission skills install-seeds    # 裝 framework 內建的 seed skills
```

內建 seeds(`harness/skills_seed/`):
- `claude-cli-headless.md` —— Claude headless 正確 invocation
- `minimax-tool-calling.md` —— OpenAI 標準 tool calling
- `flask-stdlib-json-server.md` —— dashboard backend pattern

寫新 skill 直接放進 `~/.mission/skills/`,下次 mission 自動受益。

## 10b. Milestones 階層(v0.3 新增)

長 mission(> 8 subtasks)可用 milestones 分組:

```json
{
  "mission": "...",
  "milestones": [{"id": "M1", "desc": "..."}],
  "subtasks": [{"id": "T-01", "milestone_id": "M1", ...}]
}
```

Dashboard 自動畫 milestone section header。短 mission 不用標,向後相容。

## 11. 寫新 mission 的 checklist

1. **建專案資料夾**(任意位置)
2. 用 [0-contract.md](harness/prompts/0-contract.md) prompt 找任一 LLM 寫 `contract.md`(12+ 條二元 AC)
3. 用 [1-orchestrator.md](harness/prompts/1-orchestrator.md) prompt + Opus 寫 `manifest.json`:
   - 每個 subtask 一個動作(不是驗收項)
   - 標難度 `T1`/`T2`/`T3`(T1 = 無 fs/shell 限制,只能 Minimax write_file/run_shell;T2 = Sonnet 寫 code;T3 = Opus 主架構)
   - 標 `needs_validator: true`(複雜任務)/`false`(README/configs 等簡單任務)
   - 標 `covers: ["AC-N", ...]`
4. `mission run path/to/manifest.json`
5. 另開視窗 `mission console <project>` 看進度
6. 跑完後啟動產出物驗證

---

## 12. 警告:這些事不要做

- ❌ 用 `--system-prompt`(REPLACE)給 Claude Worker —— 會刪掉 agent-loop default,模型退化成 chat
- ❌ 把長 prompt 透過 argv 傳給 Claude(Windows 8191 字限制)
- ❌ 把需 shell-copy 的任務標成 T1 + 不提供 `run_shell` 工具(Minimax 會生 placeholder)
- ❌ Manifest 漏掉 `needs_validator: true` 對重要任務(會跳過驗證,worker 一回就標 done)
- ❌ Validator 不給 cwd(它看不到 worker 寫的檔,只能基於 worker_output 文本判)
- ❌ 在 worker SOUL 寫超過 2000 字 —— Claude 對長 prompt 不友善,會傾向退化成 chat

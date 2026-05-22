# Harness 優化設計說明

> 這份檔記錄為什麼這樣設計,讓你之後改的時候知道哪些決策是有意的。

## 1. 三層 Prompt 結構(token / cache 核心)

每次呼叫 LLM,system prompt 由穩定到動態分四層拼接:

```
┌───────────────────────────────────────────────────────────┐
│ Layer 1: SOUL.md        (harness/souls/<role>.md)         │  穩定
│   - 角色身份、紀律、輸出格式、失敗模式                       │  ← prompt cache 命中
│                                                            │
│ Layer 2: TEMPLATE       (harness/prompts/<n>-<role>.md)   │  半穩定
│   - 當前 call 的任務框架                                    │
├───────────────────────────────────────────────────────────┤  ← system / user 分界
│ Layer 3: CONTRACT       (動態組裝)                          │  半穩定
│   - Worker:只給 covers 指向的 AC 條目(_relevant_contract)│
│   - Validator:整份契約                                     │
│                                                            │
│ Layer 4: DYNAMIC        (subtask JSON + handoff)           │  完全動態
└───────────────────────────────────────────────────────────┘
```

**為什麼這樣分**:Claude / Codex 的 prompt cache 從 prefix 開始匹配。SOUL 永不變,template 偶爾調,把它們疊在最前面,**同一個 mission 內第二個 subtask 起的 system prompt 命中率 > 70%**(因為 SOUL + Template 是同一份)。

## 2. SOUL.md(角色靈魂)

每個角色一份不變的「人格與紀律」。包含:

- **身份**:你是誰、不做什麼
- **核心紀律**:5 條左右的可執行原則
- **輸出格式**:精確的結構規範
- **失敗模式**:列出常見錯誤讓 LLM 自我規避

**SOUL 與 TEMPLATE 的分工**:
- SOUL = 角色「永久法則」,跨 mission 不變
- TEMPLATE = 此 role 在這次 call 的「任務輸入框架」

兩者刻意分開,讓 SOUL 容易進 cache、TEMPLATE 容易為特殊任務臨時微調。

## 3. AC 條目擷取(Worker 端 token 殺手鐧)

`_relevant_contract(contract, covers)`:解析契約裡的 `AC-XX` 標記,**只把該 subtask 負責的 AC 條目給 Worker**。

效果:多 subtask mission(例如 10 個 subtask、20 條 AC),原本 Worker 每次都吃整份契約 → 現在只吃自己負責的 2-3 條,輸入 token **平均省 60-80%**。

Validator 仍看完整契約 —— 它要找的就是「Worker 是否漏了什麼」,必須有全域視野。

## 4. 回應快取(`.cache/`)

`hash(provider + model + system + user)` → 輸出文字。同樣輸入下次直接讀檔案,跳過 LLM 呼叫。

**何時有用**:
- 你重跑同一個 manifest(改 bug、補環境變數)
- 同一 subtask 被多次 retry 但內容沒變(provider 切換才會打破 cache key)

**何時無用**(刻意):
- Validator 打回後 Worker 重做 —— 因為 handoff / 前次失敗訊息會改變 user prompt,自然繞過 cache
- 切到 escalation profile —— provider 名稱進 hash key,自動換 key

## 5. 每角色 token budget

```python
TOKEN_BUDGETS = {
    "worker": 8192,         # 可能要寫程式碼 + 測試 + handoff
    "validator": 2048,      # 判決 + 列表,短就好
    "orchestrator": 4096,   # Manifest JSON + 文字計畫
}
```

Validator 砍到 2048 是有意的:它的工作是逐項對照 + 列點,**長 = 多廢話 = 多虛假發現**。短的判決會更聚焦。

## 6. Handoff 強制 200 字截斷

SOUL 已要求 ≤ 200 字,但 LLM 不一定遵守。`_extract_handoff` 在 runner 端硬截斷,避免 handoff 累積污染下游 Worker 的 context。

## 7. 判決解析(機器可讀)

Validator SOUL 要求最後一行**逐字精確**為 `判決:通過` 或 `判決:打回`。runner 用 regex 嚴格匹配:

```python
_VERDICT_RE = re.compile(r"判決\s*[:：]\s*(通過|打回)\s*$")
```

允許半形/全形冒號、容許前後空白,但**不接受其他變體**。模糊時 fallback 到關鍵字掃描(tail 3 行),仍然無法判定 → 視為打回(保守)。

## 8. 跨模型紀律(自動強制)

`_ensure_diverse(worker, validator)` 在每次組對前檢查 —— 同 provider + 同模型一定拋錯。

升級到 escalation profile 後也會重新檢查,因為新 worker 可能與 validator 撞型。

## 9. 沒做什麼(刻意不做)

- ❌ **Token 計費追蹤** —— 訂閱模式下不直接計費,加了也只是徒增複雜度
- ❌ **並行 readonly-parallel** —— manifest schema 有欄位但 runner 不用。Worker 預設序列執行,順序錯了比效能差更糟
- ❌ **Pattern library** —— patent-ai 用得到(專利寫作風格庫),通用 harness 不需要
- ❌ **Memory 系統** —— 留給上層應用決定,harness 本身保持無狀態

## 10. 難度路由模型(實戰確認版)

Worker 由 **subtask 難度**而非 capability profile 路由 —— Orchestrator 打的標籤直接決定算力:

```
T1 (例行)  →  Minimax-M2.5     便宜 / 快 / 有 tools(write/read/list/run_shell)
T2 (標準)  →  Claude Sonnet 4.6  agentic / 寫 code 主力
T3 (困難)  →  Claude Opus 4.7    最強推理,留給架構級任務
```

每層都有 4 個 fallback。Validator 固定 Codex 為主 —— 它對 directive prompt 服從性最高。

詳見 [SPEC.md §2](SPEC.md) 與 `harness/config.py`。

## 11. Worker → Orchestrator Escalation 協議

Worker 卡到全局決策 → emit `## ESCALATE_TO_ORCHESTRATOR` JSON block → runner 自動呼叫 Orchestrator (Opus) → Opus 回 `DIRECTIVE` / `REPLAN` / `PROCEED_AS_IS`。同 subtask 最多 escalate 1 次(防無限迴圈)。

設計細節在 [SPEC.md §6](SPEC.md)。

## 12. FILES_TO_WRITE Safety Net

不管 Worker 有沒有用 Write tool,**都要**在輸出末段附 `## FILES_TO_WRITE` 區塊(每個檔一個 `### path` 標題 + fenced code block)。Runner 解析後落檔到 cwd。

**為什麼需要**:
- Minimax 不熟 tool calling 時會 emit code blocks(現在加了 tool 通道,但保險)
- Claude Sonnet 在某些 path 下宣稱寫了實際沒寫(Windows 路徑 normalization bug)
- 給 Validator 一個明確的「應該存在哪些檔」清單

實作在 `runner.py::_apply_files_to_write`,路徑 traversal 安全檢查(拒絕 `..` / 絕對路徑 / drive letter)。

## 13. 沒做什麼(刻意不做)

- ❌ **Token 計費追蹤** —— 訂閱模式下不直接計費
- ❌ **並行 readonly-parallel** —— manifest schema 有欄位但 runner 預設序列。順序錯了比效能差更糟
- ❌ **Pattern library** —— 通用 harness 不需要
- ❌ **Memory 系統** —— 留給上層應用決定
- ❌ **Gemini in default chains** —— headless mode 有上游 bug(見 LESSONS #8),等 Google 修

## 14. 之後想優化的方向

- 真正的 prompt cache 觀測:Claude 的 `usage.cache_read_input_tokens`、Codex 的 `cached_input_tokens` 落到 log
- Validator JSON schema 強制:用 Codex `--output-schema` 把判決結構化,廢掉字串解析
- `readonly-parallel` 真實並行:`concurrent.futures` 跑非依賴 subtask
- Provider 自動退化偵測:若 Worker 連 2 次 token=0/0,框架自動跳下個 provider(不必等 validator reject)
- Auto-load `CLAUDE.md` / `AGENTS.md` 干擾防護:Worker 在 project_dir 前先檢查是否有殘留檔

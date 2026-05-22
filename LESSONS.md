# LESSONS — 採坑紀錄(實戰學到的事)

> 這份檔記錄 framework 從「概念可行」走到「真實能跑」每一個坑。
> 下次跑新專案前先看這份,**省下 90% 的 debug 時間**。

---

## 坑 #1:Claude `--system-prompt` 會刪掉 agent-loop 預設指令

**症狀**:Claude 回 `"I'll wait for your request. What would you like to work on?"`,`num_turns=1`,零工具呼叫,零 token 統計。

**根因**:`--system-prompt` 是 **REPLACE** flag,會把 Claude Code 內建的「act on user request」default system prompt 整段丟掉。我們的 SOUL 沒重新教 Claude 動工(只列了「不要 X」「不要 Y」),模型就退化成 chat 模式禮貌等指令。

**正解**:
```python
# 對的
cmd += ["--append-system-prompt", soul]

# 錯的
cmd += ["--system-prompt", soul]
```

**文件依據**:[claude-code/docs/headless](https://code.claude.com/docs/en/headless)
> "Use a REPLACEMENT flag when the surface, identity, or permission model differs… Replacing **drops all of the default prompt, including tool guidance and safety instructions**."

**怎麼自己沒看到**:smoke test 用了非常短的 imperative prompt(`"Create hello.txt"`),Claude 在沒有 agent-loop 指令時還能猜出意圖。但 SOUL + 結構化 JSON subtask 就失效了。**小規模測試會誤導**。

---

## 坑 #2:Windows argv 8191 字截斷

**症狀**:長 prompt(SOUL + contract + subtask JSON)被靜默截斷,Claude 看到的是不完整輸入,回 `"It looks like your message got cut off"`。

**根因**:Windows CMD 的單一 argv 上限是 8191 字。Python `subprocess` 不會警告,直接截斷。

**正解**:user prompt 走 stdin 不走 argv。
```python
cmd = [binary, "-p", ...]  # NO positional prompt
subprocess.run(cmd, input=user_prompt, ...)
```

**怎麼自己沒看到**:smoke test 用了短 prompt,argv 沒到 8191 字所以沒截。

---

## 坑 #3:Validator 沒 cwd → 一律 reject

**症狀**:Worker 真的寫了 `frontend/index.html`(10KB),但 Validator(Codex)說「工作目錄內找不到 frontend/index.html」,連續 reject 3 次直到 mission failed。

**根因**:Validator 跑在 Python subprocess 的 default cwd(框架資料夾),不是專案資料夾。Codex 看不到 Worker 在 `<project>/frontend/` 寫的檔。

**正解**:runner 呼叫 validator 時也要傳 cwd:
```python
v_text, _, _ = _call_with_failover(
    validator_chain, ...,
    cwd=str(project_dir),  # ← 這行
)
```

**怎麼自己沒看到**:Validator 上次只用 Minimax(HTTPS API,沒有 fs 通道),verdict 純靠 worker_output 文本,不需要看 disk。換成 Codex 後它會 `ls cwd` 找檔,沒給對 cwd 就掛。

---

## 坑 #4:Minimax 沒傳 `tools=[...]` 會吐 XML 給你

**症狀**:Minimax worker artifact 內容是 `<minimax:tool_call><invoke name="Read">...</invoke></minimax:tool_call>`,零檔案寫出。

**根因**:Minimax M2.5 想用 tool,但我們 request 沒宣告 `tools` schema。hosted endpoint(`api.minimax.io`)只在 request 帶 `tools` 時才會解析 XML 回標準 `tool_calls`,否則 XML 留在 content 內。

**正解**:
```python
body = {
    "model": "MiniMax-M2.5",
    "messages": [...],
    "tools": [...],         # OpenAI 標準 function schema
    "tool_choice": "auto",
}
# 收到 response → 若有 tool_calls,執行 → role:"tool" 結果送回 → loop
```

**文件依據**:[MiniMax-M2 tool_calling_guide.md](https://huggingface.co/MiniMaxAI/MiniMax-M2/blob/main/docs/tool_calling_guide.md)

---

## 坑 #5:Minimax 沒 `run_shell` 通道 → T1 shell-copy 任務生 placeholder

**症狀**:T-01 "從 Star-Office-UI/ 複製 sprite + font + vendor 過來" → Minimax 寫了 `frontend/assets/README.md`(寫著「此檔將會被取代」)與 `frontend/vendor/placeholder.md`,真正 sprite 沒過來。

**根因**:Minimax 的 tools 只有 `write_file/read_file/list_dir` 時,**沒法做 shell copy**。它只能 emit 新檔內容,不能讀 Star-Office-UI 既有檔案複製。所以它生 placeholder 文檔告訴後人「這裡應該要有 X」。

**正解**:給 Minimax 加 `run_shell` 工具(白名單 verb:`cp/mv/mkdir/ls/find/rm/echo/cat`),路徑安全檢查。

```python
TOOLS_SCHEMA.append({
    "type": "function",
    "function": {
        "name": "run_shell",
        "parameters": {"command": "string"},
    },
})
_ALLOWED_SHELL_VERBS = {"cp", "mv", "mkdir", "ls", "find", "rm", "echo", "cat"}
```

**比 Minimax 限制更深的設計教訓**:**Orchestrator SOUL 應該知道 T1=Minimax 沒 shell**,把需要 shell 的任務標 T2+ 才合理。但我們現在透過給 Minimax shell 工具,把這條約束放寬了。

---

## 坑 #6:Manifest 漏 `needs_validator` 等同跳過驗證

**症狀**:Worker 回 `"I'll wait for your request"`(零工作),但 subtask 直接標 `done` 進下一個。

**根因**:Runner 邏輯:
```python
if not subtask.get("needs_validator", False):
    subtask["status"] = "done"
    continue
```
預設 `False`。Manifest 忘了寫 `needs_validator: true` → 完全跳過驗證 → Worker 廢回應也被當成功。

**正解**:Orchestrator SOUL 必須強調 `needs_validator: true` 是預設,並列出哪些情況才能 `false`(純文字 README、configs 不關鍵的)。

---

## 坑 #7:Cache 命中讓 retry 卡死

**症狀**:Worker 第一次回 "I'll wait",Validator reject。Worker 第二次嘗試 → cache 命中 → 回**同樣的** "I'll wait"。第三次 escalate 也撞 cache。失敗。

**根因**:Cache key = `hash(provider+model+system+user)`。retry 時這四個都沒變 → 同 key → 命中。

**正解**(已實作):
```python
# Retry 時關閉 cache
_call_with_failover(..., use_cache=False)

# 並把 validator 反饋拼到 user prompt(同時自然改 cache key 又給 Worker 新資訊)
retry_user = f"{worker_user}\n\n## Previous attempt REJECTED\n{validator_text}\n\nAddress every item above."
```

---

## 坑 #8:Gemini CLI headless mode 在 Windows 上壞掉

**症狀**:不論 `--approval-mode plan`(只讀)、`--yolo`、prompt 多明確,Gemini 都會跑 20+ tool calls(write_file、run_shell_command、google_web_search),最後回 `"response": ""` + `INVALID_STREAM` 錯誤。

**根因**:Google 官方 CLI 在 headless / non-interactive 模式有 bug,`--approval-mode plan` 被忽略。

**正解**:**從預設 chains 移除**。註解寫清楚等 Google 修。我們已驗證這不是我們的 wrapper 問題:在 clean cwd + 短 prompt 也一樣。

**附帶坑**:Gemini token usage 全 0/0 — `usage` 欄位即使在 plan 模式也不回傳,別依賴它。

---

## 坑 #9:Sonnet 在 Windows + Temp 路徑寫到錯地方

**症狀**:Sonnet 宣稱 `"hello.txt has been created at /tmp/test/hello.txt"`,但 `C:\Users\User\AppData\Local\Temp\test\` 沒檔。實際檔案在 `C:\tmp\test\hello.txt` 或 `C:\home\user\hello.txt`。

**根因**:Sonnet 在 Windows 上對 cwd 路徑做了 Unix-style normalization(`Temp` → `/tmp`、`Users/User` → `/home/user`),然後 Windows 解析這個 Linux 風路徑變成完全不同的位置。

**正解**:把 cwd 設在**非 Temp / 非 Users\<name>** 的路徑(例如 `C:\Users\User\Desktop\<project>` 就不會踩到 — `Desktop` 沒被 normalize)。**Framework user 的專案資料夾原則上都該在 Desktop 或自訂位置,不要在 Temp**。

---

## 坑 #10:Codex ChatGPT 訂閱拒絕所有顯式 `-m`

**症狀**:`codex exec -m gpt-5 ...` → API 回 400 `"The 'gpt-5' model is not supported when using Codex with a ChatGPT account."`

**根因**:ChatGPT 訂閱認證下,codex 只允許帳號層級預設模型,不接受任何 `-m` 覆寫。我們測了 13 種候選名稱(`gpt-5-codex` / `o3` / `gpt-4.1` 等)全被拒。

**正解**:`model=None`,不傳 `-m`。Codex CLI 自己挑訂閱方案內最強模型。

```python
# CodexCLI.__init__
def __init__(self, model: str | None = None):
    self.model = model  # leave None — subscription decides
```

---

## 坑 #11:Worker SOUL 太長 → Claude 退化

**症狀**:SOUL + template = 3757 字時,即使 `--append-system-prompt` 用對,Claude 也偶爾回 "I'll wait"。

**根因**:長 prompt 增加「這像是 documentation」的可能性,Claude 不確定要不要立即動工。

**正解**:Worker SOUL 控制在 **<1500 字**,以 imperative bullet 為主,失敗模式列在最後。詳細解釋移到 SPEC.md / DESIGN.md。

**驗證**:SOUL 從 3757 → 1215 字後,Claude 在 round 5 mission 一次過 T-03 + T-04 + T-05 共 5 個 Worker call。

---

## 坑 #12:CWD 內隱含的 CLAUDE.md / AGENTS.md auto-load

**症狀**:Claude 在某些 cwd 下行為怪異(包含我們的 project_dir 裡有 `Star-Office-UI/CLAUDE.md`)。

**根因**:Claude Code 預設會 walk up cwd 找 `CLAUDE.md` / `.claude/`,自動 inject context。我們的專案資料夾若有殘留檔可能污染。

**正解(待加)**:Worker 模式可以加 `--no-plugins`(若版本支援)避免 auto-load。或在 SPEC.md 提醒使用者:**專案資料夾根目錄不要放 CLAUDE.md / AGENTS.md(會被 auto-load)**。

---

## 坑 #13:Windows `subprocess.DEVNULL` 對 codex CLI 失效

**症狀**:`subprocess.run(cmd, stdin=subprocess.DEVNULL)` 在 Linux 直接傳 EOF,Codex 收到正常啟動。Windows 下相同呼叫 Codex 卡死 5+ 分鐘等 stdin。

**根因**:Windows 的 console stdin 行為與 *nix 不同。Codex CLI 從 cmd shim 啟動時,Python 給的 DEVNULL handle 在 .cmd → node.exe 的層層轉發後變成「stdin opened but no data」,觸發 Codex 的 stdin wait 模式。

**正解**:用 `shell=True` 並顯式重導 `< NUL`:
```python
cmd_str = f'"{binary}" exec ... - < "{prompt_path}"'
subprocess.run(cmd_str, shell=True, ...)
```

---

## 坑 #14:.env 的 BOM 讓 dotenv loader 抓不到 key

**症狀**:PowerShell `Out-File -Encoding utf8` 寫 .env 時加了 UTF-8 BOM,第一行 key 變成 `﻿MINIMAX_API_KEY`,`os.environ.get("MINIMAX_API_KEY")` 抓不到。

**正解**:loader 用 `utf-8-sig` 讀:
```python
for line in path.read_text(encoding="utf-8-sig").splitlines():
    ...
```

---

## 坑 #15:Rich console 在 Windows cp950 終端崩潰

**症狀**:`mission dashboard` 第一次跑掛掉:`UnicodeEncodeError: 'cp950' codec can't encode character '✓'`(✓ 符號)。

**根因**:Windows console 預設用 legacy code page(cp950 / cp1252),不認 Unicode 像素符號 ● ◐ ✓。

**正解**:CLI 啟動時強制 reconfigure stdout:
```python
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass
```

也設 `PYTHONIOENCODING=utf-8`(setup.ps1 已加到 User env)。

---

## 跨坑共通教訓

1. **`--bare` smoke test 會騙人**:單字 imperative prompt 看似 work,但複雜 SOUL + 結構化輸入會踩雷。**測試要用真實量級的 prompt**。

2. **官方文件警告要看**:Claude 文件明寫 `--system-prompt` 會刪 default。我先讀過、忽略過、走完 5 個失敗 round 後再回來才認。

3. **每個 provider 的 quirk 都是 cross-cutting**:Windows path handling、subprocess stdin、CLI flag semantics、tool calling protocol、cwd 預設 —— 任一錯都會讓表面看起來像「LLM 笨」實際是 wrapper bug。

4. **Token=0 是訊號不是噪音**:Claude 回 0/0 input/output 通常代表它沒真的進入 tool loop。如果 worker 宣稱 done 但 token=0,**幾乎必然**沒做事。

5. **Validator 也是 Worker**(從 cwd 看 disk)— 不能假設 Validator 純基於 worker_output 文本判決。**給它 cwd**。

---

## v0.3 新增的觀察(從 Factory Missions 對齊得到的)

### 16. 同 Worker 重試難以救自己,要換 context

連續被 Validator reject 後,**讓 Orchestrator 拆 fix-feature 出來**(乾淨 context 的新 Worker)比讓原 Worker retry 有效。Factory 自己的數據:34.4% 工作是 fix features。

### 17. Two-tier Validator 的盲區

`scrutiny` validator(讀 code)抓不到 runtime bug(missing dependency、wrong endpoint、HTTP 500 in production)。對任何「會被使用者跑起來」的 artifact,**配 `validation_kind: "functional"`** —— Codex 帶著 cwd 真的 `curl` / `python -m pytest` 才會發現。

### 18. Trajectory turn count 是早期 stuck 警報

Claude `num_turns` / Codex `turn.completed` 計數 / Minimax 我們自己數的 tool loop 輪數。Cap 設 80(Factory median 51),超過幾乎一定是 stuck loop。**比 timeout 早 5-10 分鐘觸發**,省 token + 早期介入。

### 19. Skill library 不靠魔法,靠紀律

`~/.mission/skills/` 目前是手動 curate + keyword search。**先別追自動萃取**,先確保每次踩坑後手動寫一條(像本文件 #1-15 一樣)。一年後累積 50+ 條,任何新 mission 都自動帶上歷史教訓。

### 20. Test-first 不是 ideology,是反「為了過 test 改 test」

Worker SOUL 強制 `FILES_TO_WRITE` 內測試檔在前、實作檔在後。順序定義意圖(by behavior)而非結果(by code)。後者會出現「測試剛好對應 implementation 細節 → 用 mock 過 test → 真實 user 行為失敗」的反向操作。

# Orchestrator 任務框架 — 處理 Worker 升級

SOUL 已定義你接收 escalation 的三條路徑(DIRECTIVE / REPLAN / PROCEED_AS_IS)。

下方使用者訊息提供:
- **Mission**:整個任務目標
- **Current manifest**:目前的子任務狀態
- **Subtask in question**:被卡住的那一個
- **Worker's ESCALATE block**:Worker 送回來的問題與 context
- **Validation contract (relevant section)**:該 subtask covers 的驗收項

執行流程:
1. 讀 Worker 的 `blocking_question` 與 `options_considered`
2. 對照 mission 全局判斷
3. 用一個 `## ORCHESTRATOR_DECISION` JSON 區塊回應
4. **不要寫額外的散文 / 道歉 / 解釋** —— 只有 JSON 區塊,runner 用程式解析

務必輸出有效 JSON,且 `action` 必為 `"DIRECTIVE"` / `"REPLAN"` / `"PROCEED_AS_IS"` 之一。

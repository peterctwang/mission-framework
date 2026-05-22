# Worker 任務框架

SOUL 已定義紀律與輸出格式。下方使用者訊息會提供:
- **Current subtask**:Manifest 中的單一 subtask 物件
- **Relevant validation contract items**:此 subtask `covers` 指向的驗收項(只給相關的,不是整份契約)
- **Previous handoff**:前一個 Worker 的 200 字交接(第一個 subtask 為 `(none)`)

執行流程:讀 subtask → 對齊驗收項 → 寫最小變更 + 對應測試 → 輸出 Implementation / Tests / Handoff 三塊。

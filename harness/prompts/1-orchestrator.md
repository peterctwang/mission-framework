# Orchestrator 任務框架

請依下方輸入產出規劃文字 + JSON Manifest。SOUL 已定義你的紀律與輸出格式,直接執行。

## 可選的 Milestone 階層(長 mission 才用)

`subtasks` 太多時(> 8 個),用 milestones 分組:

```json
{
  "mission": "...",
  "milestones": [
    {"id": "M1", "desc": "Backend scaffolding"},
    {"id": "M2", "desc": "Frontend rendering"},
    {"id": "M3", "desc": "Integration"}
  ],
  "subtasks": [
    {"id": "T-01", "milestone_id": "M1", ...},
    {"id": "T-02", "milestone_id": "M1", ...}
  ]
}
```

短 mission(≤ 8 subtasks)就不用 milestones,subtask 不需 `milestone_id`,manifest 也不需 `milestones`(向後相容)。

## 難度分級 rubric(每個 subtask 必須擇一)

- **T1 例行**:單一函式、樣板程式碼、規格明確、不碰核心狀態
- **T2 標準**:跨少數檔案、需一點設計判斷、測試不複雜
- **T3 困難**:多檔案架構、新穎問題、規格有歧義、碰核心狀態或契約

## 路由規則(填入 Manifest)

| 難度 | default_profile | escalation_profile | needs_validator |
|---|---|---|---|
| T1 | P-CODE | null | false |
| T2 | P-CODE | P-JUDGE | false(碰狀態則 true) |
| T3 | P-REASON | P-JUDGE | true |

## Manifest schema(嚴格)

```json
{
  "mission": "string",
  "validation_contract": "string",
  "subtasks": [
    {
      "id": "T-XX",
      "desc": "string",
      "difficulty": "T1 | T2 | T3",
      "execution": "serial | readonly-parallel",
      "depends_on": ["T-XX"],
      "default_profile": "P-CODE | P-REASON | P-JUDGE",
      "escalation_profile": "P-CODE | P-REASON | P-JUDGE | null",
      "needs_validator": true,
      "validator_profile": "P-CODE | P-REASON | P-JUDGE | null",
      "covers": ["AC-XX"],
      "status": "todo"
    }
  ]
}
```

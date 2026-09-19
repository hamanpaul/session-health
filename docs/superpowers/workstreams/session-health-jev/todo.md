---
status: accepted
work_item: session-health-jev
task_type: feature
issue: 4
domain_breadth: 2
state_consistency: 1
invariant_count: 11
artifact_classes: [source, tests, documentation]
---

# session-health Jev implementation

使用者於 2026-09-19 明示授權本計畫與 Cortex 實作及對抗 review。
Tracking: https://github.com/hamanpaul/session-health/issues/4 。相關 #3 保留獨立 scope，不作本 work 的 closure target。
完整範圍以 [accepted plan](../../plans/session-health-jev.md)、[spec](../../specs/session-health-jev-spec.md)、[design](../../specs/session-health-jev-design.md) 為準。
單一 writer 依序完成各包；不得只交付 transport 或部分軸便宣稱全案完成。此為 parent work，不直接派 Red=7 的整包 build；依總計畫順序執行 offline、semantic、routing 三個 Yellow 子工作，全部驗收後才關閉 parent。

Sizing 依據：包含資料、評估、模型執行三類邊界；狀態一致性為本機報告與 request ledger，沒有分散式交易。11 條不變量對應 spec 的 Required behavior；驗收包含 parser、CLI/JSON/HTML、API adapter 與模型 executor。

## Tasks

- [ ] T01 — 修正 Codex/Copilot parser 並加入有意義的 regression fixtures。
- [ ] T02 — 建立 portable bundle、evidence refs、limits、redaction 與案例 cutoff。
- [ ] T03 — 完成無 LLM 七軸 process-v2、coverage、legacy 透明性與外部 outcome join。
- [ ] T04 — 所有 CLI 入口支援 offline，single/batch terminal/JSON/HTML 一致。
- [ ] T05 — 完成 Jev typed transport、有限 retries/budget 與 request usage ledger。
- [ ] T06 — 完成七軸 semantic checks，實際共享 state 合批與棄權規則。
- [ ] T07 — 完成可用模型目錄、硬條件篩選、Jev 具體選模、override/fallback。
- [ ] T08 — 執行選定 analyzer、記錄 actual identity/tokens，Jev postcheck 與有界修正。
- [ ] T09 — 完成 E2E、標註 pilot fixtures、相容性文件與可追溯驗證記錄。
- [ ] T10 — AGY 對抗 review，root 核實每條 finding，Luna 修復後重審。
- [ ] T11 — Root 獨立驗收 exact candidate，分開報告 live/platform 未執行項。

Builder 固定 `codex/gpt-5.6-luna`、effort `max`；reviewer 固定 `agy/gemini-3.1-pro-high`。各角色必須讀 plan 第 9 節的權限、對抗判準與輸出契約。
只有實際完成且有證據的項目才能勾選；Cortex 工作與 job 狀態由 Manager 管理，不由 builder 修改 registry。

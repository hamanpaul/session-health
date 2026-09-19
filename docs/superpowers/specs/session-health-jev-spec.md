---
status: accepted
work_item: session-health-jev
task_type: feature
---

# session-health Jev support specification

本 spec 與 [implementation plan](../plans/session-health-jev.md) 共同定義 2026-09-19 使用者已接受的功能；plan 包含各軸量測、API、用量與完整測試細節。

## Requirements

1. 七軸 SNR/STATE/CTX/REACT/DEPTH/CONV/TOOL 保留 ID；新版量測只用可觀察事實與有來源的判讀。DEPTH 評分析/驗證充分性、CONV 分開交付宣告與實際 external outcome。
2. 1-1 不需要 LLM/SDK/key/network/CLI agent，明確 offline 模式的所有入口都保持無模型；產生 single/batch JSON、terminal、HTML。
3. Source adapters 提供版本化、可攜式 events/evidence bundle，未知不補成功，解析失敗不靜默消失；可匯出/匯入重播。
4. 1-2 Jev 用 Choice/Noul/Score 補七軸語意，共享 state 合併多案例/多題，依證據關係分批，有界依賴/時間/請求預算，保留 coverage 與 raw answers。
5. Python 控制精確計算、型別驗證、採用/棄權、去重、分母與有限重試；資料不足、服務失敗、無適用案例分開。
6. 第二階段從本機/明示目錄的具體 route/model/settings 選模型，Python 檢查硬條件、Jev 決策；explicit override 優先，Jev 不可用時使用有來源的固定政策。
7. 可用性來源/時效清楚，安裝 CLI 不等於帳號模型可用；候選資料不足、沒有合適模型、執行失敗有明確結果與有限替代。
8. 綜合分析區分事實/假說/建議；Jev 回原始證據檢查其支持性與限制；不自動宣稱因果或 correctness，不無限修正。
9. 所有模型操作記 requested/actual identity、設定、題組與輸入版本、request attempts、實際 usage；未知用量 null、共用 request 不重複計數，分析模型與被分析模型分開。
10. Legacy 計分與 JSON 可回歸；新版公式/語義標版本，uncalibrated 不宣稱正式成功等級。可選 outcome fixture join 不取得外部 authority。
11. 憑證不寫入 argv/report/log/bundle，來源內容先 allowlist/redact；純診斷不授予修改專案或任意工具執行。

## Acceptance

以 plan T01–T11 及測試矩陣驗收。具體實作/測試/外部 API/平台/審查證據分層記錄。先交付 1-1，再完成 1-2/2，不能以只有 transport 或幾個新增分數替代完整工作。

## Boundaries

僅此 repo；保留既有 ProblemMap taxonomy；不訓練 predictive model、不製作 leaderboard、不取得私有 chain-of-thought、不改其他專案 runtime/authority。
Luna max 實作，agy 對抗 review，root 獨立驗收。Builder 不自行 push/PR/merge/deploy。

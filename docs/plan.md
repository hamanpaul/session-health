> Synced mirror of the active session plan.
> Copilot plan-mode control file lives in session state; this repo copy is the formal project-facing version.

# Plan: `docs/` 納入研究與規劃文件，並檢視分析屬性種類是否足夠

## 狀態更新（2026-03-17）

- 已完成第一個 refactor milestone：`docs/` 同步、`SessionReport / BatchReport` 包裝層、`ProblemMap / Atlas` 診斷整合、bundle-aware terminal / JSON / HTML renderers。
- 已完成第二個 refactor milestone：新增 **weighted diagnosis**，以 `Fx` 加權方式把 ProblemMap 診斷結果耦合進量化分析的解讀層，並補上 `PM1 / PM2 / PM3` 中文欄位說明。
- `session-health <SESSION_OR_PATH>` 現在可直接接受 `Session ID`、單一 `session` 目錄、或 `sessions` 目錄，並以單參數 bundle flow 產生 terminal 摘要與 HTML 報告。
- SQL todos 目前已全部完成；`docs/todo.md` 已同步反映現況。

## 建議的下一步

1. 加入可提交的 fixture / regression tests，減少後續 refactor 回歸風險。
2. 用更多真實 session 校正 `Fx` 權重與維度影響矩陣。
3. 視需要補強 batch HTML 的 drill-down 細節與匯出控制。

## 新規劃（2026-03-17）：整合 ProblemMap 與量化分析診斷面

### 問題

目前輸出層有兩個相鄰但分離的診斷區塊：

- `evidence_summary` / 量化分析摘要：弱項維度、failure signals、failed tools、representative turns
- `problemmap`：PM1 候選、Atlas family、broken invariant、fix route

它們其實都在描述「這個 session 為什麼失衡、優先該修哪裡」，只是表達角度不同。若繼續分開擴充，terminal / HTML / JSON / agent prompt 會越來越像是在維護兩套近似的診斷面。

### 修正版整合假設（依回饋更新）

這次改採 **加權耦合，不做硬整合**：

- 保留底層的 7 軸量化計分與 ProblemMap / Atlas route-first 推論邏輯
- 不把 ProblemMap 全欄位直接硬塞進量化 schema，也不把兩者硬壓成單一演算法
- 新增一個 **加權診斷摘要面**，把 ProblemMap 的結果以加權方式整進量化分析的解讀層
- 報表中要明列：
  - `PM1 / PM2 / ...` 欄位的**中文欄位意義**
  - 每個 ProblemMap 候選對應的**中文描述**
  - 對應 `Fx` 的**加權比重**
  - 加權後對量化診斷的影響
- 預設將這裡的 `Fx` 解讀為 **Atlas family F1 ~ F7 的權重映射**；若後續要改成別的 `Fx` 定義，再調整 schema 命名

### 不打算在這輪做的事

- 不把 7 軸分數與 ProblemMap heuristics 硬合成單一演算法或單一總分
- 不變更 PM1 / Atlas taxonomy 的語義本體
- 不移除原始 `score.radar_axes`，因為雷達圖與批次比較仍需要保留原始分數

### 擬定做法

1. 在 `lib/report_types.py` 定義新的加權診斷模型（例如 `DiagnosisSummary`），至少承載：
   - 原始 quantitative highlights
   - ProblemMap 候選（`PM1 / PM2 / ...`）與每欄中文欄位說明
   - Atlas family / route
   - `fx_weights`（暫定對應 `F1 ~ F7`）
   - 加權後的診斷摘要 / weighted highlights
   - confidence / first fix / misrepair risk / supporting evidence
2. 在 `lib/problemmap.py` 或新 helper 中建立 weight builder，把：
   - `SessionScore`
   - `evidence_summary`
   - `ProblemMapDiagnosis`
   轉成「ProblemMap -> Fx 權重 -> 量化診斷補強」的中介結果。
3. 在 `eval_session.py` 把新加權診斷摘要掛到 `SessionReport` / `BatchReport`，作為對外主要診斷層。
4. 在 `lib/radar.py` / `lib/html_report.py` 改成：
   - 顯示單一診斷摘要區塊
   - 區塊中保留 quantitative 與 ProblemMap 的來源分層
   - 明列 `PM1 / PM2 / ...` 的中文欄位意義與 `Fx` 加權比重
5. 在 `lib/agent_analysis.py` 讓 agent prompt 吃新的加權診斷摘要，避免再拼兩套平行資料。
6. 更新 JSON 輸出，讓外部消費者同時拿到：
   - raw scores
   - raw problemmap
   - weighted diagnosis summary
7. 驗證 compile / help / 真實 session / synthetic batch，並同步文件。

### 風險與注意事項

- 若加權規則不透明，使用者會看不懂 ProblemMap 是如何影響量化分析，因此 `fx_weights` 必須顯式輸出。
- 若整合過度，可能讓 raw evidence 的可追溯性下降，因此加權診斷內仍需保留 `supporting_evidence` / `representative_turns`。
- 若直接砍掉現有欄位，可能破壞既有 JSON 或 renderer 邏輯；較安全做法是先新增 weighted diagnosis，再逐步收斂舊欄位。
- batch 場景需要同時保留比較表格與整體 diagnosis，因此新摘要要能同時支援 single / batch。

## 問題陳述

目標是在專案內新增 `docs/`，把目前已產生的研究報告 `https-github-com-onestardao-wfgy-tree-main-problem.md` 納入版本化文件區，並規劃後續 `plan.md` 與 `todo.md` 也同步放進 `docs/`。同時需要判斷：目前研究與分析輸出的屬性種類是否已足夠表達結果，或是否需要在第一次重構前先補欄位/分類。

## 目前狀態

- repo 目前沒有 `docs/` 目錄。
- 專案主體仍是 `eval_session.py -> parser -> scorer -> radar/html -> optional agent analysis`。
- 既有 HTML 報告目前只接受 `SessionScore` 與單一 `agent_section` 字串插槽，尚未有正式的多層報告資料模型。
- 已完成的研究報告放在 session workspace 的 `research/` 目錄，尚未進 repo。
- 先前研究判斷：PM1/Atlas/量化評分三層的「診斷分類」本身大致已足夠；真正較可能需要新增的是「報告層/文件層」的中繼屬性，而不是再發明新的診斷 family。
- 已確認：repo 內的 `docs/plan.md` 與 `docs/todo.md` 應作為**持續同步的正式文件**，不是一次性快照。

## 初步判斷：目前屬性種類是否足夠

### 結論

目前的核心分析屬性種類 **大致足夠** 表達結果；第一次重構**不建議**先改 PM1、Atlas family、或 7 軸量化分數的分類本體。

### 建議維持不動的分類

- 量化分析：`SNR / STATE / CTX / REACT / DEPTH / CONV / TOOL`
- ProblemMap：`pm1_candidates`
- Atlas：`primary_family / secondary_family / why_primary_not_secondary / broken_invariant / fit / confidence / evidence_sufficiency`
- Agent 分析：作為自然語言綜述層

### 若要補，建議補的是「報告包裝層」屬性

這些欄位是為了讓 repo 內的 `docs/`、未來 HTML 報告、以及 batch/single-session 流程更容易對齊，而不是改動診斷語義本體：

- `target_kind`: `session_id | session_file | session_dir | sessions_dir`
- `report_kind`: `single_session | batch`
- `analysis_layers`: 本次是否包含 `quantitative / problemmap / agent`
- `evidence_summary`: 關鍵事件、失敗 signal、代表性 turns
- `artifact_sources`: research / plan / todo / html 等文件的來源路徑與對應關係
- `sync_status`: session workspace 與 repo `docs/` 是否已同步

## 擬定做法

1. 先定義 repo 內 `docs/` 的角色與結構，避免之後文件散落。
2. 明確區分：
   - session workspace 內的 `plan.md`：Copilot plan mode 的控制檔
   - repo `docs/plan.md`：人類可讀、可提交、且需與 session workspace 持續同步的專案規劃文件
3. 將既有研究報告納入 `docs/research/`，並把 `docs/plan.md` / `docs/todo.md` 視為長期同步文件來設計。
4. 核准後的**第一個執行步驟**是依此 `plan.md` 與 SQL todo 狀態產生 repo `docs/todo.md`，並以它作為第一次重構的執行面板。
5. 若實作 docs 同步機制，優先讓 `todo.md` 來自可查詢的 todo source（SQL / structured data），避免手動維護兩份真相。
6. 在真正開始第一次重構前，只補「報告/文件包裝層」屬性；不先擴充 PM1 / Atlas / 7 軸分類本體。

## 建議的 `docs/` 目錄方向

```text
docs/
  research/
    https-github-com-onestardao-wfgy-tree-main-problem.md
  plan.md
  todo.md              # 由已核准的 plan / SQL todo 產生，並作為實作執行面板
  README.md            # 可選：索引 docs 的用途與來源
```

## 風險與注意事項

- `docs/plan.md` 與 session workspace `plan.md` 若都長期存在，必須定義哪一份是 source of truth。
- `docs/todo.md` 若手寫維護，會和 SQL todo 狀態漂移；較適合做為同步輸出或可讀鏡像。
- 若之後 HTML 報告要引用 docs 內容，應先建立正式的 `SessionReport / BatchReport` 結構，再處理 renderer。

## 待辦（供後續實作）

1. 先由已核准的 `plan.md` 與 SQL todo 狀態產生 repo `docs/todo.md`。
2. 設計 repo `docs/` 結構與檔案命名。
3. 定義 docs/報告包裝層所需的新屬性，避免過早擴充診斷 taxonomy。
4. 規劃研究報告、plan、todo 從 session workspace / SQL 映射到 repo `docs/` 的流程。
5. 決定是否新增 `docs/README.md` 或 README 連結來索引文件。
6. 在 `docs/todo.md` 建立後，依它開始第一次重構。
7. 在實作階段驗證 repo docs 與 session artifacts 的一致性。

## 目前唯一需要確認的決策

已確認 `docs/plan.md` 與 `docs/todo.md` 在 repo 中應作為**持續同步的正式文件**。

因此後續實作方向應偏向：

- 設計明確的 source-of-truth 與同步流程
- 讓 `docs/todo.md` 優先由 SQL/structured data 產生
- 避免手工維護兩份會漂移的計畫/待辦文件

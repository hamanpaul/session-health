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

# session-health：七軸分析、Jev 增強與第二階段動態選模

日期：2026-09-19。使用者已接受方向並授權建立計畫、經 Cortex 派 Luna（max）實作，由 agy 對抗 review。
追蹤工作：https://github.com/hamanpaul/session-health/issues/4 。
本計畫是本次實作的範圍依據；早期 `docs/jev-support-refinement.md` 為討論記錄，與本計畫衝突時以本計畫為準。
基準：`141c2f07aa6a9d8d8ae6540aefcd3299683dae92`。僅修改 session-health；交付為可驗證的候選、測試與審查結果。推送、PR、合併和部署不得由 builder 自行執行。

## 1. 目標與分段

- **1-1 無 LLM**：以可攜式事件/證據契約強化七軸分析；不需要 key、模型套件或 CLI agent，即可產生完整基本報告。
- **1-2 Jev 增強**：沿用 1-1 的資料，利用 Choice/Noul/Score、共享 state 的多題並行，補足七軸語意判讀；基本量測保留可比對。
- **2 綜合診斷**：探索本機模型候選，Python 篩選硬條件、Jev 選出具體執行模型；產生綜合分析後，以 Jev 逐項檢查證據支持。

沒有 Jev 時 1-1 獨立可用；要求第二階段但沒有 Jev 時依明示的 deterministic routing policy 選模型並標記來源。沒有可用分析模型時仍保存基本報告及未執行原因。
「語意增強」的精度提升是評估目標，不因接上 API 就宣稱已證明。報告不取代 project tests、Cortex CompletionRecord 或其他外部 correctness authority。

## 2. 固定七軸與計分規則

保留穩定 ID `SNR / STATE / CTX / REACT / DEPTH / CONV / TOOL`。新 profile 為 `process-v2`；legacy profile 與既有 JSON 欄位保留，變更語義/公式皆有版本。

| ID | 新版評估方向 | 1-1 的可觀察量 | 1-2 的語意補強 |
|---|---|---|---|
| SNR | 資訊有效性 | 控制字元、精確重複行/輸出、截斷與資料量 | 任務相關性、摘要是否保留必要證據 |
| STATE | 決策狀態充分性 | cwd、工具結果、版本與來源、明確狀態時間 | 任務所需資訊是否充分、是否矛盾或失效 |
| CTX | 目標與約束連續性 | 按序要求/修正、明確 task refs、compaction/缺漏記錄 | 要求的補充/取代、有效限制的遵循 |
| REACT | 恢復與調整能力 | 非零結果與錯誤類別、重試/參數變化/後續結果 | 預期紅燈/正常 polling/無效重試、恢復品質 |
| DEPTH | 分析與驗證充分性 | 可見檢查、驗證與證據 refs，適用性未知時不強評 | 必要驗證是否充分、結論的支持範圍 |
| CONV | 交付完整性與執行收斂 | lifecycle、明確驗收項目、交付證據、未解狀態 | 宣告是否有支持、如實受阻交接；不等同任務成功 |
| TOOL | 工具效益與操作效率 | 成功/失敗/未知、延遲、重複呼叫及確定的依賴 | 工具選擇、參數及結果運用是否合適 |

規則：

1. Observed facts、規則推估、Jev 判讀、外部 outcome 分欄；缺欄位/缺證據用 null 與原因，不當 0 或 100。
2. 不用私有 chain-of-thought、assistant 字數、關鍵字重現或沒有 abort 推定能力、記憶或交付成功。Compaction/重試是事件，需有額外證據才判品質。
3. 每個比例有 numerator/denominator、適用條件與 excluded counts。品質、資料涵蓋率、成本/時間、重大矛盾案例分開呈現。
4. `process-v2` 不預設混合總分/A–F。可計的直接比例或明確 rubric 分數可呈現；不具量測依據的軸可只有 metrics/status。Legacy composite 明示 heuristic/uncalibrated 及實際參與公式的五軸。
5. SNR 的資訊重複與 TOOL 的操作重複、REACT 的處置品質與 CONV 的交付狀態，各有明確分母。重疊 windows 或重述同一宣告不可灌分。
6. 可選匯入外部 outcome artifact（session/task ID、時間/版本、verdict、authority/source refs），僅在身分匹配時 join；保留出處，不自行宣稱 correctness 或訓練 predictive weights。本次不完成相關 issue #3 的全部 outcome calibration 工作。

## 3. 輸入與可攜式核心

新增版本化 `SessionBundle` 與 JSON round-trip；包含 manifest、ordered events、facts、source capabilities、evidence refs、cases、coverage。程式核心維持 Python 標準庫，不依賴網路、WSL、shell、HOME discovery 或任何模型 SDK。

- Codex adapter 支援頂層及 `response_item.payload.type` 的 call/result；依 call ID 配對，保留來源行號與事件序。
- Copilot arguments 同時支援 dict/JSON string；非 dict、未知 event、缺 result/exit code 皆有診斷，不整筆靜默遺失。
- 成功、失敗、unknown 三分；nonzero exit 與非預期失敗語意分開。
- IDs、refs、時間、版本與欄位 capability 明確。使用相對 artifact refs 供 export/import；原機器絕對路徑不成為另一台重播的前置條件。
- Source ID 並非 global 唯一時加 source/session namespace；拒絕不明確配對、重複 ID 的靜默覆寫。輸入和 bundle 有 bytes/events 上限與拒收/截斷狀態。
- Session models、Jev evaluator models、第二階段 analyzer models 分開記錄。

案例建立：要求→行動、失敗→處置→結果、宣告→對應驗證。先以可重跑的句段/事件邊界廣取候選，語意關係由 Jev 判斷；候選未覆蓋或上下文不足須可觀察。以原宣告的 observation cutoff 評估，後來驗證不能回頭替早先過度宣告補證。

傳輸只使用允許欄位及 redaction 後的 bounded evidence；不傳完整原始 session、system prompt、任意環境或憑證。保留 redaction 對可判讀性的影響；不宣稱可以找出所有 secret。

## 4. Jev 後端與批次

後端共用契約：一份 state＋typed questions → typed answers＋原始 metadata＋request usage。能力宣告區分 native/emulated/unsupported，保留完整分布；一般模型自報 confidence 不等同 Jev 原生機率。

- 官方 endpoint `POST https://api.typesafe.ai/v1/systemone`，只從 `TYPESAFE_API_KEY` environment 取 key。
- 接入 `Choice`（閉集＋none/insufficient/mixed）、`Noul`（p(yes)，沒有額外 confidence）、`Score`（有意義的有序層級）；每題問句包含完整意思及指定 case/state path，不能只靠 question ID。
- 同任務、共享證據的多案例及跨軸問題合批。一份 state 只保留一份共用脈絡；不固定每案例一個 request。範例 10 cases×2–4 questions 可合一批，但非固定門檻。
- 同 state 的問題彼此獨立；適用性與假設前提下的品質可並行，Python 再採用。需要新證據才開第二輪，初版最多兩個語意相依階段，並有 request/輸入量/時間/已回報 tokens 的預算。
- 官方現況：state＋所有 questions 64k；state＋最長 question 32k。無精確 tokenizer 時採保守且可配置 bytes cap，不能宣稱 bytes=token；422 超長 request 記錄失敗，不無限重送。
- 多批的關鍵前提、反證與結果不可被切斷；預算不足列出未評案例及選樣政策，不能只看失敗樣本就外推整體。
- HTTP 401/422 不重試；429/529 尊重 Retry-After、有上限；不明執行結果的 timeout 不自動重送。回應型別、完整性、有限數值、範圍、機率分布、usage 均驗證；bool 不當 int。
- 模型/request/rubric/input snapshot/state/questions hash 與 attempts 全部入 ledger。即使答案無效，有回 usage 仍入帳；未知用量 null，不假造零。
- sum 以 request attempt ID 去重；一次多案例 request 不偽造逐案例 token。input/output/total 與 cached/reasoning 子集分清楚。
- 可重播 raw judgments；改計分政策不重跑模型。若作 cache，key 包含完整實際 state、questions、模型版本及推論/redaction 參數；alias 未固定不跨執行沿用。cache reused 與本次 tokens 分開。

各軸至少一個具版本、帶 applicability/coverage 的語意檢查。優先落地 claim support，再依同一介面完成其餘六軸；不能只做三項新增分數便宣稱七軸增強完成。

## 5. 第二階段：模型探索、Jev 選模、綜合診斷及驗證

### 5.1 模型目錄

以 `(executor/provider, route, model_id, inference_settings)` 識別具體候選。支援 operator 明示目錄＋adapter read-only discovery＋最近實際執行觀察。先接 Codex、Copilot、agy；Claude 可用明示目錄，擴充介面保留；既有 Gemini 僅保留 legacy 相容路徑，不將 agy 當 Gemini CLI 的別名。

不能從 CLI 存在或 help 範例推定帳號可用，也不能猜 model slug。記錄 capability/availability 的來源、時間、有效期、unknown，以及 context、輸出型態、語言、已測品質、延遲、成本單位。沒有枚舉 API 時使用明示 operator 候選或有時效的 observed metadata，不掃描/輸出憑證。

### 5.2 選模

Python 檢查上下文、已知可用性、設定與 budget 等硬條件。Jev 使用本次分析需求與候選模型卡，透過 Choice 選出具體 route/model/settings（含 no_suitable_model、insufficient_model_evidence）；不能只選強/弱模型等級。
各 job 預設選一個主模型。多 job 可共享模型目錄一次送多題，題目明指 job。候選資料改變則重算選擇；真正呼叫前 revalidate。

Jev 不可用時按明示 deterministic policy，標示 routing_source。已明示指定模型時尊重 override，不偷偷改成 auto。選中執行失敗後更新 availability，在剩餘合格候選有限次重選（預設最多一次替代）；沒有合格者保存基本報告，不假成功。等價候選難分時可用版本化 tie policy，不能把機率當預期品質或所有低 confidence 都判失敗。

### 5.3 綜合分析及事後檢查

生成模型接收 facts、採用的 semantic findings、相關證據及反證，區分 observations/hypotheses/recommendations。使用 structured argv/stdin；明示 model/effort，擷取實際回報 identity 及原生 usage，未知或 alias resolution 不偽造。純報告分析限制工具與寫入範圍，不預設 `--yolo`。

Jev 對生成的具體結論/建議與原始 evidence 批次檢查 supported/contradicted/insufficient、是否過度宣稱、是否符合已知限制。錯誤來源和改進方向為候選診斷，不把相關性提升為因果。最多一次有界修正，仍有分歧即列待驗證，不無限循環。

Jev 第一階段與事後檢查是同源判讀，不稱獨立 correctness proof。簡單已知問題可用 Jev＋Python template 提供結構化解釋；不偽裝為 Jev 生成自由文字。

## 6. CLI 與報告相容性

以下為待實作的語義；具體 argparse 拼法由 builder 在保持簡單的前提下固定並測試：

- 明確 offline 模式（建議 `--offline`）強制所有模型與網路關閉，包含 positional bundle 的既有 auto-analyze；legacy 行為由明示 legacy profile 保留。
- `--jev` 僅啟用 1-2；`--analyze` 啟用第二階段；兩者組合時含 Jev 選模/事後檢查。不得因 `--jev` 自動額外跑生成模型。
- `--profile legacy|process-v2`；支援 bundle export/import、model catalog read-only 展示及明示 model override；usage/report 可落指定 output path。
- JSON additive versioned fields，terminal/HTML 同一 report model；七軸、coverage、processing status、候選選模、actual model、各階段 usage 一致。
- Batch 每一筆 selected session 都有狀態，包括 parse failure；移除既有只送前 12 筆卻暗示整批已分析的問題，以 bounded batching 明示全部/部分覆蓋。
- 服務失敗保留基本產物。exit 0＝要求工作完成或正常不適用；partial/failed/nonzero 有穩定契約；not_requested、unknown、not_applicable、insufficient、failed 不混用。
- 舊七軸 JSON/legacy renderer regression fixtures；資料記錄修正造成舊分數變動需帶 parser version 解釋。

## Tasks

一個 owner / 一個 builder writer，依序提交可驗收工作包。Cortex 可再拆 bounded cards，但不得平行修改同一基底，或漏掉後續階段。

Cortex 規模檢查為 Red=7，總工作拆成三個獨立執行切片，按順序 intake；總計畫保留完整範圍，不能因第一片完成就宣告全案完成：

| 順序 | 執行 plan | 工作 | Sizing | 前置條件 |
|---|---|---|---|---|
| 1 | [offline](session-health-jev-offline.md) | T01–T04＋本片驗證/review | Yellow=5 | 現在可啟動 |
| 2 | [semantic](session-health-jev-semantic.md) | T05–T06＋本片驗證/review | Yellow=6 | offline 已驗收的 exact candidate 成為基底 |
| 3 | [routing](session-health-jev-routing.md) | T07–T09＋整合驗證/review | Yellow=6 | semantic 已驗收的 exact candidate 成為基底 |

每片使用 Luna max builder 與 AGY reviewer。Root 在前片通過後將 exact candidate 整合成本機後續基底，再啟動下一片；不以三個 stale base 同時派工。T10/T11 在所有切片及整合檢查完成後才結案。

| ID | 工作 | 主要檔案/介面 | 驗收 |
|---|---|---|---|
| T01 | fixtures 與 parser 修正 | parser_base、parser_codex、parser_copilot、tests/fixtures | 具體 regression RED→GREEN、事件配對/unknown/unsupported 診斷 |
| T02 | portable bundle/evidence | 新核心型別、序列化/案例建立 | source refs、round-trip、limits、redaction、cutoff、跨機重播 |
| T03 | 1-1 七軸 process-v2 | metrics、scorer、report_types | 七軸可觀察量、null 分母、legacy 透明公式、可選 outcome fixture join |
| T04 | offline CLI/renderers | eval_session、radar、html_report | 無 key/CLI/SDK/網路完整 single/batch 產物，基本階段可獨立交付 |
| T05 | Jev transport/ledger | semantic backend adapter | typed validation、有限 retries/budget、報錯與用量守恆 |
| T06 | 1-2 七軸題組/batch | case/questions/batch scheduler | 跨軸共用 state、多案例合批、兩輪依賴、低信心/缺證據規則 |
| T07 | discovery/catalog/router | provider adapters、routing policy | Codex/Copilot/agy 探索、具體候選 Choice、staleness/unknown、override/fallback |
| T08 | 第二階段分析/驗證 | agent_analysis、postcheck、report | 實際選模執行、正確 usage、證據檢查/有界修正、全部 batch 狀態 |
| T09 | end-to-end 與文件 | tests、README、sample reports | mock E2E、受控 live smoke、限制/例子/模型與 tokens |
| T10 | agy 對抗 review＋修復 | exact candidate、review report | 每條 finding root 獨立驗證，修復後重審 |
| T11 | root 最終驗收 | 候選 diff、offline/full tests、報告 | 實作/測試/審查/live/未執行各自分帳，沒有未處置 blocker/major |

T01→T02→T03→T04 為 1-1；T05→T06 為 1-2；T07→T08 為第二階段；T09→T10→T11 完成驗收。

## 8. 測試與精度驗證

使用 unittest/mock 或等價標準庫；以實際風險為測試目標，不只重述 implementation。

- 巢狀 Codex、Copilot 字串 arguments、missing/malformed records、同 ID 跨 session、未知結果、不完整 lifecycle。
- JSON/輸出注入與 escaping、超大輸入、非法型別/NaN/Inf/負 tokens、missing answers、timeout/429/529/401/422；有效 usage 在無效答案時仍入帳。
- 純文字長度增加不提升新版 DEPTH；無 abort 不證明交付；未知 success 不補成功；合法換目標不直接扣 CTX。
- 重疊案例/重述不灌分、晚到證據不補早先宣告、反證不被篩掉；來源 refs 可回查、觀察範圍和語意 coverage 分開。
- 關閉 LLM 時 monkeypatch transport/subprocess networking paths，確認沒有模型呼叫；positional + offline/jev/analyze/override precedence 有測試。
- Batch state 共用且確實減少重複 request；每個 question 明指 case；budget 與語意依賴有效；輸入安全上限不被誤稱 token 精確值。
- 候選不存在、不可用、狀態 stale、超 context、成本未知、Jev 選回非法 ID、呼叫失敗重選、alias→actual 差異、後端無 usage／無原生機率。
- 所有來源/失敗/跳過保留在 report；JSON/terminal/HTML 語義一致；legacy regression。
- 至少 12 個人工預先標註情境，涵蓋預期紅綠測試 vs 盲重試、目標補充/取代、compaction、矛盾完成宣告、正確受阻交接、缺記錄、工具輸出改寫、相同數值不同語意；英文/繁中對照按情境分 development/held-out，避免同情境洩漏。
- 題組/模型/樣本 hashes 固定，記錄誤判、應棄權未棄權、高信心錯誤、coverage、tokens、latency。小樣本僅稱 pilot，不能宣稱校準完畢。
- 路由與固定基線策略比較；模型能力資料若只有描述而無實測，保留 provisional。Jev 自評不能作唯一 gold label。

Linux 本機離線完整 suite 必跑；Windows/macOS 如無 runner 則明示 unrun，僅做 portable fixture/路徑測試不得稱真平台通過。API key 可見時以非敏感 synthetic state 做小型 Jev smoke，記錄真實回應模型與 tokens；不可見時保留 mock 已驗/live deferred，不能自造數字。新真實 session 報告放既有 report 目錄的新檔，不覆蓋 260919 基準，也不以新 `--latest` 宣稱同一批。

## 9. 委派與對抗 review 契約

- Builder：Cortex `codex / gpt-5.6-luna`，actual reasoning effort 必須為 `max`，獨立工作樹；不得用預設模型替代。只改本 repo 的計畫範圍，提交具名檔案，無 `git add -A`、無 push/PR/merge、無全域設定/憑證修改。
- Reviewer：Cortex `agy / gemini-3.1-pro-high`（已在本機 agy models 枚舉且 Cortex reviewer 登錄），read-only、Google independence domain；以 exact candidate SHA 審查，不自己修程式。
- 對抗判準從首輪附上：未處置缺陷/缺口→FAIL；已明文承認、影響分析有界且文件列管的殘餘風險，不單獨構成 FAIL；若不接受該風險需具體反駁影響分析。
- Reviewer 實測可測主張，輸出最多 10 條 BLOCKER/MAJOR，每條含檔案位置、觸發條件、影響及所需修復，最後 PASS/FAIL；附實際測試或 unrun。不得用泛稱「需要更多測試」代替具體失敗模式。
- 每條 finding 由 root 獨立分類修/駁/接受列管；修復仍由 Luna 在同一 writer 範圍處理，改後 exact SHA 重審。round≥3 使用 fresh thread 精簡輸出；開始後 10 分鐘無 tool 活動才判卡住，不只憑 CPU/靜默。
- Root 獨立跑適當 unit/integration/E2E，查完成條件及模型/用量紀錄；worker exit 0、candidate 或 Cortex gate 單獨不等於全部完成。

## 10. 官方來源與範圍

已查核 2026-09-19 的官方文件；實作前以官方 API 契約再確認必要欄位，不使用 cookbook 的任意閾值作通用標準。

- https://docs.typesafe.ai/api.md
- https://docs.typesafe.ai/models.md
- https://docs.typesafe.ai/primitives/choice.md
- https://docs.typesafe.ai/primitives/noul.md
- https://docs.typesafe.ai/primitives/score.md
- https://docs.typesafe.ai/patterns/fan-out.md
- https://docs.typesafe.ai/cookbooks/citation_check.md
- https://docs.typesafe.ai/concepts/use-case-map.md
- https://docs.typesafe.ai/model-jaggedness/jev-1.13.md

既有 issue #3 與本計畫共用 process/outcome 分界；本次新增 Jev 與動態路由，並不自動宣告 #3 的 predictive calibration/全 executor 支援已完成。

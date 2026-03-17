# `session-health` 第一次重構研究報告：導入 WFGY ProblemMap / skill-problemmap 的單指令整合方案

## Executive Summary

`session-health` 目前其實已經具備一條很清楚的主幹：`CLI -> 解析 session -> 7 軸量化評分 -> terminal / HTML 輸出 -> 可選 agent 評論`。也就是說，你現在缺的不是另一個產品，而是把既有量化管線和 ProblemMap 語義診斷層接成同一份報告。[^1][^2][^3][^4]

`WFGY/ProblemMap` 提供的是兩層很適合補進這個主幹的能力：PM1 的穩定故障編號 vocabulary，以及 Atlas 的 route-first 診斷合約。PM1 強調「先把症狀映射到穩定編號」，Atlas 強調「先切 primary family、說清楚 broken invariant，再給 first repair direction」，而且明確聲明它不是 full auto-repair engine。[^6][^7]

`skill-problemmap` 則已經把這件事做成可執行流程：先從 session 抽 failure-bearing case，再做 PM1 + Atlas diagnosis，需要時自動準備 upstream 參考資料，最後在 confidence / evidence 足夠時才寫出 downstream artifact。這代表第一次重構不該重寫診斷方法，而應該把它內化成 `session-health` 的第三條分析管線。[^8][^9][^10][^11][^12]

我的核心建議是：**不要拆 non-agent / agent 版本，也不要把 ProblemMap 當成外掛模式；改成單一命令、單一參數、單一分析 bundle。** 預設行為就是輸出 terminal 分數條，並落地一份含「數據分析 + ProblemMap 診斷 + agent 綜述」的 HTML 報告。[^4][^5][^17][^18]

## Architecture / System Overview

### 1. 目前 `session-health` 的主架構

目前專案的核心資料模型是 `Session -> Turn -> ToolCall`，由 `parser_codex.py` 與 `parser_copilot.py` 將兩種 session log 格式正規化成同一套結構；之後 `scorer.py` 對 session 跑 7 軸評分；最後由 `radar.py` 生成 terminal 摘要、`html_report.py` 生成單檔 HTML，`agent_analysis.py` 則在需要時把量化結果包成 prompt 丟給外部 CLI agent 做補充評論。[^1][^2][^3][^4]

目前 CLI 已經能處理三種主要入口：單一 session 檔、單一 session ID、以及帶有 `events.jsonl` 的 Copilot session directory；但一般「sessions 目錄」仍需走 `--dir`，而 agent analysis 也只在單一 session 且顯式開 `--analyze` 時才會執行。這代表它已經很接近你要的 UX，只差「把多分析層收斂成預設流程」而不是讓使用者自己決定模式。[^4][^5]

### 2. WFGY ProblemMap 實際提供什麼

PM1 不是一篇說明文而已；它是 16 個穩定故障編號的「公共 vocabulary」。官方 README 明確要求不要 renumber、不要 invent `17+`，而且在摘要時要保留 `symptom -> Problem Map number -> file path` 的映射。更重要的是，這套分類被定義在 prompt / reasoning / retrieval / infra behavior 層，但同時強調「no infra change required」預設：若沒有明確要求，不要直接把建議升級成 code-level 變更。對 `session-health` 來說，這剛好適合拿來做「session failure 語義標籤」而不是代替數據評分。[^6]

Atlas 則不是 PM1 的替代品，而是 route-first 診斷契約。Atlas 主頁明確把自己定義成 debugging decision system：先 classify failure、指出 broken invariant、分離容易混淆的 neighboring family、再給正確的 first repair direction，目的在於降低「第一刀修錯」的成本。它同時也把 Router v1 說成 compact executable surface，而不是 full Atlas、full Casebook、或 full auto-repair engine。這一點對 `session-health` 很重要：報告應該產出 diagnosis 與 first-fix direction，但不應假裝自己變成自動修復器。[^7]

### 3. `skill-problemmap` 實際提供什麼

`skill-problemmap` 已經把 Atlas 的文檔世界壓成一條可執行 workflow。`SKILL.md` 明確把 default mode 設成 `strict`，規定 reference loading order，指定 WFGY `ProblemMap/` 是 upstream source of truth，並要求「route first, then repair」；`GlobalFixMap` 只能在 route 穩定後接手。它也已經定義好 packaged helpers、command examples、以及輸出契約。[^8]

在實作層，`extract_failure_case.py` 會從 raw session JSONL 中尋找最具 failure signal 的 anchor record：它會看 failure events、關鍵錯誤詞、以及 non-zero exit code，然後抽出 expected / actual / evidence window / recent actions。這正是把一整段 session 壓縮成可診斷 case 的第一步。[^9]

`diagnose_session.py` 則不是黑盒 LLM，而是顯式 heuristic router：它定義 PM1 heuristics、七個 Atlas family、failure-signal family hints，並回傳 `pm1_candidates`、`atlas.primary_family`、`why_primary_not_secondary`、`broken_invariant`、`fix_surface_direction`、`misrepair_risk`、`confidence`、`evidence_sufficiency`、`global_fix_route`、`references_used`。這非常適合直接變成 `session-health` 報告裡的「結構化診斷層」。[^10]

另外，`ensure_upstream_problemmap.py` + `upstream-source.json` 已經處理了上游語料同步問題：它可用 sparse checkout 只抓 `ProblemMap` subtree，也支援本機 seed clone `/home/paul_chen/prj_pri/problemmap/WFGY`，並驗證必要檔案是否存在。`emit_problemmap_event.py` 也已經做了 writeback gate：只有 `confidence >= medium` 且 `evidence_sufficiency != weak` 才允許寫出 artifact，event type 也偏向 `problemmap-atlas-f*` 的 family-first 標籤。[^11][^12]

## 建議的第一次重構方向

### 1. 單一命令、單一參數、單一分析 bundle

你的目標 UX 很明確：**一個指令，一個參數（session id 或 sessions path），自動產生 terminal 摘要分數條 + HTML 報告。** 以目前程式碼來看，這不需要另開第二個 CLI，而是把 `eval_session.py` 從「模式分派器」改成「目標解析器 + pipeline orchestrator」即可。[^5][^18]

我建議把 positional argument `target` 的解析規則固定成：

1. 若是存在的檔案：當成單一 session 檔。[^5]
2. 若是存在的目錄且含 `events.jsonl`：當成單一 Copilot session directory。[^5]
3. 若是存在的目錄但不含 `events.jsonl`：當成 sessions directory，遞迴找 `*.jsonl`。這一步其實就是把今天 `--dir` 的行為吸收到 positional。[^5]
4. 否則：當成 session ID，在已知 session 根路徑下搜尋。[^5]

這樣就能滿足「一個指令一個參數」而不犧牲原本的解析能力。

### 2. 先建立 `SessionReport` / `BatchReport`，再談 render

現在的 HTML renderer 只吃 `SessionScore` 加一個字串型別的 `agent_section`，template 也只有單一 `{agent_section}` 插槽。這對加入 ProblemMap 會很快變成維護負擔，因為你接下來至少會有：

- score summary
- per-dimension drill-down
- ProblemMap diagnosis
- agent synthesis
- batch aggregate（對目錄輸入）

所以第一次重構的正確切點不是先改 HTML，而是先引入結構化報告模型，例如：

```text
SessionReport
  session
  score
  problemmap_diagnosis
  agent_analysis
  evidence_summary
  warnings

BatchReport
  sessions: list[SessionReport]
  aggregate_stats
  highlighted_sessions
```

等這個 bundle 有了，`radar.py` 和 `html_report.py` 才能變成「render structured data」而不是「拼接一個額外 HTML 字串」。[^3][^4][^17]

### 3. ProblemMap 要「內建化」，不是要求使用者另外裝 skill

如果你最終要的是「session-health <target>」這種成品級 UX，那第一次重構不應依賴外部 skill 安裝路徑，例如 `~/.agents/.../problemmap`。`skill-problemmap` 應該被當成**設計來源與可移植邏輯**，而不是最終 runtime dependency。[^8][^11][^18]

我建議做法是：

- 把 `skill-problemmap` 的 orchestration 邏輯移植或 vendor 進 `session-health/lib/problemmap_*.py`
- 把最小必要 curated references 內附在 repo
- 保留 `ensure_upstream_problemmap.py` 的思想：必要時再用 sparse checkout 去同步 WFGY upstream

這樣的好處有三個：

1. 使用者只看到一個工具，不需要先理解 skill 系統。[^18]
2. 你保留 `session-health` 目前「標準庫即可跑」的產品風格，不把執行路徑拆散。[^1][^3]
3. 你仍然維持 WFGY `ProblemMap/` 是 canonical upstream，不會失去對齊能力。[^8][^11]

### 4. 讓量化分數變成 ProblemMap 的 evidence，不是競品

這次整合最容易做錯的地方，是把 `score_session()` 和 `ProblemMap diagnosis` 做成兩套互不相干的報告。正確做法是：**把 7 軸分數和事件統計當成 ProblemMap 的 evidence surface。**

這其實和 `skill-problemmap` 的 family hints 很對得上：

| 本地訊號 | 目前來源 | 對應的 Atlas 壓力 |
|---|---|---|
| `turn_aborted` / non-zero exit / 執行失敗 | `reaction.py`, `convergence.py`, `tool_efficiency.py` | F4 `Execution & Contract Integrity` |
| `context_compacted` / continuity loss | `context.py`, `convergence.py` | F3 `State & Continuity Integrity` |
| loop / repeated commands / progression break | `reaction.py` | F2 `Reasoning & Progression Integrity` |
| 黑盒感、trace 不足、工具輸出未被利用 | `tool_efficiency.py`, `state.py` | F5 `Observability & Diagnosability Integrity` |

這個對位不是空想：本地 metrics 已經在量測 abort、compaction、failed tools、redundant tool calls、goal retention、state coverage，而 `skill-problemmap` 的 router 也明確把 `context_compacted`、`turn_aborted`、`nonzero-exit-code` 等 signal 當作 family hints。這表示你完全可以把現在的量化結果直接餵進 ProblemMap case builder，讓數字層幫助 route-first 診斷，而不是另外重掃一次 raw log。[^10][^13][^14][^15][^16]

### 5. Agent analysis 應該降級成「綜述層」，不要再做平行裁判

現在 `agent_analysis.py` 的 prompt 主要來自：

- 各維度分數
- 前幾輪 user messages
- 工具統計
- abort / compaction 計數

也就是說，agent 目前是基於量化結果做自然語言評論。導入 ProblemMap 後，最好的做法不是保留兩套彼此平行的判斷，而是改成：

1. 先做 numeric scoring
2. 再做 ProblemMap diagnosis
3. 最後把這兩者一起餵給 agent，請它做「整體綜述 / 衝突解釋 / 最重要改善行動」

這樣 agent 的角色會從「另一位評審」變成「報告總結器」，而且它能用 ProblemMap 的 `broken_invariant` / `misrepair_risk` 幫忙把改善建議講得更具體。[^4][^7][^10]

### 6. HTML / terminal 輸出應改成三層結構

#### 單一 session

我建議 HTML 報告順序固定為：

1. **Overview**：session id / model / turns / overall score
2. **Quantitative layer**：現在的 radar + dimension cards
3. **ProblemMap layer**：PM1 candidates、primary/secondary family、broken invariant、why primary not secondary、first-fix direction、misrepair risk、confidence
4. **Agent synthesis layer**：用自然語言整合上面兩層
5. **Evidence appendix**：高風險 turns、關鍵 signals、tool 失敗摘要

terminal 則維持目前的雷達分數條，接著追加兩個 box：

- `ProblemMap Diagnosis`
- `AI Synthesis`

這和你現在的 `render_radar()` + `render_agent_terminal()` 模式一致，只是把 ProblemMap 加成第二個結構化 box。[^3][^4]

#### sessions directory / batch

這是本次重構最需要提前定義的地方。因為現在 agent analysis 只在單一 session 下執行，顯然是為了避免把昂貴 CLI 分析套到整批資料。[^4]

我的建議是：

- **所有 sessions** 都跑 parser + scorer
- **所有高風險 sessions**（例如 `composite < 80`、有 abort、或有 compaction）都跑 ProblemMap
- **只有 Top-K attention sessions** 跑 agent analysis（例如最差 3 個，或最差 1 個 + 最高 abort 1 個 + 最高 compaction 1 個）

這樣 batch HTML 可以是：

```text
Batch dashboard
  aggregate stats
  sortable session table
  highlighted session drill-downs
```

這個策略不是 WFGY 強制要求，而是基於你目前單一-session agent 行為與使用者期望的折衷設計：ProblemMap 屬於低噪結構化診斷，適合批次；agent synthesis 屬於高成本解讀層，適合聚焦。[^4][^7][^10][^18]

## 我會怎麼切第一次重構的程式結構

我建議保持 `eval_session.py` 薄化，新增下列模組：

| 模組 | 角色 | 為什麼這樣切 |
|---|---|---|
| `lib/report_types.py` | `SessionReport` / `BatchReport` / `ProblemMapDiagnosis` dataclasses | 先把資料模型定下來，再改 renderer |
| `lib/target_resolution.py` | 把 `target` 解析成單一 session 或 batch | 把 CLI UX 與分析流程解耦 |
| `lib/problemmap_case.py` | 從 `Session` 建立 failure-bearing case | 盡量直接用 parser 後的正規化資料 |
| `lib/problemmap_router.py` | 內建化 `skill-problemmap` 的 PM1 + Atlas 診斷 | 避免外部 skill 依賴 |
| `lib/problemmap_refs.py` | curated refs + upstream sync | 沿用 `ensure_upstream_problemmap` 思想 |
| `lib/pipeline.py` | `analyze_session()` / `analyze_batch()` | 讓 CLI 只負責啟動 |

而原本的：

- `parser_*`
- `scorer.py`
- `radar.py`
- `html_report.py`
- `agent_analysis.py`

都可以保留，但要改成接受 `SessionReport` / `BatchReport`。[^1][^2][^3][^4][^17]

## 最重要的工程判斷

### 1. 這次重構不該把 ProblemMap 當「附加功能」

因為 ProblemMap / Atlas 的定位是 route-first diagnosis，它最有價值的地方就是讓 quantitative layer 不只是漂亮圖表，而能指向「哪一類 failure region 正在主導這個 session」。如果把它做成 `--problemmap` 這種另開模式，你最後仍然會有兩套心智模型。[^6][^7][^18]

### 2. 這次重構也不該把 agent analysis 當主體

agent synthesis 應該保留，但它應該是第三層。第一層是 deterministic scoring，第二層是 structured diagnosis，第三層才是自然語言總結。這樣才符合 Atlas 的「route first, then repair」紀律，也能避免 agent commentary 跟 ProblemMap 打架。[^7][^8][^10]

### 3. 第一次重構最值得保留的，是現有 parser / scorer / renderer 骨架

這個專案目前最成熟的資產不是 CLI option，而是那條已經跑通的「解析 -> 量化 -> 呈現」路徑。第一次重構應該做的是把 ProblemMap 嵌進這條路徑，而不是推翻它重寫另一套系統。[^1][^2][^3][^4]

## Key Repositories Summary

| Repository | Purpose | Key files |
|---|---|---|
| `session-health` | 現有 session parsing / scoring / report 主體 | `eval_session.py`, `lib/parser_*`, `lib/scorer.py`, `lib/radar.py`, `lib/html_report.py`, `lib/agent_analysis.py`[^1][^2][^3][^4][^5] |
| [onestardao/WFGY](https://github.com/onestardao/WFGY) | PM1 + Atlas + Router + GlobalFixMap 的 canonical upstream | `ProblemMap/README.md`, `ProblemMap/wfgy-ai-problem-map-troubleshooting-atlas.md`, `ProblemMap/Atlas/*`[^6][^7] |
| [hamanpaul/skill-problemmap](https://github.com/hamanpaul/skill-problemmap) | 把 ProblemMap / Atlas 壓成 session 診斷 workflow 的 skill 化版本 | `SKILL.md`, `scripts/extract_failure_case.py`, `scripts/diagnose_session.py`, `scripts/ensure_upstream_problemmap.py`, `scripts/emit_problemmap_event.py`, `references/*`[^8][^9][^10][^11][^12] |

## Confidence Assessment

### High confidence

我對以下判斷有高信心：

- `session-health` 目前的主幹已經足夠支撐第一次重構，不需要另起新工具。[^1][^2][^3][^4]
- WFGY 的 Atlas / Router 是非常清楚的 route-first 契約，不適合被實作成「直接 auto-fix」層。[^7]
- `skill-problemmap` 的價值主要在 orchestration，而不是神祕模型能力；它本質上是可內建化的 case extraction + heuristic diagnosis + refs management。[^8][^9][^10][^11][^12]

### Medium confidence / informed inference

以下是我基於現有程式與目標 UX 做的工程推論：

- batch 模式下應該讓 ProblemMap 跑得比 agent analysis 更廣，agent 則只聚焦於 attention sessions。這是根據目前 agent 只對單一 session 生效、以及你要單指令自動化的需求推得出的最佳折衷。[^4][^18]
- 第一次重構最好把 `skill-problemmap` 內建化而非 subprocess 到外部 skill 路徑。這不是上游明文要求，而是為了滿足「一個指令一個參數」的產品級 UX。[^8][^11][^18]

### Tooling limitation note

本報告對外部 GitHub repo 的引用以公開檔案內容、blob SHA、以及當前抓取到的 snapshot 為準。`WFGY` 兩份大型文件有本地 snapshot 與行號；`skill-problemmap` 的幾支腳本則以抓取當下的 blob SHA 與具名函式/段落作引用定位，因 GitHub 內容工具回傳 raw text 時未附原生行號。這不影響架構判斷，但若你要進入實作，我建議下一步直接把要復用的 `skill-problemmap` 腳本拉成本地檔案再做逐行對照。[^8][^9][^10][^11][^12]

## Footnotes

[^1]: `/home/paul_chen/prj_pri/session-health/README.md:24-50`; `/home/paul_chen/prj_pri/session-health/lib/parser_base.py:9-82`; `/home/paul_chen/prj_pri/session-health/lib/parser_codex.py:17-141`; `/home/paul_chen/prj_pri/session-health/lib/parser_copilot.py:18-147`.

[^2]: `/home/paul_chen/prj_pri/session-health/lib/scorer.py:34-215`.

[^3]: `/home/paul_chen/prj_pri/session-health/lib/radar.py:103-167`; `/home/paul_chen/prj_pri/session-health/lib/html_report.py:227-260,660-689`.

[^4]: `/home/paul_chen/prj_pri/session-health/lib/agent_analysis.py:77-243`; `/home/paul_chen/prj_pri/session-health/eval_session.py:307-338`.

[^5]: `/home/paul_chen/prj_pri/session-health/eval_session.py:249-282`; `/home/paul_chen/prj_pri/session-health/README.md:261-342`.

[^6]: [onestardao/WFGY](https://github.com/onestardao/WFGY) `ProblemMap/README.md` snapshot `/tmp/1773711633278-copilot-tool-output-2h7nge.txt:6-24`.

[^7]: [onestardao/WFGY](https://github.com/onestardao/WFGY) `ProblemMap/wfgy-ai-problem-map-troubleshooting-atlas.md` snapshot `/tmp/1773711633231-copilot-tool-output-dyr6sd.txt:147-255`.

[^8]: [hamanpaul/skill-problemmap](https://github.com/hamanpaul/skill-problemmap) `SKILL.md`, fetched from `https://github.com/hamanpaul/skill-problemmap/blob/33c5c91e4b80a696bc99802f6e258d7b7edc1186/SKILL.md` on 2026-03-17; see sections `Default Mode`, `Workflow`, `Packaged Helpers`, and `Output Contract`.

[^9]: [hamanpaul/skill-problemmap](https://github.com/hamanpaul/skill-problemmap) `scripts/extract_failure_case.py` (blob `04b7f214eafa9d5aa856deeb7328b2266ffa7075`), fetched on 2026-03-17; see `FAILURE_KEYWORDS`, `FAILURE_EVENTS`, `score_record`, `pick_anchor`, and `extract_case_at_index`.

[^10]: [hamanpaul/skill-problemmap](https://github.com/hamanpaul/skill-problemmap) `scripts/diagnose_session.py` (blob `8d06132825b087e6a17d495052935021a7926793`), fetched on 2026-03-17; see `PM1_HEURISTICS`, `ATLAS_FAMILIES`, `FAILURE_SIGNAL_FAMILY_HINTS`, `select_references`, and `build_diagnosis`.

[^11]: [hamanpaul/skill-problemmap](https://github.com/hamanpaul/skill-problemmap) `scripts/ensure_upstream_problemmap.py` (blob `43ba47da4ae8d2ef4cf43dffb40dd57a9dc286ab`) and `references/upstream-source.json` (blob `a9657bd74a2d587badab51b61117a4daed22036c`), fetched on 2026-03-17.

[^12]: [hamanpaul/skill-problemmap](https://github.com/hamanpaul/skill-problemmap) `scripts/emit_problemmap_event.py` (blob `43ea2ab8623d9284ea3fca4b213beb4f0a6d3ae9`), fetched on 2026-03-17.

[^13]: `/home/paul_chen/prj_pri/session-health/lib/metrics/reaction.py:43-88`.

[^14]: `/home/paul_chen/prj_pri/session-health/lib/metrics/context.py:78-125`.

[^15]: `/home/paul_chen/prj_pri/session-health/lib/metrics/convergence.py:43-89`.

[^16]: `/home/paul_chen/prj_pri/session-health/lib/metrics/tool_efficiency.py:32-118`; `/home/paul_chen/prj_pri/session-health/lib/metrics/state.py:53-83`.

[^17]: `/home/paul_chen/prj_pri/session-health/lib/html_report.py:227-260,676-689`; `/home/paul_chen/prj_pri/session-health/lib/agent_analysis.py:201-243`.

[^18]: User request in this session on 2026-03-17: one command, one parameter (`session id` or `sessions path`), producing a unified HTML report and terminal summary that combines quantitative analysis, agent analysis, and ProblemMap diagnosis.

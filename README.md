# session-health

> Agent CLI Session 動態 Prompt 品質量化評估工具

[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)]()
[![No Dependencies](https://img.shields.io/badge/dependencies-none-green.svg)]()

---

## 設計理念

在 Agent CLI（如 Codex CLI、Copilot CLI）的工作流程中，每一輪送給 LLM 的 **動態 Prompt** 品質，直接決定了模型能否做出正確的判斷與行動。然而，這些 Prompt 的品質往往是隱性的——使用者難以直觀感受到「這次 session 為什麼跑偏了」或「為什麼模型一直重複同樣的錯誤」。

**session-health** 的核心理念是：

1. **量化不可見的品質** — 將 session 日誌中隱含的 Prompt 品質問題，轉化為 7 個可量測的維度分數
2. **RPG 化的直覺呈現** — 以遊戲血條風格的進度條，讓每個維度的強弱一目瞭然
3. **可行動的改善建議** — 不只告訴你「哪裡不好」，更告訴你「怎麼改善」
4. **AI 加持的深度分析** — 可選呼叫外部 AI Agent 提供個人化的改善建議
5. **零依賴、即裝即用** — 純 Python 標準庫實作，無需 pip install

---

## 程式架構

```
session-health/
├── eval_session.py              # CLI 入口：參數解析、session 查找、輸出路由
├── install.sh                   # 一鍵部署腳本
├── docs/
│   ├── README.md                # 專案規劃/研究文件索引
│   ├── plan.md                  # 專案規劃文件（repo 鏡像）
│   ├── todo.md                  # 執行待辦面板（repo 鏡像）
│   └── research/
│       └── https-github-com-onestardao-wfgy-tree-main-problem.md
│                               # WFGY / ProblemMap 整合研究報告
├── lib/
│   ├── parser_base.py           # 共用資料結構：Turn, Session, ToolCall
│   ├── parser_codex.py          # Codex CLI JSONL 解析器
│   ├── parser_copilot.py        # Copilot CLI JSONL 解析器
│   ├── report_types.py          # 報告包裝層資料結構（single/batch/weighted diagnosis）
│   ├── problemmap.py            # ProblemMap / Atlas 診斷與 Fx 加權整合層
│   ├── scorer.py                # 複合計分引擎（7 維度聚合）
│   ├── radar.py                 # 終端渲染器（quant + weighted diagnosis + agent）
│   ├── html_report.py           # HTML 報告產生器（single/batch report bundle）
│   ├── agent_analysis.py        # AI Agent 分析模組（外部 CLI 呼叫）
│   └── metrics/
│       ├── snr.py               # SNR   信噪比
│       ├── state.py             # STATE 狀態完整度
│       ├── context.py           # CTX   記憶留存
│       ├── reaction.py          # REACT 反應指標
│       ├── depth.py             # DEPTH 推理深度
│       ├── convergence.py       # CONV  收斂力
│       └── tool_efficiency.py   # TOOL  工具效率
├── tests/
│   └── __init__.py
├── README.md
└── .gitignore
```

### 文件與規劃檔

第一次重構的研究、規劃與待辦文件已整理到 `docs/`：

- `docs/README.md`：文件索引與同步規則
- `docs/plan.md`：目前核准的實作計畫
- `docs/todo.md`：目前的執行待辦面板
- `docs/research/https-github-com-onestardao-wfgy-tree-main-problem.md`：WFGY / ProblemMap 整合研究報告

### 模組關係圖

```
┌─────────────────────────────────────────────────────────────────┐
│                      eval_session.py (CLI)                      │
│  argparse → session 查找 → parse → score → render → output     │
└─────────┬───────────┬───────────┬──────────┬───────────┬────────┘
          │           │           │          │           │
          ▼           ▼           ▼          ▼           ▼
   ┌──────────┐ ┌──────────┐ ┌────────┐ ┌────────┐ ┌──────────┐
   │ parser_  │ │ parser_  │ │scorer  │ │ radar  │ │  html_   │
   │ codex    │ │ copilot  │ │  .py   │ │  .py   │ │report.py │
   └────┬─────┘ └────┬─────┘ └───┬────┘ └────────┘ └──────────┘
        │             │           │
        ▼             ▼           ▼
   ┌──────────────────────────────────────────┐
   │            parser_base.py                │
   │  Turn | Session | ToolCall (dataclass)   │
   └──────────────────────────────────────────┘
                      │
                      ▼
   ┌──────────────────────────────────────────┐
   │              metrics/                    │
   │  snr │ state │ context │ reaction │      │
   │  depth │ convergence │ tool_efficiency   │
   └──────────────────────────────────────────┘
```

---

## 評估維度（7 軸）

### SNR — 信噪比 (Signal-to-Noise Ratio) 📡

**量測目標**：動態 Prompt 中「有效資訊」與「無效雜訊」的比率。

| 量測項目 | 說明 |
|----------|------|
| ANSI 逃脫碼佔比 | 終端控制字元（顏色碼、游標移動）未被過濾 |
| 進度條/Spinner | `npm install` 等安裝過程的非語義輸出 |
| 重複行壓縮率 | 超過 N 次的相似錯誤行未被 dedup |
| 無效 Token 比例 | 無語義價值的 token 佔總量的百分比 |

**計分**：`100 - (noise_chars / total_chars × 100)`，高分 = 雜訊少。

---

### STATE — 狀態完整度 (State Integrity) 🗂️

**量測目標**：每次送給 LLM 的 Prompt 是否包含決策必備的環境資訊。

| 量測項目 | 說明 |
|----------|------|
| 當前工作目錄 (cwd) | 模型是否知道自己在哪個目錄下操作 |
| 退出碼 (Exit Code) | 上一步指令是否成功的關鍵訊號 |
| 使用者權限 | 是否以 root/sudo 執行，影響可用操作 |
| Git 狀態 | 當前分支、是否有未提交變更 |

**計分**：依據四項資訊的覆蓋率加權計算，缺失即扣分。無 tool call 的 turn 自動 100 分。

---

### CTX — 記憶留存 (Context Memory Management) 🧠

**量測目標**：多輪對話中，使用者原始核心任務是否被保留。

| 量測項目 | 說明 |
|----------|------|
| 關鍵詞留存率 | 第一輪 user message 的關鍵詞在後續 turn 中出現的比率 |
| 位置權重 | 關鍵詞出現在 Prompt 越前面，權重越高 |
| 原始 Log 冗餘度 | 未經壓縮的歷史 log 佔 Context Window 的比例 |
| Compaction 頻率 | context compaction 事件觸發的次數 |

**計分**：關鍵詞留存率 × 位置權重，compaction 次數作為間接指標。

---

### REACT — 反應指標 (LLM Reaction Quality) ⚡

**量測目標**：LLM 收到 Prompt 後的反應是否正常。

| 量測項目 | 說明 |
|----------|------|
| 死迴圈偵測 (Loop Rate) | 連續重複相同指令的次數 |
| Abort 比率 | 被系統中斷的 turn 比率 |
| 解析錯誤頻率 | 模型輸出無法被系統解析的比率 |
| 策略調整能力 | 連續失敗後是否能自主切換策略 |

**計分**：`100 - loop_penalty - abort_penalty`，是最直觀的反向指標。

---

### DEPTH — 推理深度 (Reasoning Depth) 🔬

**量測目標**：Agent 在執行動作前是否有充分的推理過程。

| 量測項目 | 說明 |
|----------|------|
| Reasoning 區塊比率 | 包含 thinking/reasoning 的 turn 佔比 |
| 推理密度 | reasoning 區塊數與 tool call 數的比率 |
| 推理長度 | 推理內容是否足夠充分（非單行敷衍） |
| 計畫性 | 是否在執行前有明確的分析或計畫 |

**計分**：`reasoning_presence(30) + density(40) + length_adequacy(30)`，無 tool 的 turn 預設 50 分。

---

### CONV — 收斂力 (Convergence) 🎯

**量測目標**：整個 Session 是否成功朝目標收斂（Session 層級指標）。

| 量測項目 | 說明 |
|----------|------|
| 任務完成 | session 是否以 `task_complete` 事件結束 |
| Abort 數量 | `turn_aborted` 事件的數量 |
| Compaction 頻率 | context compaction 的間接影響 |
| 長 Session 調整 | >20 turns 的 session 給予較寬鬆的評判 |

**計分**：`completion_rate - abort_penalty - compaction_penalty`。

---

### TOOL — 工具效率 (Tool Efficiency) 🔧

**量測目標**：Agent 對工具的使用是否高效且有成效。

| 量測項目 | 說明 |
|----------|------|
| 工具成功率 | tool call 成功 / 總數的比率 |
| 冗餘呼叫偵測 | 連續相同 tool + 相同 arguments 的浪費行為 |
| 輸出利用率 | tool output 是否被後續 assistant 回應引用 |
| 恢復策略 | 失敗後是否調整參數重試而非盲目重複 |

**計分**：`success_rate(40) + non_redundancy(30) + utilization(30)`。

---

## 計分公式

### 單輪計分 (Turn Score)

```
noise_penalty    = max(0, 100 - SNR) × 0.4      # 雜訊懲罰（0~40）
reaction_penalty = max(0, 100 - REACT) × 0.4    # 反應異常懲罰（0~40）
depth_bonus      = (DEPTH - 50) × 0.2           # 推理深度獎勵（±10）

Turn Composite = STATE - noise_penalty - reaction_penalty + depth_bonus
Turn Composite = clamp(0, 100, Turn Composite)
```

### Session 計分 (Session Score)

```
raw_composite       = mean(Turn Composites)
conv_adjustment     = (CONV - 50) × 0.1         # 收斂力調整（±5）

Session Composite = clamp(0, 100, raw_composite + conv_adjustment)
```

### 等級對照

| 分數 | 等級 | 標籤 |
|------|------|------|
| ≥ 90 | A | 優秀 (Excellent) |
| ≥ 80 | B | 良好 (Good) |
| ≥ 70 | C | 尚可 (Fair) |
| ≥ 60 | D | 待改善 (Needs Improvement) |
| < 60 | F | 不及格 (Poor) |

---

## 安裝與部署

### 系統需求

- **Python 3.8+**（僅使用標準庫，無需 pip install）
- **Codex CLI** 和/或 **Copilot CLI**（至少一個，用於產生 session 日誌）
- **可選**：`codex`、`copilot`、`gemini` CLI 工具（用於 `--analyze` AI 分析功能）

### 一鍵安裝

```bash
git clone git@github.com:hamanpaul/session-health.git
cd session-health
./install.sh
```

安裝腳本會：
1. 檢查 Python 版本（需 3.8+）
2. 若 `~/.paul_tools/` 存在，自動建立 symlink `~/.paul_tools/session-health`
3. 若不存在，提示使用專案路徑直接執行或建立 alias

### 手動安裝

```bash
git clone git@github.com:hamanpaul/session-health.git
cd session-health
chmod +x eval_session.py

# 方式 A：建立 symlink（推薦）
ln -s "$(pwd)/eval_session.py" ~/.local/bin/session-health

# 方式 B：建立 alias
echo "alias session-health='$(pwd)/eval_session.py'" >> ~/.bashrc
source ~/.bashrc
```

---

## 使用方式

如果你直接給 `Session ID`、`session 目錄` 或 `sessions 目錄` 作為唯一參數，`session-health` 會自動走 **bundle 模式**：

- terminal 先輸出摘要分數條
- 同步產生 HTML 報告
- 盡可能補上 weighted diagnosis（含 PM 欄位中文說明與 Fx 比重）與 agent analysis

### 完整 Help

```
usage: eval_session [-h] [--dir DIR] [--latest N]
                    [--source {auto,codex,copilot}]
                    [--format {radar,table,json,html}]
                    [--no-color] [--output FILE] [--verbose]
                    [--analyze] [--test-agent]
                    [SESSION_OR_PATH]

Agent CLI Session 動態 Prompt 品質量化評估

positional arguments:
  SESSION_OR_PATH            Session ID、Session JSONL、session 目錄，
                             或包含多個 sessions 的目錄

options:
  -h, --help                 顯示此說明
  --dir DIR, -d DIR          批次評估目錄下所有 session（遞迴搜尋）
  --latest N, -l N           評估最近 N 個 session
  --source, -s {auto,codex,copilot}
                             指定 session 來源格式（預設：auto 自動偵測）
  --format, -f {radar,table,json,html}
                             輸出格式（預設：radar）
  --no-color                 停用 ANSI 色彩
  --output FILE, -o FILE     輸出至檔案（副檔名 .html/.json 自動偵測格式）
  --verbose, -v              顯示每輪詳細分數
  --analyze, -a              啟用 AI Agent 分析（僅限單一 session）
  --test-agent               使用測試用 agent（copilot/gpt-5-mini）
```

### 使用範例

```bash
# ── 基本查詢 ──

# 以 Session ID 查詢（支援部分 UUID 匹配）
session-health 019c8d32

# 完整 UUID
session-health 019c8d32-6e21-7693-90bf-3b63176d9c10

# 直接指定 JSONL 檔案
session-health ~/.codex/sessions/2026/02/24/rollout-2026-02-24-xxx.jsonl

# 直接指定單一 session 目錄（例如 Copilot events.jsonl 所在目錄）
session-health ~/.copilot/session-state/019c8d32-6e21-7693-90bf-3b63176d9c10

# 直接指定 sessions 目錄（不必再加 --dir）
session-health ~/.codex/sessions/2026/02/

# ── 批次評估 ──

# 評估最近 5 個 session
session-health --latest 5

# 只看 Codex CLI 的最近 10 個
session-health --latest 10 --source codex

# 評估整個目錄（舊寫法，仍相容）
session-health --dir ~/.codex/sessions/2026/02/

# ── 輸出格式 ──

# RPG 進度條（預設）
session-health 019c8d32

# JSON 輸出（可串接其他工具）
session-health 019c8d32 -f json

# 文字表格
session-health 019c8d32 -f table

# 產生 HTML 報告（同時顯示終端摘要）
session-health 019c8d32 -o report.html

# 顯示每輪詳細分數
session-health 019c8d32 -v

# ── AI 分析 ──

# 啟用 AI Agent 分析（依序嘗試 codex → copilot/sonnet → gemini → copilot/mini）
session-health 019c8d32 --analyze -o report.html

# 使用測試用 agent
session-health 019c8d32 --analyze --test-agent
```

### 輸出範例

#### 終端 RPG 進度條

```
╔════════════════════════════════════════════════════════╗
║  ⚔ Session Health Report                               ║
╠════════════════════════════════════════════════════════╣
║  ID:     019c8d2c-32de-7820-8621-c0e5dfb8cc27          ║
║  Source: codex    Model: openai    Turns: 35           ║
╠════════════════════════════════════════════════════════╣
║                                                        ║
║  Overall Score: 98.3/100 (A)                           ║
║  █████████████████████████                             ║
║                                                        ║
╠════════════════════════════════════════════════════════╣
║                                                        ║
║  SNR   信噪比                                          ║
║  █████████████████████████ 100.0  雜訊過濾品質         ║
║                                                        ║
║  STATE 狀態完整度                                      ║
║  █████████████████████████ 100.0  環境狀態覆蓋         ║
║                                                        ║
║  CTX   記憶留存                                        ║
║  ███░░░░░░░░░░░░░░░░░░░░░░  12.3  上下文記憶力         ║
║                                                        ║
║  REACT 反應指標                                        ║
║  ████████████████████████░  94.3  模型反應正常         ║
║                                                        ║
║  DEPTH 推理深度                                        ║
║  ██████████████░░░░░░░░░░░  58.0  推理深度品質         ║
║                                                        ║
║  CONV  收斂力                                          ║
║  ██████████████░░░░░░░░░░░  55.7  任務收斂程度         ║
║                                                        ║
║  TOOL  工具效率                                        ║
║  █████████████████████████ 100.0  工具使用效率         ║
║                                                        ║
╠════════════════════════════════════════════════════════╣
║  σ=4.3  min=90  max=100                                ║
║  ⚠ compactions: 1  aborts: 8                           ║
╚════════════════════════════════════════════════════════╝
```

---

## AI Agent 分析

使用 `--analyze` (`-a`) 啟用 AI 分析功能。工具會依序嘗試以下 Agent CLI：

| 順位 | Agent | 命令 |
|------|-------|------|
| 1 | Codex (GPT-5.4) | `codex -c model=gpt-5.4 exec "prompt"` |
| 2 | Copilot (Sonnet 4.6) | `copilot -s --model claude-sonnet-4.6 -p "prompt" --yolo` |
| 3 | Gemini (3 Pro) | `gemini -m gemini-3-pro-preview -p "prompt"` |
| 4 | Copilot (GPT-5 Mini) | `copilot -s --model gpt-5-mini -p "prompt" --yolo` |

分析結果包含：
- **整體評估** — 2-3 句話概述 session 的 prompt 品質
- **低分維度改善建議** — 針對 <70 分的維度給出具體建議
- **最重要的改善行動** — 單一最有效的改善步驟

使用 `--test-agent` 強制使用 copilot/gpt-5-mini 進行測試。

---

## 支援的 Session 格式

| CLI 工具 | 日誌路徑 | 格式 |
|----------|----------|------|
| Codex CLI | `~/.codex/sessions/{YYYY}/{MM}/{DD}/rollout-*.jsonl` | JSONL（timestamp/type/payload） |
| Copilot CLI | `~/.copilot/session-state/{uuid}.jsonl` | JSONL（type/data/id/timestamp） |

兩種格式均自動偵測，也可用 `--source codex` 或 `--source copilot` 強制指定。

---

## 授權

MIT

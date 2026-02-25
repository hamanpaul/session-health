# session-health

Agent CLI Session 動態 Prompt 品質量化評估工具。

## 概述

從 Codex CLI / Copilot CLI 的 JSONL session 日誌中，量化評估每輪 Prompt 的品質，
支援終端機 RPG 風格進度條顯示及 HTML 互動報告，可選搭配 AI Agent 分析。

## 評估維度（7 軸）

| 維度 | 中文 | 說明 |
|------|------|------|
| **SNR** | 信噪比 | 無效 log（進度條、ANSI 垃圾、重複行）佔 token 比例 |
| **STATE** | 狀態完整度 | 關鍵狀態覆蓋率：pwd、exit code、權限、git |
| **CTX** | 記憶留存 | 核心任務關鍵詞留存率 + 歷史 log 冗餘度 |
| **REACT** | 反應指標 | 死迴圈偵測 + 解析錯誤率 + abort 比率 |
| **DEPTH** | 推理深度 | 推理區塊密度、先思考再行動的比率 |
| **CONV** | 收斂力 | 任務完成率、abort/compaction 頻率 |
| **TOOL** | 工具效率 | 工具成功率、冗餘呼叫偵測、輸出利用率 |

## 計分公式

```
Turn Score  = State - NoisePenalty×0.4 - ReactionPenalty×0.4 + DepthBonus×0.2
Session Score = mean(Turn Scores) + ConvergenceAdjustment×0.1
```

## 使用方式

```bash
# 以 Session ID 直接查詢（支援部分匹配）
session-health 019c8d32

# 評估單一 session 檔案
session-health <session.jsonl>

# 批次評估目錄
session-health --dir ~/.codex/sessions/2026/02/

# 最近 N 個 session
session-health --latest 10
session-health --latest 5 --source copilot

# 輸出格式
session-health <id> --format radar    # RPG 進度條（預設）
session-health <id> --format table    # 文字表格
session-health <id> --format json     # JSON
session-health <id> -o report.html    # HTML 報告（自動偵測格式）

# HTML 報告（同時顯示終端摘要 + 寫入 HTML 檔案）
session-health <id> -o report.html

# AI Agent 分析（加入 AI 改善建議到報告中）
session-health <id> -o report.html --analyze
session-health <id> --analyze --test-agent  # 用測試用 agent

# 詳細模式（顯示每輪分數）
session-health <id> -v
```

## AI Agent 分析

使用 `--analyze` 或 `-a` 啟用 AI 分析功能。工具會依序嘗試以下 Agent：

1. `codex` (GPT-5.3)
2. `copilot` (Claude Sonnet 4.6)
3. `gemini` (Gemini 3 Pro)
4. `copilot` (GPT-5 Mini, fallback)

分析結果包含：整體評估、低分維度改善建議、最重要的改善行動。

使用 `--test-agent` 強制使用測試用 agent (copilot/gpt-5-mini)。

## 支援格式

- Codex CLI (`~/.codex/sessions/`)
- Copilot CLI (`~/.copilot/session-state/`)

## 安裝

已建立 symlink 至 `~/.paul_tools/session-health`，可全域使用。

## 依賴

Python 3.8+，僅使用標準庫，無需 pip install。

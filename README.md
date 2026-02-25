# session-health

Agent CLI Session 動態 Prompt 品質量化評估工具。

## 概述

從 Codex CLI / Copilot CLI 的 JSONL session 日誌中，量化評估每輪 Prompt 的品質，
並以 text-based 雷達圖呈現結果。

## 評估維度

| 維度 | 說明 |
|------|------|
| **SNR** (信噪比) | 無效 log（進度條、ANSI 垃圾、重複行）佔 token 比例 |
| **STATE** (狀態完整度) | 關鍵狀態覆蓋率：pwd、exit code、權限 |
| **CTX** (記憶留存) | 初衷遺忘率 + 歷史冗餘度 |
| **REACT** (反應指標) | 解析錯誤率 + 重複動作率 |

## 計分公式

```
Turn Score = StateIntegrity(0~100) - NoisePenalty(0~50) - ReactionPenalty(0~50)
Session Score = mean(Turn Scores)
```

## 使用方式

```bash
# 評估單一 session
python eval_session.py <session.jsonl>

# 批次評估
python eval_session.py --dir ~/.codex/sessions/2026/02/

# 最近 N 個 session
python eval_session.py --latest 10

# 指定輸出格式
python eval_session.py <session.jsonl> --format table
python eval_session.py <session.jsonl> --format json
```

## 支援格式

- Codex CLI (`~/.codex/sessions/`)
- Copilot CLI (`~/.copilot/session-state/`)

## 依賴

Python 3.8+，僅使用標準庫。

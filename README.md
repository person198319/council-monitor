# Council Monitor — 議會質詢監控系統

> **實驗性專案（POC）**：以 LLM + 語音辨識自動偵測議會直播中的市府長官 / 醫院長官質詢，
> 生成結構化摘要並通知相關單位。

---

## 動機

台北市立聯合醫院隸屬衛生局，市議員質詢市府長官或醫院長官時，
各院區業務單位必須掌握質詢內容並準備回應。
人工監看議會直播耗時費力，本專案嘗試以 AI 自動化此流程。

---

## 系統架構

```
YouTube 直播
    │  yt-dlp（Node.js PO Token）+ ffmpeg
    ▼
faster-whisper（CPU / CUDA）
    │  中文即時逐字稿（帶直播絕對時間戳）
    ▼
說話者識別（SpeakerTracker）
    │  語法模式 → 角色推斷（議員/官員）→ 已知人物識別 + 回填
    ▼
關鍵詞偵測（三層）
    │  高信心詞 / 語法判斷 / 滑動視窗
    ▼
Session 管理（狀態機）
    │  累積逐字稿，靜默逾時觸發
    ▼
Gemma 27B（Ollama）
    │  結構化摘要 JSON
    ▼
單位分派（規則引擎 → Gemma 1B fallback）
    │
├── notices/ 通知檔案（txt + json）
├── SQLite 存檔 + 逐字稿 txt（含 YouTube 跳轉連結）
└── 週報（每週五彙整）
```

---

## 硬體需求

| 環境 | 規格 | 用途 |
|------|------|------|
| 開發 / 測試 | 任何有 WSL Ubuntu 的 Windows PC | P1/P2 驗證 |
| 辦公室機房 | i7-13700K + RTX 3060 12GB + 64GB RAM | 正式監控 |
| 未來擴充 | KNEO 350（EPYC 9115, RTX 5060 Ti ×4, 128GB） | 多卡分工 |

---

## 安裝

### 前置需求

**Windows + WSL Ubuntu**（Node.js 透過 WSL 解決 YouTube PO Token 問題）：

```powershell
# PowerShell（系統管理員）
wsl --install -d Ubuntu
```

```bash
# Ubuntu WSL 內
curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
sudo apt install -y nodejs ffmpeg python3.14-venv

node --version   # 應顯示 v22.x.x
ffmpeg -version
```

### Python 環境

```bash
# venv 建在 Linux 本機（避免 Windows 掛載磁碟的權限問題）
python3 -m venv ~/council-venv
source ~/council-venv/bin/activate

cd /mnt/d/claude/council-monitor-main   # 依實際路徑調整
pip install -r requirements.txt
```

### Ollama 模型

```bash
# 安裝 Ollama（Linux）
curl -fsSL https://ollama.com/install.sh | sh

# 機房桌機（RTX 3060 12GB）
ollama pull gemma3:12b-it-q4_K_M   # 摘要生成
ollama pull gemma3:1b-it-q8_0      # 單位分派

# KNEO 350（RTX 5060 Ti ×4，VRAM 充裕）
ollama pull gemma3:27b-it-q4_K_M   # 摘要生成
ollama pull gemma3:1b-it-q8_0      # 單位分派
```

---

## 設定

編輯 `config.yaml`，至少填寫：

```yaml
youtube:
  url: "https://www.youtube.com/watch?v=直播ID"

whisper:
  model: "medium"      # CPU 環境；GPU 環境改 large-v3
  device: "cpu"        # GPU 環境改 cuda
  compute_type: "int8" # GPU 環境改 float16
```

已知說話者（市府長官 / 醫院長官）定義在 `data/known_speakers.py`，
隨人事異動直接編輯該檔即可，不需動程式邏輯。

`data/dispatch_rules.py` 的業務關鍵詞需各院區業務人員審閱確認。

---

## 使用方式

### 網頁服務（推薦，供行政同仁使用）

```bash
# 啟動 WSL venv
source ~/council-venv/bin/activate
cd /mnt/e/claude/council-monitor   # 依實際路徑調整

# 啟動 web 服務
python3 web/app.py
# 瀏覽器開啟 http://localhost:8000
```

功能說明：
- 貼入 YouTube 直播或錄影網址 → 即時逐字稿
- 多人同時開啟同一網址，共用同一畫面
- 點擊說話者標籤可手動輸入姓名，自動套用至同一說話群組的所有發言
- 每段 Session 結束後可選擇「生成摘要」（需 Ollama 服務運行）

### 命令列模式（開發 / 除錯用）

```bash
# 啟動 WSL venv
source ~/council-venv/bin/activate
cd /mnt/e/claude/council-monitor   # 依實際路徑調整

# 逐步驗證（建議順序）
python3 main.py --stage p1 --test-file sample.wav   # 驗證 ASR + 時間戳
python3 main.py --stage p2 --test-text              # 驗證關鍵詞偵測
python3 main.py --stage p3 --test-text              # 驗證摘要 + 分派
python3 main.py --stage p4 --test-text              # 完整流程含存檔

# 正式監控（接 YouTube 直播）
python3 main.py --stage p2                          # 有直播時監控
python3 main.py --stage p4                          # 含摘要 + 存檔

# 週報
python3 main.py --digest
```

---

## 輸出範例

存檔後的逐字稿 `transcripts/2026-05-14_*.txt`：

```
# 仁愛急診壅塞改善進度
日期：2026-05-14　議員：林XX
對象：王智弘院長　緊急：本週
影片時間：30:23 – 36:45
YouTube 連結：https://youtube.com/watch?v=F9oZ-9zY4c8&t=1823

[30:23–30:31][議員] 請問院長，仁愛急診等候超過四小時
[30:31–30:45][醫院長官] 是的，我們聯醫已增派兩名急診醫師
[30:47–31:02][醫院長官] 預計下個月完成分流機制調整
[31:04–31:18][醫院長官] 兩週內提交書面報告給議員辦公室
```

YouTube 連結點開直接跳到質詢時間點。

---

## 實作階段

| 階段 | 內容 | 狀態 |
|------|------|------|
| P1 | YouTube 串流擷取 + faster-whisper ASR + 絕對時間戳 | ✅ |
| P2 | 三層關鍵詞偵測 + Session 狀態機 + 說話者識別 | ✅ |
| P3 | Gemma 摘要 + 單位分派規則引擎 | ✅ |
| P4 | notices/ 通知檔案 + SQLite 存檔 + 週報 | ✅ |

---

## 專案結構

```
council-monitor/
├── main.py                   # 主程式入口（P1–P4，命令列模式）
├── config.yaml               # 設定（需填寫直播 URL 等）
├── requirements.txt
├── web/
│   └── app.py                # FastAPI + WebSocket 網頁服務入口
├── static/
│   └── index.html            # 純 HTML 前端（無 framework）
├── pipeline/
│   ├── audio_stream.py       # yt-dlp + ffmpeg 串流（帶時間戳 offset）
│   ├── transcriber.py        # faster-whisper ASR（絕對時間戳）
│   ├── keyword_detector.py   # 三層偵測（高信心 / 語法 / 共現）
│   ├── session_manager.py    # Session 狀態機（含說話者追蹤）
│   └── speaker_tracker.py    # 文字型說話者識別 + 回填
├── analysis/
│   ├── summarizer.py         # Gemma 摘要（Ollama）
│   └── dispatcher.py         # 單位分派（規則 + LLM）
├── output/
│   ├── notifier.py           # notices/ 通知檔案（txt + json）
│   ├── storage.py            # SQLite + 逐字稿 txt + YouTube 跳轉連結
│   └── digest.py             # 週報生成
└── data/
    ├── known_speakers.py     # 已知說話者（市府長官、醫院長官，需自行填入）
    ├── dispatch_rules.py     # 聯醫單位對照表（需人工維護）
    └── corrections.py        # ASR 誤辨修正詞表
```

---

## 注意事項

- 逐字稿與 SQLite 資料庫含議員質詢內容，**不納入版控**（已加入 `.gitignore`）
- `data/dispatch_rules.py` 的業務關鍵詞為虛構範本，上線前需各院業務人員校對
- `data/known_speakers.py` 隨人事異動更新（局長 / 院長姓名，不納入版控建議）

---

*Experimental project — Taipei City Hospital AI Team*
*Built with [Claude Code](https://claude.ai/code)*

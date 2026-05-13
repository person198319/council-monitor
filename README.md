# Council Monitor — 議會質詢監控系統

> **實驗性專案（POC）**：以 LLM + 語音辨識自動偵測議會直播中的衛生局長 / 聯醫院長質詢，
> 生成結構化摘要並通知相關單位。

---

## 動機

台北市立聯合醫院隸屬衛生局，市議員質詢衛生局長或聯醫院長時，
各院區業務單位必須掌握質詢內容並準備回應。
人工監看議會直播耗時費力，本專案嘗試以 AI 自動化此流程。

---

## 系統架構

```
YouTube 直播
    │  yt-dlp + ffmpeg
    ▼
faster-whisper（CPU）
    │  中文即時逐字稿
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
├── Email 通知（立即 / 本週）
├── SQLite 存檔 + 逐字稿 txt
└── 週報（每週五彙整寄送）
```

---

## 硬體需求

| 元件 | 規格 | 用途 |
|------|------|------|
| CPU | 多核（建議 8+ 核） | faster-whisper int8 + 系統協調 |
| GPU | ≥ 16GB VRAM（×1 以上） | Gemma 27B Q4 推論 |
| RAM | ≥ 32GB | 模型載入緩衝 |

> 開發與測試環境：KNEO 350（AMD EPYC 9115, RTX 5060 Ti ×4, 128GB RAM）

---

## 安裝

```bash
# 1. 安裝 Python 依賴
pip install -r requirements.txt

# 2. 安裝 Ollama 並下載模型
curl -fsSL https://ollama.com/install.sh | sh

# KNEO 350（RTX 5060 Ti ×4，VRAM 充裕）
ollama pull gemma3:27b-it-q4_K_M   # 摘要生成
ollama pull gemma3:1b-it-q8_0      # 單位分派

# 辦公室桌機（RTX 3060 12GB）→ 改用 12B
ollama pull gemma3:12b-it-q4_K_M   # 摘要生成（12GB VRAM 適用）
ollama pull gemma3:1b-it-q8_0      # 單位分派（不變）

# 3. 設定 SMTP 密碼（不寫進 config）
export SMTP_PASSWORD="your_password"
```

---

## 設定

編輯 `config.yaml`，至少填寫：

```yaml
keywords:
  targets:
    - "黃世傑"    # 現任衛生局長（隨任期更新）
    - "XXX"       # 現任聯醫院長

notification:
  email:
    contacts:
      聯醫-仁愛: ["admin@renai.gov.taipei"]
      # ... 其他院區
    director_emails: ["director@health.gov.taipei"]
    digest_recipients: ["director@health.gov.taipei"]
```

`data/dispatch_rules.py` 的業務關鍵詞需各院區業務人員審閱確認。

---

## 使用方式

```bash
# 逐步驗證（建議順序）
python main.py --stage p1 --test-file sample.wav   # 驗證 ASR 品質
python main.py --stage p2 --test-text              # 驗證關鍵詞偵測
python main.py --stage p3 --test-text              # 驗證摘要 + 分派
python main.py --stage p4 --test-text              # 完整流程含存檔

# 正式監控
python main.py --stage p4                          # 接 YouTube 直播（KNEO 350）
python main.py --config config.office.yaml --stage p4  # 辦公室桌機

# 週報
python main.py --digest                            # 手動觸發週報
```

---

## 實作階段

| 階段 | 內容 | 狀態 |
|------|------|------|
| P1 | YouTube 串流擷取 + faster-whisper ASR | ✅ |
| P2 | 三層關鍵詞偵測 + Session 狀態機 | ✅ |
| P3 | Gemma 27B 摘要 + 單位分派規則引擎 | ✅ |
| P4 | Email 通知 + SQLite 存檔 + 週報 | ✅ |

---

## 專案結構

```
council-monitor/
├── main.py                 # 主程式入口
├── config.yaml             # 設定（需填寫聯絡人等）
├── requirements.txt
├── pipeline/
│   ├── audio_stream.py     # yt-dlp + ffmpeg 串流
│   ├── transcriber.py      # faster-whisper ASR
│   ├── keyword_detector.py # 三層偵測
│   └── session_manager.py  # Session 狀態機
├── analysis/
│   ├── summarizer.py       # Gemma 27B 摘要（Ollama）
│   └── dispatcher.py       # 單位分派（規則 + LLM）
├── output/
│   ├── notifier.py         # Email 通知
│   ├── storage.py          # SQLite + 逐字稿 txt
│   └── digest.py           # 週報生成
└── data/
    ├── dispatch_rules.py   # 聯醫單位對照表（需人工維護）
    └── corrections.py      # ASR 誤辨修正詞表
```

---

## 注意事項

- 逐字稿與 SQLite 資料庫含議員質詢內容，**不納入版控**（已加入 `.gitignore`）
- SMTP 密碼請用環境變數 `SMTP_PASSWORD`，勿寫入 `config.yaml`
- `data/dispatch_rules.py` 的業務關鍵詞為虛構範本，上線前需各院業務人員校對

---

*Experimental project — Taipei City Hospital AI Team*
*Built with [Claude Code](https://claude.ai/code)*

# F22 / F27 架構與上下游關係

> 產生日期：2026-08-15　依實際程式碼掃描整理（非推測）

---

## 一句話理解

**F22 = 每月營收**（每月 10 號前公告）、**F27 = 每季損益表**（季報）。
兩支是**平行的雙胞胎**，結構幾乎一樣：一支主程式負責「抓 → 存 CSV → 發訊息」，其餘都是圍著它的工具。

---

## 一、核心資料流（兩支相同）

```
MOPS ezsearch API
   │  ①清單 API（輕）：某天有哪些公司公告
   │  ②明細頁（重）：抓實際數字
   ▼
mops_f22_daily.py / mops_f27_daily.py   ← 主程式（唯一會寫 CSV 的正規入口）
   │
   ├─→ monthly_revenue.csv / quarterly_revenue.csv   ← 唯一資料庫（真相來源）
   ├─→ sent_state_f22.json / sent_state_f27.json     ← 已發送記錄（防重複發）
   ├─→ Google Drive（upload_to_gdrive，用 token.json）
   └─→ stdout 印 JSON [{"message","group"}] → n8n → Telegram 分群推播
```

**關鍵設計**：主程式是「抓 + 存 + 發」三合一，其他所有工具**預設只讀 CSV、不碰網路**，要重抓才加 `--fetch`。這是為了避開 MOPS 限流。

---

## 二、檔案清單與角色

### F22（`mops_f22/`）

| 檔案 | 角色 | 碰網路？ |
|---|---|---|
| `mops_f22_daily.py` | **主程式**。每日抓 F22 月營收、寫 CSV、輸出 n8n JSON | ✅ |
| `check_backfill_f22.py` | **健檢/回補**。比對「MOPS 該有的」vs「CSV 實際有的」，找缺漏並補 | ✅ 清單API |
| `backfill_send_f22.py` | **補發訊息**。重發某段日期的 Telegram（預設讀 CSV 不重抓） | ❌ |
| `query_company_f22.py` | **查詢**。查某公司月營收歷史 | ❌ |
| `monthly_revenue.csv` | **資料庫**。19 欄，單位仟元 | — |
| `mops_f22_daily.json` | n8n 工作流程定義 | — |

### F27（`mops_f27/`）

| 檔案 | 角色 | 碰網路？ |
|---|---|---|
| `mops_f27_daily.py` | **主程式**。每日抓 F27 季報綜合損益表 | ✅ |
| `find_missing_f27.py` | **找缺漏**。只打輕量清單 API，算出缺哪些 → 寫入 `failed_fetches.csv` | ✅ 清單API |
| `refetch_failed_f27.py` | **重抓缺漏**。只補 `failed_fetches.csv` 裡的，用慢速率避免再被擋 | ✅ |
| `reconcile_f22_f27.py` | **跨表對帳**（唯一橫向連結，見下節） | ❌ |
| `backfill_send_f27.py` | 補發訊息 | ❌ |
| `query_company_f27.py` | 查詢 | ❌ |
| `quarterly_revenue.csv` | **資料庫**。28 欄（比 F22 多毛利/營益/稅前/淨利/EPS） | — |
| `failed_fetches.csv` | 抓取失敗待重抓清單 | — |
| `unparseable.csv` | **解析不出**的（多為金融保險業版型），與上者不同 | — |

**F22 vs F27 工具差異**：F22 的缺漏檢查與回補**合併在 `check_backfill_f22.py` 一支**；F27 拆成**兩支**（`find_missing` 找 → `refetch_failed` 補），因為 F27 明細頁重、限流嚴重，必須分開控速。

---

## 三、模組依賴

```
mops_f22_daily.py  ←── check_backfill_f22.py
                   ←── backfill_send_f22.py
                   ←── query_company_f22.py

mops_f27_daily.py  ←── find_missing_f27.py
                   ←── refetch_failed_f27.py
                   ←── backfill_send_f27.py
                   ←── query_company_f27.py
                   ←── reconcile_f22_f27.py
```

**全部工具都 `import` 主程式**，沿用它的抓取函數與格式化函數（`_fmt_m` / `_fmt_pct` / `fetch_f22_list` 等）。

> ⚠️ **改主程式時要注意**：動到 `fetch_*_list()`、`_fmt_*()`、CSV 欄位定義，會同時影響下面 4~6 支工具。

---

## 四、F22 與 F27 的唯一橫向連結

`mops_f27/reconcile_f22_f27.py` —— **交叉對帳**，同時讀兩邊 CSV：

```
F22 季末月的累計營收（3/6/9/12月）  ≈  F27 同季的累計營收
F22 季末累計 − 前季末累計          ≈  F27 單季營收
```

差異超過 1% → 標「差異待查」，寫入 `revenue_reconcile_f22_f27.csv`；若該股在觀察清單內，才推 Telegram。

**用途**：兩邊各自獨立抓，對得起來代表兩邊都沒漏抓、沒抓錯。這是資料品質的最後一道防線。

---

## 五、共用的外部檔案（在 `scripts/` 根目錄）

| 檔案 | 用途 | 誰在用 |
|---|---|---|
| `關注股票列表.xlsx` | **主檔**。決定推播分群（誰負責哪些股票） | F22 / F27 主程式 |
| `watchlist_active.csv` | 由主檔產生的作用中清單（`build_watchlist.py` 產） | F22 / F27 |
| `token.json` / `credentials.json` | Google Drive 上傳憑證 | F22 / F27 / 重訊 |

---

## 六、與「重訊」管線的關係

`daily_important_event/Daily_Important_Event_v4.py` 是**第三支獨立管線**（抓重大訊息公告、自結損益）。

- **不共用程式碼**，各自獨立。
- **共用**：`關注股票列表.xlsx` 分群邏輯、`token.json` 上傳、n8n → Telegram 輸出格式（都是 `[{"message","group"}]`）。
- **資料上互補**：F22 只有營收、F27 只有季報，**金控/壽險的月獲利只能靠重訊的自結公告**（F22 不支援金控格式，會被列為「金融股略過」）。

---

## 七、監控

`daily-parse-verify` 排程（平日 18:30）同時盯三支管線：

1. 重訊解析正確性（重讀原文重算，比對）
2. 三份 CSV 完整性（NUL byte 檢查）
3. 三份 CSV 新鮮度（`fetched_at` 超過 36 小時告警）
4. F22 缺漏抽查（跑 `check_backfill_f22.py --dry-run`）

> F27 目前**沒有 dry-run 版的缺漏檢查工具**（`find_missing_f27.py` 會直接寫入 `failed_fetches.csv`，不是唯讀），所以排程只對它做新鮮度檢查。這是目前監控的缺口。

---

## 八、已知弱點

| 弱點 | 說明 |
|---|---|
| MOPS 限流 / WAF | 抓太快會被 307 擋。F27 尤其嚴重，所以才拆成 find → refetch 兩段 |
| 金融股不支援 | F22 抓不到金控格式月營收；金控月獲利要看重訊自結 |
| F27 無唯讀缺漏檢查 | 無法在不寫檔的前提下抽查 F27 缺漏 |
| CSV 損毀無告警 | 2026-08-09 曾出現 NUL byte 導致整支腳本 crash 且無告警。已加入排程健檢 |

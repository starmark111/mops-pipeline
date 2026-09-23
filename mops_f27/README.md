# MOPS F27 — 每日季報損益（綜合損益表）爬蟲

與 `mops_f22`（每月營收）同架構的姊妹專案，抓所有上市／上櫃公司的**季度綜合損益表**（MOPS 公告類型 F27，明細頁 `ajax_t164sb04`），彙整進 CSV，並可經 n8n 每天跑四次、推到 Telegram。另含與 F22 月營收的**營收交叉對帳**工具。

## 檔案

| 檔案 | 用途 |
|---|---|
| `mops_f27_daily.py` | 核心：抓清單、解析損益表、去重寫 CSV、分群、Google Drive 上傳；含失敗記錄與重抓函式 |
| `backfill_send_f27.py` | 補發 / 排程入口（`--fetch` 重抓、`--emit` 給 n8n、`--dry-run` 預覽） |
| `find_missing_f27.py` | 用清單 API 比對 CSV，找出「已公告但缺漏」的，塞進待重抓清單 |
| `refetch_failed_f27.py` | 只重抓 `failed_fetches.csv` 裡先前被限流抓不到的；`--status` 看剩餘 |
| `reconcile_f22_f27.py` | F22 月營收 × F27 季報營收交叉對帳，輸出對帳 CSV、觀察清單差異可推 Telegram |
| `mops_f27_daily.json` | n8n 工作流程（週一至五 0/6/12/18 點，4 次 → Telegram） |
| `quarterly_revenue.csv` | 產出資料（與 monthly_revenue.csv 對稱命名） |
| `failed_fetches.csv` | 抓取失敗（多為限流）的待重抓清單；重抓成功自動移除 |
| `unparseable.csv` | 抓到但解析不出（金融保險業等），僅供檢視，**不自動重抓** |
| `probe_f27.py` | 一次性偵察腳本（可保留備查或刪除） |

## CSV 欄位（與 monthly_revenue.csv 對齊）

前段刻意與 F22 相同，方便兩表 join／比對：
`stock_id, company_name, market, industry, report_year, report_quarter,
revenue, revenue_last_year, yoy_amt, yoy_pct, ytd_revenue, ytd_revenue_last, ytd_yoy_amt, ytd_yoy_pct`

- `revenue` = 本期**單季**營收合計；`ytd_revenue` = 本期**累計**營收合計（Q1 兩者相同）。
- F22 的對應是 `revenue`=本月、`ytd_revenue`=累計，概念一致。

`sector_type` 欄標記業別（`一般 / 銀行 / 保險 / 證券 / 金控`），金融業的「主要收益」對映進 `revenue`/`ytd_revenue`：一般＝營業收入合計、證券＝收益合計、銀行/金控/票券＝淨收益、保險＝保險收入。

F27 額外欄位（皆為本期累計，金額單位仟元；EPS 為元）：
`gross_profit, operating_income, pretax_income, net_income, net_income_parent,
total_comprehensive_income, eps, eps_diluted`

meta：`remark, announce_date, announce_time, source_url, fetched_at`

去重鍵：`(stock_id, report_year, report_quarter)`。年報主旨為「第4季」，故 `report_quarter=4`。

## 與 F22 共用

- 關注清單：直接讀 `../mops_f22/關注股票列表.xlsx`（同一份）。
- Google Drive：同一個資料夾 ID，憑證 `../credentials.json`、`../token.json`，上傳檔名 `quarterly_revenue.csv`。
- Telegram：同 chatId、同 n8n 憑證名稱。

## 日常 / 排程指令

```bash
# 本機預覽某段期間（讀 CSV，不連網）
python3 backfill_send_f27.py 2026-05-14 2026-05-16 --dry-run

# 重新去 MOPS 抓並寫入 CSV、印 JSON 給 n8n（排程用）
python3 backfill_send_f27.py 2026-05-14 2026-05-16 --fetch --emit

# 單日抓取（核心腳本，印分群 JSON）
python3 mops_f27_daily.py 2026-05-15
```

## 限流（被擋）處理

MOPS 在連續快速抓太多時，明細頁會回 **`HTTP 307` + 小 body 的擋頁**（不是資料錯誤，是限流），且會**累積加重**——量一大就越擋越兇。對策：

**速率參數（環境變數）**

| 變數 | 預設 | 說明 |
|---|---|---|
| `F27_DELAY` | 0.6 | 每抓一頁明細後的間隔秒數（另加 0~0.4s 抖動） |
| `F27_LIST_DELAY` | 1.0 | 每個清單 API 請求後的間隔 |
| `F27_BACKOFF` | 3.0 | 失敗退避起始秒數（3→6→12→24） |
| `F27_RETRIES` | 4 | 每頁重試次數 |
| `F27_COOLDOWN_AFTER` | 12 | 連續失敗達此數就長冷卻 |
| `F27_COOLDOWN_SECS` | 120 | 長冷卻秒數（讓限流解除） |

**失敗會自動分流**：成功→CSV；抓取失敗（限流）→`failed_fetches.csv`（值得重抓）；解析不出（金融保險業）→`unparseable.csv`（重抓也沒用，不入重抓佇列）。第一個擋頁會存成 `last_throttle_body.html` 供診斷。

## 回補 / 補缺口流程（建議）

一次抓大量（如季報季初、回補整季）**別一口氣掃整段**，請分小段 + 慢速率。若已被擋出一堆缺口，用下列流程補齊，**不必重抓已成功的部分**：

```bash
cd mops_f27

# 1. 找缺口：比對「公告清單 vs CSV」，把缺的塞進 failed_fetches.csv
#    （只打清單 API，很輕、不易被擋）
python3 find_missing_f27.py 2026-01-01 2026-06-09

# 2. 等 20~30 分鐘讓限流解除後，慢速只補缺口
F27_DELAY=2.0 F27_BACKOFF=5 python3 refetch_failed_f27.py

# 3. 看還剩幾筆，沒歸零就隔陣子、用更慢速率再跑一次
python3 refetch_failed_f27.py --status
F27_DELAY=3.0 F27_BACKOFF=8 python3 refetch_failed_f27.py
```

`find_missing` 會抓出**所有**缺漏（被限流擋掉的＋中途中斷沒跑到的）；`refetch` 只補這些、成功即從清單移除，逐步收斂。

## 營收交叉對帳（F22 × F27）

確認 `quarterly_revenue.csv` 補齊後，用季末月累計與單季兩種口徑比對營收（容差 1%）：

```bash
# 寫對帳 CSV + 印摘要
python3 reconcile_f22_f27.py

# 預覽要推播的差異（觀察清單內的差異待查）
python3 reconcile_f22_f27.py --dry-run

# 印 JSON 給 n8n（只含觀察清單差異）
python3 reconcile_f22_f27.py --emit

# 限定期間
python3 reconcile_f22_f27.py --year 2026 --quarter 1
```

產出 `revenue_reconcile_f22_f27.csv`：依「狀態＋差異絕對值」排序、含 `in_watchlist`／`watch_group` 欄方便篩，附 EPS／淨利參考欄。狀態分 `符合 / 差異待查 / 缺F22 / 無法比對`。

## 注意事項

- **季報才有資料**：僅在財報申報期大量出現（Q1≈5/15、Q2≈8/14、Q3≈11/14、年報≈3/31），平常日多為零星補申報。每天跑四次接當日新申報，與 F22 一致。
- **金融保險業（已支援）**：銀行/保險/證券/金控的損益表科目與一般公司不同，且需「兩步驟抓取」（第一次 GET 回子公司選單中繼頁，自動補 `step=2` POST 才得真表）——這部分已內建在 `fetch_detail`，並以各業別科目對應解析，`sector_type` 欄標記業別。
  - 純銀行、保險公司多半**不申報月營收（F22）**，故對帳會是「缺F22」；其 F27 損益/EPS 仍正常入庫。
  - 票券會被歸到 `sector_type=銀行`（科目相近，不影響數字）。
  - `probe_financial_f27.py` 為金融樣本偵察腳本（一次性）。
- **對帳的合理差異**：月營收（自結）與財報營收（IFRS）可能因退回折讓、完工比例、內部交易沖銷等有小幅落差；差很大才值得查。非曆年制、KY 股可能也會落在「差異待查」。

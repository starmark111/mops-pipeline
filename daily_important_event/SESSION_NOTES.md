# Daily Important Event

## 功能
定期爬 MOPS 重大訊息，過濾關注清單公司，解析財務數字（自結/季報/年報），送 Telegram 並存 CSV 上傳 Google Drive。

## 資料夾結構
```
scripts/
├── daily_important_event/
│   ├── Daily_Important_Event_v4.py   ← 主程式
│   ├── Daily_Important_Event_v4.json ← n8n workflow（每次改完重新 import）
│   ├── 關注股票列表.xlsx              ← 觀察清單（四個分頁）
│   ├── important_events_v4.csv       ← 輸出 CSV（自動累積）
│   └── SESSION_NOTES.md
├── credentials.json                  ← Google Drive OAuth（與 mops_f22 共用，勿移動）
└── token.json                        ← Google Drive token（與 mops_f22 共用，勿移動）
```

## n8n Workflow
```
Schedule（每日 08/11/14/17/20/23 時）
→ 執行 Daily_Important_Event_v4.py
→ Code 節點（JSON parse + HTML escape）
→ Telegram（Parse Mode: HTML，每個分頁送一則）
```
⚠️ import 後需手動綁定 Telegram 憑證（Telegram - Market Bot）

## 關鍵設定（v4.py 第 37-38 行）
```python
WATCHLIST_SHEETS = []  # 留空=全部；指定如 ["Ryan"] 或 ["Ryan", "Kevin_Leo"]
```
Excel 分頁：Max_Emma（50）、Kevin_Leo（48）、Ryan（53）、其他（18）

## 篩選關鍵字

**符合任一即抓取：**
| 類型 | 關鍵字 |
|------|--------|
| 自結 | 自結合併、自結損益、財務業務等重大訊息、營運成果、注意交易資訊標準、以利投資人區別 |
| 季報 | 季合併財務報告、季個別財務報告 |
| 年報 | 年度合併財務報告、年度個別財務報告 |

**符合任一則排除（優先於上方）：**
公司債、說明會、召開、會議、提報、預計、決議日、財務比率、比率

## 關注清單邏輯

1. 執行時讀取 `關注股票列表.xlsx`，依 `WATCHLIST_SHEETS` 決定載入哪些分頁
2. 比對規則（符合任一即納入）：
   - 股票代號**完全相符**（如 `2330`）
   - 清單中的字串**包含在公司名稱內**（如清單有「國巨」→ 比對到「國巨股份有限公司」）
3. 每個分頁各自篩選，送出獨立一則 Telegram 訊息
4. 不在任何清單的公司仍會被抓到，但只出現在警告訊息中（無法解析 or 單位異常時）

財務數字統一換算為百萬元

## 2026-06-11 修復：舊公告重發 bug
- 症狀：6/8 的自結營收公告 6/11 還在重發
- 根因：`within_window` 用字串比較日期，cutoff 是補零民國格式（115/06/11），
  feed 回傳格式不同（沒補零或西元）時舊公告會被誤放行；且 sent_state.json 從未成功寫入（無備援去重）
- 修法：改用 `_parse_announce_date()` 解析成 date 物件比較（支援民國/西元、有無補零）
- 新增：`run.log` 執行紀錄、sent_state 寫入失敗會直接發 Telegram 警告、
  月營收加內容鍵去重（`_content_key`，同公司同主旨跨日只發一次）、營收行尾加公告時間
- 待觀察：下一輪排程後檢查 run.log 與 sent_state.json 是否正常產生

## 2026-08-09/10 進度：CSV損毀事件 + F22/F27/自結三支修復

**起因**：F22 `monthly_revenue.csv` 中間出現一段7,826 bytes的NUL byte損毀，
讓標準`csv.DictReader`直接crash，導致`save_to_csv()`還沒寫入就當機——整支
腳本當天全部公司都不會處理。查禾伸堂(3026)7月營收沒抓到，順藤摸瓜查出這個
系統性問題（不只禾伸堂，是整個管線的空窗期）。

**已修復（三支都改）：**
1. **CSV讀取NUL容錯**：F22(`mops_f22_daily.py`的`_old_row`/`save_to_csv`)、
   F27(`mops_f27_daily.py`新增`_read_csv_rows()`共用函式)、本支
   (`Daily_Important_Event_v4.py`新增`_read_csv_rows()`，套用到`save_to_csv`
   與`_load_ref_dbs`讀F22/F27/自己CSV共3處)，遇到NUL byte會濾除+印警告，
   不再讓整支腳本當機。
2. **原子寫入**：本支`save_to_csv()`最後的整檔重寫改用`_atomic_write_csv()`
   （先寫暫存檔，`os.replace()`換檔），避免寫到一半被中斷留下壞檔。
   （F22目前整檔重寫、F27是append，暫未改F22的寫入方式，之後可比照處理）
3. **已發送去重**：F22、F27新增`sent_state_f22.json`/`sent_state_f27.json`
   （key=股號+期間+數字），防止n8n重跑/重試造成重複發送；本支原本就有
   `sent_state.json`但用co_id/date/time當key，容易因時間格式不一致失效。
4. **F22清單解析靜默跳過→改印警告**：`fetch_f22_list()`主旨解析不出年月時
   （regex匹配失敗），改印出公司代號+完整主旨原文，不再默默丟棄。
5. **F22回補腳本加重試+自動降速**：`check_backfill_f22.py`偵測到連續5筆
   解析失敗（疑似被MOPS限流擋掉）會自動把間隔從0.4秒放慢到2秒，並重試一次；
   解析失敗時印出回應片段（HTTP狀態碼+內容前150字）方便診斷。
   **8/6~8/9那次回補517筆缺漏只補回66筆、476筆「無法解析」（含禾伸堂），
   待重跑`check_backfill_f22.py 2026-08-06 2026-08-09`驗證這次能否救回。**

**其他這幾天順手修的：**
- 3167大量、8150南茂、1310台苯：qtr_label年份/季度標錯（同一bug家族，已在
  `extract_period_labels()`加寬regex，接受「同期」二字插在標籤與增減%中間）
- 6488環球晶：公司公告表格本身把「單季」兩欄都誤標「115年第1季」，程式抓錯欄，
  改用正確欄位並記錄在`db_review_queue.csv`
- 4915致伸：新增格式F，辨識「XXX年MM月至MM月」季度區間表頭（原本會誤判成單月）
- 2327國巨\*：敘述文字型自結公告解析器抓不到，改用官方H1累計財報反推Q2數字
- 8105凌巨：財報延期申報准駁通知被誤判成季報，`EXCLUDE_KEYWORDS`新增
  「延期」「展延」「准駁」
- 早鳥版月營收標頭加註「(早鳥·非正式)」，避免跟F22正式版訊息長得太像

**F27格式異動**：`format_company_block()`改成多行、獨立欄位風格（比照本支
「季報（董事會通過）」版型），`backfill_send_f27.py`因直接呼叫該函式會自動
套用新格式，`query_company_f27.py`是獨立邏輯不受影響。

**待辦**：
- [x] 驗證8/6~8/9重跑`check_backfill_f22.py`是否救回476筆「無法解析」（已救回605筆）
- [x] F22整檔重寫也比照本支加`_atomic_write_csv`（2026-08-24 完成）
- [ ] 3374精材那筆Q1營收異常還缺原始公告全文，尚未解決

---

# 2026-08-14 ~ 09-10 進度

## 一、去重機制搬到「n8n實際執行的腳本」

**根因**：n8n呼叫的是 `backfill_send_f22.py` / `backfill_send_f27.py`（`--fetch --emit`），
不是 `mops_f22_daily.py` / `mops_f27_daily.py`。先前加在 `_daily.py` 的 `run()` 裡的
sent_state 去重從未被執行過，導致同一天多輪排程重複發送。

**修法**：把 `_dedupe_against_sent_state()` 搬進兩支 backfill 腳本的
`build_messages_from_csv()` 與 `build_messages_for_date()`（後者才是 `--fetch` 路徑）。

**關鍵陷阱**：`_mark_sent()` 不能寫在組訊息函式裡——`main()` 的 dry-run／emit／send
分流發生在之後，會導致 `--dry-run` 預覽也把項目標記成已發送，之後永遠不再發出。
改成累積到 `_PENDING_MARK`，由 `main()` 結尾 `if not dry_run: flush_pending_mark()`。

驗證：連跑3次 dry-run，sent_state 筆數與檔案時間戳皆未變動。

## 二、訊息格式與標籤

- M99（自結速報）、M31（季報董事會通過）標題加上代號，與 F22/F27 命名一致
- M31 新增「⚡ 領先指標」：查 `_F27_QTR` 若該季 F27 官方數字尚未出現則標註
- F22/F27 新增新高標註（🚀單月／單季、🚀累計，均為「資料庫收集以來」）
- **累計欄標籤錯誤修正**：`format_company_block_v2()` 季段原本一律標「季單季」，
  但「當月數／累計數」型公告（如 3042 晶技 115/07）第二欄其實是年初至今累計。
  改為依 month_label 還原成「1-N月累計」，F22 月營收加總能對上時加註「（DB核對）」。
  回歸測試：1,608 筆存檔公告，1,592 筆輸出完全相同，17 筆為預期中的重新標記，0 例外。

## 三、資料清理

**3000 髒資料**：`important_events_v4.csv` 有 916 個欄位值為裸字串 "3000"（無千分位），
涉及 211 筆、98 家公司、114/01~115/06。根因在 `_fmt_num()`：單位為百萬元時
`factor==1` 直接原樣回傳字串，未經格式化，解析殘留 token 因此混入。
以 F22 官方月營收交叉驗證 131 筆可比對案例，127 筆確定錯誤 → 全數清空並在 note 註記。
Bug 本身已在 115/07 前修復（其後 152 筆同條件紀錄 0 筆再現）。

**誤判記錄**：曾懷疑「民國年被當數字擷取」（113/114/115 等值），查證後為誤判——
百和 116 是稅前淨利 116,351 仟元、揚博 113 是 113 百萬，均為正確值，未做更動。

## 四、F22 缺漏月份反推補值（新工具）

`mops_f22/derive_missing_f22.py`：用相鄰月份累計欄反推缺漏月份，不需連網。

```
N月營收 = ytd(N+1) - ytd(N-1) - 單月(N+1)      # N=1 時 ytd(N-1) 視為 0
```

三個輸入值全部來自 MOPS 官方欄位，屬官方數字的算術重組，非估計值。

**可靠度**（兩層驗證）：
- 公式回測 27,731 筆已知月份：90.18% 分毫不差、99.15% 誤差 <0.5%
- 獨立交叉驗證：以隔年同月公告的 `revenue_last_year` 欄（不參與計算）比對 595 筆，99.2% 相符
- 防呆：與官方去年同期欄差異 >5% 則不補，實測擋掉 3 筆（2832 台產、2852 第一保、1313 聯成，
  前兩者為保險業重編報表）

已補入 979 筆。`monthly_revenue.csv` 新增 `data_source` 欄
（空=官方、`derived_ytd`=反推）。**必須列在 `CSV_COLUMNS` 中**，否則
`save_to_csv()` 整檔重寫時會被 `extrasaction="ignore"` 靜默丟棄。
官方數字日後回補時會整列覆蓋，標記自動清除，不需手動清理。

訊息顯示：去年值後加 ‡，尾部圖例改為「*官方YoY †DB推估 ‡去年值由累計欄反推」。

## 五、F22 原子寫入（`save_to_csv`）

原本直接對正式檔開 `w`，寫到一半中斷會留下半截檔案（即 2026-08 NUL byte 事故類型）。
改為：寫暫存檔 → `flush()` + `os.fsync()` → `os.replace()` 原子換檔；
失敗則清除暫存檔並拋出，正式檔不受影響。暫存檔須與正式檔同目錄（跨檔案系統無法原子換檔）。

驗證：模擬寫入第 5,000 列時崩潰 → 正式檔 35,779 列、雜湊完全相同、暫存檔已清除。
對照組（舊寫法）同情境下檔案僅剩開頭數列。

## 六、排程漏抓事故（重要）

**現象**：8月營收（9月上旬公告）大量漏抓。9/7 當天 315 筆公告只抓到 107 筆。

**根因有二**：
1. n8n 指令為 `DATE=$(date +%Y-%m-%d); ... $DATE $DATE --fetch --emit`，
   起訖日皆為當天，**每輪只看當天、從不回頭**。原排程最後一輪為 18:00，
   因此 18:00~24:00 發布的公告（營收公告最密集時段）永久掉入縫隙。
2. cron 為 `0 0,6,12,18 * * 1-5`，週末不跑。實測週末確實有公告
   （9/5 週六 11 筆含鴻海、鴻準、大立光；9/6 週日 4 筆），100% 漏抓。

**修法**（2026-09-08 已於 n8n 介面調整 F22/F27 兩個工作流程）：
cron 改為 `0 6,12,18,23 * * *` — 0 點那輪移到 23:00（當天公告發完才抓）、加入週末。

**效果**：9/7 缺漏從 208 筆降為 3 筆（且該 3 筆為投控股格式，屬已知不支援）；
9/6 週日 4 筆公告缺 0。

**已回補**：9/1~9/7 全數補齊（9/7 單日補 205 筆）。

## 七、尚未解決

- [ ] **回補的資料不會發 Telegram**：`check_backfill_f22.py` 只寫 CSV。
      經比對 sent_state，8月營收有 32 檔已入庫但從未發送
      （含禾伸堂、大立光、鴻海、創意、致茂、信昌電、川湖）。
      補發方式：`python3 backfill_send_f22.py <起日> <迄日>`（會自動跳過已發送者）。
      **M99 沒有補發模式**——帶日期參數是歷史模式，只印 JSON 不發送，需另行處理。
- [ ] 9/9~9/10（8月營收申報截止日）的公告尚未查核，需跑
      `check_backfill_f22.py 2026-09-01 2026-09-10 --dry-run --no-gdrive` 確認缺漏
- [ ] 7月營收僅收錄 1,248 家（正常約 1,800），為 8 月上旬漏抓的舊帳，待回補
      `check_backfill_f22.py 2026-08-01 2026-08-12`
- [ ] Google Drive 上傳失敗（套件未安裝）：
      `pip3 install --user google-api-python-client google-auth-oauthlib`
- [ ] `1626 艾美特-KY 2026/6` 有兩列完全相同（CSV 損毀事故殘留，無害）
- [ ] 3374 精材 Q1 稅前／淨利／EPS 待原始公告確認（營收已確認正確）
- [ ] 投控股格式（3715 定穎投控、3709 鑫聯大投控、3713 新晶投控等）解析不支援

## 七之二、M99（自結／重訊）現況與已知問題

### 排程與 F22/F27 不同，未受漏抓事故波及

M99 的 n8n 指令是 `python3 Daily_Important_Event_v4.py`（**不帶日期參數**），
排程 `0 8,11,14,17,20,23 * * *` — 本來就每天跑、含週末、末輪 23:00。
因此第六節的漏抓事故（末輪 18:00 + 週末不跑）**沒有波及 M99**。

且 M99 內建防漏設計：即時模式會同時抓即時 feed、REST API 今日、以及
`get_lookback_dt()` 回溯日，三者合併去重，再用當日視窗過濾。
去重靠 `sent_state.json`（`_sent_key` + `_content_key` 雙鍵），`SENT_KEEP_DAYS = 4`。

實測 115/09/01~09/10 每日 4~8 筆穩定入庫，無斷日。

### 已知問題（依嚴重度）

**1. 沒有補發模式（與 F22/F27 不對等）**
`Daily_Important_Event_v4.py <日期>` 是歷史模式，只印 JSON 到 stdout、不發 Telegram，
也不寫 sent_state。因此一旦某輪漏發（例如公司當時不在清單、或 n8n 當下失敗），
**無法補發**。F22/F27 有 `backfill_send_*.py` 可補，M99 沒有對應工具。
實例：2455 全新、3450 聯鈞 於 115/09/01 公告時不在清單，資料已入庫但通知永久遺失。

**2. 月營收類型完全不留痕跡**
`run()` 中 `revenue_items` 迴圈只組訊息，**既不寫 CSV 也不呼叫 `_archive_item()`**
（CSV 中 `type=月營收` 為 0 筆）。後果：
- 無法事後查核早鳥版月營收發了什麼、數字對不對
- 沒有樣本可判斷 `extract_monthly_revenue()` 還漏抓哪些欄位

**3. `extract_monthly_revenue()` 擷取欄位偏少**
目前只取 7 項：month_label、cur、cur_unit、mom、yoy、ytd、ytd_yoy。
原文常見但未擷取：毛利／毛利率、營業利益／營益率、稅後淨利／淨利率、EPS、
**下季財測展望**（如聯發科、聯詠會給營收區間、毛利率區間、營業費用率區間及匯率假設）、
去年同期絕對數字、增減原因說明。
（註：聯發科／聯詠那類其實走 `extract_financials_both()`，非本函式；
真正的純月營收公告因問題 2 沒有樣本，實際漏抓什麼尚未查證。）

**4. 解析失敗仍有 58 筆（raw_archive 1,796 筆中）**

| 類別 | 筆數 | 說明 |
|---|---|---|
| sanity 警示 | 39 | 數學自檢異常，已發訊息但標⚠️待人工核對 |
| 解析不到財務數字 | 9 | 多為金控（2885 元大金、2883 凱基金）等特殊格式 |
| 季報解析不到數字 | 7 | 3529 力旺、8105 凌巨、4927 泰鼎-KY、6726 鑫亞電通、4117 普生 |
| 單位無法識別 | 3 | 3293 鈊象等 |

**5. 從未做過缺漏查核**
F22 有 `check_backfill_f22.py` 比對 MOPS 清單找漏，**M99 沒有對應工具**，
也從未實際比對過 `important_events_v4.csv` 與 MOPS 重訊清單。
因此「M99 有沒有漏抓」目前**無法回答**——每日 4~8 筆看似穩定，
但那只是「我們抓到的」，不等於「MOPS 上有的」。

**6. raw_archive 僅涵蓋 2026-07-27 起**
36 天、1,796 檔。此前的公告無原文可回溯，
先前 3374 精材、3000 髒資料等問題無法查證根因即因於此。

### 建議優先序
1. 加缺漏查核工具（問題 5）——不知道漏多少就無從談可靠度
2. 月營收類型補上 `_archive_item()`（問題 2）——成本極低，且是問題 3 的前置
3. 加補發模式（問題 1）
4. 擴充擷取欄位（問題 3）——需先有問題 2 的樣本

## 七之三、回補與補救機制總覽（2026-09-10 盤點）

### F22（最完整，四種工具）

| 用途 | 指令 | 是否連網 | 觸發方式 |
|---|---|---|---|
| 查漏（只報告） | `check_backfill_f22.py <起> <迄> --dry-run --no-gdrive` | 是 | 手動 |
| 補資料 | `check_backfill_f22.py <起> <迄> --no-gdrive` | 是 | 手動 |
| 補訊息 | `backfill_send_f22.py <起> <迄>` | 否（讀CSV） | 手動 |
| 離線補值 | `derive_missing_f22.py [--stock N] [--dry-run]` | 否 | 手動 |

- 查漏工具內建 WAF/限流偵測（`WAF_MARKERS`：因為安全性考量／FOR SECURITY REASONS／
  Overrun／Too many query requests），偵測到即進入 60 秒全域冷卻後重試。
- 連不上 MOPS 時會明確輸出「無法驗證：ezsearch API 完全連不上，本次檢查結果不可信」，
  不會誤報 0 缺漏。
- `backfill_send_f22.py` 靠 sent_state 自動跳過已發送者，可安全重跑。
- **n8n 另有工作流程「MOPS F22 Backfill - 回補檢查」**（每日 02:00／14:00，
  執行 `check_backfill_f22.py --hours 48 --emit` 並推播回補摘要），
  但目前 `active: False` **停用中**。若啟用，2026-09 那次漏抓可被自動補上。

### F27（中等）

| 用途 | 指令 | 說明 |
|---|---|---|
| 查漏 | `find_missing_f27.py <起> <迄>` | 只打清單 API（輕量），結果寫入 `failed_fetches.csv` |
| 補資料 | `F27_DELAY=2.0 F27_BACKOFF=5 python3 refetch_failed_f27.py` | 只重抓 failed 名單，慢速避免再被擋 |
| 補訊息 | `backfill_send_f27.py <起> <迄>` | 同 F22 |
| 交叉對帳 | `reconcile_f22_f27.py` | F22 季末月累計 vs F27 季累計，容差 1% |

無 F22 那種一鍵查補的整合工具，需兩步驟（find → refetch）。

### M99（幾乎沒有）

- 查漏工具：**無**
- 補發模式：**無**（帶日期參數是歷史模式，只印 JSON 不發送）
- 僅有的防護：腳本內建三路抓取（即時 feed + REST API 今日 + `get_lookback_dt()` 回溯日）
  合併去重，加上 `sent_state.json` 雙鍵防重複發送、`SENT_KEEP_DAYS = 4`。
- 結論：**漏了就是漏了，事後補不回來**。

### 自動化補救：`daily-parse-verify`（排程 Claude 任務，非 n8n）

平日 18:30 執行，兩部分：
- Part A：核對當日 `raw_archive` 解析正確性，可自動修正確定的解析 bug
- Part B（2026-08-10 新增）：CSV NUL byte 檢查（發現即備份並清除）、
  三份 CSV 新鮮度檢查（>36 小時告警）、F22 缺漏抽查（`--days 2 --dry-run`）

**限制**：Part B 的缺漏「只標記不回補」——當初刻意如此設計，
避免無人看著時觸發 MOPS WAF/限流被大範圍擋下。結果寫入 `PARSE_VERIFY_LOG.md`。

### 三個結構性缺口

**1. 回補只救資料，不救通知**
`check_backfill_f22.py` 只寫 CSV；正常排程只看當天新公告、不回頭掃 CSV。
因此回補進來的資料**永遠不會觸發訊息**。
實例：2026/08 月營收有 32 檔已入庫但從未發送（禾伸堂、大立光、鴻海、創意、
致茂、信昌電、川湖等）。需另外手動跑 `backfill_send_f22.py` 才會補發。

**2. 全部手動觸發**
唯一自動化的是 `daily-parse-verify`，但它只標記不回補。
F22 那支能自動回補的 n8n 工作流程處於停用狀態。

**3. 沒有告警**
漏抓發生時無任何主動通知。2026-09 那次是使用者察覺「訊息變少」才追查出來的，
距離事發已數日。

### 建議補強優先序

1. **啟用 n8n「MOPS F22 Backfill - 回補檢查」工作流程** — 現成的，
   開啟即有每日兩次自動查補（但仍不會補發訊息，見缺口 1）
2. **讓回補後自動補發訊息** — 於 `check_backfill_f22.py` 補完後接續呼叫
   `backfill_send_f22.py`，或加 `--send` flag
3. **M99 加查漏工具** — 目前完全黑箱，連漏多少都不知道

## 八、觀察清單異動（`watchlist_active.csv`，178 檔）

| 群組 | 檔數 |
|---|---|
| Ryan | 118（Leo／Ryan 23 + Max／Ryan 95）|
| Max | 97 |
| Monitor List | 50 |
| Leo | 26 |
| Edison | 7 |
| Sam | 1 |

- 依 Book2.xlsx 更新 Ryan 清單為 118 檔（Ryan_1 + Ryan_2），僅移除 Ryan 歸屬、
  不動 Leo/Max 的覆蓋範圍（3306、6980 轉為 Leo 獨有；3580、6425 轉為 Max 獨有；
  8 檔鞋類因僅 Ryan 負責而整列移除）
- Monitor List 加入「其他」分頁台股 31 檔，排除 18 項非台股（.JP 日股、QEFFF 等）
- 陸續加入：3008 大立光、3406 玉晶光、2455 全新、3450 聯鈞、3605 宏致、2059 川湖
- 移除 7 檔重複標記（原同時掛 Max／Ryan 與 Monitor List，會收到兩則相同訊息）：
  2327、2492、2493、3026、3167、6739、8064

⚠️ **注意**：`build_watchlist.py` 會從 `關注股票列表.xlsx` 重新產生此檔，
會覆蓋上述所有手動調整（含全部 Monitor List 標記）。要長期保留須同步回主檔，
或改用 `watchlist_extra.csv` 疊加檔維護。

## 舊檔案清理
- 2026-06-11 已刪除：舊版/副本 scripts（market_morning_check*、rotation.py、
  three_big_investors_oold.py、test_onclick.py、fix_csv_dates_*、backfill_send_f22拷貝.py）、
  舊 CSV 與 .bak 備份、舊 workflow JSON（daily_event_n8n_workflow.json、
  n8n_workflow_simple/fixed.json、mops_f22_daily.json.bak）

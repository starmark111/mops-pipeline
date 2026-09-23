"""
mops_f27_daily.py
每日 F27 季度綜合損益表彙整，依「關注股票列表.xlsx」分群送出訊息

用法：
  python3 mops_f27_daily.py              # 今天
  python3 mops_f27_daily.py yesterday   # 昨天
  python3 mops_f27_daily.py 2026-05-15  # 指定日期

輸出：JSON 陣列，格式與 mops_f22_daily.py 相同
  [{"message": "...", "group": "ryan"}, ...]

資料來源：MOPS ezsearch（PRO_ITEM=F27，AN_CODE=F27，綜合損益表）
  明細頁 = ajax_t164sb04（合併/個別綜合損益表）

數字單位：仟元（與 MOPS 原始資料一致），顯示時換算為百萬；EPS 為元。

── 與 F22 的對齊關係 ───────────────────────────────────────────────────────
  F27 的損益表一列有 4 個金額欄：
    [0] 本期累計（如 1~3 月）   → ytd_revenue（對應 F22 的累計）
    [1] 本期單季（第 N 季）     → revenue    （對應 F22 的本月概念，單期）
    [2] 去年同期累計           → ytd_revenue_last
    [3] 去年同期單季           → revenue_last_year
  Q1 時累計＝單季，故 [0]=[1]、[2]=[3]。
  CSV 前段欄位刻意與 monthly_revenue.csv 對齊（stock_id…ytd_yoy_pct），
  方便兩張表直接 join / 比對；後段再附 F27 才有的獲利與 EPS 欄位。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import requests
import pandas as pd
from datetime import datetime, timedelta


# =========================
# 路徑設定
# =========================
_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_DIR)

# 要查詢的市場：sii=上市, otc=上櫃, rotc=興櫃（daily / backfill / find_missing 共用）
MARKETS = ("sii", "otc", "rotc")

# 共用主檔：scripts/關注股票列表.xlsx（_PARENT 即 scripts 目錄）
# 2026-06 新格式：改讀「原始資料」分頁（股票代號 / 股票名稱 / 研究員 / 實習生）。
# 只收研究員或實習生有內容者；不分群，每檔加註負責人名字。
WATCHLIST_PATH   = os.path.join(_PARENT, "關注股票列表.xlsx")
WATCHLIST_SHEET  = "原始資料"   # 主檔新格式分頁（fallback 用）
# 優先讀精簡小清單 CSV（build_watchlist.py 產生），不存在才退回主檔 xlsx。
WATCHLIST_CSV    = os.path.join(_PARENT, "watchlist_active.csv")

_WATCHLIST     = {}   # {group_key: {"display","names"}}；group_key = 主檔組合(如 Leo／Ryan) 或 疊加者(如 Edison)
_STOCK_GROUPS  = {}   # 代號(str) / 正規化公司名 -> [所屬群組, ...]


# =========================
# 已發送狀態（訊息去重，避免 n8n 重跑/重試造成重複發送）
# =========================
SENT_STATE_PATH = os.path.join(_DIR, "sent_state_f27.json")
SENT_KEEP_DAYS  = 100   # 季報間隔較長，保留久一點避免季度交界誤判成新內容


def _sent_key(item: dict, rec: dict) -> str:
    """以「股號＋所屬年季＋累計營收數字」當key：同一期間同一數字視為已發送過；
    若公司更正重發（數字不同）仍視為新內容，照常送出。"""
    return f"{item['stock_id']}|{item['report_year']}|{item['report_quarter']}|{rec.get('ytd_revenue', '')}"


def _load_sent_state() -> dict:
    if not os.path.exists(SENT_STATE_PATH):
        return {}
    try:
        with open(SENT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sent_state(state: dict) -> bool:
    cutoff = datetime.now() - timedelta(days=SENT_KEEP_DAYS)
    pruned = {}
    for k, v in state.items():
        try:
            if datetime.strptime(v, "%Y-%m-%d %H:%M:%S") >= cutoff:
                pruned[k] = v
        except Exception:
            pruned[k] = v
    try:
        with open(SENT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        sys.stderr.write(f"⚠️  sent_state_f27.json 寫入失敗：{e}\n")
        return False


def _wl_norm(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() == "nan":
        return ""
    return s.rstrip("*").strip()


def _read_watchlist_rows():
    """回傳 [(代號, 名稱, 主檔組合字串, [疊加人,...]), ...]。CSV 優先，否則退回主檔 xlsx。
    主檔組合 = 研究員／實習生 合成一組；疊加人（如 Edison）每人各自一組。"""
    if os.path.exists(WATCHLIST_CSV):
        import csv
        rows = []
        with open(WATCHLIST_CSV, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                team = "／".join(o.strip() for o in (r.get("owners") or "").split("／") if o.strip())
                extra = [o.strip() for o in (r.get("extra") or "").split("／") if o.strip()]
                rows.append((_wl_norm(r.get("stock_id")), _wl_norm(r.get("name")), team, extra))
        return rows
    df = pd.read_excel(WATCHLIST_PATH, sheet_name=WATCHLIST_SHEET, dtype=str)
    rows = []
    for _, r in df.iterrows():
        owners = [x for x in (_wl_norm(r.get("研究員")), _wl_norm(r.get("實習生"))) if x]
        rows.append((_wl_norm(r.get("股票代號")), _wl_norm(r.get("股票名稱")), "／".join(owners), []))
    return rows


# =========================
# 觀察清單載入（新格式）
# 主檔研究員＋實習生＝合成「一組」（一則，如 👤 Leo／Ryan）；疊加檔的人（如 Edison）各自獨立一組。
# 一檔可同時屬於多組（如廣達 → Leo／Ryan 一則 + Edison 一則）。
# =========================
def _load_watchlist():
    global _WATCHLIST, _STOCK_GROUPS
    _WATCHLIST = {}
    _STOCK_GROUPS = {}
    groups = {}   # group_key -> set(代號/名稱)
    for code, name, team, extra in _read_watchlist_rows():
        gkeys = ([team] if team else []) + extra
        if not gkeys:
            continue
        if code:
            _STOCK_GROUPS[code] = gkeys
        if name:
            _STOCK_GROUPS.setdefault(name, gkeys)
        for g in gkeys:
            if code:
                groups.setdefault(g, set()).add(code)
            if name:
                groups.setdefault(g, set()).add(name)
    for g in sorted(groups):
        _WATCHLIST[g] = {"display": g, "names": groups[g]}

    # ---- 負責人過濾（與 F22 共用 scripts/send_owners_f22.txt；環境變數 F27_OWNERS 可覆寫）----
    # 檔案不存在且無環境變數 → 不過濾（與舊版相同）。只影響發送分組，不影響抓取存檔。
    allow = _load_owner_filter()
    if allow:
        keep = {g for g in _WATCHLIST if set(g.split("／")) & allow}
        _WATCHLIST = {g: v for g, v in _WATCHLIST.items() if g in keep}
        for k in list(_STOCK_GROUPS):
            gs = [g for g in _STOCK_GROUPS[k] if g in keep]
            if gs:
                _STOCK_GROUPS[k] = gs
            else:
                del _STOCK_GROUPS[k]


def _load_owner_filter() -> set:
    """回傳允許的負責人名字集合；空集合 = 不過濾。"""
    raw = os.environ.get("F27_OWNERS", "").strip()
    if not raw:
        path = os.path.join(_PARENT, "send_owners_f22.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                raw = ",".join(ln.strip() for ln in f
                               if ln.strip() and not ln.strip().startswith("#"))
    if not raw:
        return set()
    return {x.strip() for x in re.split(r"[,，／/]", raw) if x.strip()}


def get_groups(co_id: str, co_name: str) -> list:
    """回傳此公司所屬的群組（命中回 [主檔組合, 疊加者...]，否則 []）。先比代號，再比公司名子字串。"""
    cid = _wl_norm(co_id)
    if cid in _STOCK_GROUPS:
        return _STOCK_GROUPS[cid]
    cn = _wl_norm(co_name)
    for nm, gk in _STOCK_GROUPS.items():
        if not nm.isdigit() and nm and nm in cn:
            return gk
    return []


# =========================
# API 設定
# =========================
EZSEARCH_URL = "https://mopsov.twse.com.tw/mops/web/ezsearch_query"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin":  "https://mopsov.twse.com.tw",
    "Referer": "https://mopsov.twse.com.tw/mops/web/ezsearch",
}

# ── 速率控制（可用環境變數調整，回補大量資料時建議調慢一點）──
SLEEP_LIST    = float(os.environ.get("F27_LIST_DELAY", "1.0"))   # 每個 ezsearch 清單請求後
SLEEP_DETAIL  = float(os.environ.get("F27_DELAY", "0.6"))        # 每抓一頁明細後（會加 0~0.4s 抖動）
MAX_RETRIES   = int(os.environ.get("F27_RETRIES", "4"))          # 明細頁失敗重試次數
BACKOFF_BASE  = float(os.environ.get("F27_BACKOFF", "3.0"))      # 退避起始秒數（3→6→12→24）
COOLDOWN_AFTER = int(os.environ.get("F27_COOLDOWN_AFTER", "12")) # 連續失敗達此數，長冷卻
COOLDOWN_SECS  = int(os.environ.get("F27_COOLDOWN_SECS", "120")) # 長冷卻秒數（讓 MOPS 限流解除）

# 失敗紀錄檔（供事後重抓）／無法解析檔（金融保險等，僅供檢視，不自動重抓）
FAILED_LOG   = os.path.join(_DIR, "failed_fetches.csv")
UNPARSED_LOG = os.path.join(_DIR, "unparseable.csv")
LOG_COLUMNS  = ["stock_id", "company_name", "market", "industry",
                "report_year", "report_quarter", "announce_date", "announce_time",
                "source_url", "reason", "failed_at"]

_DEBUG_SAVED = False   # 只存第一個擋頁 body 供診斷

T164_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t164sb04"


def _is_interstitial(html: str) -> bool:
    """金融公司第一次 GET 會回「子公司選單」中繼頁（有 fm1 表單與『詳細資料』鈕、無會計項目）。"""
    return ("name='fm1'" in html or 'name="fm1"' in html) and "詳細資料" in html and "會計項目" not in html


def _fetch_step2(session: requests.Session, interstitial_html: str, co_id: str) -> str:
    """對金融中繼頁補打 step=2 POST，取得真正的綜合損益表。"""
    def hid(name, default=""):
        m = re.search(r'name="%s"[^>]*value="([^"]*)"' % name, interstitial_html)
        return m.group(1) if m else default
    data = {
        "id": "", "key": "", "TYPEK": hid("TYPEK", "sii"), "step": "2",
        "year": hid("year"), "season": hid("season"), "co_id": co_id, "firstin": "1",
    }
    r = session.post(T164_URL, data=data, headers=HEADERS, timeout=30)
    return r.text


def fetch_detail(session: requests.Session, url: str) -> str | None:
    """抓單頁明細，含指數退避重試。
    - 金融公司會先回「子公司選單」中繼頁，自動補 step=2 POST 取得真表。
    - MOPS 限流時常回 307 + 小 body 的擋頁；偵測到就重試，並把第一個擋頁存檔供診斷。
    成功標準：內容含『會計項目』（一般與金融損益表皆有）。"""
    global _DEBUG_SAVED
    delay = BACKOFF_BASE
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
            text = resp.text if resp.status_code == 200 else ""
            if text and _is_interstitial(text):
                m = re.search(r"co_id=(\w+)", url)
                if m:
                    try:
                        text = _fetch_step2(session, text, m.group(1))
                    except Exception as e:
                        sys.stderr.write(f"     ↻ step2 失敗：{e}\n")
            if text and "會計項目" in text:
                return text
            if not _DEBUG_SAVED:
                try:
                    with open(os.path.join(_DIR, "last_throttle_body.html"), "w", encoding="utf-8") as f:
                        f.write(f"<!-- HTTP {resp.status_code}  url={url} -->\n{resp.text}")
                    _DEBUG_SAVED = True
                except Exception:
                    pass
            sys.stderr.write(f"     ↻ 第{attempt}次內容異常（HTTP {resp.status_code}, len={len(resp.text)}）\n")
        except Exception as e:
            sys.stderr.write(f"     ↻ 第{attempt}次失敗：{e}\n")
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay *= 2
    return None


# =========================
# 失敗紀錄管理（持久化，供事後重抓）
# =========================
def _read_log(path) -> dict:
    """讀紀錄檔 → {(stock, year, quarter): row}"""
    out = {}
    for r in _read_csv_rows(path):
        out[(r["stock_id"], r["report_year"], r["report_quarter"])] = r
    return out


def _write_log(path, rows_by_key):
    import csv as _csv
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows_by_key.values():
            w.writerow(r)


def _item_to_logrow(item, reason):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "stock_id": item["stock_id"], "company_name": item.get("company_name", ""),
        "market": item.get("market", ""), "industry": item.get("industry", ""),
        "report_year": item["report_year"], "report_quarter": item["report_quarter"],
        "announce_date": item.get("announce_date", ""), "announce_time": item.get("announce_time", ""),
        "source_url": item["source_url"], "reason": reason, "failed_at": now,
    }


def logrow_to_item(r) -> dict:
    return {
        "stock_id": r["stock_id"], "company_name": r["company_name"],
        "market": r["market"], "industry": r["industry"],
        "report_year": int(r["report_year"]), "report_quarter": int(r["report_quarter"]),
        "announce_date": r["announce_date"], "announce_time": r["announce_time"],
        "source_url": r["source_url"],
    }


def update_logs(results, fetch_failed, parse_none):
    """results: [(item,rec)]；fetch_failed/parse_none: [item]
    成功者從 FAILED_LOG 移除；抓取失敗者寫入 FAILED_LOG；無法解析者寫入 UNPARSED_LOG。"""
    def key(it): return (str(it["stock_id"]), str(it["report_year"]), str(it["report_quarter"]))

    failed = _read_log(FAILED_LOG)
    for item, _ in results:
        failed.pop(key(item), None)                    # 這輪成功 → 移除舊失敗紀錄
    for item in parse_none:
        failed.pop(key(item), None)                    # 解析不出（非限流）→ 不留在重抓佇列，改進 unparseable
    for item in fetch_failed:
        failed[key(item)] = _item_to_logrow(item, "fetch_failed")
    _write_log(FAILED_LOG, failed)

    if parse_none:
        unp = _read_log(UNPARSED_LOG)
        for item in parse_none:
            unp[key(item)] = _item_to_logrow(item, "parse_none")
        _write_log(UNPARSED_LOG, unp)


# =========================
# 共用：逐筆抓取 + 解析（含連續失敗長冷卻）
# =========================
def process_items(session, items):
    """回傳 (results, fetch_failed, parse_none)。
    fetch_failed＝抓不到（多為限流 307），值得事後重抓；
    parse_none＝抓到了但解析不出（多為金融保險業格式），重抓也沒用。"""
    import random
    results, fetch_failed, parse_none = [], [], []
    consecutive = 0
    for item in items:
        html = fetch_detail(session, item["source_url"])
        if html is None:
            fetch_failed.append(item)
            consecutive += 1
            sys.stderr.write(f"  ⚠️  {item['stock_id']} 多次重試後仍抓不到（已記錄待重抓）\n")
            if consecutive >= COOLDOWN_AFTER:
                sys.stderr.write(f"  ⏸️  連續 {consecutive} 筆失敗，疑似被限流，冷卻 {COOLDOWN_SECS}s…\n")
                time.sleep(COOLDOWN_SECS)
                consecutive = 0
            continue
        consecutive = 0
        rec = parse_income_statement(html)
        if rec:
            results.append((item, rec))
        else:
            parse_none.append(item)
            sys.stderr.write(f"  ⚠️  {item['stock_id']} 無法解析（可能為金融保險業格式）\n")
        time.sleep(SLEEP_DETAIL + random.uniform(0, 0.4))
    return results, fetch_failed, parse_none

# CSV / Google Drive
CSV_PATH         = os.path.join(_DIR, "quarterly_revenue.csv")
GDRIVE_FOLDER_ID = "1PSZv1zVCK1Z5McaeKyxeDZynf1JIb1tt"
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]
CREDS_PATH       = os.path.join(_PARENT, "credentials.json")
TOKEN_PATH       = os.path.join(_PARENT, "token.json")
GDRIVE_FILENAME  = "quarterly_revenue.csv"

CSV_COLUMNS = [
    # ── 與 monthly_revenue.csv 對齊的前段（key + 營收 + YoY）──
    "stock_id", "company_name", "market", "industry", "sector_type",
    "report_year", "report_quarter",
    "revenue", "revenue_last_year", "yoy_amt", "yoy_pct",
    "ytd_revenue", "ytd_revenue_last", "ytd_yoy_amt", "ytd_yoy_pct",
    # ── F27 損益表才有的獲利與 EPS（皆為本期累計，仟元；EPS 為元）──
    "gross_profit", "operating_income", "pretax_income",
    "net_income", "net_income_parent", "total_comprehensive_income",
    "eps", "eps_diluted",
    # ── meta（與 F22 對齊）──
    "remark", "announce_date", "announce_time", "source_url", "fetched_at",
]

# 依人員組合分群後，只送「當天有公告」的組合（空組合不送）。
# 某組合不管有無公告都想收 → 把組合名加進此 set（如 {"Max／Ryan"}）；設 "all" = 全部強制送。
ALWAYS_SEND_GROUPS = set()
SEPARATOR = "————————"
MAX_LEN   = 4000


# =========================
# 日期工具
# =========================
def parse_date_arg() -> datetime:
    if len(sys.argv) < 2:
        return datetime.now()
    arg = sys.argv[1]
    if arg == "yesterday":
        return datetime.now() - timedelta(days=1)
    try:
        return datetime.strptime(arg, "%Y-%m-%d")
    except Exception:
        sys.stderr.write("❌ 日期格式錯誤，請用 YYYY-MM-DD 或 yesterday\n")
        sys.exit(1)


# =========================
# 抓取 F27 公告清單
# =========================
def fetch_f27_list(session: requests.Session, dt: datetime, market: str) -> list:
    """抓取指定日期 / 市場的 F27（綜合損益表）公告清單"""
    date_ymd = dt.strftime("%Y%m%d")
    try:
        resp = session.post(
            EZSEARCH_URL,
            data={
                "step":      "00",
                "RADIO_CM":  "1",
                "TYPEK":     market,
                "CO_MARKET": "",
                "CO_ID":     "",
                "PRO_ITEM":  "F27",
                "SUBJECT":   "",
                "SDATE":     date_ymd,
                "EDATE":     date_ymd,
                "lang":      "TW",
                "AN":        "",
            },
            headers=HEADERS,
            timeout=20,
        )
        data = json.loads(resp.content.decode("utf-8-sig"))
    except Exception as e:
        sys.stderr.write(f"⚠️  ezsearch API 失敗（{market}）：{e}\n")
        return []

    if not isinstance(data, dict) or data.get("status") != "success":
        return []

    items = []
    for row in data.get("data", []):
        if row.get("AN_CODE") != "F27":
            continue
        required = ["COMPANY_ID", "COMPANY_NAME", "CDATE", "SUBJECT", "HYPERLINK"]
        if any(k not in row for k in required):
            continue

        # 日期：ROC → 西元
        parts = row["CDATE"].split("/")
        if len(parts) != 3:
            continue
        announce_date = f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"

        # 從主旨解析報告年/季：
        #   季報 →「115年第1季綜合損益表」
        #   年報 →「114年度綜合損益表」（視為第4季）
        subj = row["SUBJECT"]
        m = re.search(r"(\d+)\s*年\s*第\s*(\d+)\s*季", subj)
        if m:
            report_year, report_quarter = int(m.group(1)) + 1911, int(m.group(2))
        else:
            m2 = re.search(r"(\d+)\s*年\s*度", subj)
            if not m2:
                continue
            report_year, report_quarter = int(m2.group(1)) + 1911, 4

        items.append({
            "stock_id":     row["COMPANY_ID"],
            "company_name": row["COMPANY_NAME"],
            "market":       row.get("TYPEK", market),
            "industry":     row.get("CODE_NAME", ""),
            "report_year":  report_year,
            "report_quarter": report_quarter,
            "announce_date": announce_date,
            "announce_time": row.get("CTIME", ""),
            "source_url":   row["HYPERLINK"],
        })

    return items


# =========================
# 解析綜合損益表 HTML（t164sb04）
# =========================
def _clean_label(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", "").replace("　", "").strip()
    # 斜線正規化：季報用全形 ／(U+FF0F)、年報用 ∕(U+2215)，統一成半形 /
    s = s.replace("∕", "/").replace("／", "/")
    return s


def _clean_value(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", "").replace("　", "").replace(",", "").strip()
    return s


def _to_int(s):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return int(float(s))   # 容忍極少數帶小數的金額
    except Exception:
        return None


def _to_float(s):
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except Exception:
        return None


def _detect_columns(html: str):
    """從表頭判斷每個金額欄的意義。
    回傳 {(is_cum, is_current): 值欄索引}；抓不到回 None。
    表頭那列含『會計項目』與多個欄組標籤，例如：
      Q1  : 會計項目 | 115年01月01日至115年03月31日 | 115年第1季 | 114年… | 114年第1季
      Q2/3: 會計項目 | 114年第2季 | 113年第2季 | 114年01月01日至…06月30日 | 113年…
    每組占 2 個值欄（金額,%），故第 i 組的金額在值欄索引 2*i。
    is_cum = 標籤含『至』（日期區間＝累計）；is_current = 年份為表頭中較大者。
    """
    header = None
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S | re.I):
        ths = [_clean_label(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", row, re.S | re.I)]
        if ths and "會計項目" in ths[0] and any(("至" in t or "季" in t) for t in ths[1:]):
            header = ths[1:]
            break
    if not header:
        return None

    parsed, years = [], []
    for i, g in enumerate(header):
        is_cum = "至" in g
        m = re.search(r"(\d+)\s*年", g)
        yr = int(m.group(1)) if m else None
        parsed.append((i, is_cum, yr))
        if yr is not None:
            years.append(yr)
    if not years:
        return None
    cur = max(years)
    cols = {}
    for i, is_cum, yr in parsed:
        cols[(is_cum, yr == cur)] = 2 * i
    return cols


def parse_income_statement(html: str) -> dict | None:
    """從 t164sb04 HTML 解析綜合損益表。
    每資料列：第 1 個 <td>=會計項目，後面 8 個 <td>=
        [本期累計金額, %, 本期單季金額, %, 去年累計金額, %, 去年單季金額, %]
    回傳 dict 或 None（無法解析 / 金融保險業等格式不同）。
    """
    if not html or "tblHead" not in html or "會計項目" not in html:
        return None

    # 收集每個會計項目的值欄（季報通常 8 欄=本期累計/單季×今年去年，
    # 年報可能只有 4 欄=本期/去年。故不寫死欄數，自動判斷版面。）
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.S | re.I)
    table = {}  # label -> [值欄...]（字串，已清洗）
    for row in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S | re.I)
        if len(tds) < 3:          # 至少要有 標籤 + 1組(金額,%)
            continue
        label = _clean_label(tds[0])
        if not label:
            continue
        vals = [_clean_value(x) for x in tds[1:]]
        # 同名項目（如「基本每股盈餘」標題列為空、子列才有值）：有值者優先，不被空列覆蓋
        if label not in table or any(v for v in vals):
            table[label] = vals

    # 從表頭判斷各欄意義（累計/單季 × 今年/去年）。
    #   重要：Q1 表頭順序是 [本期累計, 本期單季, 去年累計, 去年單季]，
    #         Q2/Q3 卻是 [本期單季, 去年單季, 本期累計, 去年累計]，順序不同，
    #         故不能寫死欄位，必須依表頭日期區間（含「至」=累計）與年份判斷。
    cols = _detect_columns(html)
    ncol = max((len(v) for v in table.values()), default=0)
    if cols:
        I_CUM    = cols.get((True, True))
        I_Q      = cols.get((False, True), I_CUM)        # 年報無單季 → 退回累計
        I_CUM_LY = cols.get((True, False))
        I_Q_LY   = cols.get((False, False), I_CUM_LY)
    elif ncol >= 8:                                       # 退而求其次（表頭抓不到時）
        I_CUM, I_Q, I_CUM_LY, I_Q_LY = 0, 2, 4, 6
    else:
        I_CUM, I_Q, I_CUM_LY, I_Q_LY = 0, 0, 2, 2

    def amt(cands, idx):
        """依候選標籤清單，取第一個存在且該欄有值的金額。"""
        for label in cands:
            v = table.get(label)
            if v and idx is not None and idx < len(v) and v[idx].strip() != "":
                return _to_int(v[idx])
        return None

    def eps(cands):
        for label in cands:
            v = table.get(label)
            if v and v[0].strip() != "":
                return _to_float(v[0])
        return None

    # ── 業別判斷（決定主要收益科目與顯示）──
    if "營業收入合計" in table:
        sector = "一般"
    elif "保險收入" in table or "保險服務結果合計" in table:
        sector = "保險"
    elif "收益合計" in table and ("經紀手續費收入" in table or "承銷業務收入" in table):
        sector = "證券"
    elif "保險服務結果" in table or "手續費及佣金淨收益" in table:
        sector = "金控"
    elif "淨收益" in table:
        sector = "銀行"          # 含票券
    else:
        sector = "其他"

    # 主要收益科目（對映進 revenue/ytd_revenue）：一般＝營業收入合計；
    # 證券＝收益合計；銀行/金控/票券＝淨收益；保險＝保險收入
    REV_PRIORITY = ["營業收入合計", "收益合計", "淨收益", "保險收入"]
    rev_label = next((c for c in REV_PRIORITY if c in table), None)
    rev_cands = [rev_label] if rev_label else []

    rev_cum    = amt(rev_cands, I_CUM)
    rev_q      = amt(rev_cands, I_Q)
    rev_cum_ly = amt(rev_cands, I_CUM_LY)
    rev_q_ly   = amt(rev_cands, I_Q_LY)

    def yoy(cur, last):
        if cur is None or last is None:
            return None, None
        amt_diff = cur - last
        pct = round(amt_diff / abs(last) * 100, 2) if last != 0 else None
        return amt_diff, pct

    yoy_amt, yoy_pct = yoy(rev_q, rev_q_ly)
    ytd_yoy_amt, ytd_yoy_pct = yoy(rev_cum, rev_cum_ly)

    # 底部科目：各業別用語不同，用多重候選對應
    net_income = amt(["本期淨利（淨損）", "本期稅後淨利（淨損）"], I_CUM)
    parent = amt(["母公司業主（淨利/損）", "母公司業主（淨利/淨損）"], I_CUM)
    if parent is None:
        parent = net_income     # 純銀行/保險/票券常無「歸屬母公司」分列，退回本期淨利

    rec = {
        "sector_type":        sector,
        # 營收（對齊 F22；金融業為主要收益）
        "revenue":            rev_q,
        "revenue_last_year":  rev_q_ly,
        "yoy_amt":            yoy_amt,
        "yoy_pct":            yoy_pct,
        "ytd_revenue":        rev_cum,
        "ytd_revenue_last":   rev_cum_ly,
        "ytd_yoy_amt":        ytd_yoy_amt,
        "ytd_yoy_pct":        ytd_yoy_pct,
        # 獲利（本期累計，仟元）
        "gross_profit":               amt(["營業毛利（毛損）"], I_CUM),
        "operating_income":           amt(["營業利益（損失）", "營業利益"], I_CUM),
        "pretax_income":              amt(["稅前淨利（淨損）", "繼續營業單位稅前淨利（淨損）",
                                           "繼續營業單位稅前純益（純損）", "繼續營業單位稅前損益"], I_CUM),
        "net_income":                 net_income,
        "net_income_parent":          parent,
        "total_comprehensive_income": amt(["本期綜合損益總額", "本期綜合損益總額（稅後）"], I_CUM),
        # EPS（元）
        "eps":          eps(["基本每股盈餘", "基本每股盈餘合計"]),
        "eps_diluted":  eps(["稀釋每股盈餘", "稀釋每股盈餘合計"]),
        "remark": "",
    }

    # 至少要有營收或淨利才算解析成功
    if rec["revenue"] is None and rec["ytd_revenue"] is None and rec["net_income"] is None:
        return None
    return rec


def _read_csv_rows(path):
    """NUL byte容錯的CSV讀取（曾發生過F22的CSV混入NUL byte讓csv.DictReader直接crash，
    導致整支腳本當掉、當天全部公司都不會寫入/發送；F27共用同一份風險，一併防護）。
    回傳 list[dict]，不crash，遇到NUL會濾除並印警告。"""
    import csv as _csv, io
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        sys.stderr.write(f"⚠️  {path} 偵測到NUL byte損毀，已自動濾除後讀取（原檔未覆蓋，請盡快確認）\n")
        raw = raw.replace(b"\x00", b"")
    text = raw.decode("utf-8", errors="replace")
    return list(_csv.DictReader(io.StringIO(text)))


# =========================
# QoQ：從 quarterly_revenue.csv 查上一季單季營收（明細頁無上一季資料）
# =========================
_REV_CACHE = None   # {(stock_id, year, quarter): (revenue, ytd_revenue)}


def _rev_cache() -> dict:
    global _REV_CACHE
    if _REV_CACHE is None:
        _REV_CACHE = {}
        if os.path.exists(CSV_PATH):
            for r in _read_csv_rows(CSV_PATH):
                    try:
                        key = (r["stock_id"], int(r["report_year"]), int(r["report_quarter"]))
                    except Exception:
                        continue
                    def _i(s):
                        try:
                            return int(float(s))
                        except Exception:
                            return None
                    _REV_CACHE[key] = (_i(r.get("revenue")), _i(r.get("ytd_revenue")))
    return _REV_CACHE


def _single_q_rev(stock_id: str, y: int, q: int):
    """取單季營收。Q4 為年報，CSV revenue 存的是全年累計，
    需用 ytd(Q4) − ytd(Q3) 推回單季。"""
    c = _rev_cache()
    rev, ytd = c.get((stock_id, y, q), (None, None))
    if q != 4:
        return rev
    _, ytd_q3 = c.get((stock_id, y, 3), (None, None))
    if ytd is not None and ytd_q3 is not None:
        return ytd - ytd_q3
    return None   # 缺 Q3，無法推回單季，避免拿全年數誤算


def _new_high_tags(item: dict, rec: dict) -> list:
    """比對「本資料庫收集以來」的歷史（quarterly_revenue.csv），回傳新高標註（僅供參考，非官方史上新高）。
    單季新高：本季單季營收 > 過去所有季度單季營收最大值（沿用 _single_q_rev 的 Q4 全年拆算邏輯）
    累計新高：累計營收(YTD) > 過去「同季別」累計營收最大值（Q1比Q1、Q2比Q2...避免拿H1比全年）
    """
    try:
        y, q = int(item["report_year"]), int(item["report_quarter"])
    except Exception:
        return []
    c = _rev_cache()

    cur_single = rec.get("revenue") if q != 4 else None
    if cur_single is None:
        cur_single = _single_q_rev(item["stock_id"], y, q)
    cur_ytd = rec.get("ytd_revenue")

    tags = []
    if cur_single is not None:
        past_single = []
        for (sid, hy, hq) in c:
            if sid != item["stock_id"] or (hy == y and hq == q):
                continue
            v = _single_q_rev(sid, hy, hq)
            if v is not None:
                past_single.append(v)
        if past_single and cur_single > max(past_single):
            tags.append("🚀單季營收創新高（收集以來）")
    if cur_ytd is not None:
        past_ytd = [v[1] for (sid, hy, hq), v in c.items()
                    if sid == item["stock_id"] and hq == q and hy != y and v[1] is not None]
        if past_ytd and cur_ytd > max(past_ytd):
            tags.append("🚀累計營收創新高（同季比較，收集以來）")
    return tags


def get_qoq_pct(item: dict, rec: dict):
    """單季營收 vs 上一季增減%；CSV 無上一季資料時回 None（顯示 -）"""
    try:
        y, q = int(item["report_year"]), int(item["report_quarter"])
    except Exception:
        return None
    cur = rec.get("revenue") if q != 4 else None   # Q4 即時解析也是全年數，一律推回
    if cur is None:    # backfill 走 CSV 路徑時 rec 沒帶單季營收，補查
        cur = _single_q_rev(item["stock_id"], y, q)
    py, pq = (y - 1, 4) if q == 1 else (y, q - 1)
    prev = _single_q_rev(item["stock_id"], py, pq)
    if cur is None or not prev:
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


# =========================
# 格式化（Telegram 訊息）
# =========================
def _fmt_m(n):
    """仟元 → 百萬"""
    if n is None:
        return "-"
    val = n / 1000
    if abs(val) >= 100:
        return f"{val:,.0f}"
    elif abs(val) >= 1:
        return f"{val:,.1f}"
    else:
        return f"{val:.2f}"


def _fmt_pct(n):
    if n is None:
        return "-"
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.1f}%"


def _fmt_eps(n):
    if n is None:
        return "-"
    return f"{n:.2f}"


def format_company_block(item: dict, rec: dict) -> str:
    """多行、獨立欄位風格（比照 daily_important_event 的季報（董事會通過）版型）"""
    q_tag    = f"{item['report_year']}Q{item['report_quarter']}"
    dt_str   = " ".join(s for s in (item.get("announce_date", ""), item.get("announce_time", "")) if s)
    header   = f"{item['stock_id']} {item['company_name']}" + (f" {dt_str}" if dt_str else "")

    lines = [header, "", f"{q_tag} 累計損益（百萬）"]
    lines.append(f"營收: {_fmt_m(rec['ytd_revenue'])}")
    if rec.get("gross_profit"):
        lines.append(f"毛利: {_fmt_m(rec['gross_profit'])}")
    lines.append(f"營益: {_fmt_m(rec['operating_income'])}")
    lines.append(f"稅前淨利: {_fmt_m(rec['pretax_income'])}")
    lines.append(f"母公司淨利: {_fmt_m(rec['net_income_parent'])}")
    lines.append(f"EPS: {_fmt_eps(rec['eps'])}")

    yoy = _fmt_pct(rec.get("ytd_yoy_pct"))
    qoq = _fmt_pct(get_qoq_pct(item, rec))
    if yoy != "-" or qoq != "-":
        lines.append(f"YoY {yoy}  QoQ {qoq}")

    for tag in _new_high_tags(item, rec):
        lines.append(tag)

    return "\n".join(lines)


# =========================
# CSV 儲存
# =========================
def save_to_csv(records: list) -> int:
    """寫入 quarterly_revenue.csv，以 (stock_id, report_year, report_quarter) 去重。"""
    import csv as _csv

    existing_keys = set()
    for row in _read_csv_rows(CSV_PATH):
        key = (row.get("stock_id", ""), row.get("report_year", ""), row.get("report_quarter", ""))
        existing_keys.add(key)

    new_rows = []
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item, rec in records:
        key = (item["stock_id"], str(item["report_year"]), str(item["report_quarter"]))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        row = {
            "stock_id":           item["stock_id"],
            "company_name":       item["company_name"],
            "market":             item["market"],
            "industry":           item["industry"],
            "report_year":        item["report_year"],
            "report_quarter":     item["report_quarter"],
            "announce_date":      item["announce_date"],
            "announce_time":      item["announce_time"],
            "source_url":         item["source_url"],
            "fetched_at":         fetched_at,
        }
        for k in ["sector_type",
                  "revenue", "revenue_last_year", "yoy_amt", "yoy_pct",
                  "ytd_revenue", "ytd_revenue_last", "ytd_yoy_amt", "ytd_yoy_pct",
                  "gross_profit", "operating_income", "pretax_income",
                  "net_income", "net_income_parent", "total_comprehensive_income",
                  "eps", "eps_diluted", "remark"]:
            v = rec.get(k)
            row[k] = "" if v is None else v
        new_rows.append(row)

    if not new_rows:
        return 0

    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", encoding="utf-8", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    return len(new_rows)


# =========================
# Google Drive 上傳
# =========================
def upload_to_gdrive() -> str | None:
    if not os.path.exists(CSV_PATH):
        return None
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.stderr.write("⚠️  Google Drive 套件未安裝，跳過上傳\n")
        return None

    if not os.path.exists(CREDS_PATH):
        sys.stderr.write(f"⚠️  找不到 {CREDS_PATH}，跳過上傳\n")
        return None

    try:
        creds = None
        if os.path.exists(TOKEN_PATH):
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, GDRIVE_SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, GDRIVE_SCOPES)
                creds = flow.run_local_server(port=0)
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())

        service = build("drive", "v3", credentials=creds)
        existing = service.files().list(
            q=f"name='{GDRIVE_FILENAME}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name)",
        ).execute()
        files = existing.get("files", [])
        media = MediaFileUpload(CSV_PATH, mimetype="text/csv", resumable=False)

        if files:
            file_id = files[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            metadata = {"name": GDRIVE_FILENAME, "parents": [GDRIVE_FOLDER_ID]}
            result  = service.files().create(body=metadata, media_body=media, fields="id").execute()
            file_id = result.get("id", "")

        return f"https://drive.google.com/file/d/{file_id}/view" if file_id else None
    except Exception as e:
        sys.stderr.write(f"⚠️  Google Drive 上傳失敗：{e}\n")
        return None


# =========================
# 組裝群組訊息
# =========================
def build_group_messages(group: str, blocks: list, today_str: str) -> list:
    group_display = _WATCHLIST[group]["display"]
    header = f"📈 F27 季報損益 {today_str}｜👤 {group_display}"

    if not blocks:
        return []

    messages = []
    current = header
    for block in blocks:
        segment = f"\n{SEPARATOR}\n{block}"
        if len(current) + len(segment) + len(f"\n{SEPARATOR}") > MAX_LEN \
                and current != header:
            current += f"\n{SEPARATOR}"
            messages.append(current)
            current = header + segment
        else:
            current += segment
    current += f"\n{SEPARATOR}"
    messages.append(current)
    return messages


# =========================
# 主流程
# =========================
def run():
    _load_watchlist()
    dt        = parse_date_arg()
    today_str = f"{dt.month}/{dt.day}"

    all_items = []
    with requests.Session() as session:
        for market in MARKETS:
            items = fetch_f27_list(session, dt, market)
            sys.stderr.write(f"  {market}: {len(items)} 筆\n")
            all_items.extend(items)
            time.sleep(SLEEP_LIST)

        seen, unique_items = set(), []
        for item in all_items:
            key = (item["stock_id"], item["announce_date"], item["announce_time"])
            if key not in seen:
                seen.add(key)
                unique_items.append(item)

        sys.stderr.write(f"去重後共 {len(unique_items)} 筆，開始抓詳細資料…\n")

        results, fetch_failed, parse_none = process_items(session, unique_items)

    sys.stderr.write(f"成功解析 {len(results)} 筆；抓取失敗 {len(fetch_failed)} 筆（待重抓）；無法解析 {len(parse_none)} 筆\n")

    # CSV / log 一律寫入完整結果，去重只影響「訊息」
    written    = save_to_csv(results)
    update_logs(results, fetch_failed, parse_none)
    gdrive_url = upload_to_gdrive()
    sys.stderr.write(f"CSV 新寫入 {written} 筆\n")
    if fetch_failed:
        sys.stderr.write(f"⚠️  有 {len(fetch_failed)} 筆抓取失敗，已記錄到 {FAILED_LOG}，可稍後跑 refetch_failed_f27.py 重抓\n")

    # ── 已發送去重（避免 n8n 重跑/重試造成同一期間同數字重複發送）──────────────
    sent_state = _load_sent_state()
    now_s      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    to_send, skipped = [], 0
    for item, rec in results:
        k = _sent_key(item, rec)
        if k in sent_state:
            skipped += 1
            continue
        to_send.append((item, rec))
    if skipped:
        sys.stderr.write(f"  🔁 {skipped} 筆同期間同數字已發送過，本輪跳過\n")

    blocks_by_group = {k: [] for k in _WATCHLIST}
    for item, rec in to_send:
        grps  = get_groups(item["stock_id"], item["company_name"])
        block = format_company_block(item, rec)   # 負責人改放在群組標題，這裡不重覆標註
        for g in grps:
            blocks_by_group[g].append(block)

    output = []
    for group in list(_WATCHLIST.keys()):
        group_has_msg = False
        for msg in build_group_messages(group, blocks_by_group[group], today_str):
            output.append({"message": msg, "group": group})
            group_has_msg = True
        if not group_has_msg and (ALWAYS_SEND_GROUPS == "all" or group in ALWAYS_SEND_GROUPS):
            output.append({"message": "📭 今日無季報損益公告", "group": group})

    if not output:
        if results and not to_send:
            output.append({"message": "📭 今日無新增季報損益公告（本輪皆為重複，已跳過）", "group": "all"})
        else:
            output.append({"message": "📭 今日無季報損益公告", "group": "all"})

    # ── 記錄已發送（先存檔再輸出，失敗時附警告，避免之後每輪重發）───────────────
    for item, rec in to_send:
        sent_state[_sent_key(item, rec)] = now_s
    ok = _save_sent_state(sent_state)
    if to_send and not ok:
        output.append({
            "message": "⚠️ sent_state_f27.json 寫入失敗：去重狀態未保存，之後每輪可能重發相同訊息，請檢查資料夾權限",
            "group": "standalone",
        })

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    run()

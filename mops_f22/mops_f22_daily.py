"""
mops_f22_daily.py
每日 F22 月營收彙整，依「關注股票列表.xlsx」分群送出訊息

用法：
  python3 mops_f22_daily.py              # 今天
  python3 mops_f22_daily.py yesterday   # 昨天
  python3 mops_f22_daily.py 2026-05-15  # 指定日期

輸出：JSON 陣列，格式與 Daily_Important_Event_v4.py 相同
  [{"message": "...", "group": "ryan"}, ...]

數字單位：仟元（與 MOPS 原始資料一致），顯示時換算為百萬
"""

from __future__ import annotations  # 讓 dict | None 等型別註記在 Python 3.9 也能用

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

# 共用主檔：scripts/關注股票列表.xlsx（_PARENT 即 scripts 目錄）
# 2026-06 新格式：改讀「原始資料」分頁，欄位含 股票代號 / 股票名稱 / 研究員 / 實習生。
# 只收「研究員或實習生欄位有內容」的股票（其餘 1690 檔無人負責者不送）。
# 不再分群組（舊版用分頁名 Max_Emma 等當群組）；改成單一清單，每檔在訊息中加註負責人名字。
WATCHLIST_PATH   = os.path.join(_PARENT, "關注股票列表.xlsx")
WATCHLIST_SHEET  = "原始資料"   # 主檔新格式分頁（fallback 用）
# 優先讀精簡小清單 CSV（由 build_watchlist.py 從主檔產生，解耦會變動的工作規劃主檔）；
# 不存在時才退回直接解析主檔 xlsx 的「原始資料」分頁。
WATCHLIST_CSV    = os.path.join(_PARENT, "watchlist_active.csv")

_WATCHLIST     = {}   # {group_key: {"display","names"}}；group_key = 主檔組合(如 Leo／Ryan) 或 疊加者(如 Edison)
_STOCK_GROUPS  = {}   # 代號(str) / 正規化公司名 -> [所屬群組, ...]


# =========================
# 已發送狀態（訊息去重，避免 n8n 重跑/重試造成重複發送）
# =========================
SENT_STATE_PATH = os.path.join(_DIR, "sent_state_f22.json")
SENT_KEEP_DAYS  = 45   # 狀態保留天數（月營收公告不太可能超過這個天數還重發）


def _sent_key(item: dict, rev: dict) -> str:
    """以「股號＋所屬年月＋營收數字」當key：同一期間同一數字視為已發送過；
    若公司更正重發（數字不同）仍視為新內容，照常送出。"""
    return f"{item['stock_id']}|{item['report_year']}|{item['report_month']}|{rev.get('revenue', '')}"


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
            pruned[k] = v   # 時間格式異常就保留，避免誤刪
    try:
        with open(SENT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=1)
        return True
    except Exception as e:
        sys.stderr.write(f"⚠️  sent_state_f22.json 寫入失敗：{e}\n")
        return False


def _wl_norm(v) -> str:
    """儲存格 → 乾淨字串：去空白、去尾端 *，過濾 pandas 的 nan。"""
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
# 主檔的研究員＋實習生＝合成「一組」（一則，如 👤 Leo／Ryan）；
# 疊加檔的人（如 Edison）＝各自「獨立一組」（各一則，如 👤 Edison）。
# 一檔可同時屬於多組（如廣達 → Leo／Ryan 一則 + Edison 一則）。
# =========================
def _load_watchlist():
    global _WATCHLIST, _STOCK_GROUPS
    _WATCHLIST = {}
    _STOCK_GROUPS = {}
    groups = {}   # group_key -> set(代號/名稱)
    for code, name, team, extra in _read_watchlist_rows():
        gkeys = ([team] if team else []) + extra
        if not gkeys:             # 無人負責，不收
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

    # ---- 負責人過濾（只發送指定負責人的群組）----
    # 來源優先序：環境變數 F22_OWNERS（如 F22_OWNERS=Ryan）> scripts/send_owners_f22.txt（每行一個名字，# 開頭為註解）。
    # 兩者皆無 → 不過濾，行為與舊版完全相同。
    # 注意：daily / backfill_send_f22 / check_backfill_f22 都用本檔的 _WATCHLIST，所以過濾對三者同時生效。
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
    raw = os.environ.get("F22_OWNERS", "").strip()
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
# 要查詢的市場：sii=上市, otc=上櫃, rotc=興櫃（三支腳本共用此常數）
MARKETS = ("sii", "otc", "rotc")

EZSEARCH_URL = "https://mopsov.twse.com.tw/mops/web/ezsearch_query"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin":  "https://mopsov.twse.com.tw",
    "Referer": "https://mopsov.twse.com.tw/mops/web/ezsearch",
}

# Ryan 有或沒有公告都要送一則訊息
# CSV / Google Drive
CSV_PATH         = os.path.join(_PARENT, "mops_f22", "monthly_revenue.csv")
GDRIVE_FOLDER_ID = "1PSZv1zVCK1Z5McaeKyxeDZynf1JIb1tt"
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]
CREDS_PATH       = os.path.join(_PARENT, "credentials.json")
TOKEN_PATH       = os.path.join(_PARENT, "token.json")

CSV_COLUMNS = [
    "stock_id", "company_name", "market", "industry",
    "report_year", "report_month",
    "revenue", "revenue_last_year", "yoy_amt", "yoy_pct",
    "ytd_revenue", "ytd_revenue_last", "ytd_yoy_amt", "ytd_yoy_pct",
    "remark", "announce_date", "announce_time", "source_url", "fetched_at",
    # 2026-08-19 新增：資料來源標記。空字串=MOPS官方公告（原有行為，絕大多數）；
    # "derived_ytd"=由相鄰月份的累計欄(ytd_revenue)相減反推補上的缺漏月份，
    # 由 derive_missing_f22.py 產生。必須列在 CSV_COLUMNS 裡，否則 save_to_csv()
    # 整檔重寫時會被 extrasaction="ignore" 靜默丟掉。
    # 注意：日後真的抓到官方數字時，save_to_csv() 會整列覆蓋，此欄自動回復空值，
    # 代表已升級為官方資料，不需額外清理。
    "data_source",
]

# 依人員組合分群後，只送「當天有公告」的組合（空組合不送，避免 17 組天天洗版）。
# 若某組合不管有無公告都想收，把組合名加進此 set，例如 {"Max／Ryan"}；設 "all" = 全部強制送。
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
# 抓取 F22 公告清單
# =========================
# 抓取結果統計：讓呼叫端能區分「真的沒公告」與「根本沒查成」。
# fetch_f22_list 失敗時回傳 []，與「當天真的 0 筆」長得一模一樣，
# 過去會導致 check_backfill 在 API 被擋時謊報「✅ 無缺漏」（假陰性）。
# 呼叫端請在開始前呼叫 reset_fetch_stats()，結束後讀 FETCH_STATS。
FETCH_STATS = {"ok": 0, "fail": 0, "reasons": []}


def reset_fetch_stats():
    FETCH_STATS["ok"] = 0
    FETCH_STATS["fail"] = 0
    FETCH_STATS["reasons"] = []


def _stat_fail(market, reason):
    FETCH_STATS["fail"] += 1
    r = f"{market}：{reason}"
    if r not in FETCH_STATS["reasons"]:
        FETCH_STATS["reasons"].append(r)


def fetch_f22_list(session: requests.Session, dt: datetime, market: str) -> list:
    """抓取指定日期 / 市場的 F22（月營收）公告清單"""
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
                "PRO_ITEM":  "F22",
                "SUBJECT":   "",
                "SDATE":     date_ymd,
                "EDATE":     date_ymd,
                "lang":      "TW",
                "AN":        "",
            },
            headers=HEADERS,
            timeout=15,
        )
        # API 回傳 UTF-8 with BOM，需用 utf-8-sig 解碼
        data = json.loads(resp.content.decode("utf-8-sig"))
    except Exception as e:
        sys.stderr.write(f"⚠️  ezsearch API 失敗（{market}）：{e}\n")
        _stat_fail(market, f"連線/解析失敗（{type(e).__name__}）")
        return []

    if not isinstance(data, dict) or data.get("status") != "success":
        # MOPS ezsearch_query 的怪癖：查詢當天「真的 0 筆」時不是回 success+空陣列，
        # 而是回 status=fail、message=["查無公告資料"]。過去把這種情況跟真連線失敗
        # 混為一談，導致淡季（無公告日）被誤判成「無法驗證」。
        # 已用 2026-08-19/20（0筆，此訊息）vs 2026-08-07（621筆，success）實測比對確認，
        # 兩者用同一組參數格式送出，差別只在當天有沒有公告。
        msg = data.get("message") if isinstance(data, dict) else None
        msg_str = "".join(msg) if isinstance(msg, list) else (msg or "")
        if isinstance(data, dict) and "查無公告資料" in msg_str:
            FETCH_STATS["ok"] += 1
            return []
        _stat_fail(market, f"回應非 success（status={data.get('status') if isinstance(data, dict) else type(data).__name__}）")
        return []

    FETCH_STATS["ok"] += 1

    items = []
    for row in data.get("data", []):
        if row.get("AN_CODE") != "F22":
            continue
        required = ["COMPANY_ID", "COMPANY_NAME", "CDATE", "SUBJECT", "HYPERLINK"]
        if any(k not in row for k in required):
            continue

        # 日期：ROC → 西元
        parts = row["CDATE"].split("/")
        if len(parts) != 3:
            continue
        announce_date = f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"

        # 從主旨解析報告月份（如「115年4月份」）
        m = re.search(r"(\d+)\s*年\s*(\d+)\s*月", row["SUBJECT"])
        if not m:
            # 曾發生清單API有抓到公告、卻因主旨措辭不同讓這個regex匹配失敗，
            # 導致該公司整筆被默默跳過、CSV永遠不會有這期資料（3026禾伸堂案例，
            # 找不到觸發原因是因為完全沒有留下任何紀錄）。改成印出來，之後
            # 再發生同類狀況至少能從log看到是「主旨解析失敗」而非「完全沒抓到」。
            sys.stderr.write(
                f"⚠️  月份解析失敗，略過：{row.get('COMPANY_ID','?')} {row.get('COMPANY_NAME','?')} "
                f"主旨={row['SUBJECT']!r}\n"
            )
            continue

        items.append({
            "stock_id":     row["COMPANY_ID"],
            "company_name": row["COMPANY_NAME"],
            "market":       row.get("TYPEK", market),
            "industry":     row.get("CODE_NAME", ""),
            "report_year":  int(m.group(1)) + 1911,
            "report_month": int(m.group(2)),
            "announce_date": announce_date,
            "announce_time": row.get("CTIME", ""),
            "source_url":   row["HYPERLINK"],
        })

    return items


# =========================
# 解析月營收 HTML（t05st10）
# =========================
def parse_revenue_html(html: str) -> dict | None:
    """從 t05st10 HTML 解析月營收數字，回傳 dict 或 None（無資料）"""
    if not html or len(html) < 100 or "tblHead" not in html:
        return None

    # 標籤格子可能是 <TH class='tblHead'>（一般公司）或 <TD class='tblHead'>（KY 外國股），
    # 且 KY 股一列有兩個值欄（新台幣 + 功能性貨幣），只取緊接其後的「第一個」值欄＝新台幣。
    # 不再要求 </TR> 結尾，才能同時吃單欄(一般)與雙欄(KY)格式。
    row_re = re.compile(
        r"<T[HD][^>]*class='tblHead'[^>]*>([^<]+)</T[HD]>"
        r"\s*<TD[^>]*class='(?:odd|even)'[^>]*>([^<]+)</TD>",
        re.IGNORECASE,
    )

    def strip_tags(s):
        return re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ").strip()

    def parse_int(s):
        c = s.replace(",", "").strip()
        try:
            return int(c)
        except Exception:
            return None

    def parse_float(s):
        c = s.replace(",", "").strip()
        try:
            return float(c)
        except Exception:
            return None

    rev = {
        "revenue": None, "revenue_last_year": None,
        "yoy_amt": None, "yoy_pct": None,
        "ytd_revenue": None, "ytd_revenue_last": None,
        "ytd_yoy_amt": None, "ytd_yoy_pct": None,
        "remark": "",
    }
    amt_count = pct_count = 0

    for m in row_re.finditer(html):
        label = strip_tags(m.group(1))
        value = strip_tags(m.group(2))
        if "項目" in label or "營業收入淨額" in label:
            continue
        if label == "本月":
            rev["revenue"] = parse_int(value)
        elif label == "去年同期":
            rev["revenue_last_year"] = parse_int(value)
        elif label == "本年累計":
            rev["ytd_revenue"] = parse_int(value)
        elif label == "去年累計":
            rev["ytd_revenue_last"] = parse_int(value)
        elif label == "增減金額":
            if amt_count == 0:
                rev["yoy_amt"] = parse_int(value)
            else:
                rev["ytd_yoy_amt"] = parse_int(value)
            amt_count += 1
        elif label == "增減百分比":
            if pct_count == 0:
                rev["yoy_pct"] = parse_float(value)
            else:
                rev["ytd_yoy_pct"] = parse_float(value)
            pct_count += 1
        elif label.startswith("備註"):
            rev["remark"] = value

    return rev if rev["revenue"] is not None else None


# =========================
# MoM：從 monthly_revenue.csv 查上月營收（明細頁無上月資料）
# =========================
_REV_CACHE = None   # {(stock_id, year, month): revenue}


def _rev_cache() -> dict:
    global _REV_CACHE
    if _REV_CACHE is None:
        import csv as _csv
        _REV_CACHE = {}
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, "r", encoding="utf-8", newline="") as f:
                for r in _csv.DictReader(f):
                    try:
                        _REV_CACHE[(r["stock_id"], int(r["report_year"]), int(r["report_month"]))] = \
                            int(float(r["revenue"]))
                    except Exception:
                        continue
    return _REV_CACHE


def get_mom_pct(item: dict, rev: dict):
    """本月 vs 上月營收增減%；CSV 無上月資料時回 None（顯示 -）"""
    try:
        y, m = int(item["report_year"]), int(item["report_month"])
    except Exception:
        return None
    cur = rev.get("revenue")
    if cur is None:
        cur = _rev_cache().get((item["stock_id"], y, m))
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    prev = _rev_cache().get((item["stock_id"], py, pm))
    if cur is None or not prev:
        return None
    return round((cur - prev) / abs(prev) * 100, 2)


# =========================
# 格式化
# =========================
def _fmt_m(n):
    """仟元 → 百萬，格式化顯示"""
    if n is None:
        return "-"
    val = n / 1000  # 仟元 → 百萬
    if abs(val) >= 100:
        return f"{val:,.0f}"
    elif abs(val) >= 1:
        return f"{val:,.1f}"
    else:
        return f"{val:.2f}"


def _fmt_pct(n):
    if n is None:
        return "沒資料"  # 缺前期可比數字（CSV 無上月/去年同月資料），不要顯示模糊的「-」
    sign = "+" if n >= 0 else ""
    return f"{sign}{n:.1f}%"


# 異常增減門檻：基期過小（金融/保險業淡季、新成立業務等）常出現失真的暴增暴跌%，
# 超過門檻就加 ⚠️ 提醒使用者這個數字可能不適合直接拿來判斷成長動能
EXTREME_PCT_THRESHOLD = 300


def _fmt_pct_warn(n, threshold=EXTREME_PCT_THRESHOLD):
    s = _fmt_pct(n)
    if n is not None and abs(n) >= threshold:
        return f"⚠️{s}"
    return s


_OLD_ROWS = None
_HISTORY_BY_STOCK = None  # {stock_id: [(year, month, revenue, ytd_revenue), ...]}，供「新高」判斷用


def _hnum(s):
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def _old_row(item: dict):
    """CSV 既有列快取（供「更正」判別與原值標註）。首次呼叫時載入。
    容錯：檔案若混入NUL byte等損毀字元（曾發生過，會讓csv.DictReader直接crash
    導致整支腳本當掉、當天全部公司都不會寫入/發送），讀取時先濾掉NUL再解析，
    不讓單一損毀字元癱瘓整個每日流程。
    同一次讀取順便建立 _HISTORY_BY_STOCK（新高判斷用），避免多讀一次CSV浪費資源。"""
    global _OLD_ROWS, _HISTORY_BY_STOCK
    if _OLD_ROWS is None:
        import csv as _csv, io
        _OLD_ROWS = {}
        _HISTORY_BY_STOCK = {}
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, "rb") as f:
                raw = f.read()
            if b"\x00" in raw:
                sys.stderr.write(f"⚠️  {CSV_PATH} 偵測到NUL byte損毀，已自動濾除後讀取（原檔未覆蓋，請盡快確認）\n")
                raw = raw.replace(b"\x00", b"")
            text = raw.decode("utf-8", errors="replace")
            for r in _csv.DictReader(io.StringIO(text)):
                _OLD_ROWS[(r.get("stock_id", ""), r.get("report_year", ""),
                           r.get("report_month", ""))] = r
                sid = r.get("stock_id", "")
                if sid:
                    _HISTORY_BY_STOCK.setdefault(sid, []).append((
                        _hnum(r.get("report_year")), _hnum(r.get("report_month")),
                        _hnum(r.get("revenue")), _hnum(r.get("ytd_revenue")),
                    ))
    return _OLD_ROWS.get((str(item.get("stock_id", "")), str(item.get("report_year", "")),
                          str(item.get("report_month", ""))))


def _new_high_tags(item: dict, rev: dict) -> list:
    """比對「本資料庫收集以來」的歷史，回傳新高標註（不足以判斷「史上」新高，僅供參考）。
    單月新高：本月營收 > 過去所有月份營收最大值
    累計新高：累計營收(YTD) > 過去「同月份」累計營收最大值（避免拿7月累計比1月累計）
    """
    _old_row({})  # 確保 _HISTORY_BY_STOCK 已載入
    hist = _HISTORY_BY_STOCK.get(str(item.get("stock_id", "")), [])
    cur_month = _hnum(item.get("report_month"))
    cur_year  = _hnum(item.get("report_year"))
    cur_rev   = _hnum(rev.get("revenue"))
    cur_ytd   = _hnum(rev.get("ytd_revenue"))

    tags = []
    prior = [h for h in hist if not (h[0] == cur_year and h[1] == cur_month)]
    if cur_rev is not None:
        past_rev = [h[2] for h in prior if h[2] is not None]
        if past_rev and cur_rev > max(past_rev):
            tags.append("🚀單月營收創新高（收集以來）")
    if cur_ytd is not None and cur_month is not None:
        past_ytd = [h[3] for h in prior if h[1] == cur_month and h[3] is not None]
        if past_ytd and cur_ytd > max(past_ytd):
            tags.append("🚀累計營收創新高（同月比較，收集以來）")
    return tags


def format_company_block(item: dict, rev: dict) -> str:
    month_tag = f"{item['report_month']}月"
    title = f"{item['stock_id']} {item['company_name']}  {month_tag}營收（百萬）"
    old = _old_row(item)
    if old and str(old.get("announce_date", "")) != str(item.get("announce_date", "")):
        title += f"〔更正，原公告 {old.get('announce_date','?')}：YoY {old.get('yoy_pct','?')}%〕"
    lines = [
        title,
        f"本月: {_fmt_m(rev['revenue'])}  YoY {_fmt_pct_warn(rev['yoy_pct'])}  MoM {_fmt_pct_warn(get_mom_pct(item, rev))}",
        f"累計: {_fmt_m(rev['ytd_revenue'])}  YoY {_fmt_pct_warn(rev['ytd_yoy_pct'])}",
    ]
    if rev.get("remark"):
        lines.append(f"備註: {rev['remark']}")
    for tag in _new_high_tags(item, rev):
        lines.append(tag)
    return "\n".join(lines)


# =========================
# CSV 儲存
# =========================
def save_to_csv(records: list) -> int:
    """寫入/更新 monthly_revenue.csv。新公告→新增；同月重複公告（更正）→覆蓋舊列。
    回傳新增+更新筆數。
    """
    import csv as _csv, io

    _old_row({})  # 先載入舊值快取（供訊息「更正」標註，須在覆蓋前完成）

    rows, index = [], {}
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, "rb") as f:
            raw = f.read()
        if b"\x00" in raw:
            sys.stderr.write(f"⚠️  {CSV_PATH} 偵測到NUL byte損毀，已自動濾除後讀取（原檔未覆蓋，請盡快確認）\n")
            raw = raw.replace(b"\x00", b"")
        text = raw.decode("utf-8", errors="replace")
        for row in _csv.DictReader(io.StringIO(text)):
            key = (row.get("stock_id", ""), row.get("report_year", ""), row.get("report_month", ""))
            index[key] = len(rows)
            rows.append(row)

    changed = 0
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item, rev in records:
        key = (item["stock_id"], str(item["report_year"]), str(item["report_month"]))
        new_row = ({
            "stock_id":           item["stock_id"],
            "company_name":       item["company_name"],
            "market":             item["market"],
            "industry":           item["industry"],
            "report_year":        item["report_year"],
            "report_month":       item["report_month"],
            "revenue":            rev.get("revenue", ""),
            "revenue_last_year":  rev.get("revenue_last_year", ""),
            "yoy_amt":            rev.get("yoy_amt", ""),
            "yoy_pct":            rev.get("yoy_pct", ""),
            "ytd_revenue":        rev.get("ytd_revenue", ""),
            "ytd_revenue_last":   rev.get("ytd_revenue_last", ""),
            "ytd_yoy_amt":        rev.get("ytd_yoy_amt", ""),
            "ytd_yoy_pct":        rev.get("ytd_yoy_pct", ""),
            "remark":             rev.get("remark", ""),
            "announce_date":      item["announce_date"],
            "announce_time":      item["announce_time"],
            "source_url":         item["source_url"],
            "fetched_at":         fetched_at,
        })
        if key in index:
            old = rows[index[key]]
            if (str(old.get("announce_date", "")) == str(new_row["announce_date"])
                    and str(old.get("revenue", "")) == str(new_row["revenue"])):
                continue  # 同一筆公告，跳過
            rows[index[key]] = new_row  # 更正 → 覆蓋
        else:
            index[key] = len(rows)
            rows.append(new_row)
        changed += 1

    if not changed:
        return 0

    # ── 原子寫入（2026-08-24）────────────────────────────────────────────────
    # 本函式是「整檔重寫」：先把全檔讀進 rows、改完再全部寫回。若直接對正式檔開 "w"，
    # 寫到一半遇到當機／被 kill／磁碟滿，正式檔就會停在半截狀態而毀損——2026-08 那次
    # monthly_revenue.csv 混入 NUL byte、導致 csv.DictReader 直接 crash、整支腳本靜默
    # 當掉漏抓多日資料，就是這一類事故。
    # 改法：先寫到同目錄的暫存檔，寫完 flush + fsync 落盤，再用 os.replace() 換檔。
    # os.replace() 在同一檔案系統上是原子操作，所以正式檔永遠只會是「舊的完整版」或
    # 「新的完整版」，不存在中間狀態。暫存檔放同目錄是必要條件（跨檔案系統無法原子換檔）。
    tmp_path = f"{CSV_PATH}.tmp-{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore", restval="")
            writer.writeheader()
            writer.writerows(rows)
            f.flush()
            os.fsync(f.fileno())   # 確保資料真的落到磁碟，而非留在OS快取
        os.replace(tmp_path, CSV_PATH)
    except Exception:
        # 寫入失敗 → 清掉暫存檔，正式檔維持原狀（未被動過），再把錯誤往上拋
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise

    return changed


# =========================
# Google Drive 上傳
# =========================
def upload_to_gdrive() -> str | None:
    """上傳 monthly_revenue.csv 到 Google Drive，回傳檔案連結或 None"""
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
            q=f"name='monthly_revenue.csv' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name)",
        ).execute()
        files = existing.get("files", [])
        media = MediaFileUpload(CSV_PATH, mimetype="text/csv", resumable=False)

        if files:
            file_id = files[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            metadata = {"name": "monthly_revenue.csv", "parents": [GDRIVE_FOLDER_ID]}
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
    header = f"📊 F22 月營收 {today_str}｜👤 {group_display}"

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

    # ── 抓取所有 F22 公告 ────────────────────────────────────────────────────
    all_items = []
    with requests.Session() as session:
        for market in MARKETS:
            items = fetch_f22_list(session, dt, market)
            sys.stderr.write(f"  {market}: {len(items)} 筆\n")
            all_items.extend(items)
            time.sleep(1)

        # 去重（同一筆可能在不同市場各出現一次）
        seen, unique_items = set(), []
        for item in all_items:
            key = (item["stock_id"], item["announce_date"], item["announce_time"])
            if key not in seen:
                seen.add(key)
                unique_items.append(item)

        sys.stderr.write(f"去重後共 {len(unique_items)} 筆，開始抓詳細資料…\n")

        # ── 逐筆抓 HTML 解析月營收 ───────────────────────────────────────────
        results = []
        for item in unique_items:
            try:
                resp = session.get(item["source_url"], headers=HEADERS, timeout=20)
                rev  = parse_revenue_html(resp.text)
                if rev:
                    results.append((item, rev))
                else:
                    sys.stderr.write(f"  ⚠️  {item['stock_id']} 無法解析\n")
            except Exception as e:
                sys.stderr.write(f"  ⚠️  {item['stock_id']} 抓取失敗：{e}\n")
            time.sleep(0.5)

    sys.stderr.write(f"成功解析 {len(results)} 筆\n")

    # ── CSV 寫入 + Google Drive 上傳（一律寫入完整結果，去重只影響「訊息」）──────
    written    = save_to_csv(results)
    gdrive_url = upload_to_gdrive()
    sys.stderr.write(f"CSV 新寫入 {written} 筆\n")

    # ── 已發送去重（避免 n8n 重跑/重試造成同一期間同數字重複發送）──────────────
    sent_state = _load_sent_state()
    now_s      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    to_send, skipped = [], 0
    for item, rev in results:
        k = _sent_key(item, rev)
        if k in sent_state:
            skipped += 1
            continue
        to_send.append((item, rev))
    if skipped:
        sys.stderr.write(f"  🔁 {skipped} 筆同期間同數字已發送過，本輪跳過\n")

    # ── 依觀察清單分群 ────────────────────────────────────────────────────────
    blocks_by_group = {k: [] for k in _WATCHLIST}

    for item, rev in to_send:
        grps  = get_groups(item["stock_id"], item["company_name"])
        block = format_company_block(item, rev)   # 負責人改放在群組標題，這裡不重覆標註
        for g in grps:
            blocks_by_group[g].append(block)

    # ── 組裝輸出 JSON ─────────────────────────────────────────────────────────
    output = []
    for group in list(_WATCHLIST.keys()):
        group_has_msg = False
        for msg in build_group_messages(group, blocks_by_group[group], today_str):
            output.append({"message": msg, "group": group})
            group_has_msg = True
        if not group_has_msg and (ALWAYS_SEND_GROUPS == "all" or group in ALWAYS_SEND_GROUPS):
            output.append({"message": "📭 今日無月營收公告", "group": group})

    if not output:
        if results and not to_send:
            output.append({"message": "📭 今日無新增月營收公告（本輪皆為重複，已跳過）", "group": "all"})
        else:
            output.append({"message": "📭 今日無月營收公告", "group": "all"})

    # ── 記錄已發送（先存檔再輸出，失敗時附警告，避免之後每輪重發）───────────────
    for item, rev in to_send:
        sent_state[_sent_key(item, rev)] = now_s
    ok = _save_sent_state(sent_state)
    if to_send and not ok:
        output.append({
            "message": "⚠️ sent_state_f22.json 寫入失敗：去重狀態未保存，之後每輪可能重發相同訊息，請檢查資料夾權限",
            "group": "standalone",
        })

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    run()

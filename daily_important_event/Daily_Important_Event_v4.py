"""
Daily_Important_Event_v4.py
月 + 季自結彙整版

輸出格式（每公司一行）：
  3189 景碩  115/03: 3,939/444/289/0.58 | 2026 Q1: 10,388/1,222/666/1.7
  6921 嘉雨思  115/03: 9/1/1/0.02

差異說明（vs v3）：
  - 新增最近一季數字擷取
  - 格式精簡為一行，月份 | 季度並排
  - 不寫 CSV，不含 Google Drive 上傳
  - 完全獨立，不依賴 v3
"""

import csv
import json
import os
import re
import sys
import requests
import pandas as pd
from datetime import datetime, timedelta, date as _date


# =========================
# 基本設定
# =========================
KEYWORDS = ["自結合併", "自結損益", "財務業務等重大訊息", "營運成果", "注意交易資訊標準", "以利投資人區別"]
QRPT_KEYWORDS   = ["季合併財務報告", "季個別財務報告"]    # 季報（Q1/Q2/Q3）
ANNUAL_KEYWORDS = ["年度合併財務報告", "年度個別財務報告"] # 年報
REPORT_KEYWORDS = QRPT_KEYWORDS + ANNUAL_KEYWORDS         # 合併供 scrape 過濾用
EXCLUDE_KEYWORDS = ["公司債", "說明會", "法說會", "受邀", "召開", "會議", "提報", "預計", "決議日", "財務比率", "比率", "更正",
                    "補正",  # 2026-08-05：4927泰鼎-KY「更補正…附註」屬更正類但不含「更正」二字，會誤報解析不到數字
                    "延期", "展延", "准駁"]  # 8105凌巨型：財報延期申報准駁通知，主旨含報表名稱但無財務數字


def is_annual_subject(subject_norm: str) -> bool:
    """是否為年報主旨。
    排除「上半年度／半年度合併財務報告」——含「年度合併財務報告」子字串但實為
    Q2 半年報（2026-08-06 3325 旭品：期間 115/01/01~115/06/30 卻被歸為年報）。
    """
    if not any(k in (subject_norm or "") for k in ANNUAL_KEYWORDS):
        return False
    if re.search(r'半\s*年\s*度\s*(合併|個別)財務報告', subject_norm or ""):
        return False
    return True


def classify_item_type(subject_norm: str, is_jishi: bool, is_qrpt: bool, is_annual: bool) -> str:
    """決定公告類型。
    - 年報 / 季報：依關鍵字
    - 月營收：主旨含「營收」或「營業收入」且不含「損益」（純月營收重大訊息，
              如國巨「自結合併淨營收」、興勤「自結合併營業收入」）→ 不進自結損益解析器
    - 自結：其餘（自結合併損益等）
    """
    if is_annual:
        return "年報"
    if is_qrpt:
        return "季報"
    if ("營收" in subject_norm or "營業收入" in subject_norm) and "損益" not in subject_norm:
        return "月營收"
    return "自結"

# ── 路徑設定（需在此處先定義）──────────────────────────────────────────────
_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_DIR)

# ── 關注清單（從 Excel 動態載入）────────────────────────────────────────────
# 共用主檔：scripts/關注股票列表.xlsx（_PARENT 即 scripts 目錄）
# 2026-06 新格式：改讀「原始資料」分頁，欄位含 股票代號 / 股票名稱 / 研究員 / 實習生。
# 只收「研究員或實習生欄位有內容」的股票（無人負責者不送）。
# 不再分群組（舊版用分頁名當群組）；改成單一清單，每檔在訊息中加註負責人名字。
WATCHLIST_PATH   = os.path.join(_PARENT, "關注股票列表.xlsx")
WATCHLIST_SHEET  = "原始資料"   # 主檔新格式分頁（fallback 用）
# 優先讀精簡小清單 CSV（build_watchlist.py 產生），不存在才退回主檔 xlsx。
WATCHLIST_CSV    = os.path.join(_PARENT, "watchlist_active.csv")

# 執行時載入。{group_key: {"display","names"}}；group_key = 主檔組合(如 Leo／Ryan) 或 疊加者(如 Edison)。
_WATCHLIST     = {}
_STOCK_GROUPS  = {}   # 代號(str) / 正規化公司名 -> [所屬群組, ...]


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


def _load_watchlist():
    # 主檔研究員＋實習生＝合成一組（如 👤 Leo／Ryan）；疊加檔的人（如 Edison）各自獨立一組。
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

    # ---- 負責人過濾（與 F22/F27 共用 scripts/send_owners_f22.txt；環境變數 EVENT_OWNERS 可覆寫）----
    # 檔案不存在且無環境變數 → 不過濾（與舊版相同）。
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
    raw = os.environ.get("EVENT_OWNERS", "").strip()
    if not raw:
        path = os.path.join(_PARENT, "send_owners_f22.txt")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig") as f:
                raw = ",".join(ln.strip() for ln in f
                               if ln.strip() and not ln.strip().startswith("#"))
    if not raw:
        return set()
    return {x.strip() for x in re.split(r"[,，／/]", raw) if x.strip()}


NEW_MOPS_API        = "https://mops.twse.com.tw/mops/api/t05st02"
NEW_MOPS_DETAIL_API = "https://mops.twse.com.tw/mops/api/t05st02_detail"
NEW_MOPS_HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Referer":      "https://mops.twse.com.tw/",
    "Origin":       "https://mops.twse.com.tw",
}

CSV_PATH   = os.path.join(_DIR,    "important_events_v4.csv")
CREDS_PATH = os.path.join(_PARENT, "credentials.json")            # 與 mops_f22 共用
TOKEN_PATH = os.path.join(_PARENT, "token.json")
GDRIVE_FOLDER_ID = "1PSZv1zVCK1Z5McaeKyxeDZynf1JIb1tt"
GDRIVE_SCOPES    = ["https://www.googleapis.com/auth/drive"]

CSV_COLUMNS = [
    "announce_date", "announce_time", "co_id", "co_name",
    "type",
    "unit",
    "month_label", "月_營收", "月_稅前", "月_母利", "月_EPS",
    "qtr_label",   "季_營收", "季_稅前", "季_母利", "季_EPS",
    "note", "url",
]

BASE = "https://mopsov.twse.com.tw/mops/web"
HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer":      BASE,
}

# 指標關鍵字（與 v3 對齊）
METRICS = [
    ("營業收入",   "營收"),
    ("稅前淨利",   "稅前"),
    ("稅前純益",   "稅前"),
    ("稅前淨損",   "稅前"),
    ("稅前損益",   "稅前"),    # alias：部分公司用「稅前損益」（如 2017 官田鋼）
    ("歸屬予母",   "母利"),    # alias：「歸屬予母公司」寫法（多一個「予」字）
    ("歸屬母",     "母利"),
    ("業主淨利",   "母利"),
    ("業主淨損",   "母利"),
    ("歸屬本公司", "母利"),
    ("本期淨利",   "母利"),
    ("每股盈餘",   "EPS"),
    ("每股虧損",   "EPS"),
    ("稅後每股盈餘", "EPS"),  # alias：如 2237 華德動能
]

METRICS_ORDER  = ["營收", "稅前", "母利", "EPS"]
COMPLETE_LABELS = set(METRICS_ORDER)

# 漢字季度對照
KANJI_Q = {"一": "1", "二": "2", "三": "3", "四": "4"}

MOPS_GENERAL  = "https://mops.twse.com.tw/mops/#/web/t05sr01_1"
GSHEET_LINK   = "https://docs.google.com/spreadsheets/d/1QhirkF30Nk-v4krVMfMIhKth17lYveE7jiJNrEl0Qfg/edit?gid=1468015229#gid=1468015229"


def mops_link(co_id):
    return f"https://mops.twse.com.tw/mops/#/web/t146sb05?companyId={co_id}"


# =========================
# 工具
# =========================
def clean_text(text):
    return re.sub(r"\n\s*\n", "\n", text).strip()


def get_groups(co_id: str, co_name: str) -> list:
    """回傳此公司所屬的群組（命中回 [主檔組合, 疊加者...]，否則 [] = standalone）。
    先比代號，再比公司名子字串。"""
    cid = _wl_norm(co_id)
    if cid in _STOCK_GROUPS:
        return _STOCK_GROUPS[cid]
    cn = _wl_norm(co_name)
    for nm, gk in _STOCK_GROUPS.items():
        if not nm.isdigit() and nm and nm in cn:
            return gk
    return []


def parse_date_arg():
    """解析執行參數：
    無參數          → None（即時模式，抓當日 00:00 起公布的公告，已發送者由 sent_state 去重）
    summary         → 自動判斷最近交易日（時間 < 07:00 取昨天，否則取今天）
    yesterday       → 昨天
    YYYY-MM-DD      → 指定日期
    """
    if len(sys.argv) < 2:
        return None
    arg = sys.argv[1]
    if arg == "summary":
        now = datetime.now()
        if now.hour < 7:
            return now - timedelta(days=1)
        return now
    if arg == "yesterday":
        return datetime.now() - timedelta(days=1)
    try:
        return datetime.strptime(arg, "%Y-%m-%d")
    except Exception:
        sys.stderr.write("❌ 日期格式錯誤 (YYYY-MM-DD 或 summary / yesterday)\n")
        sys.exit(1)


def get_lookback_dt():
    """當天 00:00 為起點：只抓「今天公布」的公告。
    每日各輪次（含週末）都會涵蓋當天全天；重複發送由 sent_state.json 去重。
    注意：前一天 23:00 排程之後才發布的公告（23:00–24:00）不會被隔天補抓。"""
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# =========================
# MOPS 抓取
# =========================
def scrape_today(session):
    """即時 feed（HTML API），補足 REST API 尚未更新的最新筆"""
    from bs4 import BeautifulSoup
    results  = []
    seen_ids = set()
    for off in range(1, 20):
        resp = session.post(
            f"{BASE}/ajax_t05sr01_1",
            data={"step": "0", "firstin": "true", "off": str(off), "TYPEK": "all"},
            headers={**HEADERS, "Referer": f"{BASE}/t05sr01_1"},
        )
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("tr.even, tr.odd")
        if not rows:
            break
        new_found = False
        for row in rows:
            cols = row.find_all("td")
            if len(cols) < 5:
                continue
            subject      = cols[4].get_text(strip=True)
            subject_norm = re.sub(r'\s+', '', subject)
            is_jishi  = any(k in subject_norm for k in KEYWORDS)
            is_qrpt   = any(k in subject_norm for k in QRPT_KEYWORDS)
            is_annual = is_annual_subject(subject_norm)
            if not (is_jishi or is_qrpt or is_annual):
                continue
            if any(k in subject_norm for k in EXCLUDE_KEYWORDS):
                continue
            btn    = cols[5].find("input", type="button") if len(cols) > 5 else None
            onclick = btn.get("onclick", "") if btn else ""
            params = dict(re.findall(r"\.(\w+)\.value='([^']*)'", onclick))
            uid = (cols[0].get_text(strip=True), cols[2].get_text(strip=True), cols[3].get_text(strip=True))
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            new_found = True
            detail = ""
            if params.get("SEQ_NO"):
                dr = session.post(
                    f"{BASE}/ajax_t05sr01_1",
                    data={
                        "step": "1", "firstin": "1", "off": "1", "TYPEK": "all",
                        "SEQ_NO":     params.get("SEQ_NO", ""),
                        "SPOKE_TIME": params.get("SPOKE_TIME", ""),
                        "SPOKE_DATE": params.get("SPOKE_DATE", ""),
                        "COMPANY_ID": params.get("COMPANY_ID", ""),
                        "skey":       params.get("skey", ""),
                    },
                    headers={**HEADERS, "Referer": f"{BASE}/t05sr01_1"},
                )
                pre = BeautifulSoup(dr.text, "html.parser").find("pre")
                detail = clean_text(pre.get_text()) if pre else ""
            results.append({
                "co_id":     cols[0].get_text(strip=True),
                "co_name":   cols[1].get_text(strip=True),
                "date":      cols[2].get_text(strip=True),
                "time":      cols[3].get_text(strip=True),
                "subject":   subject,
                "detail":    detail,
                "item_type": classify_item_type(subject_norm, is_jishi, is_qrpt, is_annual),
            })
        if not new_found:
            break
    return results


def _fetch_detail_new(session, params):
    try:
        resp = session.post(NEW_MOPS_DETAIL_API, json=params,
                            headers=NEW_MOPS_HEADERS, timeout=30)
        data = resp.json()
    except Exception as e:
        sys.stderr.write(f"⚠️  detail API 失敗：{e}\n")
        return ""
    if data.get("code") != 200:
        return ""
    result = data.get("result", {})
    for key in ("content", "text", "body", "detail", "data"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return clean_text(val)
    if isinstance(result, str) and result.strip():
        return clean_text(result)
    return clean_text(str(result)) if result else ""


def scrape_history(session, dt):
    roc_year = dt.year - 1911
    results  = []
    try:
        resp = session.post(
            NEW_MOPS_API,
            json={"year": str(roc_year), "month": str(dt.month), "day": str(dt.day)},
            headers=NEW_MOPS_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except Exception as e:
        sys.stderr.write(f"⚠️  MOPS API 失敗：{e}\n")
        return results
    if data.get("code") != 200:
        return results

    for row in data.get("result", {}).get("data", []):
        if len(row) < 5:
            continue
        date_str    = row[0]
        # API 偶有回非查詢日的資料：嚴格只保留「查詢那天」的公告
        _dd = _parse_announce_date(date_str)
        if _dd is not None and _dd != dt.date():
            continue
        time_str    = row[1]
        co_id       = str(row[2])
        co_name     = row[3]
        subject     = row[4]
        detail_info = row[5] if len(row) > 5 else {}

        subject_norm = re.sub(r'\s+', '', subject)
        is_jishi  = any(k in subject_norm for k in KEYWORDS)
        is_qrpt   = any(k in subject_norm for k in QRPT_KEYWORDS)
        is_annual = is_annual_subject(subject_norm)
        if not (is_jishi or is_qrpt or is_annual):
            continue
        if any(k in subject_norm for k in EXCLUDE_KEYWORDS):
            continue

        detail = ""
        if isinstance(detail_info, dict):
            params = detail_info.get("parameters", {})
            if params:
                detail = _fetch_detail_new(session, params)

        results.append({
            "co_id":     co_id,
            "co_name":   co_name,
            "date":      date_str,
            "time":      time_str,
            "subject":   subject,
            "detail":    detail,
            "item_type": classify_item_type(subject_norm, is_jishi, is_qrpt, is_annual),
        })
    return results


def normalize_item(item):
    """統一換行處理，建立 financial_text"""
    detail = item.get("detail", "")
    detail = detail.replace('\\n', '\n').replace('\\t', '\t')
    item["detail"] = detail
    item["financial_text"] = detail
    return item


# =========================
# 期間標籤解析
# =========================
def extract_period_labels(text):
    """從表頭解析月份與季度標籤
    月份：XXX年Y月  → "XXX/MM"（ROC 年/月）
    季度：XXX年第N季 → "YYYY QN"（西元年 + 季）
    """
    month_label   = None
    quarter_label = None

    # 月份：如 "115年3月" 或 "115年03月"
    # 少數公司用西元年（如 6451 訊芯-KY「2026年06月」），原本 (\d{3}) 會抓到
    # "026" → month_label "026/06"。改為 (?<!\d)(\d{3,4}) 並統一正規化成 ROC。
    m = re.search(r'(?<!\d)(\d{3,4})年(\d{1,2})月', text)
    if m:
        _y = int(m.group(1))
        _roc = _y - 1911 if _y >= 1911 else _y
        month_label = f"{_roc}/{int(m.group(2)):02d}"
    else:
        # Fallback（6715 嘉基型）：表頭把年月拆開寫成「115年 7月」（年後有空白），
        # 上面的緊湊規則抓不到 → month_label=None → 下游「純季度格式」誤判，
        # 把「最近一月」的數字整組搬去當單季值（159 誤當 Q2，實際 Q2 為 411）。
        # 僅在緊湊規則完全無匹配時才放寬空白，避免在同時含去年比較欄的版型
        # （如 6173 信昌電）改抓到更前面的「114年 7月」而回歸變壞。
        m = re.search(r'(?<!\d)(\d{3,4})年\s*(\d{1,2})\s*月', text)
        if m:
            _y = int(m.group(1))
            _roc = _y - 1911 if _y >= 1911 else _y
            month_label = f"{_roc}/{int(m.group(2)):02d}"

    # 季度：如 "114年第四季" / "114年第4季" / "115年第一季"
    # 用 negative lookahead 排除「X年第N季 至」這種累計範圍起點（如 6141 柏承的
    # 「114年第2季至115年第1季」），避免誤抓累計起點當成實際季度。
    # 優先找「緊接著增減%」的季度標籤：這是「最近一季」欄本身的標籤，
    # 可靠地與「累計四季」欄的起訖季度（如「114年第2季至115年第1季」）區分開。
    # 背景：MOPS 表格轉文字時常把累計欄的「季...至...季」拆到不同行、
    # 中間夾雜其他欄位文字，讓單純的「(?!至)」判斷失效（見 3167 案例：
    # 誤將累計起點「114年第2季」當成單季標籤，實際單季應為「115年第1季」，
    # 已用 F22 月營收加總 1,946 百萬 驗證吻合）。
    # 「第」字可省略（如 1303 南亞「(115年1季)」）→ 第? 選擇性
    # 「至」判斷改為容許中間有空白／換行（如 8150 南茂：「(114年第2季\n至115年第1季)」
    #   累計欄起點與「至」被換行拆開，原本 (?!至) 失效而誤抓累計起點）
    # 「增減」前常夾「同期」二字（如 1310 台苯「115年第1季　同期增減%」），
    #   原本 (?=\s{0,10}增減) 匹配失敗 → 退到下一段規則而誤抓累計欄起點
    #   「114年第2季」→ 2025 Q2（實際應為 2026 Q1）。故允許中間出現「同期」。
    # 年份改 (?<!\d)(\d{3,4})：相容西元年寫法（6451 訊芯-KY「2026年第1季」，
    #   原本抓到 "026" → 026+1911 = 1937 Q1）。
    q = re.search(r'(?<!\d)(\d{3,4})年第?([一二三四1-4])季(?=[\s]{0,10}(?:同期)?[\s]{0,10}增\s*減)', text)
    if not q:
        # 括號完整閉合的形式「(115年第1季)」＝單季欄標籤，
        # 依定義不可能是累計區間起點（8150 南茂：累計欄「(114年第2季」與
        # 「至115年第1季)」跨行拆開，(?!至) 無法排除 → 誤抓 2025 Q2）
        # 2026-08-23：「年」改為選擇性（年?）。5314 世紀* 單季欄寫「(115第2季)」
        #   缺「年」字 → 本規則不匹配，退到下一條後誤抓四季累計反序區間
        #   「(115年第2季至114年第3季)」的末端「114年第3季」→ 2025 Q3（實際 2026 Q2）。
        # 同時允許括號內帶「單季／最近一季／本季」前綴：6187 萬潤同行並列
        #   「(單季115第2季)」與去年同期欄「(114第2季)」，若不吃下前綴，
        #   放寬後會改抓到去年同期欄 → 2025 Q2（實際 2026 Q2）。
        # 回歸驗證：raw_archive 全部 1,661 檔重跑，僅 5314 一筆改變且改為正確。
        q = re.search(r'[（(]\s*(?:單季|最近一季|本季)?\s*(?<!\d)(\d{3,4})年?第?([一二三四1-4])季\s*[)）]', text)
    if not q:
        q = re.search(r'(?<!\d)(\d{3,4})年第?([一二三四1-4])季(?!\s*至)', text)
    if not q:
        # fallback：若全部都是「至」結尾（不太可能），退回原本邏輯
        q = re.search(r'(?<!\d)(\d{3,4})年第?([一二三四1-4])季', text)
    if q:
        _qy = int(q.group(1))
        western_year = _qy if _qy >= 1911 else _qy + 1911
        q_char = q.group(2)
        q_num  = KANJI_Q.get(q_char, q_char)
        quarter_label = f"{western_year} Q{q_num}"

    return month_label, quarter_label


# =========================
# 單位偵測與換算
# =========================
# 換算係數（目標：百萬元）
UNIT_FACTORS = {
    "仟元": 1 / 1000,
    "千元": 1 / 1000,
    "百萬元": 1,
    "佰萬元": 1,
    "億元": 100,
}
# 顯示時不標注的單位（百萬元視為預設）
UNIT_SILENT = {"百萬元", "佰萬元", "百萬"}


def detect_unit(text):
    """偵測公告單位，回傳 (unit_str, factor)。
    - 偵測失敗或單位矛盾 → (None, None)
    - 百萬元系列 → ("百萬元", 1)，不標注
    - 仟元 / 億元 等 → (unit_str, factor)，需標注並換算
    """
    # 1. 表頭全域宣告，如「(單位：仟元)」、「單位：百萬元」、「(單位:新台幣仟元)」
    #    也接受省略「元」字的寫法：「單位:新台幣佰萬」「單位:百萬」（如 6141 柏承）
    hm = re.search(r'單位[：:\s]*(?:新台幣|NT\$|NTD)?\s*(仟元|千元|百萬元|佰萬元|百萬|佰萬|億元)', text)
    if hm:
        u = hm.group(1)
        # 統一 佰萬元 / 百萬 / 佰萬 → 百萬元（避免下游 UNIT_FACTORS 查不到）
        if u in ("佰萬元", "百萬", "佰萬"):
            u = "百萬元"
        return u, UNIT_FACTORS.get(u, None)

    # 1.5 數值行上直接標注的單位，如「營業收入(仟元):139,922,549」
    #     這是最權威的來源（單位緊貼數字）。用於排除敘述文字中的其他單位干擾
    #     （2409 友達：表格為仟元，但「其他應敘明事項」敘述用億元 → 單位矛盾判為
    #      偵測失敗，數字未換算成百萬）
    inline_val_units = set()
    for m2 in re.finditer(
            r'[（(](?:新台幣|NT\$|NTD)?\s*(仟元|千元|百萬元|佰萬元|百萬|佰萬|億元)\s*[)）]\s*[:：]\s*\(?-?[\d,]',
            text):
        u2 = m2.group(1)
        inline_val_units.add("百萬元" if u2 in ("佰萬元", "百萬", "佰萬") else u2)
    if len(inline_val_units) == 1:
        u2 = inline_val_units.pop()
        return u2, UNIT_FACTORS.get(u2, None)

    # 2. 從資料行的 inline 單位（如「營業收入(百萬元)」）收集
    found_units = set()
    for m in re.finditer(r'[（(](?:新台幣|NT\$|NTD)?\s*(仟元|千元|百萬元|佰萬元|百萬|佰萬|億元)[)）]', text):
        found_units.add(m.group(1))

    # 3. 數字後直接接單位（如「203,573仟元」）
    #    排除「百萬股」「億股」等股數單位（如 2409 友達註2「(7,547百萬股)」
    #    會被誤認為金額單位百萬，與本文仟元衝突 → 單位偵測失敗、數字未換算）
    for m in re.finditer(r'[\d,]+(?:\.\d+)?\s*(仟元|千元|百萬元|佰萬元|百萬|佰萬|億元)(?!\s*股)', text):
        found_units.add(m.group(1))

    # 4. Fallback：敘述文字中的裸單位（如「餘為百萬元」「金額單位百萬元計」）
    # 僅在第 2、3 層都沒抓到時啟動，避免污染既有 path
    if not found_units:
        for unit_kw in ("仟元", "千元", "百萬元", "佰萬元", "百萬", "佰萬", "億元"):
            if unit_kw in text:
                found_units.add(unit_kw)

    # 去掉 EPS 行常見的「元」單位，避免干擾
    found_units.discard("元")

    if not found_units:
        # 無任何單位線索 → 無法判斷
        return None, None

    # 統一 佰萬元 / 百萬 / 佰萬 → 百萬元
    normalized = set()
    for u in found_units:
        normalized.add("百萬元" if u in ("佰萬元", "百萬", "佰萬") else u)

    if len(normalized) > 1:
        # 單位矛盾（混用）
        return None, None

    u = normalized.pop()
    return u, UNIT_FACTORS.get(u, None)


def _fmt_num(val_str, factor, label):
    """將 val_str 依 factor 換算，EPS 不動。回傳格式化字串。"""
    if label == "EPS" or factor == 1:
        return val_str
    try:
        raw = float(val_str.replace(",", ""))
        converted = raw * factor
        # 依數值大小決定小數位
        if abs(converted) >= 100:
            return f"{converted:,.0f}"
        elif abs(converted) >= 1:
            return f"{converted:,.2f}"
        else:
            return f"{converted:.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return val_str


# =========================
# 財務數字擷取
# =========================
def _numbers_after_keyword(s, keyword):
    """在行 s 中找 keyword 之後所有非百分比數字，回傳 list[str]

    為了讓「稅前淨(損)利」「每股(損失)盈餘」這類中間插括號標注的關鍵字也能比對，
    比對前先做整行 normalize：(NUM) → -NUM 保留負數，再移除其餘括號文字標注。
    """
    # 先 (NUM) → -NUM 保留負數，再移除括號內文字標注（含單位、損失標注等）
    # 2026-08-13：括號改為半形/全形皆可（1742 台蠟用全形（18,410）表示負數，
    # 原本只認半形 → 全形被下一行整段移除，稅前/母利抓不到值）
    s_clean = re.sub(r'[（(]\s*(\d[\d,]*(?:\.\d+)?)\s*[)）]', r'-\1', s)
    s_clean = re.sub(r'[（(][^)）]*[)）]', '', s_clean)
    # 修正公告原文打錯的千分位：小數點後接「3位數+逗號+3位數」→ 必為千分位誤植
    # （2026-08-12 9918 欣天然：營收原文寫 "1.169,443"，應為 1,169,443）
    s_clean = re.sub(r'(?<=\d)\.(?=\d{3},\d{3})', ',', s_clean)
    if keyword not in s_clean:
        return []
    after = s_clean[s_clean.index(keyword) + len(keyword):]
    # 抓取所有非百分比數字（數字 token 至少要含一位數字，避免孤立的「,」「-」被誤判為數字）
    nums = []
    for m in re.finditer(r'([-+]?\d[\d,]*(?:\.\d+)?)', after):
        rest = after[m.end():]
        if not re.match(r'\s*[%％]', rest):
            nums.append(m.group(1))
    return nums


def _has_two_compare_cols(text):
    """判別格式 D：明講「去年同月」，或資料行同時有 ≥2 個 % 且 ≥4 個數值
    （= 月與季各帶去年比較欄，如 8383 千附）。"""
    if re.search(r'同月', text or ""):
        return True
    for kw in ("營業收入", "稅前淨利", "每股盈餘"):
        for line in (text or "").split("\n"):
            if kw in re.sub(r'[（(][^)）]*[)）]', '', line):
                pcts = len(re.findall(r'[\d.,]+\s*[%％]', line))
                nums = len(_numbers_after_keyword(line, kw))
                if pcts >= 2 and nums >= 4:
                    return True
                break
    return False


def extract_financials_both(text):
    """從財務文字同時擷取月份（第1欄）與季度（第2欄）數字
    回傳：(month_data, month_label, qtr_data, qtr_label, unit_str)
      month_data / qtr_data：{label: value_str}（已換算為百萬元）或 {}
      unit_str：原始單位（"百萬元" / "仟元" / None=偵測失敗）
    """
    month_label, qtr_label = extract_period_labels(text)
    unit_str, unit_factor = detect_unit(text)

    # ── 格式 E（6768 志強-KY 型）：會計項目對齊表格 ──────────────────────────
    #   表頭「2026年04~06月  2026年01~06月  2025年04~06月  2025年01~06月」
    #   欄序 = [本單季, 本累計, 去年單季, 去年累計] → 取第 0 欄為單季值
    e_hdr = re.search(r'(\d{4})年\s*(\d{1,2})\s*~\s*(\d{1,2})\s*月', text)
    if e_hdr and "會計項目" in text:
        q_num  = (int(e_hdr.group(3)) - 1) // 3 + 1
        e_keys = [("營業收入", "營收"), ("營業毛利", "毛利"), ("稅前淨利", "稅前"),
                  ("歸屬母公司淨利", "母利"), ("本期淨利", "_本期"),
                  ("基本每股盈餘", "EPS")]
        e_found = {}
        for line in text.split('\n'):
            s = line.strip()
            for kw, label in e_keys:
                if s.startswith(kw) and "率" not in s[:len(kw) + 2] and label not in e_found:
                    nums = re.findall(r'\(?-?[\d,]+(?:\.\d+)?\)?', s[len(kw):])
                    if nums:
                        v = nums[0]
                        neg = v.startswith('(')
                        e_found[label] = _fmt_num(('-' if neg else '') + v.strip('()'),
                                                  unit_factor or 1, label)
                    break
        if "母利" not in e_found and "_本期" in e_found:
            e_found["母利"] = e_found["_本期"]
        e_found.pop("_本期", None)
        if len(e_found) >= 4:
            return {}, None, e_found, f"{e_hdr.group(1)} Q{q_num}", unit_str

    # ── 格式 F（4915 致伸型）：ROC年「MM月至MM月」單季 vs「MM月至MM月」累計對照表 ──
    #   表頭「115年04月至06月　115年01月至06月」，第1欄=本季、第2欄=本年累計。
    #   舊版月份regex會把「115年04月」誤判成單月(115/04)標籤，實際上17,211,510
    #   是Q2單季營收，不是4月單月營收（見4915案例：與F22 4月營收5,468.32對不上，
    #   已用「本季」定義釐清）。只在沒有命中格式E時才試，避免搶走既有格式。
    if not (e_hdr and "會計項目" in text):
        f_hdr = re.search(r'(?<!\d)(\d{3,4})年\s*(\d{1,2})\s*月\s*至\s*(\d{1,2})\s*月', text)
        if f_hdr:
            f_year = int(f_hdr.group(1))
            f_year = f_year if f_year >= 1911 else f_year + 1911
            f_q    = (int(f_hdr.group(3)) - 1) // 3 + 1
            f_keys = [("每股盈餘", "EPS"), ("母公司稅後淨利", "母利"), ("母公司淨利", "母利"),
                      ("稅前淨利", "稅前"), ("營業淨利", "營益"), ("本期淨利", "_本期"),
                      ("營業收入", "營收"), ("營業毛利", "毛利")]
            f_found = {}
            for line in text.split('\n'):
                s = line.strip()
                if not s or "率" in s[:14]:
                    continue
                for kw, label in f_keys:
                    if kw in s and label not in f_found:
                        after = s[s.index(kw) + len(kw):]
                        nums = re.findall(r'\(?-?[\d,]+(?:\.\d+)?\)?', after)
                        if nums:
                            v = nums[0]
                            neg = v.startswith('(')
                            f_found[label] = _fmt_num(('-' if neg else '') + v.strip('()'),
                                                       unit_factor or 1, label)
                        break
            if "母利" not in f_found and "_本期" in f_found:
                f_found["母利"] = f_found["_本期"]
            f_found.pop("_本期", None)
            if len(f_found) >= 4:
                return {}, None, f_found, f"{f_year} Q{f_q}", unit_str

    month_found = {}
    qtr_found   = {}

    has_minority  = bool(re.search(r'非控制權益|少數股東|少數股權', text))
    is_parent_only = bool(re.search(r'個別|個體', text))

    # 決定是否允許以「稅後純益」替代「母利」
    allow_after_tax_sub = (not has_minority) or is_parent_only

    # ── 格式 G（6581 鋼聯型）：月自結損益四欄表，完全沒有「季」欄 ──────────────
    #   表頭「115年7月  114年7月  115年1-7月  114年1-7月」
    #   欄序 = [本月, 去年同月, 本年累計, 去年累計]
    #   舊版 qtr_col=1 會把「去年同月」當成單季值發出（2026-08-06 6581：
    #   季營收誤報 182、季稅前誤報 73.67，實為去年 7 月數；已與 mops_f22
    #   monthly_revenue.csv 核對：115/07 營收 90,260、114/07 營收 182,441 仟元）。
    #   → 偵測到此格式時清空季資料，寧可不報也不報錯。
    _g_hdr = re.search(
        r'(\d{3})年\s*(\d{1,2})\s*月\s+(\d{3})年\s*(\d{1,2})\s*月\s+'
        r'(\d{3})年\s*1\s*[-~至]\s*(\d{1,2})\s*月', text)
    is_month_cum_table = bool(_g_hdr and _g_hdr.group(2) == _g_hdr.group(4))

    # ── 格式偵測 ──────────────────────────────────────────────────────────────
    # 格式 A（7734 型）：月和季是獨立的兩個表格
    #   單月(註1) … / 最近一季單季(註2) …
    #   → 各自掃描第 0 欄（當期值）
    #
    # 格式 B（3024 型）：月和季在同一行，增減% 欄無 % 符號
    #   月值 | 月增減 | 季值 | 季增減 | 累計  全在同一行
    #   → qtr_col = 2
    #
    # 格式 C（一般）：標準兩欄（月值 | 季值）
    #   → qtr_col = 1

    # 「單季」段落標記順序不一：多數為「(2)單季」，但 3374 精材型是「單季(註2)」
    # （標記在後）。兩種都要認得，否則會退回誤把「月」表格自身兩欄
    # （本月／去年同月）錯當成（月值／季值）配對（如 3374 案例：季營收誤抓成
    # 去年同月值 475，正確應為單季表的 1,914，已用原文人工核對確認）。
    qtr_sect_m = re.search(
        r'最近一季單季|[（(]\s*註?\s*\d+\s*[）)]\s*單季|單季\s*[（(]\s*註?\s*\d+\s*[）)]'
        r'|[（(]\s*[一二三四五六七八九十]+\s*[）)]\s*單季',
        text)
    if qtr_sect_m:
        # 格式 A：切出月份段和季度段
        month_section = text[:qtr_sect_m.start()]
        rest          = text[qtr_sect_m.start():]
        four_q_m      = re.search(r'最近四季', rest)
        qtr_section   = rest[:four_q_m.start()] if four_q_m else rest
        use_sections  = True
    else:
        # 偵測「YoY 增減%」欄位：除了連續字串外，也認「與去年同期」出現 ≥ 2 次
        # （6531 型：表頭「與去年同期」+「增 減%」分兩行寫，連續比對抓不到）
        has_pct_cols = (
            bool(re.search(r'與去年同期增減|同期增減', text))
            or text.count('與去年同期') >= 2
        )
        has_explicit_pct = bool(re.search(r'[\d,]+(?:\.\d+)?\s*[%％]', text))
        # 格式 B（3024 / 6531 型）：交錯格式且增減欄無 % 符號 → qtr_col=2
        # 格式 C / 6805 / 6669 型：有 % 符號，過濾後仍用 qtr_col=1
        qtr_col      = 2 if (has_pct_cols and not has_explicit_pct) else 1
        # 格式 D（6173 信昌電 / 8383 千附型）：月表含去年比較欄
        #   本月|去年同月|增減%|單季|去年同季|增減%|四季累計
        #   % 欄已被過濾 → 數字序為 [本月, 去年同月, 單季, 去年同季, 四季]，單季在第 2 欄
        if _has_two_compare_cols(text):
            qtr_col = 2
        use_sections = False

    def _scan_once(targets, result_dict, scan_text, col_idx):
        """單一段落掃描：取 nums[col_idx] 存入 result_dict，支援 split-line"""
        lines   = scan_text.split('\n')
        pending = {}

        for line in lines:
            s      = line.strip()
            is_sep = not s or (re.fullmatch(r'[-=＝ ]+', s) and re.search(r'[-=＝]{4,}', s))

            if pending and not is_sep and re.search(r'\d', s):
                s_conv = re.sub(r'\((\d[\d,]*(?:\.\d+)?)\)', r'-\1', s)
                s_conv = re.sub(r'[（(][^)）]*[)）]', '', s_conv)
                raw_nums = []
                for mm in re.finditer(r'([-+]?\d[\d,]*(?:\.\d+)?)', s_conv):
                    rest_s = s_conv[mm.end():]
                    if not re.match(r'\s*[%％]', rest_s):
                        raw_nums.append(mm.group(1))
                for label in list(pending.keys()):
                    if raw_nums and label not in result_dict and len(raw_nums) > col_idx:
                        result_dict[label] = raw_nums[col_idx]
                    del pending[label]

            if is_sep:
                pending.clear()
                continue

            # 為 keyword 比對建立 normalize 版（移除括號文字標注），
            # 讓「稅前淨(損)利」「每股(損失)盈餘」這類也能 set pending
            s_for_kw = re.sub(r'[（(][^)）]*[)）]', '', s)
            for keyword, label in targets:
                nums = _numbers_after_keyword(s, keyword)
                if nums:
                    if label not in result_dict and len(nums) > col_idx:
                        result_dict[label] = nums[col_idx]
                elif keyword in s_for_kw:
                    if label not in result_dict and label not in pending:
                        pending[label] = keyword

    def _scan_metrics(targets, mf, qf):
        if use_sections:
            # 格式 A：獨立段落，各取第 0 欄
            _scan_once(targets, mf, month_section, col_idx=0)
            _scan_once(targets, qf, qtr_section,   col_idx=0)
        else:
            # 格式 B / C：同一段落，月取 col 0，季取 qtr_col
            _scan_once(targets, mf, text, col_idx=0)
            _scan_once(targets, qf, text, col_idx=qtr_col)

    # 主要指標掃描
    _scan_metrics(METRICS, month_found, qtr_found)

    # Fallback：format A 切段後完全沒抓到（如 1568 倉佑：說明文字含
    # 「最近一季單季」「最近四季累計」誤觸切段，把資料表切掉）→ 改用 format C 全文重掃
    if use_sections and not month_found and not qtr_found:
        use_sections = False
        _has_pct  = bool(re.search(r'與去年同期增減|同期增減', text))
        _has_epct = bool(re.search(r'[\d,]+(?:\.\d+)?\s*[%％]', text))
        qtr_col   = 2 if (_has_pct and not _has_epct) else 1
        _scan_metrics(METRICS, month_found, qtr_found)

    # 稅後純益備援（無少數股東 or 個別報表）
    if "母利" not in month_found and allow_after_tax_sub:
        AFTER_TAX = [("稅後純益", "母利"), ("稅後淨利", "母利"), ("稅後損失", "母利"), ("稅後損益", "母利")]
        _scan_metrics(AFTER_TAX, month_found, qtr_found)

    # 純季度格式（如 3491 昇達科）：無月份標籤但有季度標籤
    # col_idx=0 是當季值（已存入 month_found），col_idx=1 是比較期（去年同季），捨棄
    if month_label is None and qtr_label is not None and month_found:
        qtr_found  = month_found
        month_found = {}

    # 格式 G：本表無季欄位 → 清空季資料，避免把「去年同月」誤報成單季
    if is_month_cum_table:
        qtr_found = {}
        qtr_label = None

    # 換算（unit_factor=None 代表偵測失敗，不換算但保留原值）
    factor = unit_factor if unit_factor is not None else 1
    for d in (month_found, qtr_found):
        for lbl in list(d.keys()):
            d[lbl] = _fmt_num(d[lbl], factor, lbl)

    # ── Fallback：純文字敘述型自結公告（6231 系微 / 6270 倍微型）────────────
    # 僅在表格解析「幾乎沒抓到東西」（月＋季合計不足 2 項）時啟動，
    # 這種結果本來就無法使用，故不影響既有正常表格案例
    if len(month_found) + len(qtr_found) < 2:
        n_found = extract_narrative_metrics(text)
        if n_found:
            # 敘述型每個數字自帶單位（可能混用），不回傳全篇單位以免標注誤導
            return {}, None, n_found, qtr_label, None

    return month_found, month_label, qtr_found, qtr_label, unit_str


# =========================
# 純文字敘述型自結公告 fallback
# =========================
# 每個數字自帶單位（如「新台幣9.56億元」「2,804萬元」「464,744仟元」），
# 因此逐一換算，不使用全篇單一單位（避免億元／萬元混用被判為單位矛盾）
_NARR_UNIT = {"億元": 100, "億": 100, "萬元": 0.01, "萬": 0.01,
              "仟元": 1 / 1000, "千元": 1 / 1000,
              "百萬元": 1, "百萬": 1, "佰萬元": 1, "佰萬": 1}

# 目標科目；帶底線者為「誘餌」關鍵字，只用來吃掉鄰近的無關數字，最後丟棄
_NARR_KEYS = [
    ("營收", ["合併營收", "營業收入", "營收"]),
    ("毛利", ["合併毛利", "營業毛利"]),
    ("稅前", ["稅前利益", "稅前淨利", "稅前獲利", "稅前損益", "稅前純益"]),
    ("母利", ["歸屬於母公司業主淨利", "歸屬母公司業主淨利", "歸屬於母公司業主之淨利",
              "歸屬母公司淨利", "母公司業主淨利"]),
    ("_棄", ["營業利益", "營業損失", "營業淨利", "實收股本", "股本", "稅後淨利",
             "本期淨利", "總資產", "總負債", "每股稅前盈餘", "每股淨值"]),
]


def extract_narrative_metrics(text, window=30):
    """從敘述文字抽取單季財務數字。
    作法：找出所有「數字＋單位」，往前 window 字內找「最靠近」的科目關鍵字配對。
    含「累計」字樣的句子屬累計數，捨棄（只取本季單季數）。
    回傳 {label: 值字串(百萬)}；抓不到 2 項以上則回傳 {}。
    """
    found = {}
    pat = r'([\d,]+(?:\.\d+)?)\s*(億元|億|萬元|萬|仟元|千元|百萬元|百萬|佰萬元|佰萬)'
    for m in re.finditer(pat, text):
        head = text[max(0, m.start() - window):m.start()]
        head = re.sub(r'\s+', '', head)
        # 累計句 → 略過（只保留本季單季數）
        if "累計" in head:
            continue
        best_label, best_pos = None, -1
        for label, kws in _NARR_KEYS:
            for kw in kws:
                p = head.rfind(kw)
                if p > best_pos:
                    best_pos, best_label = p, label
        if best_label is None or best_pos < 0 or best_label in found:
            continue
        val = float(m.group(1).replace(',', '')) * _NARR_UNIT[m.group(2)]
        found[best_label] = val
    found.pop("_棄", None)
    if len(found) < 2:
        return {}
    # 已是百萬元，用 factor=1e-6 走既有格式化（等同乘 1 但套用小數位規則）
    return {k: _fmt_num(f"{v * 1000000:.0f}", 1e-6, k) for k, v in found.items()}


# =========================
# 精簡損益表 fallback（銀行 / 金控縮寫欄名格式）
# =========================
def extract_compact_pnl(text):
    """處理金控／銀行常見的精簡自結損益表：欄名縮寫且分多行、數字集中在一行。
    例（5876 上海商銀）：
        單位:億元
        ------5月損益----------累計1-5月損益--------
         合併    母公司      合併     母公司   基本EPS
         稅前   業主稅後     稅前    業主稅後
         22.30   14.03      125.75   81.71     1.68
    對應：月稅前 / 月母利 / 累計稅前 / 累計母利 / EPS（位置對應）。

    僅在偵測到此格式（含「業主稅後」）時啟動；欄數與表頭推算不符 → 回傳 None
    （維持原警告，絕不亂填）。
    回傳：(month_data, month_label, cum_data, cum_label, unit_str) 或 None
    """
    if "業主稅後" not in text:
        return None

    unit_str, factor = detect_unit(text)
    factor = factor if factor is not None else 1

    month_label, _ = extract_period_labels(text)

    # 累計標籤：累計1-5月 / 累計5月
    cum_label = None
    cm = re.search(r'累計\s*1?\s*[-~至]?\s*(\d{1,2})\s*月', text)
    if cm:
        cum_label = f"累計1-{int(cm.group(1))}月"
    elif "累計" in text:
        cum_label = "累計"

    has_cum = cum_label is not None
    has_eps = bool(re.search(r'EPS|每股', text))

    # 每期指標：合併稅前 + 母公司業主稅後 → [稅前, 母利]
    labels_per_period = ["稅前", "母利"]
    periods           = ["month", "cum"] if has_cum else ["month"]
    expected          = len(periods) * len(labels_per_period) + (1 if has_eps else 0)

    # 數字資料行：取「純數字、無中文/冒號/年月日」且 token 最多的一行
    best_nums = []
    for line in text.split('\n'):
        if re.search(r'[一-鿿:：]', line):
            continue
        nums = re.findall(r'-?\d[\d,]*(?:\.\d+)?', line)
        if len(nums) > len(best_nums):
            best_nums = nums

    if len(best_nums) != expected:
        return None  # 版面與推算不符，放棄（不亂填）

    def conv(num_str, is_eps=False):
        try:
            v = float(num_str.replace(",", ""))
        except ValueError:
            return num_str
        if is_eps:
            return num_str
        return _fmt_num(num_str, factor, "稅前")

    idx = 0
    month_data, cum_data = {}, {}
    for p in periods:
        target = month_data if p == "month" else cum_data
        target["稅前"] = conv(best_nums[idx]);     idx += 1
        target["母利"] = conv(best_nums[idx]);     idx += 1
    if has_eps:
        # 單一 EPS 多為累計值，放累計區；無累計則放月區
        (cum_data if has_cum else month_data)["EPS"] = best_nums[idx]
        idx += 1

    return month_data, month_label, cum_data, cum_label, unit_str


# =========================
# 金控矩陣表自結（獨立備援，不影響既有流程）
# =========================
# 起因：金控自結為「表頭在上、各子公司數值在下」的矩陣表，既有解析器都假設
# 「關鍵字與數值同一行」，因此整則抓不到、完全不發訊息（漏報）。
# 2026-08-12 元大金、08-14 富邦金/第一金/合庫金 連續中招。
# 設計原則：**只在既有解析全部失敗時才呼叫**，抓不到就回 None 交回原流程，
# 不動任何既有邏輯，確保不會影響已正常的公司。
#
# 已驗證的 5 檔（2885 元大金、2881 富邦金、2892 第一金、5880 合庫金）欄序一致：
#   [單月稅前, 單月稅後, 累計稅前, 累計稅後, 累計EPS]
# 母公司列一律是「第一個列名包含 co_name 的資料列」（富邦金另有 FVOCI 第二區塊，
# 取第一筆即為正確的損益表列）。
_HOLDCO_ROW = re.compile(r'^\s*(\S{2,12}?)\s+((?:-?[\d,]+(?:\.\d+)?\s+){4}-?[\d,]+(?:\.\d+)?)')


def _looks_misparsed(month_data, qtr_data):
    """同一期間「稅前 == 母利」＝ 幾乎確定是欄位對錯（稅前與稅後不可能相同）。

    單獨這條件會誤傷（全歷史 raw_archive 有 134 檔符合，多為正常公司），
    因此**只當作輔助條件**，必須再加上「該文本能被金控矩陣表解析」才成立——
    兩者同時符合的全歷史只有 1 檔（2026-08-14 5880 合庫金），無誤傷。
    """
    for x in (month_data or {}), (qtr_data or {}):
        if x.get("稅前") and x.get("稅前") == x.get("母利"):
            return True
    return False


def extract_holdco_matrix(text, co_name, announce_date=""):
    """金控矩陣表自結解析。成功回傳 (month_data, month_label, cum_data, cum_label, unit_str)，
    失敗回傳 None（交回原流程，維持既有行為）。"""
    if not co_name:
        return None
    # 僅在「表頭確實是金控自結損益表」時啟用，避免誤吃其他版型
    head = text.replace(" ", "")
    if not (("稅前" in head) and ("稅後" in head) and ("累計" in head or "累積" in head)):
        return None

    unit_str, unit_factor = detect_unit(text)
    if unit_factor is None:
        if "億元" in head:
            unit_str, unit_factor = "億元", 100
        else:
            return None   # 單位不明就不猜

    row = None
    for line in text.split('\n'):
        m = _HOLDCO_ROW.match(line)
        if not m:
            continue
        label = m.group(1)
        if co_name in label or label.startswith(co_name[:2]):
            row = m
            break
    if row is None:
        return None

    nums = row.group(2).split()
    if len(nums) < 5:
        return None
    try:
        vals = [float(n.replace(',', '')) for n in nums[:5]]
    except ValueError:
        return None

    def _mm(v):   # 億元 → 百萬
        c = v * unit_factor
        return f"{c:,.0f}" if abs(c) >= 100 else f"{c:,.2f}"

    month_data = {"稅前": _mm(vals[0]), "母利": _mm(vals[1])}
    cum_data   = {"稅前": _mm(vals[2]), "母利": _mm(vals[3]), "EPS": f"{vals[4]:.2f}"}

    # 期間標籤：優先抓「115年7月」；抓不到（第一金/合庫金無此字樣）則用
    # 公告日往前推一個月——月自結固定於次月公告，是可靠推法。
    yr = mon = None
    mo = re.search(r'(\d{3})\s*年\s*(\d{1,2})\s*月', text)
    if mo:
        yr, mon = int(mo.group(1)), int(mo.group(2))
    elif announce_date:
        ad = re.match(r'(\d{3})/(\d{1,2})/', announce_date)
        if ad:
            yr, mon = int(ad.group(1)), int(ad.group(2)) - 1
            if mon == 0:
                yr, mon = yr - 1, 12
    if mon:
        month_label = f"{yr}/{mon:02d}"
        cum_label   = f"累計1-{mon}月"
    else:
        month_label, cum_label = "", "累計"
    return month_data, month_label, cum_data, cum_label, unit_str


def format_compact_block(item, month_data, month_label, cum_data, cum_label,
                         unit_str=None, omit_keys=(), footnote=""):
    """精簡損益表專屬格式化：月 + 累計（正確標示「累計1-N月」而非「季」）"""
    dt_str   = _fmt_datetime(item.get("date", ""), item.get("time", ""))
    unit_tag = f" (原單位是{unit_str}，已換算成百萬)" if unit_str and unit_str not in UNIT_SILENT else ""
    parts = [f"{item['co_id']} {item['co_name']} {dt_str}{unit_tag}"]
    if month_data:
        # 金控月段沒有單月 EPS（EPS 只公告累計），空白行會被誤讀成漏抓 → 整行不顯示。
        # 僅在呼叫端有指定 omit_keys（＝金控路徑）時生效，既有 compact 呼叫不受影響。
        m_omit = tuple(omit_keys) + (("EPS",) if (omit_keys and not month_data.get("EPS")) else ())
        parts.append(_period_block(f"{_month_short(month_label)}自結", month_data, m_omit))
    if cum_data:
        parts.append(_period_block(f"{cum_label or '累計'}自結", cum_data, omit_keys))
    if footnote:
        parts.append(footnote)
    return "\n\n".join(parts)


# =========================
# 季報（合併財務報告）擷取
# =========================
# 季報專用指標（主要 + 備援分兩層，確保「歸屬母公司」優先於「本期淨利」）
REPORT_METRICS = [
    ("營業收入",             "營收"),
    ("營業毛利",             "毛利"),
    ("稅前淨利",             "稅前"),
    ("稅前純益",             "稅前"),
    ("稅前損益",             "稅前"),
    ("歸屬於母公司業主淨利",  "母利"),   # 最精確
    ("歸屬母公司業主淨利",   "母利"),
    ("業主淨利",             "母利"),
    ("歸屬母",               "母利"),
    ("基本每股盈餘",         "EPS"),
    ("每股盈餘",             "EPS"),
]
# 僅在上面的 母利 全都沒找到時才用「本期淨利」
REPORT_METRICS_FALLBACK = [
    ("本期淨利", "母利"),
    ("稅後淨利", "母利"),
    ("稅後純益", "母利"),
    # 少數公司（如 1718 中纖）欄名只寫「收入」而非「營業收入」→ 僅在
    # 第一輪抓不到營收時才用，避免誤抓「利息收入/其他收入」等雜項。
    ("收入", "營收"),
]


def extract_quarterly_report(text):
    """從季報（合併財務報告）擷取季度數字
    回傳：(qtr_data, qtr_label, unit_str)
      qtr_data：{label: value_str}（已換算為百萬元）
      qtr_label：如 "2026 Q1"
      unit_str：原始單位
    """
    # 期間標籤：優先從 "115年第一季" 格式取得
    _, qtr_label = extract_period_labels(text)

    # 備援 1：從 "114/01/01~114/12/31" 日期範圍推算期間
    if not qtr_label:
        # 容許 115/1/1-115/6/30 這類單位數月日與 - 分隔（3527 聚積 / 4995 晶達）
        m = re.search(r'\d{3}/\d{1,2}/\d{1,2}\s*[~～〜\-–－至]\s*(\d{3})/(\d{1,2})/\d{1,2}', text)
        if m:
            end_year  = int(m.group(1)) + 1911
            end_month = int(m.group(2))
            if end_month == 12:
                qtr_label = f"{end_year} 年報"   # 全年度
            else:
                q = (end_month - 1) // 3 + 1
                qtr_label = f"{end_year} Q{q}"

    # 備援 2：從主旨中的 "114年度" 推算年報
    if not qtr_label:
        a = re.search(r'(\d{3})年度', text)
        if a:
            qtr_label = f"{int(a.group(1)) + 1911} 年報"

    # 累計期間判別（第43款財報：數字全為「1月1日累計至本期止」）
    # → 標籤明示累計範圍，避免 Q2/Q3 被誤讀成單季（如 7853 政美應用）
    if "累計至本期止" in text:
        mr = re.search(r'\d{3}/\d{1,2}/\d{1,2}\s*[~～〜\-–－至]\s*(\d{3})/(\d{1,2})/\d{1,2}', text)
        if mr:
            y, em = int(mr.group(1)) + 1911, int(mr.group(2))
            if em == 6:
                qtr_label = f"{y} 上半年累計(1-6月)"
            elif em == 9:
                qtr_label = f"{y} 前三季累計(1-9月)"
            elif em == 12:
                qtr_label = f"{y} 年報"
            # em==3：Q1 累計即單季，標籤不變

    unit_str, unit_factor = detect_unit(text)
    factor = unit_factor if unit_factor is not None else 1

    qtr_found = {}
    lines = text.split('\n')

    # 第一輪：主要指標（歸屬母公司業主淨利 優先）
    for line in lines:
        s = line.strip()
        for keyword, label in REPORT_METRICS:
            if label in qtr_found:
                continue
            nums = _numbers_after_keyword(s, keyword)
            if nums:
                qtr_found[label] = _fmt_num(nums[0], factor, label)
                break

    # 第二輪備援：若 母利 仍未找到，改用「本期淨利 / 稅後淨利」
    if "母利" not in qtr_found or "營收" not in qtr_found:
        for line in lines:
            s = line.strip()
            for keyword, label in REPORT_METRICS_FALLBACK:
                if label in qtr_found:
                    continue
                nums = _numbers_after_keyword(s, keyword)
                if nums:
                    qtr_found[label] = _fmt_num(nums[0], factor, label)
                    break

    # 保護：單位偵測失敗且只抓到 ≤1 個指標 → 幾乎必為法說會新聞稿等純敘述文字，
    # 表格式掃描抓到的是句中數字，單位常差 100 倍（2026-08-06 3034 聯詠：
    # 「營業收入淨額為新台幣286億6仟萬元」被抓成 286 百萬，實際 Q2 營收
    #  28,660 百萬＝F22 月營收 4~6 月合計 9,225+9,412+10,023）→ 不回傳數字。
    if unit_str is None and len(qtr_found) <= 1:
        return {}, qtr_label, unit_str

    return qtr_found, qtr_label, unit_str


# =========================
# CSV 儲存
# =========================
def _read_csv_rows(path):
    """NUL byte容錯的CSV讀取（曾發生F22的monthly_revenue.csv混入NUL byte，
    讓csv.DictReader直接crash、拖累讀取同一份資料的其他腳本；這支也會讀
    F22/F27的CSV，一併防護，避免單一檔案損毀連環當掉多支腳本）。
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


def _atomic_write_csv(path, fieldnames, rows):
    """原子寫入：先寫暫存檔，寫完再rename換檔。避免寫到一半被中斷（程式被砍掉、
    磁碟暫時滿等）留下壞檔——曾發生monthly_revenue.csv中間出現一段NUL byte，
    讓整支腳本之後每次讀取都crash、當天全部公司都不會處理。rename是原子操作，
    要嘛完整成功、要嘛完全不動舊檔，不會有寫一半的中間狀態。"""
    import csv as _csv
    tmp_path = f"{path}.tmp-{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = _csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)   # 同一檔案系統上是原子操作


def save_to_csv(records):
    """將擷取結果寫入 v4_data.csv，以 (co_id, announce_date, announce_time) 去重。
    records: list of dict，每筆含 item + 月/季數字。
    回傳新寫入的筆數。
    """
    existing_keys = set()
    for row in _read_csv_rows(CSV_PATH):
        key = (row.get("co_id", ""), row.get("announce_date", ""), row.get("announce_time", ""))
        existing_keys.add(key)

    new_rows = []
    for rec in records:
        item       = rec["item"]
        month_data = rec.get("month_data", {})
        qtr_data   = rec.get("qtr_data", {})
        key = (item["co_id"], item.get("date", ""), item.get("time", ""))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        new_rows.append({
            "announce_date": item.get("date", ""),
            "announce_time": item.get("time", ""),
            "co_id":         item["co_id"],
            "co_name":       item["co_name"],
            "type":          rec.get("type", ""),
            "unit":          rec.get("unit_str", ""),
            "month_label":   rec.get("month_label", ""),
            "月_營收":        month_data.get("營收", ""),
            "月_稅前":        month_data.get("稅前", ""),
            "月_母利":        month_data.get("母利", ""),
            "月_EPS":         month_data.get("EPS", ""),
            "qtr_label":     rec.get("qtr_label", ""),
            "季_營收":        qtr_data.get("營收", ""),
            "季_稅前":        qtr_data.get("稅前", ""),
            "季_母利":        qtr_data.get("母利", ""),
            "季_EPS":         qtr_data.get("EPS", ""),
            "note":           rec.get("note", ""),
            "url":            rec.get("url", ""),
        })

    if not new_rows:
        return 0

    file_exists = os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    # 重新排序：最新在最上面
    all_rows = _read_csv_rows(CSV_PATH)
    # 補齊欄位：舊版 CSV 缺少新欄位時填空字串，並移除多餘的 None key
    normalized_rows = []
    for row in all_rows:
        clean = {col: row.get(col, "") for col in CSV_COLUMNS}
        normalized_rows.append(clean)
    normalized_rows.sort(key=lambda r: (r.get("announce_date", ""), r.get("announce_time", "")), reverse=True)
    _atomic_write_csv(CSV_PATH, CSV_COLUMNS, normalized_rows)

    return len(new_rows)


# =========================
# 已發送狀態（訊息去重，僅即時模式使用）
# =========================
SENT_STATE_PATH = os.path.join(_DIR, "sent_state.json")
RUN_LOG_PATH    = os.path.join(_DIR, "run.log")
SENT_KEEP_DAYS  = 4   # 狀態保留天數（涵蓋週末回溯）


def _runlog(msg):
    """執行紀錄（diagnostics）。寫入失敗不影響主流程。"""
    try:
        with open(RUN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _parse_announce_date(d):
    """公告日期字串 → datetime.date。
    支援民國（115/06/08、115/6/8）與西元（2026/06/08、2026-6-8）；解析失敗回 None。"""
    m = re.match(r"^\s*(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})", str(d))
    if not m:
        return None
    y, mo, dy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:
        y += 1911
    try:
        return _date(y, mo, dy)
    except ValueError:
        return None


def _sent_key(item):
    return f"{item['co_id']}|{item.get('date', '')}|{item.get('time', '')}"


def _content_key(item):
    """月營收內容鍵：同一公司＋同主旨（= 同月份營收），公司跨日重發同樣公告也只通知一次。"""
    if item.get("item_type") == "月營收":
        subj = re.sub(r"\s+", "", item.get("subject", ""))
        return f"C|{item['co_id']}|{subj}"
    return None


def _load_sent_state():
    """{key: 'YYYY-MM-DD HH:MM:SS'(發送時間)}"""
    try:
        with open(SENT_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sent_state(state):
    """回傳 True=寫入成功。失敗會記到 run.log 與 stderr。"""
    cutoff = (datetime.now() - timedelta(days=SENT_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    state = {k: v for k, v in state.items() if v >= cutoff}
    try:
        with open(SENT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=0)
        return True
    except Exception as e:
        sys.stderr.write(f"⚠️  sent_state.json 寫入失敗：{e}\n")
        _runlog(f"sent_state SAVE FAILED: {e}")
        return False


# =========================
# Google Drive 上傳
# =========================
def upload_to_gdrive():
    """將 important_events_v4.csv 上傳（覆蓋）到指定 Google Drive 資料夾
    回傳：上傳成功時回傳 Google Drive 檔案連結，失敗時回傳 None
    """
    if not os.path.exists(CSV_PATH):
        return None

    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.stderr.write(
            "⚠️  Google Drive 套件未安裝，跳過上傳。\n"
            "請執行：pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib\n"
        )
        return None

    if not os.path.exists(CREDS_PATH):
        sys.stderr.write(f"⚠️  找不到 {CREDS_PATH}，跳過 Google Drive 上傳。\n")
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
            with open(TOKEN_PATH, "w") as token:
                token.write(creds.to_json())

        service = build("drive", "v3", credentials=creds)

        existing = service.files().list(
            q=f"name='important_events_v4.csv' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name)",
        ).execute()
        files = existing.get("files", [])

        media = MediaFileUpload(CSV_PATH, mimetype="text/csv", resumable=False)

        if files:
            file_id = files[0]["id"]
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            metadata = {"name": "important_events_v4.csv", "parents": [GDRIVE_FOLDER_ID]}
            result  = service.files().create(body=metadata, media_body=media, fields="id").execute()
            file_id = result.get("id", "")

        return f"https://drive.google.com/file/d/{file_id}/view" if file_id else None

    except Exception as e:
        sys.stderr.write(f"⚠️  Google Drive 上傳失敗：{e}\n")
        return None


# =========================
# 格式化
# =========================
SEPARATOR = "————————"

# 對外顯示名稱（key = 內部 label）
DISPLAY_NAMES = {
    "營收": "營收",
    "毛利": "毛利",
    "稅前": "稅前淨利",
    "母利": "母公司淨利",
    "EPS":  "EPS",
}


def _roc_to_western(date_str):
    """'115/04/22' → '2026/4/22'"""
    parts = date_str.split('/')
    if len(parts) == 3:
        return f"{int(parts[0]) + 1911}/{int(parts[1])}/{int(parts[2])}"
    return date_str


def _fmt_datetime(date_str, time_str):
    """'115/04/22', '17:00:29' → '4/22 5:00 PM'（不含年，12小時制，不含秒）"""
    parts = date_str.split('/')
    md = f"{int(parts[1])}/{int(parts[2])}" if len(parts) == 3 else date_str
    try:
        h, m, _ = time_str.split(':')
        h, m = int(h), int(m)
        period = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{md} {h12}:{m:02d} {period}"
    except Exception:
        return f"{md} {time_str}"


def _month_short(month_label):
    """'115/03' → '3月'"""
    if not month_label:
        return "月"
    m = re.search(r'/(\d{1,2})$', month_label)
    return f"{int(m.group(1))}月" if m else "月"


def _display_val(val_str, label):
    """統一顯示格式：
    - EPS：固定兩位小數
    - 其餘：abs >= 10 → 整數；abs < 10 → 一位小數
    """
    if not val_str:
        return val_str
    try:
        num = float(val_str.replace(',', ''))
    except ValueError:
        return val_str

    if label == "EPS":
        return f"{num:.2f}"

    return f"{num:,.0f}" if abs(num) >= 10 else f"{num:.1f}"


def _period_block(title, data, omit_keys=()):
    """一個期間的區塊：標題行 + 四個指標（有值填入，無值留空）

    omit_keys：整行不顯示的指標。用於金控／壽險——這類公司**本來就沒有「營收」
    這個科目**（獲利看利息淨收益、保費、投資收益，不是營業收入），印「營收: 」
    空白行會讓人誤以為是漏抓。預設空 tuple，既有呼叫端行為完全不變。
    """
    lines = [title]
    # 毛利僅在有解析到時顯示（目前只有季報會有），插在營收之後
    keys = (METRICS_ORDER[:1] + ["毛利"] + METRICS_ORDER[1:]) \
        if (data or {}).get("毛利") else METRICS_ORDER
    keys = [k for k in keys if k not in omit_keys]
    for key in keys:
        val = (data or {}).get(key, "")
        if val:
            val = _display_val(val, key)
        lines.append(f"{DISPLAY_NAMES[key]}: {val}")
    return "\n".join(lines)


def format_company_block(item, month_data, month_label, qtr_data, qtr_label, unit_str=None):
    """格式化一家公司的完整區塊

    格式：
      5297 廣化 2026/4/22 15:41:14

      3月自結
      營收: ...
      ...

      季自結
      營收: ...
      ...
    """
    dt_str   = _fmt_datetime(item.get("date", ""), item.get("time", ""))
    unit_tag = f" (原單位是{unit_str}，已換算成百萬)" if unit_str and unit_str not in UNIT_SILENT else ""
    co_header = f"{item['co_id']} {item['co_name']} {dt_str}{unit_tag}"

    parts = [co_header]

    if month_data:
        month_title = f"{_month_short(month_label)}自結"
        parts.append(_period_block(month_title, month_data))

    if qtr_data:
        qtr_title = f"季自結 ({qtr_label})" if qtr_label and not month_data else "季自結"
        parts.append(_period_block(qtr_title, qtr_data))

    warns = _sanity_warns(month_data) + _sanity_warns(qtr_data)
    if warns:
        parts.append(f"⚠️ 自檢異常: {'、'.join(sorted(set(warns)))}（請人工核對原文）")

    return "\n\n".join(parts)


RAW_ARCHIVE_DIR = os.path.join(_DIR, "raw_archive")

def _archive_item(item, text, parsed, output, flags):
    """公告原文＋解析結果＋輸出 存成 JSON（語料庫：供回歸測試與 LLM 待查核對）。
    存檔失敗只記 log，不影響主流程。"""
    try:
        import json as _json
        parts = item.get("date", "").split("/")
        day = f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}" if len(parts) == 3 else "unknown"
        sub = os.path.join(RAW_ARCHIVE_DIR, day)
        os.makedirs(sub, exist_ok=True)
        fname = f"{item.get('co_id', '')}_{item.get('time', '').replace(':', '')}.json"
        with open(os.path.join(sub, fname), "w", encoding="utf-8") as f:
            _json.dump({
                # 2026-08-05：meta 增存 subject（公告主旨），供校驗時追查分類根因
                "meta": {k: item.get(k, "") for k in ("co_id", "co_name", "date", "time", "subject")},
                "raw_text": text,
                "parsed": parsed,
                "output": output,
                "flags": flags,
            }, f, ensure_ascii=False, indent=1)
    except Exception as e:
        _runlog(f"raw_archive FAILED {item.get('co_id')}: {e}")


def _sanity_warns(data):
    """解析結果數學自檢（攔截抓錯欄位/單位）。回傳警示 list。"""
    if not data:
        return []
    g = lambda k: _num(data.get(k))
    rev, gp, pre, ni, eps = g("營收"), g("毛利"), g("稅前"), g("母利"), g("EPS")
    w = []
    if rev is not None and gp is not None and gp > rev:
        w.append("毛利>營收")
    if rev is not None and rev < 0:
        w.append("營收為負")
    if eps is not None and abs(eps) > 100:
        w.append("EPS 絕對值>100")
    if ni is not None and pre is not None and abs(ni) > abs(pre) * 2 + 1:
        w.append("母淨與稅前差異過大")
    return w


# =========================
# Reviewer：資料完整性檢核
# 對「有成功解析」的公告，檢查四大指標是否有缺；有缺時判別原因：
#   - 公告原文含該指標關鍵字但沒抓到值 → 疑解析失敗（附原始連結供人工比對）
#   - 原文根本沒提 → 公開資訊未提供（正常，很多自結只公布稅前/EPS）
# =========================
_REVIEW_KEYWORDS = {
    "營收": ["營業收入", "淨營收", "營收"],
    "稅前": ["稅前淨利", "稅前純益", "稅前淨損", "稅前損益"],
    "母利": ["歸屬予母", "歸屬母", "業主淨利", "業主淨損", "歸屬本公司", "本期淨利",
             "稅後純益", "稅後淨利", "稅後損失", "稅後損益"],
    "EPS":  ["每股盈餘", "每股虧損", "稅後每股盈餘"],
}


def review_missing(text, period_dicts):
    """period_dicts = [(期間名, data_dict), ...]（只放有顯示的期間）。
    回傳檢核字串（無缺回空字串）。"""
    norm = re.sub(r'[（(][^)）]*[)）]', '', text or "")
    notes = []
    for pname, data in period_dicts:
        missing = [k for k in METRICS_ORDER if not (data or {}).get(k)]
        for k in missing:
            if any(kw in norm for kw in _REVIEW_KEYWORDS[k]):
                notes.append(f"{pname}{DISPLAY_NAMES[k]}⚠️疑解析失敗")
            else:
                notes.append(f"{pname}{DISPLAY_NAMES[k]}＝公告未提供")
    return "、".join(notes)


# =========================
# 對照資訊：公告官方增減% ＋ 自家資料庫去年同期
# 原則：公告（法定資訊）能解析就列；資料庫數字一律附上，供交叉驗證。
# 資料來源：mops_f22/monthly_revenue.csv（月營收）、mops_f27/quarterly_revenue.csv（季稅前/母利/EPS）
# =========================
_F22_REV  = None   # {(sid, y, m): 營收百萬}
_F22_DERIVED = set()  # 其中屬「累計欄反推補值」的 key（顯示時標 ‡）
_F27_QTR  = None   # {(sid, y, q): {"稅前","母利","EPS"}}
_JISHI_M  = None   # {(sid, y, m): {"稅前","母利","EPS"}} 歷史「月自結」（重訊 CSV），供去年同月獲利比較


def _load_ref_dbs():
    global _F22_REV, _F27_QTR, _JISHI_M
    if _F22_REV is not None:
        return
    _F22_REV, _F27_QTR, _JISHI_M = {}, {}, {}
    p22 = os.path.join(_PARENT, "mops_f22", "monthly_revenue.csv")
    p27 = os.path.join(_PARENT, "mops_f27", "quarterly_revenue.csv")
    # 用 _read_csv_rows（NUL byte容錯）而非直接 csv.DictReader：
    # 任一份CSV壞掉（曾發生過）不會讓這支腳本連環當機。
    for r in _read_csv_rows(p22):
        try:
            _k = (r["stock_id"], int(r["report_year"]), int(r["report_month"]))
            _F22_REV[_k] = int(r["revenue"].replace(",", "")) / 1000.0
            # data_source="derived_ytd" 者為 derive_missing_f22.py 由相鄰月累計欄反推補上的
            # 缺漏月份（非MOPS原始公告）。記下來，訊息顯示時要標 ‡ 讓人知道來源。
            if r.get("data_source") == "derived_ytd":
                _F22_DERIVED.add(_k)
        except (ValueError, KeyError):
            pass
    for r in _read_csv_rows(p27):
        def _n(k, div=1000.0):
            try:
                return float(r[k].replace(",", "")) / div
            except (ValueError, KeyError, AttributeError):
                return None
        try:
            _F27_QTR[(r["stock_id"], int(r["report_year"]), int(r["report_quarter"]))] = {
                "稅前": _n("pretax_income"), "母利": _n("net_income_parent"),
                "EPS":  _n("eps", div=1.0), "src": "財報",
            }
        except (ValueError, KeyError):
            pass
    # 自結快數補位：important_events_v4.csv 的季自結（已是百萬）。
    # 原則「財報為準」：只補財報還沒有的季度，財報一進 quarterly_revenue.csv 就自動蓋過。
    # 同時建 _JISHI_M：歷史月自結獲利（去年同月比較用）。
    if True:
        for r in _read_csv_rows(CSV_PATH):
                if r.get("type") != "自結":
                    continue
                # 月自結：month_label 如 "114/06"（ROC 年，防呆限制 100~130）
                mm_ = re.match(r'(1[0-3]\d)/(\d{1,2})$', (r.get("month_label") or "").strip())
                if mm_:
                    def _mv(col):
                        s = (r.get(col) or "").replace(",", "").strip()
                        try:
                            return float(s)
                        except ValueError:
                            return None
                    dmy = {"稅前": _mv("月_稅前"), "母利": _mv("月_母利"), "EPS": _mv("月_EPS")}
                    if any(v is not None for v in dmy.values()):
                        _JISHI_M[(r["co_id"], int(mm_.group(1)) + 1911, int(mm_.group(2)))] = dmy
                m = re.match(r'(\d{4})\s*Q(\d)', r.get("qtr_label") or "")
                if not m:
                    continue
                key = (r["co_id"], int(m.group(1)), int(m.group(2)))
                if key in _F27_QTR and _F27_QTR[key].get("src") == "財報":
                    continue
                def _v(col, ok_float=True):
                    s = (r.get(col) or "").replace(",", "").strip()
                    try:
                        return float(s)
                    except ValueError:
                        return None
                d = {"稅前": _v("季_稅前"), "母利": _v("季_母利"), "EPS": _v("季_EPS"),
                     "src": "自結"}
                if any(v is not None for k, v in d.items() if k != "src"):
                    _F27_QTR[key] = d   # 同季多筆自結：後面的（較新公告）覆蓋前面


def _fmt_m_ref(v):
    return f"{v:,.0f}" if abs(v) >= 10 else f"{v:.1f}"


def _official_row_nums(text, keywords):
    """回傳關鍵字行上的非% 數字序列（格式D：[本月, 去年同月, 單季, 去年同季, 四季]）"""
    for line in (text or "").split("\n"):
        s = re.sub(r'[（(][^)）]*[)）]', '', line)
        for kw in keywords:
            if kw in s:
                nums = _numbers_after_keyword(line, kw)
                if nums:
                    return nums
    return []


def _official_pcts(text, keywords):
    """從公告原文中，找含關鍵字的行上的 % 數字（官方增減%）。回傳 list[str]。"""
    out = []
    for line in (text or "").split("\n"):
        s = re.sub(r'[（(][^)）]*[)）]', '', line)
        if any(kw in s for kw in keywords):
            for m in re.finditer(r'([-+]?[\d,]+(?:\.\d+)?)\s*[%％]', line):
                out.append(m.group(1).replace(",", ""))
    return out


def build_comparison(item, month_data, month_label, qtr_data, qtr_label, text):
    """組「官方增減% + 資料庫去年同期」對照行。回傳 list[str]（可能為空）。"""
    _load_ref_dbs()
    sid = item["co_id"]
    lines = []

    # ── 官方增減%（能解析就列，原樣呈現不加解讀）──
    for mkey, kws in (("營收", _REVIEW_KEYWORDS["營收"]), ("稅前", _REVIEW_KEYWORDS["稅前"]),
                      ("母利", _REVIEW_KEYWORDS["母利"])):
        if (month_data or {}).get(mkey) or (qtr_data or {}).get(mkey):
            pcts = _official_pcts(text, kws)
            if pcts:
                lines.append(f"官方增減% {DISPLAY_NAMES[mkey]}: {' / '.join(pcts[:4])}%")

    # ── 資料庫對照 ──
    # 月：F22 該月 vs 去年同月
    m_ym = None
    if month_data and month_label:
        m = re.search(r'(\d{2,3})/(\d{1,2})$', month_label)
        if m:
            m_ym = (int(m.group(1)) + 1911, int(m.group(2)))
    if m_ym:
        y, mo = m_ym
        now, last = _F22_REV.get((sid, y, mo)), _F22_REV.get((sid, y - 1, mo))
        if now is not None or last is not None:
            seg = [f"DB {mo}月營收 {_fmt_m_ref(now) if now is not None else '無'}"]
            if last is not None:
                pct = f"（去年 {_fmt_m_ref(last)}" + (f"，YoY {(now/last-1)*100:+.1f}%）" if now and last else "）")
                seg.append(pct)
            lines.append("".join(seg))
            # 公告 vs DB 差異檢核（>10% 標警示，常見原因：期間標籤誤判/單位錯）
            try:
                ann = float((month_data or {}).get("營收", "").replace(",", ""))
                if now and abs(ann - now) / now > 0.10:
                    lines.append(f"⚠️ 公告月營收 {_fmt_m_ref(ann)} 與 DB {_fmt_m_ref(now)} 差異大，請確認期間/單位")
            except (ValueError, AttributeError):
                pass

    # 季：F22 加總三個月營收 + F27 去年同季獲利
    q_yq = None
    if qtr_data and qtr_label:
        m = re.search(r'(\d{4})\s*Q(\d)', qtr_label)
        if m:
            q_yq = (int(m.group(1)), int(m.group(2)))
    if q_yq:
        y, q = q_yq
        mons = range(3 * q - 2, 3 * q + 1)
        def _qsum(yy):
            vals = [_F22_REV.get((sid, yy, mm)) for mm in mons]
            return sum(vals) if all(v is not None for v in vals) else None
        qnow, qlast = _qsum(y), _qsum(y - 1)
        if qnow is not None:
            seg = f"DB Q{q}營收 {_fmt_m_ref(qnow)}"
            if qlast is not None:
                seg += f"（去年 {_fmt_m_ref(qlast)}，YoY {(qnow/qlast-1)*100:+.1f}%）"
            lines.append(seg)
        f27 = _F27_QTR.get((sid, y - 1, q))
        if f27:
            parts = [f"{DISPLAY_NAMES[k]} {_fmt_m_ref(v) if k != 'EPS' else f'{v:.2f}'}"
                     for k, v in f27.items() if k != "src" and v is not None]
            if parts:
                lines.append(f"DB 去年Q{q}（{f27.get('src', '財報')}）: " + "、".join(parts))
    return lines


# =========================
# 精簡版公司區塊（v2）：公告值｜DB對照 同行排列
#   - 缺值顯示「—」；原文有提但沒解析到顯示「—⚠️」並附原始連結
#   - DB 來源：月/季營收=F22 官方月營收；去年季獲利=F27 財報（缺時用自結快數，標「自結」）
# =========================
REVIEW_QUEUE = os.path.join(_DIR, "db_review_queue.csv")

# 本輪需要人工確認的事項（run() 開頭清空；統一彙整成一則訊息，不散落在各股訊息中）
_ATTENTION = []   # [(co_id, co_name, 說明, link或""), ...]


def _log_review_queue(sid, name, announce, field, ann_val, db_val):
    """公告 vs DB 對不上且無法自動判別 → 記入待查檔，人工確認後修 DB。"""
    import csv as _csv
    new = not os.path.exists(REVIEW_QUEUE)
    with open(REVIEW_QUEUE, "a", encoding="utf-8", newline="") as f:
        w = _csv.writer(f)
        if new:
            w.writerow(["logged_at", "co_id", "co_name", "announce_date", "field",
                        "announced", "db_value", "resolved"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), sid, name, announce,
                    field, ann_val, db_val, ""])


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _est_yoy(ann, last):
    """公告本期 vs DB 去年 → 推估 YoY 字串；去年為負/零時給轉盈虧字樣。"""
    if ann is None or last is None:
        return ""
    if last > 0:
        return f"{(ann/last-1)*100:+.1f}%"
    if last <= 0 and ann > 0:
        return "轉盈"
    if last < 0 and ann < 0:
        return ""
    return "轉虧"


# 速報排版：短標籤（等寬字體下 EPS+空格 = 兩個全形字寬）
_SHORT = {"營收": "營收", "稅前": "稅前", "母利": "母淨", "EPS": "EPS "}
_DIGEST_WARN = []   # 速報置頂警示 [(警示文字, groups), ...]；run() 開頭清空


def format_company_block_v2(item, month_data, month_label, qtr_data, qtr_label, unit_str, text):
    """速報格式：每行「標籤 本期值｜去年值 YoY記號」，* = 官方公告 YoY、† = DB 推估。
    數字右對齊（配合 n8n <pre> 等寬字體）。與 DB 不符 → 置頂警示 + 待查檔。"""
    _load_ref_dbs()
    sid  = item["co_id"]
    grps = item.get("_groups", [])
    norm = re.sub(r'[（(][^)）]*[)）]', '', text or "")
    fail_fields = []

    def _val(k, data):
        """回傳（右對齊字串, 數值 or None）"""
        raw = (data or {}).get(k, "")
        if raw:
            return f"{_display_val(raw, k):>8}", _num(raw)
        if any(kw in norm for kw in _REVIEW_KEYWORDS.get(k, [])):
            if DISPLAY_NAMES[k] not in fail_fields:
                fail_fields.append(DISPLAY_NAMES[k])
            return f"{'—⚠️':>7}", None    # 解析失敗（詳見人工確認清單）
        return f"{'—':>8}", None           # 公告未提供

    official = {}
    for k in ("營收", "稅前", "母利", "EPS"):
        pcts = _official_pcts(text, _REVIEW_KEYWORDS[k])
        if pcts:
            official[k] = [p + "%" for p in pcts[:2]]

    def _off(k, period):
        v = official.get(k)
        if not v:
            return ""
        return (v[0] if period == "m" else v[1]) if len(v) >= 2 else v[0]

    def _pct(k, period, ann, last):
        """官方% 優先（*），否則 DB 推估（†）"""
        p = _off(k, period)
        if p:
            return f"{p}*"
        est = _est_yoy(ann, last)
        return f"{est}†" if est else ""

    def _line(k, vstr, last=None, is_eps=False, pct="", last_derived=False):
        """last_derived=True → 去年值後加 ‡，代表該月是由累計欄反推補上的（非原始公告）。"""
        s = f"{_SHORT[k]}{vstr}"
        if last is not None:
            ls = f"{last:.2f}" if is_eps else _fmt_m_ref(last)
            s += f"｜去年{ls:>7}{'‡' if last_derived else ' '}  {pct}".rstrip()
        elif pct:
            s += f"｜{pct}"
        return s

    # 季營收加總（單季/累計判別也要用）
    mq = re.search(r'(\d{4})\s*Q(\d)', qtr_label or "")
    qnow = qlast = None
    if mq:
        _y, _q = int(mq.group(1)), int(mq.group(2))
        def _qsum(yy):
            vals = [_F22_REV.get((sid, yy, mm)) for mm in range(3 * _q - 2, 3 * _q + 1)]
            return sum(vals) if all(v is not None for v in vals) else None
        qnow, qlast = _qsum(_y), _qsum(_y - 1)

    lines = [f"{sid} {item['co_name']}"]

    # 格式D（公告自帶去年同月/去年同季欄）：一律以公告值為準
    has_d = _has_two_compare_cols(text)
    # 格式E（會計項目表格）欄序 = [本單季, 本累計, 去年單季, 去年累計]
    #   → 去年單季在第 2 欄（格式D是第 3 欄）；且視同「公告自帶去年欄」
    is_e = bool("會計項目" in text
                and re.search(r'\d{4}年\s*\d{1,2}\s*~\s*\d{1,2}\s*月', text))
    if is_e:
        has_d = True
    q_last_idx = 2 if is_e else 3
    _fac_d = (detect_unit(text)[1] or 1) if has_d else 1

    def _d_last(k, idx):
        """取公告行上第 idx 欄（1=去年同月, 3=去年同季），換算成百萬。無則 None。"""
        if not has_d:
            return None
        nums = _official_row_nums(text, _REVIEW_KEYWORDS[k])
        if len(nums) > idx:
            v = _num(nums[idx])
            if v is not None and k != "EPS":
                v *= _fac_d
            return v
        return None

    # ── 月段 ──
    if month_data:
        m  = re.search(r'(\d{2,3})/(\d{1,2})$', month_label or "")
        ym = (int(m.group(1)) + 1911, int(m.group(2))) if m else None
        title = f"▍{_month_short(month_label)}"
        now = last = None
        last_derived = False
        if ym:
            now  = _F22_REV.get((sid, *ym))
            last = _d_last("營收", 1)
            if last is None:
                last = _F22_REV.get((sid, ym[0] - 1, ym[1]))
                # 公告本身沒附去年同月欄，改用DB值時，若該月是反推補值要標註
                last_derived = (sid, ym[0] - 1, ym[1]) in _F22_DERIVED
        vstr, ann = _val("營收", month_data)
        if ann is not None and now and abs(ann - now) / now > 0.10:
            if qnow and abs(ann - qnow) / qnow < 0.02:
                title = "▍單季（DB核對：此為單季數，非單月）"
                last  = qlast
            else:
                _log_review_queue(sid, item["co_name"], item.get("date", ""),
                                  f"{ym[1]}月營收", ann, now)
                _DIGEST_WARN.append((f"{sid} {ym[1]}月營收 {_fmt_m_ref(ann)} 與DB {_fmt_m_ref(now)} 不符，已記待查檔", grps, mops_link(sid)))
                vstr += "⚠️"
        lines.append(title)
        lines.append(_line("營收", vstr, last, pct=_pct("營收", "m", ann, last),
                           last_derived=last_derived))
        # 去年同月獲利：公告有「去年同月」欄（格式D）用官方值(*)；否則查 DB 歷史月自結(†)
        jm_last = _JISHI_M.get((sid, ym[0] - 1, ym[1])) if ym else None
        for k in ("稅前", "母利", "EPS"):
            vstr, ann_k = _val(k, month_data)
            lastv = _d_last(k, 1)
            if lastv is None and jm_last:
                lastv = jm_last.get(k)
            pct = _pct(k, "m", ann_k, lastv) if lastv is not None else (f"{_off(k,'m')}*" if _off(k, "m") else "")
            lines.append(_line(k, vstr, lastv, is_eps=(k == "EPS"), pct=pct))

    # ── 季段 ──
    if qtr_data:
        # 無季別標籤者（「當月數／累計數」對照表，如 3042 晶技 115/07 自結）：
        # 第二欄其實是「年初至今累計」（該例已用 F22 官方月營收加總驗證：1-7月=8,404百萬，
        # 與公告累計數 8,403,593 仟元完全吻合），不是單季。
        # 舊版一律標「季單季」會讓人誤判成單一季度數字，這裡依月份標籤還原實際累計期間。
        ytd_ym = None
        if not qtr_label:
            _mm = re.search(r'(\d{2,3})/(\d{1,2})$', month_label or "")
            if _mm:
                ytd_ym = (int(_mm.group(1)) + 1911, int(_mm.group(2)))

        if ytd_ym:
            _yy, _mn = ytd_ym

            def _ytd_sum(yy):
                vals = [_F22_REV.get((sid, yy, mm)) for mm in range(1, _mn + 1)]
                return sum(vals) if all(v is not None for v in vals) else None

            ytd_now, ytd_last = _ytd_sum(_yy), _ytd_sum(_yy - 1)
            title = f"▍1-{_mn}月累計"
            vstr, ann_rev = _val("營收", qtr_data)
            if ann_rev is not None and ytd_now and abs(ann_rev - ytd_now) / ytd_now < 0.02:
                title += "（DB核對）"
            base_last = ytd_last
        else:
            qtag = qtr_label.replace("20", "", 1).replace(" ", "") if qtr_label else "季"
            title = f"▍{qtag}單季"
            vstr, ann_rev = _val("營收", qtr_data)
            base_last = _d_last("營收", q_last_idx) or qlast
        if ann_rev is not None and qnow and abs(ann_rev - qnow) / qnow > 0.10:
            _y2, _q2 = int(mq.group(1)), int(mq.group(2))
            _yvals = [_F22_REV.get((sid, _y2, mm)) for mm in range(1, 3 * _q2 + 1)]
            ysum  = sum(v for v in _yvals if v is not None) if any(v is not None for v in _yvals) else None
            if ysum and abs(ann_rev - ysum) / ysum < 0.02:
                title = f"▍1-{3*_q2}月累計（DB核對）"
                _lyv  = [_F22_REV.get((sid, _y2 - 1, mm)) for mm in range(1, 3 * _q2 + 1)]
                base_last = sum(v for v in _lyv if v is not None) if all(v is not None for v in _lyv) else None
            else:
                _log_review_queue(sid, item["co_name"], item.get("date", ""),
                                  f"{qtr_label}營收", ann_rev, qnow)
                _DIGEST_WARN.append((f"{sid} {qtag}營收 {_fmt_m_ref(ann_rev)} 與DB {_fmt_m_ref(qnow)} 不符，已記待查檔", grps, mops_link(sid)))
                vstr += "⚠️"
        # 公告未提供營收但 DB 有 → 以 DB 月營收加總補位（標 (DB)）
        if ann_rev is None and qnow is not None and "⚠️" not in vstr:
            vstr, ann_rev = f"{_fmt_m_ref(qnow):>5}(DB)", qnow
        lines.append(title)
        lines.append(_line("營收", vstr, base_last, pct=_pct("營收", "q", ann_rev, base_last)))
        last27 = _F27_QTR.get((sid, int(mq.group(1)) - 1, int(mq.group(2)))) if mq else None
        for k in ("稅前", "母利", "EPS"):
            vstr, ann_k = _val(k, qtr_data)
            lv = _d_last(k, q_last_idx)
            if lv is None:
                lv = last27.get(k) if last27 else None
            pct = _pct(k, "q", ann_k, lv) if lv is not None else (f"{_off(k,'q')}*" if _off(k, "q") else "")
            lines.append(_line(k, vstr, lv, is_eps=(k == "EPS"), pct=pct))
        if last27 and last27.get("src") == "自結":
            lines.append("（去年值為自結快數）")

    if fail_fields:
        _ATTENTION.append((sid, item["co_name"],
                           "、".join(fail_fields) + " 解析失敗", mops_link(sid)))
    return "\n".join(lines)


# =========================
# 月營收（敘述體重大訊息）擷取
# =========================
_REV_UNIT_FACTOR = {
    "億元": 100, "億": 100,
    "仟元": 1 / 1000, "千元": 1 / 1000,
    "百萬元": 1, "百萬": 1, "佰萬元": 1, "佰萬": 1,
}


def extract_monthly_revenue(text):
    """從『自結合併營收 / 營業收入』敘述體重大訊息擷取月營收摘要。
    回傳 dict（值換算為百萬元）或 None。
      {month_label, cur, cur_unit, mom, yoy, ytd, ytd_yoy}
    """
    if not text:
        return None

    # 報告月份：「115年05月」或「5 月份」
    month_label = ""
    m = re.search(r'\d{3}\s*年\s*(\d{1,2})\s*月', text)
    if not m:
        m = re.search(r'(\d{1,2})\s*月份', text)
    if m:
        month_label = f"{int(m.group(1))}月"

    def to_million(num_str, unit):
        try:
            v = float(num_str.replace(",", ""))
        except ValueError:
            return None
        f = _REV_UNIT_FACTOR.get(unit)
        return v * f if f is not None else None

    # 營收數字：（淨營收 / 營業收入 / 營收）後面接 數字 + 單位
    rev_pat = re.compile(
        r'(?:淨營收|營業收入|營收)[^0-9%]{0,8}?(?:新台幣|NT\$|NTD)?\s*'
        r'([\d,]+(?:\.\d+)?)\s*(億元|億|仟元|千元|百萬元|百萬|佰萬元|佰萬)'
    )

    cum_m   = re.search(r'累計', text)
    cum_pos = cum_m.start() if cum_m else len(text)

    cur = cur_unit = ytd = None
    for mm in rev_pat.finditer(text):
        val = to_million(mm.group(1), mm.group(2))
        if val is None:
            continue
        if mm.start() < cum_pos:
            if cur is None:      # 累計之前第一個 = 本月（去年同期會排在其後，忽略）
                cur, cur_unit = val, mm.group(2)
        else:
            if ytd is None:      # 累計之後第一個 = 本年累計
                ytd = val

    if cur is None and ytd is None:
        return None

    def _pct_near(segment):
        """在 segment 內找第一個『較…(增/減/成長/衰退) X%』的數字（含負號判斷）。"""
        pm = re.search(r'(增加|成長|減少|衰退|下滑)[^%]{0,6}?([\d.]+)\s*[%％]', segment)
        if not pm:
            pm = re.search(r'([\d.]+)\s*[%％]', segment)
            if not pm:
                return None
            num = float(pm.group(1))
        else:
            num = float(pm.group(2))
            if pm.group(1) in ("減少", "衰退", "下滑"):
                num = -num
        return num

    # 單月 MoM / YoY：取累計之前段落
    head = text[:cum_pos]
    mom_seg = re.search(r'較上月[^。]*', head)
    yoy_seg = re.search(r'(較去年同期|較上年同期)[^。]*', head)
    mom = _pct_near(mom_seg.group(0)) if mom_seg else None
    yoy = _pct_near(yoy_seg.group(0)) if yoy_seg else None

    # 累計 YoY：取累計之後段落
    tail = text[cum_pos:]
    ytd_yoy_seg = re.search(r'(較去年同期|較上年同期)[^。]*', tail)
    ytd_yoy = _pct_near(ytd_yoy_seg.group(0)) if ytd_yoy_seg else None

    return {
        "month_label": month_label,
        "cur": cur, "cur_unit": cur_unit,
        "mom": mom, "yoy": yoy,
        "ytd": ytd, "ytd_yoy": ytd_yoy,
    }


def _fmt_rev_m(val):
    """百萬元顯示：abs>=10 整數帶千分位；否則一位小數。"""
    if val is None:
        return "-"
    return f"{val:,.0f}" if abs(val) >= 10 else f"{val:.1f}"


def _fmt_signed_pct(val):
    if val is None:
        return None
    return f"{'+' if val >= 0 else ''}{val:.1f}%"


def format_revenue_line(item, rev):
    """精簡一行：2327 國巨* 5月營收 15,058 (MoM +7.3% / YoY +47.5%) ｜累計 67,263 (YoY +27.4%)"""
    mlabel = rev.get("month_label") or "月"
    parts = [f"{item['co_id']} {item['co_name']} {mlabel}營收 {_fmt_rev_m(rev.get('cur'))}"]

    chg = []
    if rev.get("mom") is not None:
        chg.append(f"MoM {_fmt_signed_pct(rev['mom'])}")
    if rev.get("yoy") is not None:
        chg.append(f"YoY {_fmt_signed_pct(rev['yoy'])}")
    if chg:
        parts.append(f"({' / '.join(chg)})")

    if rev.get("ytd") is not None:
        ytd_str = f"｜累計 {_fmt_rev_m(rev['ytd'])}"
        if rev.get("ytd_yoy") is not None:
            ytd_str += f" (YoY {_fmt_signed_pct(rev['ytd_yoy'])})"
        parts.append(ytd_str)

    if item.get("date"):
        parts.append(f"｜{_fmt_datetime(item['date'], item.get('time', ''))}公告")

    return " ".join(parts)


def format_report_block(item, qtr_data, qtr_label, unit_str=None, check_f27_lead=False):
    """格式化一家公司的季報區塊（僅季度，無月份欄）。
    check_f27_lead=True（僅M31/季報董事會通過區塊使用）：
    M31（董事會決議通過財務報告）跟F27（mops_f27官方季報API）本質是同一個財報事件，
    只是資料來源不同，M31常會早於F27出現。若F27尚未有這季的官方數字，標註「領先指標」提醒。"""
    dt_str    = _fmt_datetime(item.get("date", ""), item.get("time", ""))
    unit_tag  = f" (原單位是{unit_str}，已換算成百萬)" if unit_str and unit_str not in UNIT_SILENT else ""
    co_header = f"{item['co_id']} {item['co_name']} {dt_str}{unit_tag}"
    qtr_title = qtr_label or "季報"
    qtr_block = _period_block(qtr_title, qtr_data)
    warns = _sanity_warns(qtr_data)
    tail = f"\n\n⚠️ 自檢異常: {'、'.join(warns)}（請人工核對原文）" if warns else ""

    lead_note = ""
    if check_f27_lead:
        m = re.match(r'(\d{4})\s*Q(\d)', qtr_label or "")
        if m:
            _load_ref_dbs()
            key = (item["co_id"], int(m.group(1)), int(m.group(2)))
            f27_rec = _F27_QTR.get(key)
            if not f27_rec or f27_rec.get("src") != "財報":
                lead_note = "\n\n⚡ 領先指標：F27尚未出現此季官方數字，M31先到"

    return f"{co_header}\n\n{qtr_block}{tail}{lead_note}"


# =========================
# 主流程
# =========================
def run():
    del _ATTENTION[:]     # 每輪重置人工確認清單
    del _DIGEST_WARN[:]   # 每輪重置速報置頂警示
    _load_watchlist()
    dt = parse_date_arg()
    _runlog(
        f"start argv={sys.argv[1:]} "
        f"mode={'history:' + dt.strftime('%Y-%m-%d') if dt else 'realtime'}"
    )

    with requests.Session() as session:
        if dt:
            results = scrape_history(session, dt)
        else:
            today       = datetime.now()
            lookback_dt = get_lookback_dt()

            results_rt       = scrape_today(session)           # 即時 feed（最新）
            results_today    = scrape_history(session, today)  # REST API 今日
            results_lookback = []
            if lookback_dt.date() < today.date():
                results_lookback = scrape_history(session, lookback_dt)

            seen    = set()
            results = []
            for item in results_rt + results_today + results_lookback:
                uid = (item["co_id"], item["date"], item["time"])
                if uid not in seen:
                    seen.add(uid)
                    results.append(item)

            # 當日視窗過濾（只保留今天 00:00 起公布的公告）
            # 注意：必須解析成 date 物件比較。舊版用字串比較，遇到沒補零
            # （115/6/8）或西元（2026/06/08）格式會誤判放行，導致舊公告重發。
            cutoff_d = lookback_dt.date()

            def within_window(item):
                d = item.get("date", "")
                dd = _parse_announce_date(d)
                if dd is None:
                    _runlog(f"window keep(unparsed date) {item.get('co_id')} raw_date={d!r}")
                    return True
                if dd < cutoff_d:
                    _runlog(f"window drop {item.get('co_id')} {item.get('co_name')} raw_date={d!r}")
                    return False
                return True

            results = [x for x in results if within_window(x)]
            _runlog(
                f"realtime scrape rt={len(results_rt)} today={len(results_today)} "
                f"lookback={len(results_lookback)} after_window={len(results)}"
            )

    if not results:
        print(json.dumps([{"message": f"📭 無符合條件的自結訊息\n🔗 {MOPS_GENERAL}"}], ensure_ascii=False))
        return

    # 標準化
    for item in results:
        normalize_item(item)

    # 過濾「鬼魂公告」：清單 API 有列、但 detail API 回 406（查無相符資料）導致內文為空。
    # 通常是公告事後被撤回但清單未同步（如 911868 同方友友-DR 2026/6/1）。
    # 無內文本來就無法解析財務數字，發 no_data 警告純屬雜訊。
    ghosts  = [x for x in results if not x.get("financial_text", "").strip()]
    for g in ghosts:
        sys.stderr.write(
            f"⚠️  detail 為空，略過：{g.get('co_id')} {g.get('co_name')} "
            f"{g.get('date')} {g.get('time')}\n"
        )
    results = [x for x in results if x.get("financial_text", "").strip()]

    # 過濾「可轉債／公司債」類注意交易公告（如 7610 聯友金屬-創）：
    # 主旨含「注意交易資訊標準／以利投資人區別」被收進來，但內容是債券資訊（無財務報表），
    # 非自結，直接剔除避免誤觸 no_data 警告。
    BOND_EXCLUDE = ["公司債相關資訊", "可轉債相關資訊"]
    results = [
        x for x in results
        if not any(k in x.get("financial_text", "") for k in BOND_EXCLUDE)
    ]
    if not results:
        print(json.dumps([{"message": f"📭 無符合條件的自結訊息\n🔗 {MOPS_GENERAL}"}], ensure_ascii=False))
        return

    # ── 已發送去重（僅即時模式）──────────────────────────────────────────────
    # 時間窗固定從當天 08:00 起算是防漏機制，但每 3 小時一輪會重複發送同樣公告。
    # 這裡以 sent_state.json 記錄已發送的 (co_id, date, time)，發過的不再進訊息。
    realtime   = dt is None
    sent_state = {}
    groups_with_any = set()   # 去重前，本窗內各群組是否有任何公告（供 📭 判斷）
    if realtime:
        sent_state = _load_sent_state()
        for x in results:
            for g in get_groups(x["co_id"], x["co_name"]):
                groups_with_any.add(g)
        results = [
            x for x in results
            if _sent_key(x) not in sent_state
            and (_content_key(x) or "") not in sent_state
        ]
        if not results:
            # 窗內公告皆已發送過 → 安靜結束，不再重發
            print(json.dumps([], ensure_ascii=False))
            return

    # 依時間升冪排序
    results.sort(key=lambda x: (x.get("date", ""), x.get("time", "")))

    # 依類型分流
    # 訊息標頭日期：歷史模式用「查詢日」而非今天，避免查 6/8 卻標成 6/11 造成誤導
    today_str    = (dt or datetime.now()).strftime("%-m/%-d")
    jishi_items   = [x for x in results if x.get("item_type") == "自結"]
    qrpt_items    = [x for x in results if x.get("item_type") == "季報"]
    annual_items  = [x for x in results if x.get("item_type") == "年報"]
    revenue_items = [x for x in results if x.get("item_type") == "月營收"]

    csv_recs       = []
    blocks_m       = []   # 自結損益/月（含月度數字）
    blocks_q       = []   # 自結損益/季（僅季/年度數字）
    no_data        = []
    unit_unknown   = []
    qrpt_blocks    = []
    qrpt_no_data   = []
    annual_blocks  = []
    annual_no_data = []
    revenue_blocks = []   # 月營收精簡一行（不寫 CSV、不發警告，正式紀錄交由 mops_f22）

    # ── 階段 1：收集資料（自結 + 季報 + 年報）────────────────────────────────
    DEBUG_IDS = set()  # 放要 debug 的股票代號，如 {"3042", "3689"}；留空不 debug
    for item in jishi_items:
        grps = get_groups(item["co_id"], item["co_name"])
        item["_groups"] = grps
        text = item.get("financial_text", "")
        if item["co_id"] in DEBUG_IDS:
            sys.stderr.write(f"\n=== DEBUG {item['co_id']} {item['co_name']} ===\n{text}\n===END===\n")
        month_data, month_label, qtr_data, qtr_label, unit_str = extract_financials_both(text)

        # Fallback：標準解析失敗時，嘗試精簡損益表（銀行/金控縮寫欄名格式）
        if not month_data and not qtr_data:
            compact = extract_compact_pnl(text)
            if compact:
                cm_data, cm_label, cc_data, cc_label, cu_str = compact
                csv_recs.append({
                    "item":        item,
                    "type":        "自結",
                    "month_data":  cm_data,
                    "month_label": cm_label or "",
                    "qtr_data":    cc_data,
                    "qtr_label":   cc_label or "",
                    "unit_str":    cu_str or "",
                    "note":        "",
                    "url":         "",
                })
                _cb = format_compact_block(item, cm_data, cm_label, cc_data, cc_label, cu_str)
                (blocks_m if cm_data else blocks_q).append((_cb, grps))
                _archive_item(item, text,
                              {"month": cm_data, "month_label": cm_label,
                               "qtr": cc_data, "qtr_label": cc_label, "unit": cu_str},
                              _cb, {"format": "compact"})
                continue

        # Fallback 2：金控矩陣表（表頭在上、子公司數值在下）。
        # 觸發條件二擇一：(a) 前面全都解析失敗；(b) 解析出來但「稅前==母利」＝明顯欄位對錯，
        # 且該文本確實是金控矩陣表（兩條件同時成立者全歷史僅 5880 合庫金一檔，無誤傷）。
        # 抓不到就往下走原本流程，不影響既有行為。
        if (not month_data and not qtr_data) or _looks_misparsed(month_data, qtr_data):
            holdco = extract_holdco_matrix(text, item.get("co_name", ""), item.get("date", ""))
            if holdco:
                hm_data, hm_label, hc_data, hc_label, hu_str = holdco
                csv_recs.append({
                    "item":        item,
                    "type":        "自結",
                    "month_data":  hm_data,
                    "month_label": hm_label or "",
                    "qtr_data":    hc_data,
                    "qtr_label":   hc_label or "",
                    "unit_str":    hu_str or "",
                    "note":        "金控矩陣表",
                    "url":         "",
                })
                _hb = format_compact_block(
                    item, hm_data, hm_label, hc_data, hc_label, hu_str,
                    omit_keys=("營收", "毛利"),   # 金控無「營業收入」科目，整行不顯示
                    footnote="※ 金控自結，無營收科目；稅後為集團合併數，未必等於歸屬母公司")
                (blocks_m if hm_data else blocks_q).append((_hb, grps))
                _archive_item(item, text,
                              {"month": hm_data, "month_label": hm_label,
                               "qtr": hc_data, "qtr_label": hc_label, "unit": hu_str},
                              _hb, {"format": "holdco_matrix"})
                continue

        if not month_data and not qtr_data:
            no_data.append(item)
            csv_recs.append({
                "item":        item,
                "type":        "自結",
                "month_data":  {},
                "month_label": "",
                "qtr_data":    {},
                "qtr_label":   "",
                "unit_str":    "",
                "note":        "解析不到財務數字，請人工確認",
                "url":         mops_link(item["co_id"]),
            })
            _archive_item(item, text, {}, "", {"error": "解析不到財務數字"})
        elif unit_str is None:
            unit_unknown.append(item)
            csv_recs.append({
                "item":        item,
                "type":        "自結",
                "month_data":  month_data,
                "month_label": month_label,
                "qtr_data":    qtr_data,
                "qtr_label":   qtr_label,
                "unit_str":    "unknown",
                "note":        "單位無法識別，請人工確認",
                "url":         mops_link(item["co_id"]),
            })
            _archive_item(item, text,
                          {"month": month_data, "month_label": month_label,
                           "qtr": qtr_data, "qtr_label": qtr_label},
                          "", {"error": "單位無法識別"})
        else:
            # Reviewer：檢查顯示期間的四大指標完整性
            periods = ([("月", month_data)] if month_data else []) + \
                      ([("季", qtr_data)] if qtr_data else [])
            review = review_missing(text, periods)
            csv_recs.append({
                "item":        item,
                "type":        "自結",
                "month_data":  month_data,
                "month_label": month_label,
                "qtr_data":    qtr_data,
                "qtr_label":   qtr_label,
                "unit_str":    unit_str or "",
                "note":        review,
                "url":         mops_link(item["co_id"]) if review else "",
            })
            block = format_company_block_v2(
                item, month_data, month_label, qtr_data, qtr_label, unit_str, text
            )
            (blocks_m if month_data else blocks_q).append((block, grps))
            _archive_item(item, text,
                          {"month": month_data, "month_label": month_label,
                           "qtr": qtr_data, "qtr_label": qtr_label, "unit": unit_str},
                          block,
                          {"sanity": _sanity_warns(month_data) + _sanity_warns(qtr_data),
                           "review": review})

    for item in qrpt_items:
        grps = get_groups(item["co_id"], item["co_name"])
        item["_groups"] = grps
        text = item.get("financial_text", "")
        if item["co_id"] in DEBUG_IDS:
            sys.stderr.write(f"\n=== DEBUG {item['co_id']} {item['co_name']} (季報) ===\n{text}\n===END===\n")
        qtr_data, qtr_label, unit_str = extract_quarterly_report(text)
        # 季報不寫 CSV：正式數字由 mops_f27（官方綜合損益表 API）負責，較完整可靠
        if not qtr_data:
            qrpt_no_data.append(item)
            _archive_item(item, text, {}, "", {"error": "季報解析不到數字"})
        else:
            _qb = format_report_block(item, qtr_data, qtr_label, unit_str, check_f27_lead=True)
            qrpt_blocks.append((_qb, grps))
            _archive_item(item, text, {"qtr": qtr_data, "qtr_label": qtr_label, "unit": unit_str},
                          _qb, {"type": "季報", "sanity": _sanity_warns(qtr_data)})

    for item in annual_items:
        grps = get_groups(item["co_id"], item["co_name"])
        item["_groups"] = grps
        text = item.get("financial_text", "")
        qtr_data, qtr_label, unit_str = extract_quarterly_report(text)
        if not qtr_data:
            annual_no_data.append(item)
            _archive_item(item, text, {}, "", {"error": "年報解析不到數字"})
        else:
            _ab = format_report_block(item, qtr_data, qtr_label, unit_str)
            annual_blocks.append((_ab, grps))
            _archive_item(item, text, {"qtr": qtr_data, "qtr_label": qtr_label, "unit": unit_str},
                          _ab, {"type": "年報", "sanity": _sanity_warns(qtr_data)})

    # 月營收（純營收重大訊息）：精簡一行；不寫 CSV、不發人工確認警告。
    # 正式結構化紀錄由 mops_f22_daily.py（官方月營收 API）負責，較完整可靠。
    for item in revenue_items:
        grps = get_groups(item["co_id"], item["co_name"])
        item["_groups"] = grps
        rev = extract_monthly_revenue(item.get("financial_text", ""))
        if rev:
            revenue_blocks.append((format_revenue_line(item, rev), grps))
        else:
            sys.stderr.write(
                f"⚠️  月營收解析失敗（略過，不影響 CSV）：{item['co_id']} {item['co_name']}\n"
            )

    # ── 階段 2：CSV 寫入 + Google Drive 上傳（在訊息組裝前執行）─────────────
    written    = save_to_csv(csv_recs)
    csv_link   = upload_to_gdrive()
    sheet_link = csv_link or GSHEET_LINK

    # ── 階段 3：組裝訊息 ─────────────────────────────────────────────────────
    output        = []
    qrpt_header   = f"📋{today_str} 季報（董事會通過）📋"
    annual_header = f"📊{today_str} 年報（董事會通過）📊"
    MAX_LEN       = 4000

    def _build_messages_for_group(header, block_tuples, group, digest=False):
        """block_tuples: list of (block_text, groups_list)，只取屬於 group 的。
        digest=True（自結速報）：{n} 換家數、⚠️警示置頂、尾附 */† 圖例。"""
        filtered = [b for b, g in block_tuples if group in g]
        if not filtered:
            return []
        header = header.replace("{n}", str(len(filtered)))
        if digest:
            warns = [(t, lk) for t, gs, lk in _DIGEST_WARN if group in gs]
            if warns:
                header += "\n" + "\n".join(f"⚠️ {t}\n🔗 {lk}" for t, lk in warns)
        messages = []
        current  = header
        for block in filtered:
            segment = f"\n{SEPARATOR}\n{block}"
            if len(current) + len(segment) + len(f"\n{SEPARATOR}") > MAX_LEN and current != header:
                current += f"\n{SEPARATOR}"
                messages.append(current)
                current = header + segment
            else:
                current += segment
        tail = "\n*官方YoY †DB推估 ‡去年值由累計欄反推" if digest else ""
        current += f"\n{SEPARATOR}{tail}\n📊 {sheet_link}"
        messages.append(current)
        return messages

    def _build_revenue_message_for_group(header, line_tuples, group):
        """月營收精簡區塊：一家一行，行間不加分隔線。回傳 list[str]（含分頁）。"""
        filtered = [ln for ln, g in line_tuples if group in g]
        if not filtered:
            return []
        messages = []
        current  = header
        for ln in filtered:
            segment = f"\n{ln}"
            if len(current) + len(segment) > MAX_LEN and current != header:
                messages.append(current)
                current = header + segment
            else:
                current += segment
        messages.append(current)
        return messages

    def _emit_warnings(warn_items, warn_label):
        """對警告清單逐筆送出（debug 用途，全部送出，接收者為管理者）
        - 在監控清單：標頭加 [群組名]
        - 不在任何清單：不加標注
        """
        GROUP_LABEL = {k: v["display"] for k, v in _WATCHLIST.items()}
        for item in warn_items:
            grps   = item.get("_groups", [])
            date_w = _roc_to_western(item.get("date", ""))
            time_s = item.get("time", "")
            link   = mops_link(item["co_id"])
            group_tag  = "[" + " / ".join(GROUP_LABEL[g] for g in grps) + "] " if grps else ""
            group_note = "" if grps else "📋 不屬於任何清單\n"
            msg_text = (
                f"{group_tag}⚠️ {warn_label}\n"
                f"{item['co_id']} {item['co_name']} {date_w} {time_s}\n"
                f"{group_note}"
                f"🔗 {link}"
            )
            output.append({"message": msg_text, "group": "standalone"})

    # 依人員組合分群後，只送「當天有公告」的組合（空組合不送）。
    # 某組合不管有無公告都想收 → 加進此 set（如 {"Max／Ryan"}）。
    ALWAYS_SEND_GROUPS = set()

    # 主訊息：依人員組合分群組裝（組合名放標題 [Max／Ryan]，版型沿用舊格式）
    for group in list(_WATCHLIST.keys()):
        group_display = _WATCHLIST[group]["display"]
        group_has_msg = False
        for header_tmpl, block_tuples, digest in [
            # 自結（月+季合併）：速報格式，{n}=家數，警示置頂，尾附圖例
            (f"📈 M99 自結速報 {today_str}（{{n}}家｜百萬）｜👤 {group_display}", blocks_m + blocks_q, True),
            (f"📋 M31 季報（董事會通過） {today_str}｜👤 {group_display}", qrpt_blocks, False),
            (f"📑 年報（董事會通過） {today_str}｜👤 {group_display}", annual_blocks, False),
        ]:
            for m in _build_messages_for_group(header_tmpl, block_tuples, group, digest):
                output.append({"message": m, "group": group})
                group_has_msg = True
        # 月營收精簡區塊（早鳥訊號；正式紀錄由 mops_f22 負責）
        for m in _build_revenue_message_for_group(
            f"🧾 自結營收/月(早鳥·非正式) {today_str}｜👤 {group_display}", revenue_blocks, group
        ):
            output.append({"message": m, "group": group})
            group_has_msg = True
        # 📭 僅在「本窗內完全沒有該群組的公告」時發送（已發送被去重者不算沒有）
        if not group_has_msg and group in ALWAYS_SEND_GROUPS and group not in groups_with_any:
            output.append({"message": f"📭 今日無自結／季報／年報\n🔗 {MOPS_GENERAL}", "group": group})

    # 警告訊息：所有有問題的公告，逐筆送出
    # 需人工確認事項全部彙整進 _ATTENTION（統一一則訊息，不逐筆發）
    for warn_items, warn_label in ((no_data,        "解析不到財務數字"),
                                   (qrpt_no_data,   "季報解析不到財務數字"),
                                   (annual_no_data, "年報解析不到財務數字"),
                                   (unit_unknown,   "單位無法識別")):
        for it in warn_items:
            _ATTENTION.append((it["co_id"], it["co_name"], warn_label, mops_link(it["co_id"])))

    # ── 階段 4：記錄已發送（僅即時模式；先存檔再輸出，失敗時直接發警告）──────
    if realtime:
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for x in results:
            sent_state[_sent_key(x)] = now_s
            ck = _content_key(x)
            if ck:
                sent_state[ck] = now_s
        ok = _save_sent_state(sent_state)
        if not ok:
            output.append({
                "message": "⚠️ sent_state.json 寫入失敗：去重狀態未保存，之後每輪可能重發相同訊息，請檢查資料夾權限",
                "group": "standalone",
            })
        _runlog(f"run done: sent={len(results)} msgs={len(output)} sent_state_saved={ok}")

    # ── 人工確認清單：本輪所有需人工介入事項 + 待查檔未處理數，彙整成一則 ──
    if output:
        att_lines = []
        seen_att = set()
        for sid_a, name_a, why, link in _ATTENTION:
            key = (sid_a, why)
            if key in seen_att:
                continue
            seen_att.add(key)
            att_lines.append(f"{len(att_lines)+1}. {sid_a} {name_a}：{why}\n   🔗 {link}")
        pending = []
        try:
            import csv as _csv
            with open(REVIEW_QUEUE, encoding="utf-8") as f:
                pending = [r for r in _csv.DictReader(f) if not (r.get("resolved") or "").strip()]
        except OSError:
            pass
        if att_lines or pending:
            msg = "🧑‍💻 人工確認清單"
            if att_lines:
                msg += "\n" + "\n".join(att_lines)
            if pending:
                heads = "、".join(f"{r['co_id']}({r['field']})" for r in pending[:5])
                more  = f" 等共{len(pending)}筆" if len(pending) > 5 else ""
                msg += (f"\n⏳ 待查檔未處理 {len(pending)} 筆：{heads}{more}"
                        f"\n（處理後在 db_review_queue.csv 的 resolved 欄填註記）")
            output.append({"message": msg, "group": "standalone"})

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    run()

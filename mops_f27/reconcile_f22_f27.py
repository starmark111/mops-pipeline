"""
reconcile_f22_f27.py
F22（每月營收）× F27（季報綜合損益表）營收交叉對帳。

核心邏輯：
  F22 在「季末月」(第N季→第N×3月) 的累計營收，應該 ≈ F27 第N季的累計營收。
    ytd 比對：F22 ytd_revenue(月=季×3)  vs  F27 ytd_revenue
    單季比對：F22 ytd(季末) - ytd(前季末)  vs  F27 revenue(單季)
  兩邊單位都是仟元。差額/百分比超過容差 → 標「差異待查」。

設定（依需求）：
  - 容差 1%（並設絕對下限，避免小基數被百分比放大）
  - 全市場比對
  - 對帳結果寫一張 CSV，依「狀態 + 差異絕對值」排序，含 in_watchlist 欄方便篩
  - 另附 EPS / 淨利 參考欄（不參與符合判定）
  - 「差異待查」且在觀察清單內的，才推 Telegram

輸出模式：
  python3 reconcile_f22_f27.py              # 寫 CSV + 印摘要（不發 Telegram）
  python3 reconcile_f22_f27.py --dry-run    # 另把要推播的訊息印出來預覽
  python3 reconcile_f22_f27.py --emit       # 印 JSON 給 n8n（只含觀察清單差異）
  export TELEGRAM_BOT_TOKEN=...; python3 reconcile_f22_f27.py --send   # 直接發
  可選：--year 2026 --quarter 1  限定期間（預設全部）
"""

import os
import sys
import csv
import json
import math
import time
import requests
from datetime import datetime

# 重用 F27 的觀察清單載入與分群邏輯
import mops_f27_daily as F27


# =========================
# 路徑 / 設定
# =========================
_DIR    = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_DIR)

F22_CSV = os.path.join(_PARENT, "mops_f22", "monthly_revenue.csv")
F27_CSV = os.path.join(_DIR, "quarterly_revenue.csv")
OUT_CSV = os.path.join(_DIR, "revenue_reconcile_f22_f27.csv")

# 容差：累計或單季差 ≥ 此百分比視為待查。
# 2026-08-15 由 1.0 調為 3.0：一般製造業月營收（營業收入）與季報營收（可能含其他
# 營業收入、合併範圍差異）本來就會差 1~2%，1% 容差讓同一批公司每季固定跳警報，
# 久了會被無視——那比沒有告警更糟。
TOLERANCE_PCT = 3.0
ABS_FLOOR_K   = 100          # 絕對下限（仟元＝10萬）：差額在此之內一律視為符合，僅用來吸收近零基數的雜訊

# 金融業不適用本對帳：F22 對保險是保費收入、對銀行是利息收入，與季報「營業收入」
# 定義不同，硬比一定不合（如 2851 中再保 2026Q1 差 47.6%，並非資料錯誤）。
# 這類直接標「不適用比對」，不列入待查、不推播。
NON_COMPARABLE_SECTORS = {"保險", "銀行", "證券"}

# 體質差異判定：同一家公司若有 ≥3 個季度都被判待查，且差異方向一致、幅度都在
# 此上限內，視為「認列基準不同」造成的長期性差異，而非漏抓或抓錯。
# （真的漏抓會是某一季突然差很多，不會每季穩定差 1.5%）
CHRONIC_MIN_QUARTERS = 3
CHRONIC_MAX_PCT      = 10.0

CHAT_ID   = "1085373824"
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SEND_DELAY = 1.0

OUT_COLUMNS = [
    "stock_id", "company_name", "market", "industry", "sector_type",
    "year", "quarter", "in_watchlist", "watch_group",
    "f22_ytd_revenue", "f27_ytd_revenue", "ytd_diff", "ytd_pct_diff",
    "f22_q_revenue", "f27_q_revenue", "q_diff", "q_pct_diff",
    "net_income", "net_income_parent", "eps", "eps_diluted",
    "status",
]

# 狀態排序優先序（小在前）
STATUS_ORDER = {"差異待查": 0, "體質差異": 1, "缺F22": 2, "無法比對": 3,
                "不適用比對": 4, "符合": 5}


# =========================
# 小工具
# =========================
def _i(s):
    s = (str(s) if s is not None else "").replace(",", "").strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _f(s):
    s = (str(s) if s is not None else "").replace(",", "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_opts():
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    kv = {}
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a in ("--year", "--quarter") and i + 1 < len(argv):
            kv[a.lstrip("-")] = argv[i + 1]
    return {
        "dry_run": "--dry-run" in flags,
        "emit":    "--emit" in flags,
        "send":    "--send" in flags,
        "year":    kv.get("year"),
        "quarter": kv.get("quarter"),
    }


# =========================
# 載入 F22：以 (stock_id, year) → {month: ytd_revenue} 整理
# =========================
def load_f22():
    if not os.path.exists(F22_CSV):
        sys.stderr.write(f"❌ 找不到 {F22_CSV}\n")
        sys.exit(1)
    data = {}   # (stock, year) -> {month(int): ytd_revenue(int)}
    names = {}  # stock -> name（備用）
    with open(F22_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            stock = r.get("stock_id", "").strip()
            year  = str(r.get("report_year", "")).strip()
            month = _i(r.get("report_month"))
            ytd   = _i(r.get("ytd_revenue"))
            if not stock or not year or month is None:
                continue
            data.setdefault((stock, year), {})[month] = ytd
            if r.get("company_name"):
                names[stock] = r["company_name"]
    return data, names


def f22_quarter_revenue(f22_map, stock, year, quarter):
    """回傳 (ytd_revenue_at_quarter_end, single_quarter_revenue) 仟元，缺則 None。"""
    months = f22_map.get((stock, year))
    if not months:
        return None, None
    end_m  = quarter * 3
    prev_m = (quarter - 1) * 3
    ytd_end  = months.get(end_m)
    ytd_prev = months.get(prev_m) if quarter > 1 else 0
    single = None
    if ytd_end is not None and ytd_prev is not None:
        single = ytd_end - ytd_prev
    return ytd_end, single


# =========================
# 載入 F27：每筆季報一列
# =========================
def load_f27(year_filter=None, quarter_filter=None):
    if not os.path.exists(F27_CSV):
        sys.stderr.write(f"❌ 找不到 {F27_CSV}\n")
        sys.exit(1)
    rows = []
    with open(F27_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            year = str(r.get("report_year", "")).strip()
            q    = str(r.get("report_quarter", "")).strip()
            if year_filter and year != str(year_filter):
                continue
            if quarter_filter and q != str(quarter_filter):
                continue
            rows.append(r)
    return rows


# =========================
# 對帳
# =========================
def pct_diff(a, b):
    if a is None or b is None or b == 0:
        return None
    return round((a - b) / abs(b) * 100, 2)


def judge(ytd_diff, ytd_pct, q_diff, q_pct, use_q=True):
    """依容差判斷是否符合。差額在絕對下限內一律視為符合。
    use_q=False 時只看累計（年報無單季資料，不比單季）。"""
    def within(diff, pct):
        if diff is None or pct is None:
            return True   # 無法計算的一邊不作為不符依據
        if abs(diff) <= ABS_FLOOR_K:
            return True
        return abs(pct) < TOLERANCE_PCT
    ok = within(ytd_diff, ytd_pct)
    if use_q:
        ok = ok and within(q_diff, q_pct)
    return ok


def build_rows(f27_rows, f22_map, f22_names):
    F27._load_watchlist()
    out = []
    for r in f27_rows:
        stock = r.get("stock_id", "").strip()
        year  = str(r.get("report_year", "")).strip()
        q     = _i(r.get("report_quarter"))
        if not stock or not year or q is None:
            continue

        f27_ytd = _i(r.get("ytd_revenue"))
        f27_q   = _i(r.get("revenue"))
        f22_ytd, f22_q = f22_quarter_revenue(f22_map, stock, year, q)

        # 年報（第4季）的 F27 損益表只有「全年累計」、沒有單季數字，
        # 拿全年去比 F22 的第4季單月加總會是假警報，故年報只比累計、不比單季。
        is_annual = (q == 4)
        if is_annual:
            f22_q = None
            f27_q = None

        ytd_diff = (f22_ytd - f27_ytd) if (f22_ytd is not None and f27_ytd is not None) else None
        q_diff   = (f22_q - f27_q)     if (f22_q   is not None and f27_q   is not None) else None
        ytd_pct  = pct_diff(f22_ytd, f27_ytd)
        q_pct    = pct_diff(f22_q,   f27_q)

        # 狀態判定
        if r.get("sector_type", "").strip() in NON_COMPARABLE_SECTORS:
            status = "不適用比對"        # 金融業營收定義不同，比了沒有意義
        elif f27_ytd is None:
            status = "無法比對"          # F27 無營收欄
        elif f22_ytd is None:
            status = "缺F22"             # 對應季末月的月營收資料缺（純銀行/保險常無月營收）
        elif judge(ytd_diff, ytd_pct, q_diff, q_pct, use_q=not is_annual):
            status = "符合"
        else:
            status = "差異待查"

        groups = F27.get_groups(stock, r.get("company_name", ""))
        out.append({
            "stock_id": stock,
            "company_name": r.get("company_name", "") or f22_names.get(stock, ""),
            "market": r.get("market", ""),
            "industry": r.get("industry", ""),
            "sector_type": r.get("sector_type", ""),
            "year": year,
            "quarter": q,
            "in_watchlist": "Y" if groups else "N",
            "watch_group": ",".join(groups),
            "f22_ytd_revenue": "" if f22_ytd is None else f22_ytd,
            "f27_ytd_revenue": "" if f27_ytd is None else f27_ytd,
            "ytd_diff": "" if ytd_diff is None else ytd_diff,
            "ytd_pct_diff": "" if ytd_pct is None else ytd_pct,
            "f22_q_revenue": "" if f22_q is None else f22_q,
            "f27_q_revenue": "" if f27_q is None else f27_q,
            "q_diff": "" if q_diff is None else q_diff,
            "q_pct_diff": "" if q_pct is None else q_pct,
            "net_income": r.get("net_income", ""),
            "net_income_parent": r.get("net_income_parent", ""),
            "eps": r.get("eps", ""),
            "eps_diluted": r.get("eps_diluted", ""),
            "status": status,
            # 排序輔助（不寫進 CSV）
            "_abs": max(abs(ytd_pct) if ytd_pct is not None else 0,
                        abs(q_pct) if q_pct is not None else 0),
        })

    _mark_chronic(out)

    # 排序：狀態優先序 → 差異絕對值由大到小
    out.sort(key=lambda x: (STATUS_ORDER.get(x["status"], 9), -x["_abs"]))
    return out


def _mark_chronic(rows):
    """把「長期穩定差異」從待查改標為體質差異，就地修改 rows。

    判定：同一 stock_id 的待查筆數 ≥ CHRONIC_MIN_QUARTERS，且
      (a) 差異方向全部一致（都是 F22 偏高或都偏低），
      (b) 幅度都在 CHRONIC_MAX_PCT 內。
    符合者＝認列基準不同造成的固定落差，不是漏抓，不需要每季重複告警。

    只要有一季方向相反或幅度暴衝，就整組維持待查——寧可多看，不可漏看。
    """
    by_stock = {}
    for r in rows:
        if r["status"] == "差異待查":
            by_stock.setdefault(r["stock_id"], []).append(r)

    for stock, group in by_stock.items():
        if len(group) < CHRONIC_MIN_QUARTERS:
            continue
        pcts = []
        for r in group:
            p = r.get("ytd_pct_diff")
            if p == "" or p is None:
                pcts = []
                break
            pcts.append(float(p))
        if not pcts:
            continue
        same_sign = all(p > 0 for p in pcts) or all(p < 0 for p in pcts)
        in_range  = all(abs(p) <= CHRONIC_MAX_PCT for p in pcts)
        if same_sign and in_range:
            for r in group:
                r["status"] = "體質差異"


def write_csv(rows):
    with open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# =========================
# Telegram 訊息（只含觀察清單的差異待查）
# =========================
def _fmt_m(n):
    if n is None or n == "":
        return "-"
    v = float(n) / 1000
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"


def _over_tol(pct) -> bool:
    """該百分比是否超出容差（空值視為未超標）。"""
    if pct in ("", None):
        return False
    try:
        return abs(float(pct)) >= TOLERANCE_PCT
    except (TypeError, ValueError):
        return False


def build_alert_messages(rows):
    """依 watch_group 分群，組裝差異待查訊息。回傳 [(group, message), ...]。"""
    targets = [r for r in rows if r["status"] == "差異待查" and r["in_watchlist"] == "Y"]
    by_group = {}
    for r in targets:
        for g in (r["watch_group"].split(",") if r["watch_group"] else []):
            by_group.setdefault(g, []).append(r)

    today = datetime.now().strftime("%-m/%-d") if os.name != "nt" else datetime.now().strftime("%m/%d")
    messages = []
    for g, items in by_group.items():
        display = F27._WATCHLIST.get(g, {}).get("display", g)
        header = f"⚠️{today} F22×F27 營收對帳差異 [{display}]"
        cur = header
        for r in items:
            # 累計與單季只要有一邊超標就會待查。原本訊息只印累計，遇到「累計吻合、
            # 單季不合」的情況會顯示「差 0.0%」卻仍告警，看起來像誤報。
            # → 兩邊都印，並標出是哪一邊觸發的。
            lines = [f"{r['stock_id']} {r['company_name']} {r['year']}Q{r['quarter']}"]
            ytd_hit = _over_tol(r["ytd_pct_diff"])
            q_hit   = _over_tol(r["q_pct_diff"])
            # 注意：差異 0.0 是合法值且為 falsy，不可用 `or '-'`（會把 0.0 印成 -）
            _p = lambda v: "-" if v in ("", None) else v
            lines.append(f"{'▶ ' if ytd_hit else '　'}累計 F22 {_fmt_m(r['f22_ytd_revenue'])}"
                         f" / F27 {_fmt_m(r['f27_ytd_revenue'])}  差 {_p(r['ytd_pct_diff'])}%")
            if r["q_pct_diff"] not in ("", None):
                lines.append(f"{'▶ ' if q_hit else '　'}單季 F22 {_fmt_m(r['f22_q_revenue'])}"
                             f" / F27 {_fmt_m(r['f27_q_revenue'])}  差 {r['q_pct_diff']}%")
            lines.append(f"（▶ 為超出容差 {TOLERANCE_PCT}% 的項目）" if (ytd_hit or q_hit) else "")
            blk = f"\n{F27.SEPARATOR}\n" + "\n".join(x for x in lines if x)
            if len(cur) + len(blk) > F27.MAX_LEN and cur != header:
                messages.append((g, cur + f"\n{F27.SEPARATOR}"))
                cur = header + blk
            else:
                cur += blk
        messages.append((g, cur + f"\n{F27.SEPARATOR}"))
    return messages


def send_telegram(text):
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": CHAT_ID, "text": safe,
                                    "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)
    if resp.status_code != 200:
        sys.stderr.write(f"  ⚠️  Telegram 發送失敗 {resp.status_code}: {resp.text[:200]}\n")
        return False
    return True


# =========================
# 主流程
# =========================
def main():
    opts = parse_opts()

    f22_map, f22_names = load_f22()
    f27_rows = load_f27(opts["year"], opts["quarter"])
    rows = build_rows(f27_rows, f22_map, f22_names)
    write_csv(rows)

    # 摘要
    summary = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    wl_review = sum(1 for r in rows if r["status"] == "差異待查" and r["in_watchlist"] == "Y")
    sys.stderr.write(f"\n✅ 對帳完成，共 {len(rows)} 筆季報，已寫入 {OUT_CSV}\n")
    for k in ["符合", "差異待查", "體質差異", "缺F22", "無法比對", "不適用比對"]:
        sys.stderr.write(f"   {k}: {summary.get(k, 0)}\n")
    sys.stderr.write(f"   其中『差異待查且在觀察清單』: {wl_review} 筆\n\n")

    # 推播 / 預覽 / emit
    alerts = build_alert_messages(rows)
    if opts["emit"]:
        print(json.dumps([{"message": m, "group": g} for g, m in alerts], ensure_ascii=False))
    elif opts["dry_run"]:
        for g, m in alerts:
            print(m); print("─" * 30)
        if not alerts:
            sys.stderr.write("（觀察清單內無差異待查，無推播）\n")
    elif opts["send"]:
        if not BOT_TOKEN:
            sys.stderr.write("❌ 未設定 TELEGRAM_BOT_TOKEN，無法發送。\n")
            sys.exit(1)
        sent = 0
        for g, m in alerts:
            if send_telegram(m):
                sent += 1
            time.sleep(SEND_DELAY)
        sys.stderr.write(f"✅ 已推播 {sent} 則差異警示。\n")


if __name__ == "__main__":
    main()

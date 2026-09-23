"""
query_company_f22.py
從 monthly_revenue.csv 查詢特定公司的月營收歷史（不碰網路）。

用法：
  python3 query_company_f22.py 禾伸堂                 # 用名稱查
  python3 query_company_f22.py 3026                   # 用股號查
  python3 query_company_f22.py 禾伸堂 國巨 2327        # 一次查多家
  python3 query_company_f22.py 禾伸堂 --months 24       # 顯示最近 24 個月（預設 12）
  python3 query_company_f22.py 禾伸堂 --all             # 顯示全部
  python3 query_company_f22.py 禾伸堂 --emit            # 輸出 [{"message","group"}] JSON（給 n8n / Telegram）
  python3 query_company_f22.py --owner Ryan             # 查某負責人名下全部股票（讀 watchlist）
  python3 query_company_f22.py --owner Ryan --from 2026-04 --to 2026-06   # 限定月份區間
  python3 query_company_f22.py 2492 --from 2026-01      # 個股 + 起始月（--to 省略 = 查到最新）

比對規則：純數字 = 比對股號（完全相同）；其他 = 比對公司名（包含即算）。
--owner 會覆寫 send_owners_f22.txt 的發送過濾（查詢不受該檔限制）。
--from/--to 指「營收所屬月份」YYYY-MM；有給區間時 --months 上限自動放寬。
數字單位：仟元 → 顯示為百萬（與每日推播一致）。
"""

import os
import sys
import csv
import json

import mops_f22_daily as F22   # 沿用 _fmt_m / _fmt_pct 與 CSV_PATH


def _parse_ym(s):
    """'2026-04' → (2026, 4)；格式錯誤回 None"""
    try:
        y, m = s.split("-")
        return (int(y), int(m))
    except Exception:
        return None


def parse_args():
    argv = sys.argv[1:]
    queries, months, show_all, emit = [], 12, False, False
    owners, ym_from, ym_to = [], None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--all":
            show_all = True
        elif a == "--emit":
            emit = True
        elif a in ("--months", "--owner", "--from", "--to"):   # 帶值旗標
            i += 1
            v = argv[i] if i < len(argv) else ""
            if a == "--months":
                try:
                    months = int(v)
                except ValueError:
                    pass
            elif a == "--owner":
                owners.append(v)
            elif a == "--from":
                ym_from = _parse_ym(v)
            elif a == "--to":
                ym_to = _parse_ym(v)
        elif a.startswith(("--months=", "--owner=", "--from=", "--to=")):
            k, v = a.split("=", 1)
            if k == "--months":
                try:
                    months = int(v)
                except ValueError:
                    pass
            elif k == "--owner":
                owners.append(v)
            elif k == "--from":
                ym_from = _parse_ym(v)
            elif k == "--to":
                ym_to = _parse_ym(v)
        elif a.startswith("--"):
            pass                          # 忽略未知旗標
        else:
            queries.append(a)
        i += 1
    if not queries and not owners:
        sys.stderr.write("用法：python3 query_company_f22.py 公司名或股號 [更多...] "
                         "[--owner 名字] [--from YYYY-MM] [--to YYYY-MM] [--months N] [--all] [--emit]\n")
        sys.exit(1)
    # 有指定月份區間時，避免被預設 12 個月截斷
    if (ym_from or ym_to) and not show_all:
        show_all = True
    return queries, months, show_all, emit, owners, ym_from, ym_to


def owner_stock_ids(owners):
    """從 watchlist 取出負責人名下的股號清單（用 F22_OWNERS 覆寫 send_owners 檔的過濾）。"""
    os.environ["F22_OWNERS"] = ",".join(owners)
    F22._load_watchlist()
    return sorted(k for k in F22._STOCK_GROUPS if k.isdigit())


def match(row, q):
    if q.isdigit():
        return row["stock_id"] == q
    return q in row["company_name"]


def to_int(s):
    s = (s or "").replace(",", "").strip()
    try:
        return int(s)
    except ValueError:
        return None


def to_float(s):
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def build_company_block(stock_id, name, rows, months, show_all):
    # 同一 (年,月) 若有重複紀錄，只留 fetched_at 最新的一筆（防禦：CSV 曾因並行回補產生重複）
    best = {}
    for r in rows:
        k = (int(r["report_year"]), int(r["report_month"]))
        if k not in best or (r.get("fetched_at") or "") > (best[k].get("fetched_at") or ""):
            best[k] = r
    # 依 年/月 排序（新到舊）
    rows = sorted(best.values(), key=lambda r: (int(r["report_year"]), int(r["report_month"])), reverse=True)
    if not show_all:
        rows = rows[:months]
    have = {(int(r["report_year"]), int(r["report_month"])) for r in rows}
    # 補出範圍內缺少的月份（顯示「無資料」），忠實反映缺口
    if rows:
        y, m = int(rows[0]["report_year"]), int(rows[0]["report_month"])
        end = (int(rows[-1]["report_year"]), int(rows[-1]["report_month"]))
        seq = []
        while (y, m) >= end:
            seq.append((y, m))
            m -= 1
            if m == 0:
                y, m = y - 1, 12
    else:
        seq = []
    lines = [f"📊 F22 {stock_id} {name} 月營收（百萬）"]
    prev_year = None
    row_by_ym = {(int(r["report_year"]), int(r["report_month"])): r for r in rows}
    for ym in seq:
        if ym not in have:
            yy, mm = ym
            if prev_year is not None and str(yy) != prev_year:
                lines.append(f"─── {yy} 年 ───")
            prev_year = str(yy)
            lines.append(f"{mm}月 無資料")
            continue
        r = row_by_ym[ym]
        year = r["report_year"]
        m = int(r["report_month"])
        rev = F22._fmt_m(to_int(r["revenue"]))
        yoy = F22._fmt_pct_warn(to_float(r["yoy_pct"]))
        item = {"stock_id": stock_id, "report_year": year, "report_month": r["report_month"]}
        rec = {"revenue": to_int(r["revenue"])}
        mom = F22._fmt_pct_warn(F22.get_mom_pct(item, rec))
        # 跨年分隔線（新到舊排序，所以前一筆是較新的年份）
        if prev_year is not None and year != prev_year:
            lines.append(f"─── {year} 年 ───")
        prev_year = year
        lines.append(f"{m}月營收 {rev}（百萬）")
        lines.append(f"{yoy} YoY")
        lines.append(f"{mom} MoM")
    return "\n".join(lines)


def main():
    queries, months, show_all, emit, owners, ym_from, ym_to = parse_args()

    if owners:
        ids = owner_stock_ids(owners)
        if not ids:
            sys.stderr.write(f"⚠️  watchlist 找不到負責人「{'，'.join(owners)}」的股票\n")
        queries.extend(ids)

    if not os.path.exists(F22.CSV_PATH):
        sys.stderr.write(f"❌ 找不到 {F22.CSV_PATH}\n")
        sys.exit(1)

    all_rows = list(csv.DictReader(open(F22.CSV_PATH, encoding="utf-8")))

    # 月份區間過濾（營收所屬月份）
    if ym_from or ym_to:
        def _in_range(r):
            ym = (int(r["report_year"]), int(r["report_month"]))
            if ym_from and ym < ym_from:
                return False
            if ym_to and ym > ym_to:
                return False
            return True
        all_rows = [r for r in all_rows if _in_range(r)]

    blocks = []
    for q in queries:
        hits = [r for r in all_rows if match(r, q)]
        if not hits:
            sys.stderr.write(f"⚠️  查無「{q}」\n")
            continue
        # 同一 query 可能命中多家公司
        by_company = {}
        for r in hits:
            by_company.setdefault((r["stock_id"], r["company_name"]), []).append(r)
        for (sid, name), rows in by_company.items():
            blocks.append((sid, name, build_company_block(sid, name, rows, months, show_all)))

    if not blocks:
        sys.stderr.write("（無結果）\n")
        sys.exit(0)

    if emit:
        payload = [{"message": b, "group": f"query_{sid}"} for sid, name, b in blocks]
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for _, _, b in blocks:
            print(b)
            print("————————")


if __name__ == "__main__":
    main()

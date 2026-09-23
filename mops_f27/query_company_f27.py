"""
query_company_f27.py
從 quarterly_revenue.csv 查詢特定公司的季報損益歷史（不碰網路）。

用法：
  python3 query_company_f27.py 2330                     # 用股號查（預設近 8 季）
  python3 query_company_f27.py 台積電                    # 用名稱查
  python3 query_company_f27.py 2330 2317 --quarters 12   # 一次查多家、近 12 季
  python3 query_company_f27.py 2330 --all                # 顯示全部
  python3 query_company_f27.py 2330 --emit               # 輸出 [{"message","group"}] JSON（給 n8n / Telegram）

比對規則：純數字 = 比對股號（完全相同）；其他 = 比對公司名（包含即算）。
數字單位：仟元 → 顯示為百萬（與每日推播一致）；EPS 為元。
"""

import os
import sys
import csv
import json

import mops_f27_daily as F27   # 沿用 _fmt_m / _fmt_pct / _fmt_eps / get_qoq_pct 與 CSV_PATH


def parse_args():
    argv = sys.argv[1:]
    queries, quarters, show_all, emit = [], 8, False, False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--all":
            show_all = True
        elif a == "--emit":
            emit = True
        elif a == "--quarters":            # --quarters 12
            i += 1
            if i < len(argv):
                try:
                    quarters = int(argv[i])
                except ValueError:
                    pass
        elif a.startswith("--quarters="):  # --quarters=12
            try:
                quarters = int(a.split("=")[1])
            except ValueError:
                pass
        elif a.startswith("--"):
            pass                          # 忽略未知旗標
        else:
            queries.append(a)
        i += 1
    if not queries:
        sys.stderr.write("用法：python3 query_company_f27.py 公司名或股號 [更多...] [--quarters N] [--all] [--emit]\n")
        sys.exit(1)
    return queries, quarters, show_all, emit


def match(row, q):
    if q.isdigit():
        return row["stock_id"] == q
    return q in row["company_name"]


def to_int(s):
    s = (s or "").replace(",", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def to_float(s):
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


EVENTS_CSV = os.path.join(os.path.dirname(F27._DIR), "daily_important_event", "important_events_v4.csv")


def _jishi_newer_than(stock_id, after_yq):
    """從重訊 CSV 取自結季快數，只回傳比最新財報更新的季度。[(y,q,dict), ...] 新到舊。
    原則：財報為準，自結只補財報還沒出的季度。數字已是百萬。"""
    import re as _re
    out = {}
    try:
        with open(EVENTS_CSV, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("type") != "自結" or r.get("co_id") != stock_id:
                    continue
                m = _re.match(r'(\d{4})\s*Q(\d)', r.get("qtr_label") or "")
                if not m:
                    continue
                yq = (int(m.group(1)), int(m.group(2)))
                if yq > after_yq:
                    out[yq] = r   # 同季多筆取較後（較新公告）
    except OSError:
        pass
    return sorted(out.items(), reverse=True)


def build_company_block(stock_id, name, rows, quarters, show_all):
    # 同一 (年,季) 若有重複紀錄，只留 fetched_at 最新的一筆（防禦，同 F22 查詢）
    best = {}
    for r in rows:
        k = (int(r["report_year"]), int(r["report_quarter"]))
        if k not in best or (r.get("fetched_at") or "") > (best[k].get("fetched_at") or ""):
            best[k] = r
    # 依 年/季 排序（新到舊）
    rows = sorted(best.values(), key=lambda r: (int(r["report_year"]), int(r["report_quarter"])), reverse=True)
    if not show_all:
        rows = rows[:quarters]
    # 補出範圍內缺少的季度（顯示「無資料」），忠實反映缺口
    if rows:
        seq, ym = [], (int(rows[0]["report_year"]), int(rows[0]["report_quarter"]))
        end = (int(rows[-1]["report_year"]), int(rows[-1]["report_quarter"]))
        while ym >= end:
            seq.append(ym)
            y, q = ym
            ym = (y, q - 1) if q > 1 else (y - 1, 4)
    else:
        seq = []
    row_by_yq = {(int(r["report_year"]), int(r["report_quarter"])): r for r in rows}
    lines = [f"📊 F27 {stock_id} {name} 季報損益（百萬）"]
    # 財報還沒出的季度，先列自結快數（財報公布後自動改列財報）
    newest = (int(rows[0]["report_year"]), int(rows[0]["report_quarter"])) if rows else (0, 0)
    for (jy, jq), jr in _jishi_newer_than(stock_id, newest):
        def _jv(col):
            s = (jr.get(col) or "").replace(",", "").strip()
            return s if s else "—"
        lines.append(f"{str(jy)[2:]}Q{jq}（自結快數，{jr.get('announce_date','')} 公告）")
        lines.append(f"營收 {_jv('季_營收')}｜稅前 {_jv('季_稅前')}｜母利 {_jv('季_母利')}｜EPS {_jv('季_EPS')}")
    for yq in seq:
        if yq not in row_by_yq:
            lines.append(f"{str(yq[0])[2:]}Q{yq[1]} 無資料")
            continue
        r = row_by_yq[yq]
        year_short = str(r["report_year"])[2:]  # "2026" → "26"
        q_tag = f"{year_short}Q{r['report_quarter']}"
        q_rev = F27._fmt_m(F27._single_q_rev(stock_id, int(r["report_year"]), int(r["report_quarter"])))
        op = F27._fmt_m(to_int(r["operating_income"]))
        pretax = F27._fmt_m(to_int(r["pretax_income"]))
        ni = F27._fmt_m(to_int(r["net_income_parent"]))
        eps = F27._fmt_eps(to_float(r["eps"]))
        lines.append(q_tag)
        lines.append(f"單季營收 {q_rev}")
        lines.append(f"營益 {op}")
        lines.append(f"稅前 {pretax}")
        lines.append(f"淨利(母) {ni}")
        lines.append(f"EPS {eps}")
    return "\n".join(lines)


def main():
    queries, quarters, show_all, emit = parse_args()

    if not os.path.exists(F27.CSV_PATH):
        sys.stderr.write(f"❌ 找不到 {F27.CSV_PATH}\n")
        sys.exit(1)

    all_rows = list(csv.DictReader(open(F27.CSV_PATH, encoding="utf-8")))

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
            blocks.append((sid, name, build_company_block(sid, name, rows, quarters, show_all)))

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

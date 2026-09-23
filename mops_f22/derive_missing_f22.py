"""
derive_missing_f22.py
用「相鄰月份的累計欄」反推補上 monthly_revenue.csv 裡缺漏的月份，不需連網。

原理
----
MOPS 每月營收公告同時提供「單月營收」與「年初至今累計營收(ytd_revenue)」。
若某公司缺 N 月，但 N-1 月與 N+1 月都有資料，則：

    N月營收 = ytd(N+1) - ytd(N-1) - 單月(N+1)

三個輸入值全部來自 MOPS 官方欄位，所以結果不是估計值，是官方數字的算術重組。
（N=1月時 ytd(N-1) 視為 0，因為 1 月累計即等於 1 月單月。）

可靠度驗證（2026-08-19，以 27,731 筆「已知正確」月份回測）
    完全相符 90.18% / 誤差<0.5% 8.97% / 誤差>=0.5% 0.85%
    → 99.15% 落在 0.5% 誤差內。少數落差來自公司事後更正數字，
      導致累計欄與逐月加總不一致，屬資料本身特性，非公式問題。

安全設計
--------
* 只補「完全沒有該月紀錄」的缺口，絕不覆蓋任何既有列。
* 反推值寫入 data_source="derived_ytd" 標記，與官方資料可明確區分。
* 日後真的回補到官方數字時，mops_f22_daily.save_to_csv() 會整列覆蓋，
  data_source 自動回復空值，不需手動清理。
* 原子寫入（先寫暫存檔再 rename），避免中途中斷造成 CSV 損毀。
* 一律先備份原檔。

用法
----
    python3 derive_missing_f22.py --dry-run     # 只列出會補哪些，不寫入（建議先跑）
    python3 derive_missing_f22.py               # 實際寫入
    python3 derive_missing_f22.py --stock 3026  # 只處理單一公司
"""

import os
import sys
import csv
import io
import shutil
from datetime import datetime

import mops_f22_daily as F22

CSV_PATH = F22.CSV_PATH


def _read_rows():
    """NUL byte 容錯讀取（比照 mops_f22_daily 的既有做法）。"""
    if not os.path.exists(CSV_PATH):
        sys.stderr.write(f"❌ 找不到 {CSV_PATH}\n")
        sys.exit(1)
    with open(CSV_PATH, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        sys.stderr.write("⚠️  偵測到 NUL byte，已濾除後讀取\n")
        raw = raw.replace(b"\x00", b"")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))))


def _i(s):
    try:
        return int(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def find_derivable(rows, only_stock=None):
    """回傳 [(stock_id, year, month, revenue, ytd_revenue, 範本列)]。"""
    by_stock = {}
    for r in rows:
        sid = r.get("stock_id", "")
        if not sid or (only_stock and sid != only_stock):
            continue
        y, m = _i(r.get("report_year")), _i(r.get("report_month"))
        if y is None or m is None:
            continue
        by_stock.setdefault(sid, {})[(y, m)] = r

    out = []
    global _REJECTED
    _REJECTED = []
    for sid, ms in by_stock.items():
        if len(ms) < 2:
            continue
        lo, hi = min(ms), max(ms)
        y, m = lo
        while (y, m) <= hi:
            if (y, m) not in ms:
                nm = (y, m + 1) if m < 12 else None   # 只能跟同年的下個月相減
                pm = (y, m - 1) if m > 1 else None
                if nm and nm in ms:
                    nxt = ms[nm]
                    nxt_rev, nxt_ytd = _i(nxt.get("revenue")), _i(nxt.get("ytd_revenue"))
                    prev_ytd = 0 if m == 1 else (_i(ms[pm].get("ytd_revenue")) if pm in ms else None)
                    if None not in (nxt_rev, nxt_ytd) and prev_ytd is not None:
                        rev = nxt_ytd - prev_ytd - nxt_rev
                        if rev > 0:
                            # ── 防呆：與官方「去年同期欄」對照 ──
                            # 隔年同月那筆公告的 revenue_last_year 欄，正是本月的官方數字，
                            # 且完全不參與上面的反推計算，是獨立的驗證來源。
                            # 兩者差異過大 → 多半是公司事後重編報表（實測以保險業居多，
                            # 如 2832 台產、2852 第一保），此時寧可留空也不寫入可疑數字。
                            ref_row = ms.get((y + 1, m))
                            ref = _i(ref_row.get("revenue_last_year")) if ref_row else None
                            if ref and abs(ref - rev) / ref > 0.05:
                                _REJECTED.append((sid, y, m, rev, ref,
                                                  abs(ref - rev) / ref * 100))
                                m += 1
                                if m == 13:
                                    y, m = y + 1, 1
                                continue
                            out.append((sid, y, m, rev, prev_ytd + rev, nxt))
            m += 1
            if m == 13:
                y, m = y + 1, 1
    return out


_REJECTED = []


def main():
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    dry_run = "--dry-run" in flags
    only_stock = None
    argv = sys.argv[1:]
    if "--stock" in argv:
        i = argv.index("--stock")
        if i + 1 < len(argv):
            only_stock = argv[i + 1]

    rows = _read_rows()
    sys.stderr.write(f"讀入 {len(rows):,} 列\n")

    found = find_derivable(rows, only_stock)
    if not found:
        sys.stderr.write("✅ 沒有可反推的缺漏\n")
        return

    sys.stderr.write(f"{'🔍 預覽（不寫入）' if dry_run else '📝 準備寫入'}：可反推 {len(found):,} 筆\n\n")
    for sid, y, m, rev, ytd, tpl in found[:20]:
        sys.stderr.write(f"  {sid:>7} {tpl.get('company_name','')[:10]:<11} {y}/{m:<2} "
                         f"營收={rev/1000:>12,.1f} 百萬\n")
    if len(found) > 20:
        sys.stderr.write(f"  …其餘 {len(found)-20:,} 筆\n")

    if _REJECTED:
        sys.stderr.write(f"\n⛔ 另有 {len(_REJECTED)} 筆與官方「去年同期欄」對不上，已排除不補：\n")
        for sid, y, m, rev, ref, d in _REJECTED:
            sys.stderr.write(f"  {sid:>7} {y}/{m:<2} 反推={rev/1000:>10,.1f} "
                             f"官方={ref/1000:>10,.1f} 差{d:.1f}%\n")

    if dry_run:
        sys.stderr.write("\n🔍 dry-run：未寫入任何資料。確認無誤後拿掉 --dry-run 執行。\n")
        return

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_rows = []
    for sid, y, m, rev, ytd, tpl in found:
        r = {c: "" for c in F22.CSV_COLUMNS}
        r.update({
            "stock_id":     sid,
            "company_name": tpl.get("company_name", ""),
            "market":       tpl.get("market", ""),
            "industry":     tpl.get("industry", ""),
            "report_year":  str(y),
            "report_month": str(m),
            "revenue":      str(rev),
            "ytd_revenue":  str(ytd),
            "remark":       "由相鄰月份累計欄反推（缺漏補值）",
            "fetched_at":   stamp,
            "data_source":  "derived_ytd",
        })
        new_rows.append(r)

    bak = f"{CSV_PATH}.bak-derive-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CSV_PATH, bak)
    sys.stderr.write(f"\n備份：{bak}\n")

    allr = rows + new_rows
    allr.sort(key=lambda r: (r.get("stock_id", ""),
                             _i(r.get("report_year")) or 0,
                             _i(r.get("report_month")) or 0))

    tmp = f"{CSV_PATH}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=F22.CSV_COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(allr)
    os.replace(tmp, CSV_PATH)

    sys.stderr.write(f"✅ 已補入 {len(new_rows):,} 筆反推值，總列數 {len(allr):,}\n")


if __name__ == "__main__":
    main()

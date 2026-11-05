"""
fix_eps_cumulative.py
一次性修正 quarterly_revenue.csv 歷史資料的 EPS 口徑（Q2/Q3）。

問題
----
mops_f27_daily.py 的 eps() 原本寫死取 v[0]，但 MOPS 季報表頭欄序隨季別變動：
    Q1   ：[本期累計, 本期單季, 去年累計, 去年單季] → 第0欄是累計（剛好對）
    Q2/Q3：[本期單季, 去年單季, 本期累計, 去年累計] → 第0欄是單季（取錯）
金額欄位走 amt(..., I_CUM) 依表頭判斷，故不受影響；只有 eps/eps_diluted 出錯。

實證（2026-10-30 全庫盤點，以 Q1 推算股數為基準逐列檢驗）：
    Q2：單季 3,622 筆 / 累計    86 筆  → 確為單季
    Q3：單季 1,987 筆 / 累計    23 筆  → 確為單季
    Q4：單季    20 筆 / 累計 1,765 筆  → 正確（年報無單季欄，I_Q 退回 I_CUM）

修法
----
原 eps 值搬到新欄 eps_q（單季），再用「前一季累計 + 本季單季」鏈式還原累計 EPS：
    Q1累計 = 原值（本來就對）
    Q2累計 = Q1累計 + Q2單季
    Q3累計 = Q2累計 + Q3單季
必須依 Q1→Q2→Q3 順序處理，因為 Q3 依賴修正後的 Q2。

Q1、Q4 不動。缺前一季而無法鏈式還原者保持原狀並列入報告。

用法
----
    python3 fix_eps_cumulative.py --dry-run    # 只報告，不寫入（建議先跑）
    python3 fix_eps_cumulative.py              # 實際修正（會先備份）
"""

import os
import sys
import csv
import io
import shutil
import collections
from datetime import datetime

import mops_f27_daily as F27

CSV_PATH = F27.CSV_PATH


def _f(v):
    try:
        return float((v or "").replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _read_rows():
    with open(CSV_PATH, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        sys.stderr.write("⚠️  偵測到 NUL byte，已濾除後讀取\n")
        raw = raw.replace(b"\x00", b"")
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace"))))


def main():
    dry_run = "--dry-run" in sys.argv
    rows = _read_rows()
    sys.stderr.write(f"讀入 {len(rows):,} 列\n")

    by = collections.defaultdict(dict)
    for r in rows:
        try:
            by[r["stock_id"]][(int(r["report_year"]), int(r["report_quarter"]))] = r
        except (ValueError, KeyError):
            pass

    stat = collections.Counter()
    samples = []

    for sid, d in by.items():
        for y in sorted({k[0] for k in d}):
            # 依 Q1→Q2→Q3 順序鏈式還原（Q3 依賴修正後的 Q2）
            cum, cum_dil = None, None
            q1 = d.get((y, 1))
            if q1:
                cum, cum_dil = _f(q1.get("eps")), _f(q1.get("eps_diluted"))

            for q in (2, 3):
                r = d.get((y, q))
                if not r:
                    cum = cum_dil = None     # 斷鏈，後續季別無法還原
                    continue
                single, single_dil = _f(r.get("eps")), _f(r.get("eps_diluted"))
                if single is None:
                    stat["跳過：無eps"] += 1
                    cum = cum_dil = None
                    continue
                if cum is None:
                    stat[f"Q{q} 無法還原（缺前一季）"] += 1
                    continue

                new_cum = round(cum + single, 2)
                new_cum_dil = (round(cum_dil + single_dil, 2)
                               if (cum_dil is not None and single_dil is not None) else None)

                if len(samples) < 8:
                    samples.append((sid, y, q, single, new_cum))

                if not dry_run:
                    r["eps_q"] = single
                    r["eps"] = new_cum
                    if single_dil is not None:
                        r["eps_q_diluted"] = single_dil
                    if new_cum_dil is not None:
                        r["eps_diluted"] = new_cum_dil

                stat[f"Q{q} 已還原"] += 1
                cum, cum_dil = new_cum, new_cum_dil

    sys.stderr.write("\n處理結果：\n")
    for k, v in sorted(stat.items()):
        sys.stderr.write(f"  {k:<26} {v:>6,}\n")

    sys.stderr.write("\n範例（單季 → 還原後累計）：\n")
    for sid, y, q, s, c in samples:
        sys.stderr.write(f"  {sid} {y}Q{q}  eps {s} → 累計 {c}（eps_q 保留 {s}）\n")

    if dry_run:
        sys.stderr.write("\n🔍 dry-run：未寫入任何資料。確認無誤後拿掉 --dry-run 執行。\n")
        return

    bak = f"{CSV_PATH}.bak-epsfix-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CSV_PATH, bak)
    sys.stderr.write(f"\n備份：{bak}\n")

    tmp = f"{CSV_PATH}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=F27.CSV_COLUMNS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CSV_PATH)
    sys.stderr.write(f"✅ 已寫入，總列數 {len(rows):,}\n")


if __name__ == "__main__":
    main()

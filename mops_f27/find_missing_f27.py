"""
find_missing_f27.py
用 ezsearch 清單比對 quarterly_revenue.csv，找出「已公告但 CSV 缺漏」的公司，
直接塞進 failed_fetches.csv，之後用 refetch_failed_f27.py 只重抓這些缺漏。

為什麼需要它：
  之前若有整批因限流（307）抓不到、又沒被記錄的公司，重跑整個區間會再次觸發限流。
  本工具只打「清單 API」（很輕、不抓明細），算出缺哪些，不必重抓已成功的部分。

用法：
  python3 find_missing_f27.py 2026-01-01 2026-06-09        # 掃這段期間的公告，找缺漏
  python3 find_missing_f27.py 2026-02-20 2026-03-15        # 只補某段（如限流最嚴重那段）

跑完看終端機統計，再執行：
  F27_DELAY=2.0 F27_BACKOFF=5 python3 refetch_failed_f27.py
"""

import os
import sys
import csv
import time
from datetime import datetime, timedelta

import mops_f27_daily as F27


def parse_args():
    a = [x for x in sys.argv[1:] if not x.startswith("--")]
    if len(a) < 2:
        sys.stderr.write("用法：python3 find_missing_f27.py 起日 迄日（YYYY-MM-DD）\n")
        sys.exit(1)
    s = datetime.strptime(a[0], "%Y-%m-%d")
    e = datetime.strptime(a[1], "%Y-%m-%d")
    if s > e:
        s, e = e, s
    return s, e


def load_csv_keys():
    keys = set()
    if os.path.exists(F27.CSV_PATH):
        with open(F27.CSV_PATH, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                keys.add((r["stock_id"], str(r["report_year"]), str(r["report_quarter"])))
    return keys


def main():
    sdate, edate = parse_args()
    import requests

    have = load_csv_keys()
    # 已知解不出的（金融保險、外國/TDR 等）也算「已涵蓋」，避免每次掃缺口都重複撈、重抓白做工
    unparsed = set(F27._read_log(F27.UNPARSED_LOG).keys())
    covered = have | unparsed
    sys.stderr.write(f"CSV 現有 {len(have)} 筆，已知解不出 {len(unparsed)} 筆。"
                     f"掃描 {sdate:%Y-%m-%d} ~ {edate:%Y-%m-%d} 的公告…\n")

    announced = {}   # key -> item（去重）
    with requests.Session() as session:
        d = sdate
        while d <= edate:
            day_items = []
            for market in F27.MARKETS:
                day_items += F27.fetch_f27_list(session, d, market)
                time.sleep(F27.SLEEP_LIST)
            for it in day_items:
                k = (str(it["stock_id"]), str(it["report_year"]), str(it["report_quarter"]))
                announced[k] = it
            if day_items:
                sys.stderr.write(f"  {d:%Y-%m-%d}: 公告 {len(day_items)} 筆\n")
            d += timedelta(days=1)

    missing = {k: it for k, it in announced.items() if k not in covered}
    sys.stderr.write(f"\n公告共 {len(announced)} 筆，其中 CSV 缺漏 {len(missing)} 筆。\n")

    if not missing:
        sys.stderr.write("✅ 沒有缺漏，CSV 已涵蓋此區間所有公告。\n")
        return

    # 併入 failed_fetches.csv（reason=missing），供 refetch 重抓
    failed = F27._read_log(F27.FAILED_LOG)
    for k, it in missing.items():
        failed[k] = F27._item_to_logrow(it, "missing")
    F27._write_log(F27.FAILED_LOG, failed)

    sys.stderr.write(f"已寫入 {F27.FAILED_LOG}（目前待重抓共 {len(failed)} 筆）。\n")
    sys.stderr.write("接著執行：F27_DELAY=2.0 F27_BACKOFF=5 python3 refetch_failed_f27.py\n")


if __name__ == "__main__":
    main()

"""
refetch_failed_f27.py
重抓 failed_fetches.csv 裡先前因限流（多為 307 擋頁）抓不到的公司。

設計：
  - 只重抓「抓取失敗」的（金融保險業那種解析不出的在 unparseable.csv，不在這裡）。
  - 成功者寫進 quarterly_revenue.csv，並自動從 failed_fetches.csv 移除。
  - 仍失敗者留在 failed_fetches.csv，可隔一段時間（等 MOPS 限流解除）再跑一次。
  - 預設用較慢的速率，降低再次被擋的機率。

用法：
  # 建議等個 10~30 分鐘讓限流解除後再跑；用較慢速率
  F27_DELAY=2.0 F27_BACKOFF=5 python3 refetch_failed_f27.py

  # 看還剩多少筆待重抓（不抓，只統計）
  python3 refetch_failed_f27.py --status
"""

import os
import sys
import time
import requests

import mops_f27_daily as F27


def main():
    if "--status" in sys.argv:
        log = F27._read_log(F27.FAILED_LOG)
        print(f"failed_fetches.csv 目前有 {len(log)} 筆待重抓")
        # 依年/季統計
        bucket = {}
        for r in log.values():
            k = f"{r['report_year']}Q{r['report_quarter']}"
            bucket[k] = bucket.get(k, 0) + 1
        for k in sorted(bucket):
            print(f"   {k}: {bucket[k]}")
        return

    log = F27._read_log(F27.FAILED_LOG)
    if not log:
        sys.stderr.write("✅ failed_fetches.csv 沒有待重抓的項目。\n")
        return

    items = [F27.logrow_to_item(r) for r in log.values()]
    sys.stderr.write(f"開始重抓 {len(items)} 筆（速率：間隔 {F27.SLEEP_DETAIL}s、退避起始 {F27.BACKOFF_BASE}s）…\n")

    with requests.Session() as session:
        results, fetch_failed, parse_none = F27.process_items(session, items)

    written = F27.save_to_csv(results)
    F27.update_logs(results, fetch_failed, parse_none)

    sys.stderr.write(
        f"\n✅ 重抓完成：成功 {len(results)} 筆（CSV 新寫入 {written}），"
        f"仍失敗 {len(fetch_failed)} 筆，解析不出 {len(parse_none)} 筆。\n"
    )
    if fetch_failed:
        sys.stderr.write(
            f"   還有 {len(fetch_failed)} 筆留在 failed_fetches.csv，"
            f"建議再等久一點、用更慢速率（如 F27_DELAY=3 F27_BACKOFF=8）再跑一次。\n"
        )


if __name__ == "__main__":
    main()

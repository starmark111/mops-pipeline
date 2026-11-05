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

  # 重抓完成後，接著補發 Telegram 訊息（比照 check_backfill_f22.py 的 --send）
  python3 refetch_failed_f27.py --send
"""

import os
import re
import sys
import time
import subprocess
import requests
from datetime import datetime, timedelta

import mops_f27_daily as F27


# =========================
# --send：補發重抓資料的 Telegram 訊息
# =========================
# 為什麼需要：本支只寫 CSV，而每日排程只看「當天新抓到的公告」、不回頭掃 CSV，
# 因此靠重抓救回來的資料，訊息永遠不會發出。加上 --send 才算真正完成
# 「有公告就一定收到訊息」。作法與 check_backfill_f22.py 一致。
#
# 兩道安全閥（2026-09-30 加入）：
#   1. SEND_MAX_DAYS：只補發近 N 天的公告。failed_fetches.csv 會累積數月舊資料
#      （實測橫跨 17 個月、337 則），全送會被舊季報洗版。
#   2. SEND_WARN_LIMIT：預估則數超過上限就停下來只報告，需加 --force 才真的送。
SEND_MAX_DAYS   = 30
SEND_WARN_LIMIT = 50


def _do_send(results):
    """呼叫既有的 backfill_send_f27.py 補發訊息（格式／分群／去重全部沿用，
    不另寫一套；sent_state_f27.json 會自動跳過已發送者，可安全重跑）。"""
    emit  = "--emit" in sys.argv
    force = "--force" in sys.argv
    sys.stderr.write("\n📨 --send：補發重抓資料的 Telegram 訊息…\n")

    all_dates = sorted({it.get("announce_date") for it, _ in results if it.get("announce_date")})
    if not all_dates:
        sys.stderr.write("  （重抓資料無公告日期，略過補發）\n")
        return

    # ── 安全閥 1：日期範圍 ──
    cutoff  = (datetime.now() - timedelta(days=SEND_MAX_DAYS)).strftime("%Y-%m-%d")
    dates   = all_dates if force else [d for d in all_dates if d >= cutoff]
    skipped = [] if force else [d for d in all_dates if d < cutoff]
    if skipped:
        sys.stderr.write(
            f"  ⏳ 略過 {len(skipped)} 個超過 {SEND_MAX_DAYS} 天的公告日"
            f"（{skipped[0]} ~ {skipped[-1]}），避免舊資料洗版。\n"
            f"     要補發這些請加 --force。\n")
    if not dates:
        sys.stderr.write("  （近期無可補發的公告）\n")
        return

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_send_f27.py")
    base   = [sys.executable, script, dates[0], dates[-1]]

    # ── 安全閥 2：先 dry-run 預估則數（dry-run 不寫 sent_state，安全）──
    if not force:
        probe = subprocess.run(base + ["--dry-run"], capture_output=True, text=True)
        n = sum(int(m.group(1)) for m in re.finditer(r"本日 (\d+) 則", probe.stderr))
        if n > SEND_WARN_LIMIT:
            sys.stderr.write(
                f"  ⚠️  預估會送出 {n} 則（超過上限 {SEND_WARN_LIMIT}），已停止未發送。\n"
                f"     區間 {dates[0]} ~ {dates[-1]}。確認要送請加 --force，或先手動跑：\n"
                f"     python3 backfill_send_f27.py {dates[0]} {dates[-1]} --dry-run\n")
            return
        sys.stderr.write(f"  預估 {n} 則，開始補發…\n")

    cmd = base + (["--emit"] if emit else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(r.stderr)
    if emit and r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode != 0:
        sys.stderr.write(f"⚠️  補發失敗（return code {r.returncode}），資料已寫入但訊息未送出\n")


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

    if "--send" in sys.argv and results:
        _do_send(results)


if __name__ == "__main__":
    main()

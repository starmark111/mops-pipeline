"""
find_missing_f27.py
用 ezsearch 清單比對 quarterly_revenue.csv，找出「已公告但 CSV 缺漏」的公司，
直接塞進 failed_fetches.csv，之後用 refetch_failed_f27.py 只重抓這些缺漏。

為什麼需要它：
  之前若有整批因限流（307）抓不到、又沒被記錄的公司，重跑整個區間會再次觸發限流。
  本工具只打「清單 API」（很輕、不抓明細），算出缺哪些，不必重抓已成功的部分。

本支即 F27 的「回補檢查」單一入口，與 F22 的 check_backfill_f22.py 同形：
一個指令做完「查漏 → 回補 → 補發訊息 → 輸出摘要」，n8n 只需一個節點。

用法：
  python3 find_missing_f27.py 2026-01-01 2026-06-09        # 只查漏（寫入 failed_fetches.csv）
  python3 find_missing_f27.py --days 2                     # 近 N 天（預設 7）
  python3 find_missing_f27.py --days 2 --send              # 查漏 + 重抓 + 補發 Telegram
  python3 find_missing_f27.py --days 2 --send --emit       # 同上，並印摘要 JSON 給 n8n
  python3 find_missing_f27.py --days 2 --dry-run           # 只報告，不寫 failed_fetches.csv

旗標說明（與 check_backfill_f22.py 一致）：
  --dry-run  只查不寫、不重抓、不補發
  --send     查漏後自動重抓缺漏，並補發 Telegram 訊息
  --emit     印出「回補摘要」JSON 給 n8n（不論有無缺漏都會印，便於確認有跑過）
  --force    略過補發的兩道安全閥（日期 30 天、則數 50 則）

單獨重抓 failed_fetches.csv 的舊帳仍可用：
  F27_DELAY=2.0 F27_BACKOFF=5 python3 refetch_failed_f27.py
"""

import os
import re
import sys
import csv
import json
import time
import subprocess
from datetime import datetime, timedelta

import mops_f27_daily as F27

# 補發安全閥（與 check_backfill_f22.py 相同）
SEND_MAX_DAYS   = 30
SEND_WARN_LIMIT = 50


def parse_args():
    argv = sys.argv[1:]
    a = [x for x in argv if not x.startswith("--")]
    days = 7
    if "--days" in argv:
        i = argv.index("--days")
        if i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
                a = [x for x in a if x != argv[i + 1]]
            except ValueError:
                pass
    for f in argv:
        if f.startswith("--days="):
            try:
                days = int(f.split("=", 1)[1])
            except ValueError:
                pass

    if len(a) >= 2:
        s = datetime.strptime(a[0], "%Y-%m-%d")
        e = datetime.strptime(a[1], "%Y-%m-%d")
        if s > e:
            s, e = e, s
    else:
        e = datetime.now()
        s = e - timedelta(days=max(days, 1) - 1)
    return s, e


def load_csv_keys():
    keys = set()
    if os.path.exists(F27.CSV_PATH):
        with open(F27.CSV_PATH, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                keys.add((r["stock_id"], str(r["report_year"]), str(r["report_quarter"])))
    return keys


def audit_watchlist_delivery(announced):
    """清單覆蓋檢查：本次 MOPS 掃到的公告中，屬於關注清單的公司是否都已發送。

    核心要求「清單內公司只要有公告就一定要收到訊息」的最終驗證。
    前面的查漏只管 MOPS→CSV，這裡補上 CSV→Telegram 那一段。

    ⚠️ 基準用「本次 MOPS 掃到的公告」(announced)，不是 CSV 的 announce_date——
       因為公司可能重複公告（同一季再發一次），CSV 只留最初那筆的日期，
       用 CSV 日期去撈會撈不到，導致摘要出現「MOPS 公告 7 筆」卻「清單內無公告」
       這種自相矛盾的結果。

    回傳 (未發送清單, 清單命中總數)。
    """
    import io as _io
    F27._load_watchlist()

    try:
        with open(F27.SENT_STATE_PATH, encoding="utf-8") as f:
            sent = set(json.load(f).keys())
    except Exception:
        sent = set()

    # CSV 的 ytd_revenue 是組 sent_state key 的必要欄位，建索引備查
    csv_idx = {}
    try:
        with open(F27.CSV_PATH, "rb") as f:
            raw = f.read().replace(b"\x00", b"")
        for r in csv.DictReader(_io.StringIO(raw.decode("utf-8", errors="replace"))):
            csv_idx[(r["stock_id"], str(r["report_year"]), str(r["report_quarter"]))] = r
    except Exception:
        pass

    unsent, total = [], 0
    oldest = None
    for (sid, y, q), it in announced.items():
        groups = F27.get_groups(sid, it.get("company_name", ""))
        if not groups:          # 不在關注清單，本來就不該發
            continue
        total += 1
        d = it.get("announce_date", "")
        if d and (oldest is None or d < oldest):
            oldest = d
        row = csv_idx.get((sid, str(y), str(q)))
        key = f"{sid}|{y}|{q}|{row.get('ytd_revenue','')}" if row else None
        if key is None or key not in sent:
            unsent.append((sid, it.get("company_name", ""), "／".join(groups)))

    # sent_state 只保留 SENT_KEEP_DAYS 天（F27=100），更舊的紀錄會被自動清除。
    # 若掃描區間早於保留期，「未發送」判定不可信——當時可能發過但紀錄已過期。
    cutoff = (datetime.now() - timedelta(days=F27.SENT_KEEP_DAYS)).strftime("%Y-%m-%d")
    stale = bool(oldest and oldest < cutoff)
    return unsent, total, stale


def build_summary(range_label, announced, missing, refetched, sent_n, dry_run, audit=None) -> str:
    """回補摘要訊息（格式比照 F22 的 build_summary，讓兩邊訊息長相一致）。"""
    lines = [f"🔁 F27 回補檢查 {range_label}",
             f"MOPS 公告 {len(announced)} 筆 / "
             f"{'偵測缺漏' if dry_run else '回補'} {len(missing)} 筆"]

    if missing:
        lines.append("── 補回清單 ──")
        for k, it in list(missing.items())[:30]:
            lines.append(f"  {it['stock_id']} {it['company_name']} "
                         f"{it['report_year']}Q{it['report_quarter']}"
                         f"（公告 {it.get('announce_date','?')}）")
        if len(missing) > 30:
            lines.append(f"  …等共 {len(missing)} 筆")

    if dry_run:
        lines.append("🔍 dry-run：未寫入、未重抓、未補發")
    elif missing:
        if sent_n is None:
            lines.append("（未加 --send，訊息未補發）")
        elif sent_n == 0:
            lines.append("📨 無需補發（皆已發送過或被安全閥擋下）")
        else:
            lines.append(f"📨 已補發 {sent_n} 則訊息")
    else:
        lines.append("✅ 區間內無缺漏，資料完整")

    # ── 清單覆蓋檢查：核心要求「清單內有公告就一定要收到訊息」的最終驗證 ──
    if audit is not None:
        unsent, total, stale = audit
        if not total:
            lines.append("👤 清單內本期無公告")
        elif stale:
            lines.append(f"👤 清單覆蓋 {total} 檔（區間超過 {F27.SENT_KEEP_DAYS} 天，"
                         f"發送紀錄已過期，無法判定）")
        elif not unsent:
            lines.append(f"👤 清單覆蓋 {total} 檔，全部已發送 ✅")
        else:
            names = "、".join(f"{s} {n}" for s, n, _ in unsent[:8])
            more  = f" 等{len(unsent)}檔" if len(unsent) > 8 else ""
            lines.append(f"👤 ⚠️ 清單內 {len(unsent)}/{total} 檔有資料卻未發送：{names}{more}")
            lines.append("   → 補發：python3 backfill_send_f27.py <起日> <迄日>")

    return "\n".join(lines)


def _run_refetch():
    """呼叫既有的 refetch_failed_f27.py 重抓缺漏（不複製重抓邏輯）。
    回傳成功筆數，解析自其 stderr 的「成功 N 筆」。"""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refetch_failed_f27.py")
    env = dict(os.environ)
    env.setdefault("F27_DELAY", "2.0")      # 預設放慢，降低被 MOPS 限流的機率
    env.setdefault("F27_BACKOFF", "5")
    r = subprocess.run([sys.executable, script], capture_output=True, text=True, env=env)
    sys.stderr.write(r.stderr)
    m = re.search(r"成功 (\d+) 筆", r.stderr)
    return int(m.group(1)) if m else 0


def _do_send(missing, emit):
    """補發 Telegram 訊息：呼叫既有的 backfill_send_f27.py，格式／分群／去重全部沿用。
    兩道安全閥與 check_backfill_f22.py 相同：
      1. 只補發近 SEND_MAX_DAYS 天的公告（避免舊季報洗版）
      2. 預估則數超過 SEND_WARN_LIMIT 就停下只報告（需 --force）
    回傳實際補發則數（被擋下或無需補發回傳 0）。"""
    force = "--force" in sys.argv
    all_dates = sorted({it.get("announce_date") for it in missing.values() if it.get("announce_date")})
    if not all_dates:
        sys.stderr.write("  （缺漏資料無公告日期，略過補發）\n")
        return 0

    cutoff  = (datetime.now() - timedelta(days=SEND_MAX_DAYS)).strftime("%Y-%m-%d")
    dates   = all_dates if force else [d for d in all_dates if d >= cutoff]
    skipped = [] if force else [d for d in all_dates if d < cutoff]
    if skipped:
        sys.stderr.write(f"  ⏳ 略過 {len(skipped)} 個超過 {SEND_MAX_DAYS} 天的公告日"
                         f"（{skipped[0]} ~ {skipped[-1]}）。要補發請加 --force。\n")
    if not dates:
        sys.stderr.write("  （近期無可補發的公告）\n")
        return 0

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_send_f27.py")
    base   = [sys.executable, script, dates[0], dates[-1]]

    if not force:
        probe = subprocess.run(base + ["--dry-run"], capture_output=True, text=True)
        n = sum(int(m.group(1)) for m in re.finditer(r"本日 (\d+) 則", probe.stderr))
        if n > SEND_WARN_LIMIT:
            sys.stderr.write(f"  ⚠️  預估會送出 {n} 則（超過上限 {SEND_WARN_LIMIT}），已停止未發送。\n"
                             f"     區間 {dates[0]} ~ {dates[-1]}，確認要送請加 --force。\n")
            return 0
        sys.stderr.write(f"  預估 {n} 則，開始補發…\n")

    cmd = base + (["--emit"] if emit else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(r.stderr)
    if emit and r.stdout.strip():
        print(r.stdout.strip())     # 補發訊息的 JSON，n8n 可一併取用
    if r.returncode != 0:
        sys.stderr.write(f"⚠️  補發失敗（return code {r.returncode}），資料已寫入但訊息未送出\n")
        return 0
    return sum(int(m.group(1)) for m in re.finditer(r"本日 (\d+) 則", r.stderr))


def main():
    sdate, edate = parse_args()
    dry_run = "--dry-run" in sys.argv
    send    = "--send" in sys.argv
    emit    = "--emit" in sys.argv
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

    range_label = f"{sdate:%m/%d} ~ {edate:%m/%d}"
    refetched, sent_n = 0, (0 if send else None)

    if not missing:
        sys.stderr.write("✅ 沒有缺漏，CSV 已涵蓋此區間所有公告。\n")
    elif dry_run:
        sys.stderr.write("🔍 dry-run：未寫入 failed_fetches.csv、未重抓、未補發。\n")
    else:
        # 併入 failed_fetches.csv（reason=missing），供 refetch 重抓
        failed = F27._read_log(F27.FAILED_LOG)
        for k, it in missing.items():
            failed[k] = F27._item_to_logrow(it, "missing")
        F27._write_log(F27.FAILED_LOG, failed)
        sys.stderr.write(f"已寫入 {F27.FAILED_LOG}（目前待重抓共 {len(failed)} 筆）。\n")

        if send:
            sys.stderr.write("\n🛠️  --send：重抓缺漏…\n")
            refetched = _run_refetch()
            sys.stderr.write("\n📨 --send：補發 Telegram 訊息…\n")
            sent_n = _do_send(missing, emit)
        else:
            sys.stderr.write("接著執行：F27_DELAY=2.0 F27_BACKOFF=5 python3 refetch_failed_f27.py\n")

    # 摘要（--emit 一律輸出，即使無缺漏也印，便於確認排程有跑過）
    audit = audit_watchlist_delivery(announced)
    msg = build_summary(range_label, announced, missing, refetched, sent_n, dry_run, audit)
    if emit:
        print(json.dumps([{"message": msg, "group": "f27_backfill"}], ensure_ascii=False))
    else:
        sys.stderr.write("\n" + msg + "\n")


if __name__ == "__main__":
    main()

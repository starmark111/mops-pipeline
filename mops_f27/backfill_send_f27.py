"""
backfill_send_f27.py
補發指定日期區間的 F27 季報損益訊息到 Telegram，並可給 n8n 用。

走法與 backfill_send_f22.py 完全一致：
  - 預設讀 quarterly_revenue.csv 組訊息（不碰網路，瞬間完成）
  - 加 --fetch 才重新去 MOPS 抓（會更新 CSV）
  - 三種輸出：--emit（印 JSON 給 n8n）/ --dry-run（人類預覽）/ 預設（直接發 Telegram）

用法：
  python3 backfill_send_f27.py 2026-05-14 2026-05-16 --dry-run
  python3 backfill_send_f27.py 2026-05-14 2026-05-16 --fetch --emit   # n8n 排程用
  export TELEGRAM_BOT_TOKEN="123456:ABC..."
  python3 backfill_send_f27.py 2026-05-14 2026-05-16                  # 直接發
"""

import os
import sys
import csv
import json
import time
import requests
from datetime import datetime, timedelta

import mops_f27_daily as F27

CHAT_ID    = "1085373824"
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SEND_EMPTY = False
SEND_DELAY = 1.0
NOTIFY_ON_EMPTY = True


def parse_args():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) < 2:
        sys.stderr.write("用法：python3 backfill_send_f27.py 起日 迄日 [--dry-run] [--fetch] [--emit]\n")
        sys.exit(1)
    try:
        sdate = datetime.strptime(args[0], "%Y-%m-%d")
        edate = datetime.strptime(args[1], "%Y-%m-%d")
    except ValueError:
        sys.stderr.write("❌ 日期格式請用 YYYY-MM-DD\n")
        sys.exit(1)
    if sdate > edate:
        sdate, edate = edate, sdate
    return sdate, edate, "--dry-run" in flags, "--fetch" in flags, "--emit" in flags


def _i(s):
    s = (s or "").replace(",", "").strip()
    try:
        return int(float(s))
    except ValueError:
        return None


def _f(s):
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def daterange(sdate, edate):
    d = sdate
    while d <= edate:
        yield d
        d += timedelta(days=1)


def send_telegram(text: str):
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": CHAT_ID, "text": safe,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, timeout=20)
    if resp.status_code != 200:
        sys.stderr.write(f"  ⚠️  Telegram 發送失敗 {resp.status_code}: {resp.text[:200]}\n")
        return False
    return True


def _assemble_messages(blocks_by_group: dict, today_str: str) -> list:
    messages = []
    for group in F27._WATCHLIST:
        group_msgs = F27.build_group_messages(group, blocks_by_group.get(group, []), today_str)
        if group_msgs:
            for m in group_msgs:
                messages.append((group, m))
        elif SEND_EMPTY:
            header = f"📈 F27 季報損益 {today_str}｜👤 {F27._WATCHLIST[group]['display']}"
            messages.append((group, f"{header}\n📭 今日無季報損益公告"))
    return messages


# 累積本次執行「已放行、準備發送」的項目，等 main() 確認不是 dry-run 才真的
# 記錄進 sent_state（dry-run 只是預覽，不該影響去重狀態）。
_PENDING_MARK = []


def _dedupe_against_sent_state(pairs):
    """比對F27的sent_state.json（key=股號+期間+累計營收），濾掉已經發送過的，
    回傳放行清單。放行的項目會先存進 _PENDING_MARK，等 main() 判斷不是
    dry-run 才呼叫 _flush_pending_mark() 真的寫入 sent_state。
    這支腳本才是n8n實際執行的每日排程（mops_f27_daily.py的run()其實沒被呼叫到，
    n8n是直接呼叫這支+--fetch），去重必須放在這裡才有效——之前只加在
    mops_f27_daily.py是白做工，同一家公司只要當天還在MOPS公告清單裡，
    n8n每觸發一次就會重新組訊息、重複發送一次。"""
    sent_state = F27._load_sent_state()
    to_send, skipped = [], 0
    for item, rec in pairs:
        k = F27._sent_key(item, rec)
        if k in sent_state:
            skipped += 1
            continue
        to_send.append((item, rec))
    if skipped:
        sys.stderr.write(f"  🔁 {skipped} 筆同期間同數字已發送過，本輪跳過\n")
    _PENDING_MARK.extend(to_send)
    return to_send


def flush_pending_mark():
    """dry-run以外的模式才呼叫：把這次真的要發送的項目寫入sent_state。"""
    if not _PENDING_MARK:
        return
    sent_state = F27._load_sent_state()
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item, rec in _PENDING_MARK:
        sent_state[F27._sent_key(item, rec)] = now_s
    if not F27._save_sent_state(sent_state):
        sys.stderr.write("⚠️ sent_state_f27.json 寫入失敗：去重狀態未保存，之後每輪可能重發相同訊息\n")


def build_messages_from_csv(rows_by_date: dict, dt: datetime) -> list:
    today_str = f"{dt.month}/{dt.day}"
    rows = rows_by_date.get(dt.strftime("%Y-%m-%d"), [])

    pairs = []
    for r in rows:
        item = {"stock_id": r["stock_id"], "company_name": r["company_name"],
                "report_year": r["report_year"], "report_quarter": r["report_quarter"]}
        rec = {"ytd_revenue": _i(r["ytd_revenue"]), "ytd_yoy_pct": _f(r["ytd_yoy_pct"]),
               "operating_income": _i(r["operating_income"]), "pretax_income": _i(r["pretax_income"]),
               "net_income_parent": _i(r["net_income_parent"]), "eps": _f(r["eps"])}
        pairs.append((item, rec))

    to_send = _dedupe_against_sent_state(pairs)

    blocks_by_group = {k: [] for k in F27._WATCHLIST}
    for item, rec in to_send:
        block = F27.format_company_block(item, rec)
        for g in F27.get_groups(item["stock_id"], item["company_name"]):
            blocks_by_group[g].append(block)
    return _assemble_messages(blocks_by_group, today_str)


def load_csv_by_date() -> dict:
    rows_by_date = {}
    if not os.path.exists(F27.CSV_PATH):
        sys.stderr.write(f"❌ 找不到 {F27.CSV_PATH}\n")
        sys.exit(1)
    with open(F27.CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows_by_date.setdefault(r["announce_date"], []).append(r)
    return rows_by_date


def build_messages_for_date(session, dt: datetime) -> list:
    today_str = f"{dt.month}/{dt.day}"

    all_items = []
    for market in F27.MARKETS:
        items = F27.fetch_f27_list(session, dt, market)
        all_items.extend(items)
        time.sleep(F27.SLEEP_LIST)

    seen, unique_items = set(), []
    for item in all_items:
        key = (item["stock_id"], item["announce_date"], item["announce_time"])
        if key not in seen:
            seen.add(key)
            unique_items.append(item)

    results, fetch_failed, parse_none = F27.process_items(session, unique_items)
    if fetch_failed:
        sys.stderr.write(f"  ⚠️  本日 {len(fetch_failed)} 筆抓取失敗，已記錄待重抓\n")

    F27.save_to_csv(results)
    F27.update_logs(results, fetch_failed, parse_none)

    to_send = _dedupe_against_sent_state(results)

    blocks_by_group = {k: [] for k in F27._WATCHLIST}
    for item, rec in to_send:
        block = F27.format_company_block(item, rec)
        for g in F27.get_groups(item["stock_id"], item["company_name"]):
            blocks_by_group[g].append(block)

    return _assemble_messages(blocks_by_group, today_str)


def main():
    sdate, edate, dry_run, do_fetch, emit = parse_args()

    if not dry_run and not emit and not BOT_TOKEN:
        sys.stderr.write("❌ 未設定 TELEGRAM_BOT_TOKEN，無法發送。加 --dry-run / --emit 或先 export token。\n")
        sys.exit(1)

    F27._load_watchlist()
    mode = "🧩 emit 模式" if emit else ("🔍 預覽模式" if dry_run else "📤 發送模式")
    source = "🌐 重抓 MOPS" if do_fetch else "📄 讀 CSV（不碰網路）"
    sys.stderr.write(f"{mode} | {source} | 區間 {sdate:%Y-%m-%d} ~ {edate:%Y-%m-%d}\n\n")

    rows_by_date = None if do_fetch else load_csv_by_date()
    session      = requests.Session() if do_fetch else None

    total_sent = produced = 0
    emit_payload = []
    try:
        for dt in daterange(sdate, edate):
            sys.stderr.write(f"===== {dt:%Y-%m-%d} =====\n")
            tagged = build_messages_for_date(session, dt) if do_fetch \
                     else build_messages_from_csv(rows_by_date, dt)
            if not tagged:
                sys.stderr.write("  （無命中關注清單的公告）\n\n")
                continue
            for group, msg in tagged:
                produced += 1
                if emit:
                    emit_payload.append({"message": msg, "group": group})
                elif dry_run:
                    print(msg)
                    print("─" * 30)
                else:
                    if send_telegram(msg):
                        total_sent += 1
                    time.sleep(SEND_DELAY)
            sys.stderr.write(f"  本日 {len(tagged)} 則\n\n")

        if produced == 0 and NOTIFY_ON_EMPTY:
            rng = (f"{sdate.month}/{sdate.day}" if sdate == edate
                   else f"{sdate.month}/{sdate.day}~{edate.month}/{edate.day}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            notice = (f"📈{rng} F27 季報損益\n✅ 已執行（{now}）\n目前無符合關注清單的季報公告")
            if emit:
                emit_payload.append({"message": notice, "group": "system"})
            elif dry_run:
                print(notice)
            else:
                if send_telegram(notice):
                    total_sent += 1
    finally:
        if session:
            session.close()

    # dry-run只是預覽，不能影響去重狀態；emit（n8n用）和真的發送才算數
    if not dry_run:
        flush_pending_mark()

    # 有重抓（--fetch）就把更新後的 CSV 上傳 Google Drive（只寫 stderr，不污染 stdout 的 JSON）
    if do_fetch:
        url = F27.upload_to_gdrive()
        sys.stderr.write(f"☁️  CSV 已上傳：{url}\n" if url else "⚠️  CSV 上傳略過/失敗\n")

    if emit:
        print(json.dumps(emit_payload, ensure_ascii=False))
        sys.stderr.write(f"✅ emit 完成，輸出 {len(emit_payload)} 則訊息 JSON。\n")
    elif dry_run:
        sys.stderr.write("✅ 預覽完成。\n")
    else:
        sys.stderr.write(f"✅ 完成，共發送 {total_sent} 則到 Telegram。\n")


if __name__ == "__main__":
    main()

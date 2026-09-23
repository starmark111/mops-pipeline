"""
backfill_send_f22.py
補發指定日期區間的 F22 月營收訊息到 Telegram。

直接走 Telegram Bot API，格式與 n8n「MOPS F22 Daily」工作流程一致：
  - chatId  : 1085373824（可用 --chat 覆寫）
  - parse_mode : HTML，發送前做 & < > escape
  - 訊息內容、分群、title 全部沿用 mops_f22_daily.py 的邏輯

資料來源（重要）：
  預設「直接讀 monthly_revenue.csv」裡已抓好的資料來組訊息，不重抓 MOPS，瞬間完成。
  只有當你真的想重新去 MOPS 抓最新數字時，才加 --fetch。

用法：
  # 先預覽（不真的發送）— 強烈建議先跑這個。讀 CSV，不碰網路
  python3 backfill_send_f22.py 2026-06-01 2026-06-05 --dry-run

  # 確認沒問題後真的發送
  export TELEGRAM_BOT_TOKEN="123456:ABC..."     # bot token（找 BotFather 或 n8n 憑證）
  python3 backfill_send_f22.py 2026-06-01 2026-06-05

  # 想重新去 MOPS 抓（較慢，會更新 CSV）才加 --fetch
  python3 backfill_send_f22.py 2026-06-01 2026-06-05 --fetch

  # 給 n8n 用：不自己發 Telegram，改印出 [{"message","group"}] JSON 由 n8n 發送
  python3 backfill_send_f22.py 2026-06-01 2026-06-05 --fetch --emit

三種輸出模式（擇一）：
  --emit     : 印 JSON 給 n8n（n8n 負責發 Telegram），不需 token
  --dry-run  : 人類可讀的訊息預覽，不發送，不需 token
  （皆不加）: 腳本直接走 Bot API 發 Telegram，需 TELEGRAM_BOT_TOKEN

設定：
  - bot token 從環境變數 TELEGRAM_BOT_TOKEN 讀取
  - SEND_EMPTY = False：補發時預設「跳過」沒命中的群組（不發 📭），避免洗版；
    想完全比照每日排程（空群組也發 📭）就改成 True
"""

import os
import sys
import csv
import json
import time
import requests
from datetime import datetime, timedelta

# 沿用 mops_f22_daily.py 的所有抓取 / 解析 / 格式化邏輯
import mops_f22_daily as F22

# =========================
# 設定
# =========================
CHAT_ID    = "1085373824"
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SEND_EMPTY = False          # True = 空群組也發「📭 今日無月營收公告」
SEND_DELAY = 1.0            # 每則訊息間隔秒數（避免 Telegram 限流）
NOTIFY_ON_EMPTY = True      # True = 整批完全沒公告時，仍送一則「已執行」回報訊息


# =========================
# 解析參數
# =========================
def parse_args():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) < 2:
        sys.stderr.write("用法：python3 backfill_send_f22.py 起日 迄日 [--dry-run] [--fetch]\n"
                         "例：  python3 backfill_send_f22.py 2026-06-01 2026-06-05 --dry-run\n")
        sys.exit(1)
    try:
        sdate = datetime.strptime(args[0], "%Y-%m-%d")
        edate = datetime.strptime(args[1], "%Y-%m-%d")
    except ValueError:
        sys.stderr.write("❌ 日期格式請用 YYYY-MM-DD\n")
        sys.exit(1)
    if sdate > edate:
        sdate, edate = edate, sdate
    return (sdate, edate,
            "--dry-run" in flags,
            "--fetch" in flags,
            "--emit" in flags)


def _to_int(s):
    s = (s or "").replace(",", "").strip()
    try:
        return int(s)
    except ValueError:
        return None


def _to_float(s):
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


# =========================
# Telegram 發送
# =========================
def send_telegram(text: str):
    """比照 n8n：parse_mode=HTML，先 escape & < >"""
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": safe,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=20)
    if resp.status_code != 200:
        sys.stderr.write(f"  ⚠️  Telegram 發送失敗 {resp.status_code}: {resp.text[:200]}\n")
        return False
    return True


# =========================
# 從分群結果組裝訊息（CSV / 重抓 共用）
# =========================
def _assemble_messages(blocks_by_group: dict, today_str: str) -> list:
    """回傳 [(group, message), ...]，保留群組標籤供 n8n / 發送使用"""
    messages = []
    for group in F22._WATCHLIST:
        group_msgs = F22.build_group_messages(group, blocks_by_group.get(group, []), today_str)
        if group_msgs:
            for m in group_msgs:
                messages.append((group, m))
        elif SEND_EMPTY:
            header = f"📊 F22 月營收 {today_str}｜👤 {F22._WATCHLIST[group]['display']}"
            messages.append((group, f"{header}\n📭 今日無月營收公告"))
    return messages


# =========================
# 從 monthly_revenue.csv 讀單日資料 → 產生分群訊息（預設，不碰網路）
# =========================
# 累積本次「已放行、準備發送」項目；dry-run不寫入sent_state，其餘模式main()結尾才寫入。
# 同一套修法比照backfill_send_f27.py（n8n實際跑的是這支+--fetch，之前mops_f22_daily.py
# 裡加的去重從沒被用到）。
_PENDING_MARK = []


def _dedupe_against_sent_state(pairs):
    """pairs: [(item, rev), ...]。回傳濾掉已發送過的清單，並記入_PENDING_MARK。"""
    sent_state = F22._load_sent_state()
    to_send, skipped = [], 0
    for item, rev in pairs:
        k = F22._sent_key(item, rev)
        if k in sent_state:
            skipped += 1
            continue
        to_send.append((item, rev))
    if skipped:
        sys.stderr.write(f"  🔁 {skipped} 筆同月同數字已發送過，本輪跳過\n")
    _PENDING_MARK.extend(to_send)
    return to_send


def flush_pending_mark():
    if not _PENDING_MARK:
        return
    sent_state = F22._load_sent_state()
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for item, rev in _PENDING_MARK:
        sent_state[F22._sent_key(item, rev)] = now_s
    if not F22._save_sent_state(sent_state):
        sys.stderr.write("⚠️ sent_state_f22.json 寫入失敗：去重狀態未保存，之後每輪可能重發相同訊息\n")


def build_messages_from_csv(rows_by_date: dict, dt: datetime) -> list:
    today_str = f"{dt.month}/{dt.day}"
    rows = rows_by_date.get(dt.strftime("%Y-%m-%d"), [])

    pairs = []
    for r in rows:
        item = {"stock_id": r["stock_id"], "company_name": r["company_name"],
                "report_year": r["report_year"], "report_month": r["report_month"]}
        rev = {"revenue": _to_int(r["revenue"]), "yoy_pct": _to_float(r["yoy_pct"]),
               "ytd_revenue": _to_int(r["ytd_revenue"]), "ytd_yoy_pct": _to_float(r["ytd_yoy_pct"]),
               "remark": r.get("remark", "")}
        pairs.append((item, rev))

    to_send = _dedupe_against_sent_state(pairs)

    blocks_by_group = {k: [] for k in F22._WATCHLIST}
    for item, rev in to_send:
        block = F22.format_company_block(item, rev)
        for g in F22.get_groups(item["stock_id"], item["company_name"]):
            blocks_by_group[g].append(block)
    return _assemble_messages(blocks_by_group, today_str)


def load_csv_by_date() -> dict:
    rows_by_date = {}
    if not os.path.exists(F22.CSV_PATH):
        sys.stderr.write(f"❌ 找不到 {F22.CSV_PATH}\n")
        sys.exit(1)
    with open(F22.CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows_by_date.setdefault(r["announce_date"], []).append(r)
    return rows_by_date


# =========================
# 抓單日 F22 → 產生分群訊息（--fetch 才會用，重新去 MOPS 抓）
# =========================
def build_messages_for_date(session, dt: datetime) -> list:
    today_str = f"{dt.month}/{dt.day}"

    # 抓 F22 公告清單（市場清單見 F22.MARKETS）
    all_items = []
    for market in F22.MARKETS:
        items = F22.fetch_f22_list(session, dt, market)
        all_items.extend(items)
        time.sleep(1)

    # 去重
    seen, unique_items = set(), []
    for item in all_items:
        key = (item["stock_id"], item["announce_date"], item["announce_time"])
        if key not in seen:
            seen.add(key)
            unique_items.append(item)

    # 逐筆抓 HTML 解析營收
    results = []
    for item in unique_items:
        try:
            resp = session.get(item["source_url"], headers=F22.HEADERS, timeout=20)
            rev = F22.parse_revenue_html(resp.text)
            if rev:
                results.append((item, rev))
        except Exception as e:
            sys.stderr.write(f"  ⚠️  {item['stock_id']} 抓取失敗：{e}\n")
        time.sleep(0.5)

    # 寫入 CSV（沿用既有去重邏輯，安全）
    F22.save_to_csv(results)

    # 同月同數字已發送過的不重發（這是n8n實際執行路徑，必須在這裡去重才有效）
    to_send = _dedupe_against_sent_state(results)

    # 依關注清單分群
    blocks_by_group = {k: [] for k in F22._WATCHLIST}
    for item, rev in to_send:
        block = F22.format_company_block(item, rev)
        for g in F22.get_groups(item["stock_id"], item["company_name"]):
            blocks_by_group[g].append(block)

    return _assemble_messages(blocks_by_group, today_str)


# =========================
# 主流程
# =========================
def main():
    sdate, edate, dry_run, do_fetch, emit = parse_args()

    # 三種輸出模式：--emit（印 JSON 給 n8n） / --dry-run（人類預覽） / 預設（直接發 Telegram）
    if not dry_run and not emit and not BOT_TOKEN:
        sys.stderr.write("❌ 未設定 TELEGRAM_BOT_TOKEN，無法發送。\n"
                         "   先 export TELEGRAM_BOT_TOKEN=\"你的token\"，或加 --dry-run / --emit。\n")
        sys.exit(1)

    F22._load_watchlist()
    if emit:
        mode = "🧩 emit 模式（印 JSON 給 n8n）"
    elif dry_run:
        mode = "🔍 預覽模式（不發送）"
    else:
        mode = "📤 發送模式"
    source = "🌐 重抓 MOPS" if do_fetch else "📄 讀 CSV（不碰網路）"
    sys.stderr.write(f"{mode} | {source} | 區間 {sdate:%Y-%m-%d} ~ {edate:%Y-%m-%d}\n\n")

    # 預設從 CSV 讀；--fetch 才開 session 重抓
    rows_by_date = None if do_fetch else load_csv_by_date()
    session      = requests.Session() if do_fetch else None

    total_sent   = 0
    produced     = 0    # 整批實際產生的訊息數
    emit_payload = []   # --emit 模式累積 [{"message":..., "group":...}]
    try:
        for dt in daterange(sdate, edate):
            sys.stderr.write(f"===== {dt:%Y-%m-%d} =====\n")
            if do_fetch:
                tagged = build_messages_for_date(session, dt)
            else:
                tagged = build_messages_from_csv(rows_by_date, dt)
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

        # 整批完全沒有公告 → 仍送一則「已執行」回報
        if produced == 0 and NOTIFY_ON_EMPTY:
            rng = (f"{sdate.month}/{sdate.day}" if sdate == edate
                   else f"{sdate.month}/{sdate.day}~{edate.month}/{edate.day}")
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            notice = (f"📊{rng} F22 月營收\n"
                      f"✅ 已執行（{now}）\n目前無符合關注清單的月營收公告")
            if emit:
                emit_payload.append({"message": notice, "group": "system"})
            elif dry_run:
                print(notice)
                print("─" * 30)
            else:
                if send_telegram(notice):
                    total_sent += 1
    finally:
        if session:
            session.close()

    # dry-run只是預覽，不能影響去重狀態；emit（n8n用）和真的發送才算數
    if not dry_run:
        flush_pending_mark()

    if emit:
        # stdout 只輸出純 JSON，供 n8n 後續節點解析發送
        print(json.dumps(emit_payload, ensure_ascii=False))
        sys.stderr.write(f"✅ emit 完成，輸出 {len(emit_payload)} 則訊息 JSON。\n")
    elif dry_run:
        sys.stderr.write("✅ 預覽完成。確認無誤後拿掉 --dry-run 即可實際發送。\n")
    else:
        sys.stderr.write(f"✅ 完成，共發送 {total_sent} 則到 Telegram。\n")


if __name__ == "__main__":
    main()

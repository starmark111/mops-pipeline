"""
check_backfill_f22.py
F22 月營收「回補檢查」腳本。

用途：
  主排程（mops_f22_daily / backfill --emit）是「只查當天」，只要公司在當天最後一班
  之後才公告（例如週五晚上發、週末沒跑），那筆就會永久漏掉。
  這支腳本專門掃描「過去 N 天」的 F22 公告，跟 monthly_revenue.csv 比對，
  把任何漏抓的補回 CSV，並（可選）同步推上 Google Drive。

  特別針對「週一~週五」：區間內每個工作日都會明列掃描結果，
  若某工作日 MOPS 有公告但 CSV 原本缺漏，會標 ⚠️ 提醒（代表主排程那天沒掃完整）。

  這支「不做每日推播」。只做資料完整性回補。需要通知時用 --emit 輸出「單則回補摘要」。

用法：
  python3 check_backfill_f22.py --hours 24       # 回補「近 24 小時」（用公告時間 CTIME 精準篩）
  python3 check_backfill_f22.py                  # 預設回補最近 7 天（到今天）
  python3 check_backfill_f22.py --days 10        # 回補最近 10 天
  python3 check_backfill_f22.py 2026-06-01 2026-06-08   # 指定區間
  python3 check_backfill_f22.py --dry-run        # 只檢查、不寫入、不上傳（看缺哪些）
  python3 check_backfill_f22.py --emit           # 額外印出「回補摘要」JSON 給 n8n
  python3 check_backfill_f22.py --no-gdrive      # 寫 CSV 但不上傳 Google Drive

模式說明：
  --hours N : 滾動時間窗。end=現在、start=現在-N 小時，用每筆公告的 CDATE+CTIME
              精準判斷是否落在窗內。每天 02:00 跑 + --hours 24 = 補昨天 02:00~今天 02:00。
  --days N / 指定區間 : 用「公告日」整天為單位掃描（保險、可重疊補多天）。
  注意：--hours 24 沒有重疊，若某天 n8n 沒跑成功，那 24 小時會永久漏掉；
        想保險可用 --hours 26（多 2 小時重疊）或改用 --days 2。

設計重點：
  - 只對「CSV 裡缺漏」的公告去抓明細，已經有的直接跳過 → 重複掃也很快。
  - 寫入沿用 mops_f22_daily.save_to_csv（以 股號+年+月 去重，安全不重複）。
  - 上傳沿用 mops_f22_daily.upload_to_gdrive（推 monthly_revenue.csv 到雲端）。
"""

from __future__ import annotations

import os
import re
import sys
import csv
import json
import time
from datetime import datetime, timedelta

import requests

# 沿用每日腳本的抓取 / 解析 / 寫入 / 上傳邏輯
import mops_f22_daily as F22

WEEKDAY_TW = ["一", "二", "三", "四", "五", "六", "日"]  # 0=週一 ... 6=週日


# =========================
# 參數
# =========================
STRICT_EXIT = False   # --strict-exit 時才用非 0 exit code（見 run() 結尾）


def parse_args():
    global STRICT_EXIT
    argv = sys.argv[1:]
    days = 7
    hours = None
    dates = []
    dry_run = emit = no_gdrive = send = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            dry_run = True
        elif a == "--emit":
            emit = True
        elif a == "--send":
            send = True
        elif a == "--no-gdrive":
            no_gdrive = True
        elif a == "--strict-exit":
            STRICT_EXIT = True
        elif a == "--days":
            i += 1
            if i < len(argv):
                try:
                    days = int(argv[i])
                except ValueError:
                    pass
        elif a.startswith("--days="):
            try:
                days = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a == "--hours":
            i += 1
            if i < len(argv):
                try:
                    hours = int(argv[i])
                except ValueError:
                    pass
        elif a.startswith("--hours="):
            try:
                hours = int(a.split("=", 1)[1])
            except ValueError:
                pass
        elif a.startswith("--"):
            pass  # 忽略未知旗標
        else:
            dates.append(a)
        i += 1

    # --hours 優先（滾動時間窗），不用日期區間
    if hours is not None:
        return hours, None, None, dry_run, emit, no_gdrive, send

    if len(dates) >= 2:
        try:
            sdate = datetime.strptime(dates[0], "%Y-%m-%d")
            edate = datetime.strptime(dates[1], "%Y-%m-%d")
        except ValueError:
            sys.stderr.write("❌ 日期格式請用 YYYY-MM-DD\n")
            sys.exit(1)
    else:
        edate = datetime.now()
        sdate = edate - timedelta(days=max(days, 1) - 1)
    if sdate > edate:
        sdate, edate = edate, sdate
    return None, sdate, edate, dry_run, emit, no_gdrive, send


def _announce_dt(item):
    """用公告日(announce_date, 西元 YYYY-MM-DD) + 時間(announce_time, HH:MM:SS) 組成 datetime。"""
    try:
        return datetime.strptime(
            f"{item['announce_date']} {item.get('announce_time', '')}".strip(),
            "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# =========================
# 讀現有 CSV 的 key（股號, 年, 月）
# =========================
def load_existing_keys() -> set:
    keys = set()
    if os.path.exists(F22.CSV_PATH):
        with open(F22.CSV_PATH, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                keys.add((row.get("stock_id", ""),
                          str(row.get("report_year", "")),
                          str(row.get("report_month", ""))))
    return keys


def _key(item) -> tuple:
    return (item["stock_id"], str(item["report_year"]), str(item["report_month"]))


def daterange(sdate, edate):
    d = sdate
    while d <= edate:
        yield d
        d += timedelta(days=1)


# =========================
# 主流程
# =========================
def run():
    hours, sdate, edate, dry_run, emit, no_gdrive, send = parse_args()
    existing = load_existing_keys()
    F22.reset_fetch_stats()   # 記錄 ezsearch 是否真的查成（見下方 _verify_verdict）

    # 決定要掃哪些「公告日」與（可選的）時間窗
    if hours is not None:
        win_end = datetime.now()
        win_start = win_end - timedelta(hours=hours)
        scan_dates = []
        d = win_start.date()
        while d <= win_end.date():
            scan_dates.append(datetime(d.year, d.month, d.day))
            d += timedelta(days=1)
        # 期間標籤格式與 F27 的 find_missing_f27.py 保持一致（M/D ~ M/D）
        range_label = f"{win_start:%m/%d} ~ {win_end:%m/%d}"
    else:
        win_start = win_end = None
        scan_dates = list(daterange(sdate, edate))
        range_label = f"{sdate:%m/%d} ~ {edate:%m/%d}"

    src = "🔍 只檢查（不寫入）" if dry_run else "🛠️  檢查並回補"
    sys.stderr.write(f"{src} | {range_label}\n\n")

    report = []          # 每天一筆統計
    to_fill = []         # [(item, rev), ...] 待寫入
    announced_all = {}   # {(股號,年,月): item} 本次 MOPS 掃到的所有公告，供清單覆蓋檢查
    fails = []           # 異常清單 [{date,id,name,reason}]
    filled_keys = set()  # 跨日去重（同一檔可能在多天清單出現）

    with requests.Session() as session:
        for d in scan_dates:
            day_str = d.strftime("%Y-%m-%d")

            # 抓當天 F22 公告清單（市場清單見 F22.MARKETS）
            items = []
            for market in F22.MARKETS:
                items.extend(F22.fetch_f22_list(session, d, market))
                time.sleep(0.8)

            # 時間窗模式：用 CDATE+CTIME 精準篩（時間不可解析者保守納入）
            if win_start is not None:
                kept = []
                for it in items:
                    adt = _announce_dt(it)
                    if adt is None or (win_start <= adt < win_end):
                        kept.append(it)
                items = kept

            # 同一檔（股號+年+月）去重
            seen, uniq = set(), []
            for it in items:
                k = _key(it)
                if k not in seen:
                    seen.add(k)
                    uniq.append(it)

            # 找出 CSV 原本缺漏的
            # 金融股（金控等）F22 頁面格式不支援、月獲利由重訊自結涵蓋 → 直接跳過不抓不列
            missing = [it for it in uniq
                       if _key(it) not in existing and _key(it) not in filled_keys
                       and "金融" not in (it.get("industry") or "")
                       and "金控" not in it["company_name"]]

            # 只對缺漏的去抓明細
            # 曾發生過一天缺漏量大（500+筆）、間隔0.4秒連續抓時被MOPS防爬蟲機制擋掉，
            # 大量筆數集體「無法解析」卻查不出原因（3026禾伸堂案例）。加防護：
            #   1. 辨識MOPS至少兩種不同的擋阻頁：
            #      (a) WAF頁：HTTP 307 +「因為安全性考量／FOR SECURITY REASONS」
            #      (b) 限流頁：HTTP 200，內容含「Overrun」「Too many query requests」
            #          （中文部分編碼常是亂碼，改抓ASCII穩定的英文字串當標記）
            #      一偵測到任一種就記下「全域冷卻時間」，之後每筆請求前先檢查是否還在
            #      冷卻中，冷卻中就先等，而不是每筆都硬撞牆重試（實測2秒不夠，牆期更長）。
            #   2. 解析失敗時印出回應片段，才看得出是「真的沒資料」還是「被擋」
            WAF_MARKERS   = ("因為安全性考量", "FOR SECURITY REASONS",
                              "Overrun", "Too many query requests")
            WAF_COOLDOWN  = 60.0   # 偵測到擋阻後的冷卻秒數（2秒實測不夠，先抓60秒）

            day_filled, parse_fail = [], 0
            fin_skip = 0   # 金融股（已知不支援）筆數，不算異常
            global _waf_cooldown_until
            if "_waf_cooldown_until" not in globals():
                _waf_cooldown_until = 0.0

            for it in missing:
                # 還在冷卻期就先等，避免明知會被擋還硬送請求
                remaining = _waf_cooldown_until - time.time()
                if remaining > 0:
                    sys.stderr.write(f"  ⏳ WAF冷卻中，等待{remaining:.0f}秒…\n")
                    time.sleep(remaining)

                rev = None
                blocked_snippet = None
                is_waf = False
                for attempt in (1, 2):
                    try:
                        resp = session.get(it["source_url"], headers=F22.HEADERS, timeout=20)
                        body = resp.text or ""
                        if resp.status_code == 307 or any(m in body for m in WAF_MARKERS):
                            is_waf = True
                            blocked_snippet = f"WAF擋阻｜HTTP{resp.status_code} {body[:80]!r}"
                            _waf_cooldown_until = time.time() + WAF_COOLDOWN
                            sys.stderr.write(f"  🚫 偵測到MOPS WAF擋阻，冷卻{WAF_COOLDOWN:.0f}秒後重試\n")
                            time.sleep(WAF_COOLDOWN)
                            continue
                        rev = F22.parse_revenue_html(body)
                        if rev:
                            break
                        # 非WAF、非成功：留一段回應內容當診斷線索（去標籤、截150字）
                        snippet = re.sub(r"<[^>]+>", "", body).strip()[:150]
                        blocked_snippet = f"HTTP{resp.status_code} len={len(body)} {snippet!r}"
                        break   # 不是WAF擋阻，重試也沒用，不用再等
                    except Exception as e:
                        blocked_snippet = f"抓取例外：{str(e)[:60]}"
                        break

                if rev:
                    day_filled.append((it, rev))
                    filled_keys.add(_key(it))
                elif is_waf:
                    parse_fail += 1
                    fails.append({"date": day_str, "id": it["stock_id"],
                                  "name": it["company_name"], "reason": f"WAF擋阻，冷卻後仍失敗｜{blocked_snippet}"})
                    sys.stderr.write(f"  ⚠️  {it['stock_id']} {it['company_name']} 仍被擋｜{blocked_snippet}\n")
                else:
                    # 金融股（金控等）明細頁格式與一般公司不同，F22 解析器不支援：
                    # 屬已知限制、非異常；其月獲利以重訊「自結損益」為準（重訊腳本已有金融格式解析）。
                    #
                    # 投控股（非金控的一般投資控股公司，如神基、定穎投控）：MOPS改回傳「查詢
                    # 彙總報表」（代子公司彙總申報），不是單一公司營收表格，同樣不支援。
                    # 用回應內容裡的「查詢彙總報表」字樣判斷，比只看公司名稱有沒有「投控」
                    # 更準（3005神基的公司名稱本身沒有「投控」兩字，但格式一樣）。
                    is_holding = "查詢彙總報表" in (blocked_snippet or "") or "代其子公司申報" in (blocked_snippet or "")
                    if "金融" in (it.get("industry") or "") or "金控" in it["company_name"]:
                        fin_skip += 1
                        fails.append({"date": day_str, "id": it["stock_id"],
                                      "name": it["company_name"], "reason": "金融股格式（已知不支援）"})
                        sys.stderr.write(f"  🏦 {it['stock_id']} {it['company_name']} 金融股格式，略過\n")
                    elif is_holding:
                        fin_skip += 1
                        fails.append({"date": day_str, "id": it["stock_id"],
                                      "name": it["company_name"], "reason": "投控股格式（已知不支援）"})
                        sys.stderr.write(f"  🏢 {it['stock_id']} {it['company_name']} 投控股格式，略過\n")
                    else:
                        parse_fail += 1
                        fails.append({"date": day_str, "id": it["stock_id"],
                                      "name": it["company_name"], "reason": f"解析失敗｜{blocked_snippet}"})
                        sys.stderr.write(f"  ⚠️  {it['stock_id']} {it['company_name']} 無法解析｜{blocked_snippet}\n")
                time.sleep(0.4)

            to_fill.extend(day_filled)
            for _it in uniq:
                announced_all[(_it["stock_id"], str(_it["report_year"]), str(_it["report_month"]))] = _it
            report.append({
                "date": day_str,
                "weekday": d.weekday(),           # 0=週一
                "announced": len(uniq),
                "missing_before": len(missing),
                "filled": len(day_filled),
                "parse_fail": parse_fail,
                "fin_skip": fin_skip,
            })

            wd = WEEKDAY_TW[d.weekday()]
            sys.stderr.write(f"  {day_str}(週{wd})：公告 {len(uniq)}、缺 {len(missing)}、"
                             f"補 {len(day_filled)}\n")

    # ── 寫入 CSV + 上傳 Google Drive ────────────────────────────────
    written = 0
    gdrive_url = None
    if dry_run:
        sys.stderr.write(f"\n🔍 dry-run：偵測到 {len(to_fill)} 筆缺漏，未寫入、未上傳。\n")
    else:
        written = F22.save_to_csv(to_fill)
        sys.stderr.write(f"\n📄 CSV 實際新寫入 {written} 筆\n")
        # 確保回補資料一定上傳：非 dry-run 一律把 CSV 同步到 Google Drive，
        # 即使本輪沒新增（補上一輪上傳失敗的情況），讓雲端永遠是最新狀態。
        if no_gdrive:
            sys.stderr.write("☁️  --no-gdrive：略過上傳\n")
        else:
            gdrive_url = F22.upload_to_gdrive()
            sys.stderr.write(f"☁️  Google Drive：{'已上傳 ' + gdrive_url if gdrive_url else '⚠️ 上傳失敗（請檢查 token.json / credentials.json）'}\n")

    # ── --send：補完資料後接著補發 Telegram 訊息 ────────────────────────
    # 為什麼需要：本支只寫 CSV，而每日排程只看「當天新抓到的公告」、不回頭掃 CSV，
    # 因此靠回補救回來的資料，訊息永遠不會發出（2026/08 曾有 32 檔清單內公司
    # 資料已入庫卻從未通知）。加上 --send 才算真正完成「有公告就一定收到訊息」。
    # 作法：直接呼叫既有的 backfill_send_f22.py，格式、分群、去重全部沿用，
    # 不另外複製一套發送邏輯。sent_state 會自動跳過已發送者，可安全重跑。
    sent_n = None
    if send and not dry_run and written:
        sent_n = _do_send(to_fill, emit)

    # ── 判定「這次檢查本身可不可信」───────────────────────────────
    verdict = _verify_verdict(report)

    # ── 組「回補摘要」訊息（給人看 / 給 n8n） ─────────────────────────
    audit = audit_watchlist_delivery(announced_all)
    msg = build_summary(range_label, report, to_fill, fails, written, dry_run, verdict, sent_n, audit)

    if emit:
        print(json.dumps([{"message": msg, "group": "f22_backfill"}], ensure_ascii=False))
    else:
        print(msg)

    # 預設維持 exit 0，避免既有 n8n 節點把「有缺漏／查不成」誤判成節點執行失敗。
    # 需要用 exit code 做分流時加 --strict-exit：0=正常、2=有缺漏、3=無法驗證、4=部分驗證/可疑
    if STRICT_EXIT:
        sys.exit(verdict["exit_code"] if verdict["exit_code"] else (2 if (to_fill or fails) else 0))


# =========================
# 驗證可信度判定
# =========================
def _verify_verdict(report) -> dict:
    """區分三種結果，避免「沒查成」被誤報成「沒問題」。

    level：ok / partial / unverified / suspicious
    exit_code：0=可信且無事、3=無法驗證、4=部分驗證或可疑（2 由呼叫端依缺漏給）
    """
    st = F22.FETCH_STATS
    ok, fail, reasons = st["ok"], st["fail"], st["reasons"]

    if ok == 0 and fail > 0:
        return {
            "level": "unverified", "exit_code": 3,
            "title": "❌ 無法驗證：ezsearch API 完全連不上，本次檢查結果不可信",
            "reasons": reasons,
            "action": [
                "此結果**不代表資料正常**，只代表沒查成。請照以下順序處理：",
                "1) 若這是沙箱／排程環境跑的 → 屬預期，改在本機終端機重跑：",
                "   cd /Users/starmark/Downloads/scripts/mops_f22 && python3 check_backfill_f22.py --days 2 --dry-run",
                "2) 若本機跑也一樣 → 可能 MOPS 維護或你被 WAF 暫擋，等 1~2 小時再跑一次。",
                "3) 若連續 2~3 天都無法驗證 → 視為真故障。先確認抓取管線是否也斷了：",
                "   tail -3 /Users/starmark/Downloads/scripts/mops_f22/monthly_revenue.csv",
                "   看最後一筆 fetched_at；若也停住超過 36 小時，代表 F22 早已在漏資料，需人工排查排程與網路。",
            ],
        }

    if fail > 0:
        return {
            "level": "partial", "exit_code": 4,
            "title": f"⚠️ 部分驗證：{fail} 次查詢失敗，未涵蓋的市場可能有漏未被發現",
            "reasons": reasons,
            "action": [
                "下方「無缺漏」只對查成的市場有效。請在本機重跑同一指令確認：",
                "   cd /Users/starmark/Downloads/scripts/mops_f22 && python3 check_backfill_f22.py --days 2 --dry-run",
                "重跑後若全部查成且無缺漏，即可視為正常，不需其他動作。",
            ],
        }

    # 全部查成，但工作日掃到 0 筆公告 → 本身就不合理
    zero_weekdays = [r["date"] for r in report if r["weekday"] <= 4 and r["announced"] == 0]
    if zero_weekdays:
        return {
            "level": "suspicious", "exit_code": 4,
            "title": f"⚠️ 可疑：{len(zero_weekdays)} 個工作日 MOPS 公告 0 筆（{'、'.join(zero_weekdays)}）",
            "reasons": [],
            "action": [
                "工作日 0 筆公告不一定有問題（月營收多集中在每月 1~10 日），但若落在 1~10 日就不合理。",
                "1) 若這幾天在每月 1~10 日 → 到 MOPS 網站手動查該日 F22 公告，確認是否真的沒有。",
                "2) 若確實有公告卻掃不到 → 代表 ezsearch 查詢條件或回應格式變了，需人工檢查 mops_f22_daily.py 的 fetch_f22_list()。",
                "3) 若不在 1~10 日 → 屬正常，可忽略。",
            ],
        }

    return {"level": "ok", "exit_code": 0, "title": "", "reasons": [], "action": []}


# =========================
# 摘要訊息
# =========================
# =========================
# --send：補發回補資料的 Telegram 訊息
# =========================
# 為什麼需要：本支只寫 CSV，而每日排程只看「當天新抓到的公告」、不回頭掃 CSV，
# 因此靠回補救回來的資料，訊息永遠不會發出（2026/08 曾有 32 檔清單內公司
# 資料已入庫卻從未通知）。加上 --send 才算真正完成「有公告就一定收到訊息」。
#
# 兩道安全閥（2026-09-30 加入）：
#   1. SEND_MAX_DAYS：只補發近 N 天的公告。failed/缺漏清單可能累積數月份的舊資料
#      （實測 F27 的 failed_fetches 橫跨 17 個月），全送會被舊季報洗版。
#   2. SEND_WARN_LIMIT：預估則數超過上限就停下來只報告，需加 --force 才真的送。
# 兩者都可被 --force 略過，不會造成「永遠送不出去」。
SEND_MAX_DAYS    = 30
SEND_WARN_LIMIT  = 50


def _do_send(to_fill, emit):
    """呼叫既有的 backfill_send_f22.py 補發訊息（不另寫一套發送邏輯，
    格式／分群／去重全部沿用；sent_state 會自動跳過已發送者，可安全重跑）。"""
    import subprocess

    force = "--force" in sys.argv
    sys.stderr.write("\n📨 --send：補發回補資料的 Telegram 訊息…\n")

    all_dates = sorted({it["announce_date"] for it, _ in to_fill if it.get("announce_date")})
    if not all_dates:
        sys.stderr.write("  （回補資料無公告日期，略過補發）\n")
        return 0

    # ── 安全閥 1：日期範圍 ──
    cutoff = (datetime.now() - timedelta(days=SEND_MAX_DAYS)).strftime("%Y-%m-%d")
    dates = [d for d in all_dates if d >= cutoff] if not force else all_dates
    skipped = [d for d in all_dates if d < cutoff] if not force else []
    if skipped:
        sys.stderr.write(
            f"  ⏳ 略過 {len(skipped)} 個超過 {SEND_MAX_DAYS} 天的公告日"
            f"（{skipped[0]} ~ {skipped[-1]}），避免舊資料洗版。\n"
            f"     要補發這些請加 --force。\n")
    if not dates:
        sys.stderr.write("  （近期無可補發的公告）\n")
        return 0

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_send_f22.py")
    base   = [sys.executable, script, dates[0], dates[-1]]

    # ── 安全閥 2：先 dry-run 預估則數（dry-run 不會寫 sent_state，安全）──
    if not force:
        probe = subprocess.run(base + ["--dry-run"], capture_output=True, text=True)
        n = sum(int(m.group(1)) for m in re.finditer(r"本日 (\d+) 則", probe.stderr))
        if n > SEND_WARN_LIMIT:
            sys.stderr.write(
                f"  ⚠️  預估會送出 {n} 則（超過上限 {SEND_WARN_LIMIT}），已停止未發送。\n"
                f"     區間 {dates[0]} ~ {dates[-1]}。確認要送請加 --force，"
                f"或先手動跑：\n"
                f"     python3 backfill_send_f22.py {dates[0]} {dates[-1]} --dry-run\n")
            return 0
        sys.stderr.write(f"  預估 {n} 則，開始補發…\n")

    cmd = base + (["--emit"] if emit else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(r.stderr)
    if emit and r.stdout.strip():
        print(r.stdout.strip())   # 補發訊息的 JSON 另印一行，n8n 可一併取用
    if r.returncode != 0:
        sys.stderr.write(f"⚠️  補發失敗（return code {r.returncode}），資料已寫入但訊息未送出\n")
        return 0
    return sum(int(m.group(1)) for m in re.finditer(r"本日 (\d+) 則", r.stderr))


def audit_watchlist_delivery(announced):
    """清單覆蓋檢查：本次 MOPS 掃到的公告中，屬於關注清單的公司是否都已發送。

    核心要求「清單內公司只要有公告就一定要收到訊息」的最終驗證。
    前面的查漏只管 MOPS→CSV，這裡補上 CSV→Telegram 那一段。

    ⚠️ 基準用「本次 MOPS 掃到的公告」(announced)，不是 CSV 的 announce_date——
       公司可能重複公告，CSV 只留最初那筆的日期，用 CSV 日期去撈會撈不到，
       導致摘要出現「MOPS 公告 N 筆」卻「清單內無公告」這種自相矛盾的結果。
    ⚠️ 與 mops_f27/find_missing_f27.py 的同名函式邏輯一致，改一邊要改另一邊。

    回傳 (未發送清單, 清單命中總數)。
    """
    import io as _io
    F22._load_watchlist()

    try:
        with open(F22.SENT_STATE_PATH, encoding="utf-8") as f:
            sent = set(json.load(f).keys())
    except Exception:
        sent = set()

    # CSV 的 revenue 是組 sent_state key 的必要欄位，建索引備查
    csv_idx = {}
    try:
        with open(F22.CSV_PATH, "rb") as f:
            raw = f.read().replace(b"\x00", b"")
        for r in csv.DictReader(_io.StringIO(raw.decode("utf-8", errors="replace"))):
            csv_idx[(r["stock_id"], str(r["report_year"]), str(r["report_month"]))] = r
    except Exception:
        pass

    unsent, total = [], 0
    oldest = None
    for (sid, y, m), it in announced.items():
        groups = F22.get_groups(sid, it.get("company_name", ""))
        if not groups:
            continue
        total += 1
        d = it.get("announce_date", "")
        if d and (oldest is None or d < oldest):
            oldest = d
        row = csv_idx.get((sid, str(y), str(m)))
        key = f"{sid}|{y}|{m}|{row.get('revenue','')}" if row else None
        if key is None or key not in sent:
            unsent.append((sid, it.get("company_name", ""), "／".join(groups)))

    # sent_state 只保留 SENT_KEEP_DAYS 天（F22=45），更舊的紀錄會被自動清除。
    # 若掃描區間早於保留期，「未發送」判定不可信——當時可能發過但紀錄已過期。
    # 回傳 stale=True 讓摘要改口徑，避免誤報成「全部沒發」。
    cutoff = (datetime.now() - timedelta(days=F22.SENT_KEEP_DAYS)).strftime("%Y-%m-%d")
    stale = bool(oldest and oldest < cutoff)
    return unsent, total, stale


def build_summary(range_label, report, to_fill, fails, written, dry_run,
                  verdict=None, sent_n=None, audit=None) -> str:
    """回補摘要訊息。
    ⚠️ 格式必須與 mops_f27/find_missing_f27.py 的 build_summary() 保持一致，
       兩邊是同一種東西，只差「期間單位」（F22=年/月、F27=年Q季）。改一邊要改另一邊。

    版型（固定四段，沒有內容的段落不出現）：
        🔁 F22 回補檢查 {期間}
        MOPS 公告 N 筆 / 回補 M 筆
        ── 補回清單 ──            ← 有缺漏才出現
        ⚠️/🏦/📨 各一行            ← 有才出現
        ✅ 結論行
    """
    fin_fails = [f for f in fails if "金融股格式" in f["reason"]]
    fails     = [f for f in fails if "金融股格式" not in f["reason"]]
    total_announced = sum(r["announced"] for r in report)

    lines = [f"🔁 F22 回補檢查 {range_label}",
             f"MOPS 公告 {total_announced} 筆 / "
             f"{'偵測缺漏' if dry_run else '回補'} {len(to_fill)} 筆"]

    if to_fill:
        lines.append("── 補回清單 ──")
        for it, _ in to_fill[:30]:
            lines.append(f"  {it['stock_id']} {it['company_name']} "
                         f"{it['report_year']}/{it['report_month']:02d}"
                         f"（公告 {it['announce_date']}）")
        if len(to_fill) > 30:
            lines.append(f"  …等共 {len(to_fill)} 筆")

    if fails:
        names = "、".join(f"{f['id']} {f['name']}" for f in fails[:5])
        more  = f" 等{len(fails)}筆" if len(fails) > 5 else ""
        lines.append(f"⚠️ 異常 {len(fails)} 筆（需人工檢查）：{names}{more}")
    if fin_fails:
        lines.append(f"🏦 金融股 {len(fin_fails)} 筆略過（不支援金控格式，月獲利見重訊自結）")

    # 結論行
    if dry_run:
        lines.append("🔍 dry-run：未寫入、未重抓、未補發")
    elif to_fill:
        if sent_n is None:
            lines.append("（未加 --send，訊息未補發）")
        elif sent_n == 0:
            lines.append("📨 無需補發（皆已發送過或被安全閥擋下）")
        else:
            lines.append(f"📨 已補發 {sent_n} 則訊息")
    elif verdict and verdict["level"] == "unverified":
        lines.append("❌ 無法驗證：MOPS 清單查不到，本次結果不可信")
    elif verdict and verdict["level"] == "partial":
        lines.append("⚠️ 部分市場未查成，不能斷定資料完整")
    else:
        lines.append("✅ 區間內無缺漏，資料完整")

    # ── 清單覆蓋檢查：核心要求「清單內有公告就一定要收到訊息」的最終驗證 ──
    if audit is not None:
        unsent, total, stale = audit
        if not total:
            lines.append("👤 清單內本期無公告")
        elif stale:
            lines.append(f"👤 清單覆蓋 {total} 檔（區間超過 {F22.SENT_KEEP_DAYS} 天，"
                         f"發送紀錄已過期，無法判定）")
        elif not unsent:
            lines.append(f"👤 清單覆蓋 {total} 檔，全部已發送 ✅")
        else:
            names = "、".join(f"{s} {n}" for s, n, _ in unsent[:8])
            more  = f" 等{len(unsent)}檔" if len(unsent) > 8 else ""
            lines.append(f"👤 ⚠️ 清單內 {len(unsent)}/{total} 檔有資料卻未發送：{names}{more}")
            lines.append("   → 補發：python3 backfill_send_f22.py <起日> <迄日>")

    return "\n".join(lines)


if __name__ == "__main__":
    run()

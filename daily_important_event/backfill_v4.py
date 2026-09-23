"""
backfill_v4.py
回溯歷史自結 / 季報資料，寫入 important_events_v4.csv

用法：
  python3 backfill_v4.py 2025-01-01              # 從 2025/1/1 到昨天
  python3 backfill_v4.py 2025-01-01 2025-06-30   # 指定區間
  python3 backfill_v4.py 2025-01-01 --limit 10   # 每次最多處理 10 天（配合排程使用）

邏輯：
  - 逐日掃描，跳過週末
  - 若 CSV 已有該日期的記錄 → 直接跳過（不重複抓）
  - 每天清單 call 後 sleep LIST_DELAY 秒
  - 每筆 detail call 後 sleep DETAIL_DELAY 秒
  - 只寫 CSV，不發 Telegram、不上傳 Google Drive
"""

import csv
import os
import sys
import time
import requests
from datetime import datetime, timedelta, date

# 從 v4 匯入共用函式
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from Daily_Important_Event_v4 import (
    KEYWORDS, REPORT_KEYWORDS, EXCLUDE_KEYWORDS,
    CSV_PATH, CSV_COLUMNS,
    NEW_MOPS_API, NEW_MOPS_DETAIL_API, NEW_MOPS_HEADERS,
    scrape_history, normalize_item,
    extract_financials_both, extract_quarterly_report,
    save_to_csv,
)

# ── 速率控制 ──────────────────────────────────────────────────────────────────
LIST_DELAY   = 2.0   # 每天清單 API 之後的等待秒數
DETAIL_DELAY = 0.8   # 每筆 detail API 之後的等待秒數（在 scrape_history 內部）

# ── Lock file ─────────────────────────────────────────────────────────────────
LOCK_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_v4.lock")
CHECKPOINT_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill_checkpoint.txt")

# ── 日期工具 ──────────────────────────────────────────────────────────────────
def to_roc(d: date) -> str:
    """date → 'YYY/MM/DD'（民國年）"""
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def trading_days(start: date, end: date):
    """產生 start~end 之間所有交易日（跳週六日）"""
    cur = start
    while cur <= end:
        if cur.weekday() < 5:   # 0=Mon … 4=Fri
            yield cur
        cur += timedelta(days=1)


# 財務欄位（任一有值即視為「有資料」）
FINANCIAL_COLS = ["月_營收", "月_稅前", "月_母利", "月_EPS",
                  "季_營收", "季_稅前", "季_母利", "季_EPS"]


def _has_data(row: dict) -> bool:
    return any(row.get(c, "").strip() for c in FINANCIAL_COLS)


# ── 讀取已掃描日期（CSV 有資料 + checkpoint 掃過但空）────────────────────────
def load_scanned_dates() -> set:
    """回傳已完整處理的日期（有資料且無空白記錄）。
    只要該日期有任一筆空白記錄，就不算完成，允許重新處理。
    """
    dates_with_data  = set()
    dates_with_empty = set()

    if os.path.exists(CSV_PATH):
        with open(CSV_PATH, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                d = row.get("announce_date", "")
                if not d:
                    continue
                if _has_data(row):
                    dates_with_data.add(d)
                else:
                    dates_with_empty.add(d)

    if os.path.exists(CHECKPOINT_PATH):
        with open(CHECKPOINT_PATH, "r", encoding="utf-8") as f:
            for line in f:
                d = line.strip()
                if d:
                    dates_with_data.add(d)

    # 有資料且沒有空白記錄 → 完成；有空白記錄 → 需重新處理
    return dates_with_data - dates_with_empty


def purge_empty_rows(roc_date: str):
    """刪除 CSV 中指定日期的空白記錄（無財務數字的列）"""
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    kept = [r for r in rows if not (r.get("announce_date") == roc_date and not _has_data(r))]
    if len(kept) < len(rows):
        with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(kept)


def mark_checkpoint(roc_date: str):
    """將日期記入 checkpoint（掃過但無資料的日期）"""
    with open(CHECKPOINT_PATH, "a", encoding="utf-8") as f:
        f.write(roc_date + "\n")


# ── 帶 delay 的 scrape_history ──────────────────────────────────────────────
def scrape_history_with_delay(session, dt) -> list:
    """呼叫 scrape_history，並在每筆 detail fetch 之間加 sleep。
    由於 scrape_history 內部的 detail fetch 不易插入 sleep，
    這裡採取：先取清單，再逐筆取 detail。
    """
    from Daily_Important_Event_v4 import (
        _fetch_detail_new, clean_text,
    )
    import re

    roc_year = dt.year - 1911
    try:
        resp = session.post(
            NEW_MOPS_API,
            json={"year": str(roc_year), "month": str(dt.month), "day": str(dt.day)},
            headers=NEW_MOPS_HEADERS,
            timeout=30,
        )
        data = resp.json()
    except Exception as e:
        sys.stderr.write(f"  ⚠️  清單 API 失敗：{e}\n")
        return []

    if data.get("code") != 200:
        return []

    results = []
    for row in data.get("result", {}).get("data", []):
        if len(row) < 5:
            continue
        date_str    = row[0]
        time_str    = row[1]
        co_id       = str(row[2])
        co_name     = row[3]
        subject     = row[4]
        detail_info = row[5] if len(row) > 5 else {}

        subject_norm = re.sub(r'\s+', '', subject)
        is_jishi  = any(k in subject_norm for k in KEYWORDS)
        is_report = any(k in subject_norm for k in REPORT_KEYWORDS)
        if not (is_jishi or is_report):
            continue
        if any(k in subject_norm for k in EXCLUDE_KEYWORDS):
            continue

        detail = ""
        if isinstance(detail_info, dict):
            params = detail_info.get("parameters", {})
            if params:
                detail = _fetch_detail_new(session, params)
                time.sleep(DETAIL_DELAY)

        results.append({
            "co_id":     co_id,
            "co_name":   co_name,
            "date":      date_str,
            "time":      time_str,
            "subject":   subject,
            "detail":    detail,
            "item_type": "季報" if is_report else "自結",
        })

    return results


# ── 主流程 ────────────────────────────────────────────────────────────────────
def run():
    # ── Lock：防止同時執行兩個 instance ──────────────────────────────────────
    if os.path.exists(LOCK_PATH):
        # 讀取 lock 建立時間，超過 30 分鐘視為殘留 lock（上次異常終止），自動清除
        try:
            lock_age = time.time() - os.path.getmtime(LOCK_PATH)
            if lock_age < 1800:
                sys.stderr.write(f"⏳ 另一個 instance 正在執行中（lock 存在 {int(lock_age)}s），本次跳過\n")
                print('[]')   # 輸出空 JSON，避免 n8n Code 節點報錯
                sys.exit(0)
            else:
                sys.stderr.write(f"⚠️  發現殘留 lock（{int(lock_age)}s 前），自動清除並繼續\n")
                os.remove(LOCK_PATH)
        except Exception:
            pass

    # 建立 lock
    try:
        with open(LOCK_PATH, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        sys.stderr.write(f"⚠️  無法建立 lock file：{e}，繼續執行\n")

    try:
        _run_inner()
    finally:
        # 無論正常結束或發生例外，都確保 lock 被刪除
        try:
            if os.path.exists(LOCK_PATH):
                os.remove(LOCK_PATH)
        except Exception:
            pass


def _run_inner():
    # 解析參數
    args = sys.argv[1:]
    if not args:
        sys.stderr.write("用法：python3 backfill_v4.py YYYY-MM-DD [YYYY-MM-DD] [--limit N]\n")
        sys.exit(1)

    # 抽出 --limit
    limit = None
    if "--limit" in args:
        idx = args.index("--limit")
        try:
            limit = int(args[idx + 1])
            args = args[:idx] + args[idx + 2:]
        except (IndexError, ValueError):
            sys.stderr.write("❌ --limit 需要一個整數\n")
            sys.exit(1)

    start = parse_date(args[0])
    end   = parse_date(args[1]) if len(args) >= 2 else date.today() - timedelta(days=1)

    if start > end:
        sys.stderr.write("❌ start 不能晚於 end\n")
        sys.exit(1)

    # 載入已掃描日期（CSV 有資料 + checkpoint 掃過但空）
    existing_dates = load_scanned_dates()
    sys.stderr.write(f"📂 已掃描 {len(existing_dates)} 個日期\n")

    days = list(trading_days(start, end))
    remaining = [d for d in days if to_roc(d) not in existing_dates]
    sys.stderr.write(f"📅 掃描範圍：{start} ~ {end}，共 {len(days)} 個交易日\n")
    sys.stderr.write(f"⏳ 尚未處理：{len(remaining)} 天")
    if limit:
        sys.stderr.write(f"，本次最多處理 {limit} 天")
    sys.stderr.write("\n\n")

    total_written  = 0
    processed_days = 0

    with requests.Session() as session:
        for i, day in enumerate(days, 1):
            roc_date = to_roc(day)

            # 已有記錄 → 跳過
            if roc_date in existing_dates:
                sys.stderr.write(f"[{i:3d}/{len(days)}] {day}  ✓ 已有記錄，跳過\n")
                continue

            # 達到 limit → 停止
            if limit and processed_days >= limit:
                sys.stderr.write(f"\n⏸  已達本次上限 {limit} 天，下次從 {day} 繼續\n")
                break

            sys.stderr.write(f"[{i:3d}/{len(days)}] {day}  → 抓取中…")

            purge_empty_rows(roc_date)  # 先清掉同日期的空白舊記錄
            items = scrape_history_with_delay(session, day)

            if not items:
                sys.stderr.write(f"  (0 筆)\n")
                mark_checkpoint(roc_date)   # 掃過但無資料，記入 checkpoint 避免重複掃
            else:
                # 標準化 + 擷取財務數字
                csv_recs = []
                for item in items:
                    normalize_item(item)
                    text = item.get("financial_text", "")
                    # 跳過鬼魂公告：清單有列但 detail API 回 406 → 內文為空
                    # （如 911868 同方友友-DR 6/1，公告被撤回但清單未同步）
                    if not text.strip():
                        sys.stderr.write(
                            f"    ↳ detail 為空，略過 {item.get('co_id')} {item.get('co_name')}\n"
                        )
                        continue
                    # 跳過可轉債／公司債類注意交易公告（如 7610 聯友金屬-創），
                    # 非自結、無財報，避免寫入 no_data 空白列
                    if any(k in text for k in ("公司債相關資訊", "可轉債相關資訊")):
                        continue
                    if item.get("item_type") == "季報":
                        qtr_data, qtr_label, unit_str = extract_quarterly_report(text)
                        csv_recs.append({
                            "item":        item,
                            "type":        "季報",
                            "month_data":  {},
                            "month_label": "",
                            "qtr_data":    qtr_data,
                            "qtr_label":   qtr_label or "",
                            "unit_str":    unit_str or "",
                        })
                    else:
                        month_data, month_label, qtr_data, qtr_label, unit_str = extract_financials_both(text)
                        csv_recs.append({
                            "item":        item,
                            "type":        "自結",
                            "month_data":  month_data,
                            "month_label": month_label,
                            "qtr_data":    qtr_data,
                            "qtr_label":   qtr_label,
                            "unit_str":    unit_str or "",
                        })

                written = save_to_csv(csv_recs)
                total_written += written
                sys.stderr.write(f"  {len(items)} 筆符合，新寫入 {written} 筆\n")

                # 成功抓到的日期加入 existing_dates，避免同日重複
                existing_dates.add(roc_date)
                # 有資料的日期不需要寫 checkpoint（CSV 本身就是紀錄）

            processed_days += 1

            # 清單 API 速率控制
            time.sleep(LIST_DELAY)

    left = max(0, len(remaining) - processed_days)
    sys.stderr.write(f"\n✅ 完成。本次處理 {processed_days} 天，共新寫入 {total_written} 筆\n")
    if left:
        sys.stderr.write(f"   剩餘約 {left} 天未處理，下次排程繼續\n")

    import json as _json
    print(_json.dumps([{
        "message": f"📦 backfill 完成：處理 {processed_days} 天，新寫入 {total_written} 筆，剩餘 {left} 天"
    }], ensure_ascii=False))


if __name__ == "__main__":
    run()

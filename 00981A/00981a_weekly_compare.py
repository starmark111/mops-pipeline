import os
import csv
import sys
import requests
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "00981A_history.csv")
DATE_FORMAT = "%Y-%m-%d"

CORE_KEYWORDS = ("淨資產", "現金及其他", "應收付證券款")
DIFF_THRESHOLD = 0.20  # 0.20%

def load_history():
    rows = []
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    rows.append({
                        "date": r["date"],
                        "code": r.get("code", "----"),
                        "name": r["name"],
                        "shares": float(r["shares"]),
                        "pct": float(r["pct"]),
                    })
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"❌ 讀取 CSV 失敗: {e}")
    return rows

def get_price(code):
    headers = {"User-Agent": "Mozilla/5.0"}
    for suffix in [".TW", ".TWO"]:
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{code}{suffix}"
            data = requests.get(url, headers=headers, timeout=5).json()
            result = data.get("chart", {}).get("result")
            if result:
                price = result[0]["meta"].get("regularMarketPrice")
                if price:
                    return price
        except Exception:
            continue
    return None

def get_overall_streak(stock_name, all_rows, sorted_dates_desc):
    """從最新日往回算連買(正)/連賣(負)天數，跨全部歷史"""
    streak = 0
    for i in range(len(sorted_dates_desc) - 1):
        d_curr = sorted_dates_desc[i]
        d_prev = sorted_dates_desc[i + 1]
        curr = next((r["shares"] for r in all_rows if r["date"] == d_curr and r["name"] == stock_name), 0)
        prev = next((r["shares"] for r in all_rows if r["date"] == d_prev and r["name"] == stock_name), 0)
        diff = curr - prev
        if streak == 0:
            if diff > 0:
                streak = 1
            elif diff < 0:
                streak = -1
            else:
                break
        else:
            if (streak > 0 and diff > 0) or (streak < 0 and diff < 0):
                streak += 1 if streak > 0 else -1
            else:
                break
    return streak

def format_amount(share_diff, price):
    if price is None:
        return "價格未知"
    sign = "+" if share_diff > 0 else "-"
    amount_ntd = abs(share_diff) * price  # 元（shares 單位）
    if amount_ntd >= 1e8:
        return f"約{sign}{amount_ntd / 1e8:.2f}億"
    else:
        return f"約{sign}{amount_ntd / 1e4:.0f}萬"

def main():
    all_rows = load_history()
    if not all_rows:
        sys.exit(1)

    all_dates_desc = sorted(set(r["date"] for r in all_rows), reverse=True)

    # 找最近兩個週五（weekday() == 4）
    fridays = [d for d in all_dates_desc if datetime.strptime(d, DATE_FORMAT).weekday() == 4]
    if len(fridays) < 2:
        print("⚠️ 資料不足兩個週五")
        sys.exit(1)

    date_this = fridays[0]
    date_prev = fridays[1]

    # 本週所有交易日（含首尾），用來算週內買賣天數
    week_dates_asc = sorted(d for d in all_dates_desc if date_prev <= d <= date_this)

    def build_map(target_date):
        return {r["name"]: r for r in all_rows if r["date"] == target_date}

    this_map = build_map(date_this)
    prev_map = build_map(date_prev)

    def get_ranks(data_map):
        stocks = sorted(
            [{"n": n, "p": d["pct"]} for n, d in data_map.items() if n not in CORE_KEYWORDS],
            key=lambda x: x["p"], reverse=True
        )
        return {item["n"]: i + 1 for i, item in enumerate(stocks)}

    this_ranks = get_ranks(this_map)
    prev_ranks = get_ranks(prev_map)

    asset_now = this_map.get("淨資產", {}).get("shares", 0) / 1e8
    asset_prev = prev_map.get("淨資產", {}).get("shares", 0) / 1e8

    # 分析每檔股票
    all_names = set(this_map) | set(prev_map)
    movers = []

    for name in all_names:
        if name in CORE_KEYWORDS:
            continue

        t = this_map.get(name)
        p = prev_map.get(name)
        shares_now = t["shares"] if t else 0
        shares_prev = p["shares"] if p else 0
        share_diff = shares_now - shares_prev

        pct_now = t["pct"] if t else 0
        pct_prev = p["pct"] if p else 0
        pct_diff = pct_now - pct_prev

        if abs(pct_diff) < DIFF_THRESHOLD:
            continue

        code = (t or p)["code"]

        # 週內每日買賣次數 & 最長連買streak
        buy_days = sell_days = 0
        cur_streak = max_streak = 0
        for i in range(1, len(week_dates_asc)):
            d_c = week_dates_asc[i]
            d_p = week_dates_asc[i - 1]
            s_c = next((r["shares"] for r in all_rows if r["date"] == d_c and r["name"] == name), None)
            s_p = next((r["shares"] for r in all_rows if r["date"] == d_p and r["name"] == name), None)
            if s_c is None or s_p is None:
                cur_streak = 0
                continue
            diff = s_c - s_p
            if diff > 0:
                buy_days += 1
                cur_streak = cur_streak + 1 if cur_streak >= 0 else 1
                max_streak = max(max_streak, cur_streak)
            elif diff < 0:
                sell_days += 1
                cur_streak = cur_streak - 1 if cur_streak <= 0 else -1
            else:
                cur_streak = 0

        movers.append({
            "name": name, "code": code,
            "share_diff": share_diff, "pct_diff": pct_diff,
            "rank_prev": prev_ranks.get(name, "NEW"),
            "rank_now": this_ranks.get(name, "--"),
            "buy_days": buy_days, "sell_days": sell_days,
            "max_streak": max_streak,
        })

    # 批次抓收盤價
    price_cache = {}
    for code in set(m["code"] for m in movers):
        price_cache[code] = get_price(code)

    buys = sorted([m for m in movers if m["share_diff"] > 0], key=lambda x: x["rank_now"])
    sells = sorted([m for m in movers if m["share_diff"] < 0], key=lambda x: abs(x["share_diff"]), reverse=True)

    def fmt_stock(m):
        lots = m["share_diff"] / 1000
        lots_str = f"{lots:+.0f}張" if lots % 1 == 0 else f"{lots:+.1f}張"
        price = price_cache.get(m["code"])
        amt = format_amount(m["share_diff"], price)
        streak_str = f" 最長連買{m['max_streak']}🔥" if m["max_streak"] >= 2 else ""
        return (
            f"[{m['rank_prev']}->{m['rank_now']}] {m['name']}({m['code']}) "
            f"[{lots_str}] [{amt}] [買{m['buy_days']}賣{m['sell_days']}]{streak_str}"
        )

    asset_diff = asset_now - asset_prev
    lines = [
        f"🔍 00981A WEEK分析 ({date_prev} -> {date_this})",
        f"💰 淨資產變動: {asset_now:.2f}億 ({asset_diff:+.2f}億)",
        "",
        f"🚀 加碼/新入榜 (門檻: {DIFF_THRESHOLD:.2f}%)",
    ]
    for m in buys:
        lines.append(fmt_stock(m))

    lines += ["", f"⬇️ 減碼/已清空 (門檻: {DIFF_THRESHOLD:.2f}%)"]
    for m in sells:
        lines.append(fmt_stock(m))

    print("\n".join(lines))

if __name__ == "__main__":
    main()

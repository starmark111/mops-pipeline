import os
import csv
import sys
import requests
from datetime import datetime

# --- 設定 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "00981A_history.csv")
DATE_FORMAT = "%Y-%m-%d"

def load_history():
    rows = []
    try:
        with open(CSV_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                try:
                    rows.append({
                        "date": r["date"],
                        "code": r.get("code", "----"),
                        "name": r["name"],
                        "pct": float(r["pct"]),
                        "shares": float(r["shares"]) 
                    })
                except (ValueError, KeyError): continue
    except Exception as e:
        print(f"❌ 讀取 CSV 錯誤: {e}")
        return []
    return rows

def build_data_dict(all_rows, target_date):
    d_map = {}
    for r in all_rows:
        # 🌟 這裡最重要：必須只抓 target_date 的資料
        if str(r["date"]) == str(target_date):
            d_map[r["name"]] = r
    return d_map


def get_price(code):
    headers = {"User-Agent": "Mozilla/5.0"}
    # 先查上市 (.TW)
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{code}.TW"
    r = requests.get(url, headers=headers, timeout=5)
    data = r.json()
    result = data.get("chart", {}).get("result")
    # 如果 .TW 沒資料 → 查 .TWO
    if not result:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{code}.TWO"
        r = requests.get(url, headers=headers, timeout=5)
        data = r.json()
        result = data.get("chart", {}).get("result")
    if not result:
        return None, None
    meta = result[0]["meta"]
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose")
    if price and prev:
        pct = (price - prev) / prev * 100
        return price, pct
    return price, None

def get_prices(codes):
    price_map = {}
    for code in codes:
        price, pct = get_price(code)
        price_map[code] = {
            "price": price if price else "-",
            "pct": pct
        }
    return price_map

def compare(latest_date, previous_date, all_rows):
    # 建立該日期的資料 Map
    def build_map(target):
        return {str(r["name"]): r for r in all_rows if str(r["date"]) == str(target)}

    today_map = build_map(latest_date)
    yesterday_map = build_map(previous_date)
    
    # 取得排序後的日期清單 (由新到舊)
    sorted_dates = sorted(list(set(r["date"] for r in all_rows)), reverse=True)

    # 1. 變數定義
    LABEL_ASSET = "淨資產"
    LABEL_CASH = "現金及其他"
    LABEL_RECEIVABLE = "應收付證券款"
    CORE_KEYWORDS = (LABEL_ASSET, LABEL_CASH, LABEL_RECEIVABLE)
    DIFF_THRESHOLD = 0.02

    # 2. 基金概況計算
    def get_fund_summary(data_map):
        asset_v = data_map.get(LABEL_ASSET, {})
        cash_v = data_map.get(LABEL_CASH, {})
        recv_v = data_map.get(LABEL_RECEIVABLE, {})
        return {
            "asset": asset_v.get("shares", 0) / 1e8,
            "cash_p": cash_v.get("pct", 0), 
            "cash_bn": cash_v.get("shares", 0) / 1e8,
            "recv_p": recv_v.get("pct", 0), 
            "recv_bn": recv_v.get("shares", 0) / 1e8
        }

    s = get_fund_summary(today_map)
    y_s = get_fund_summary(yesterday_map)
    asset_diff = s['asset'] - y_s['asset']

    # 3. 連買/連賣計算
    def get_streak(stock_name):
        streak = 0
        for i in range(len(sorted_dates) - 1):
            d_curr = sorted_dates[i]
            d_prev = sorted_dates[i+1]
            val_curr = next((r["shares"] for r in all_rows if r["date"] == d_curr and r["name"] == stock_name), 0)
            val_prev = next((r["shares"] for r in all_rows if r["date"] == d_prev and r["name"] == stock_name), 0)
            diff = val_curr - val_prev
            if streak == 0:
                if diff > 0: streak = 1
                elif diff < 0: streak = -1
                else: break
            else:
                if (streak > 0 and diff > 0) or (streak < 0 and diff < 0):
                    streak += (1 if streak > 0 else -1)
                else: break
        return streak

    # 4. 計算排名
    def get_ranks(data_map):
        stocks = sorted([
            {"n": n, "p": d['pct']} for n, d in data_map.items() 
            if n not in CORE_KEYWORDS
        ], key=lambda x: x['p'], reverse=True)
        return {item['n']: i + 1 for i, item in enumerate(stocks)}

    today_ranks = get_ranks(today_map)
    yesterday_ranks = get_ranks(yesterday_map)

    # 5. 核心比對
    buys, sells = [], []
    for name, data in today_map.items():
        if name in CORE_KEYWORDS: continue
        y_data = yesterday_map.get(name)
        
        if y_data:
            pct_diff = data['pct'] - y_data['pct']
            share_diff = data['shares'] - y_data['shares']
            old_rank = yesterday_ranks.get(name, "--")
        else:
            pct_diff, share_diff, old_rank = data['pct'], data['shares'], "NEW"

        # 門檻檢查：變動須大於等於 0.01%
        if abs(pct_diff) < DIFF_THRESHOLD: continue

        streak = get_streak(name)
        streak_msg = f" | 連{'買' if streak > 0 else '賣'}{abs(streak)}d" if abs(streak) >= 2 else ""

        info = {
            "name": name, "code": data.get('code', '----'),
            "pct": data['pct'], "pct_chg": pct_diff, "share_diff": share_diff,
            "rank_new": today_ranks.get(name, "--"), "rank_old": old_rank,
            "streak_msg": streak_msg
        }
        if share_diff > 0: buys.append(info)
        elif share_diff < 0: sells.append(info)

    # 6. 格式化輸出
    report = [
        f"📊 00981A 異動 ({latest_date})",
        f"💰 淨資產: {s['asset']:.2f}億 ({asset_diff:+.2f}億)",
        f"💰 CASH 水位: {s['cash_p']:.3f}% ({s['cash_bn']:.2f}億)",
        f"🧾 應收付證券款: {s['recv_p']:.2f}% ({s['recv_bn']:.2f}億)",
        "------------------"
    ]

    def line_format(title, items, emoji):
        if not items: 
            return
        report.append(f"{emoji} {title}")
        # 依變動幅度排序
        sorted_items = sorted(items, key=lambda x: abs(x['pct_chg']), reverse=True)
        codes = [i["code"] for i in sorted_items]
        price_map = get_prices(codes)
        for i in sorted_items:
            p = price_map.get(i["code"], {})
            price = p.get("price") or "-"
            pct = p.get("pct")
            pct_str = f"{pct:+.2f}%" if pct is not None else "-"
            change_lots = i['share_diff'] / 1000
            lots_str = f"{change_lots:+.1f}張" if change_lots % 1 != 0 else f"{change_lots:+.0f}張"
            report.append(
                f"[{i['rank_old']}->{i['rank_new']}]  "
                f"{i['name']}({i['code']}) ${price}({pct_str}) | "
                f"{lots_str} {i['pct']:.2f}%({i['pct_chg']:+.2f}%){i['streak_msg']}"
            )
        report.append("")

    line_format("買進/新入榜", buys, "🚀")
    line_format("賣出/已清空", sells, "⬇️")
    final_msg = "\n".join(report)
    print(final_msg)
    return final_msg
    
def main():
    all_rows = load_history()
    if not all_rows: sys.exit(1)
    
    unique_dates = sorted(list(set(r["date"] for r in all_rows)), reverse=True)
    if len(unique_dates) < 2:
        print("⚠️ 錯誤：資料不足兩天")
        sys.exit(1)

    latest_date = unique_dates[0]
    previous_date = unique_dates[1]

    if len(sys.argv) == 3:
        latest_date, previous_date = sys.argv[1], sys.argv[2]
    
    compare(latest_date, previous_date, all_rows)

if __name__ == "__main__":
    main()
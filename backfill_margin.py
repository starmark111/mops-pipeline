"""一次性回補：為 CSV 既有日期補上融資餘額，並重放狀態機。
用法：python3 backfill_margin.py [回補天數，預設40]
"""
import sys
import time
import requests
from three_big_investors import (CSV_FILE, load_history, save_history,
                                 decide_state, fetch_foreign_futures)

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40

def fetch_margin_on(date_ad):  # date_ad: 2026-07-17
    d = date_ad.replace("-", "")
    url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d}&selectType=MS&response=json"
    j = requests.get(url, timeout=15).json()
    if j.get("stat") != "OK":
        return None, None
    for row in j["tables"][0]["data"]:
        if row[0].startswith("融資金額"):
            prev = round(float(row[4].replace(",", "")) / 100_000, 1)
            today = round(float(row[5].replace(",", "")) / 100_000, 1)
            return today, prev
    return None, None

rows = load_history()
target = rows[-N:]
for r in target:
    if not r.get("margin_bal"):
        bal, prev = fetch_margin_on(r["date"])
        if bal is None:
            print(f"{r['date']}: 融資無資料")
        else:
            r["margin_bal"] = bal
            r["margin_chg"] = round(bal - prev, 1)
            print(f"{r['date']}: 融資 {bal:,.1f} 億 ({bal-prev:+,.1f})")
        time.sleep(3)  # TWSE 有流量限制，勿移除
    if not r.get("foreign_oi"):
        oi = fetch_foreign_futures(r["date"].replace("-", "/"))
        if oi is not None:
            r["foreign_oi"] = oi
            print(f"{r['date']}: 外資空單 {oi:+,} 口")
        time.sleep(3)

# 重放狀態機
margins, state = [], ""
for r in rows:
    if not r.get("margin_bal"):
        r["state"] = ""
        continue
    margins.append(float(r["margin_bal"]))
    state, reason = decide_state(state, margins)
    r["state"] = state
print(f"\n最新狀態: {state}｜{reason}")

save_history(rows)
print(f"完成，共 {len(margins)} 日融資資料寫入 {CSV_FILE}")

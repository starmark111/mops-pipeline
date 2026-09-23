import os
import csv
import re
from datetime import datetime

# --- 設定路徑 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 這是你從官網下載回來的原始檔案 (請確認檔名一致)
SOURCE_EXCEL = os.path.join(BASE_DIR, "00990A.csv") 
# 這是轉換後要給 compare 用的歷史紀錄檔
HISTORY_CSV = os.path.join(BASE_DIR, "00990A_history.csv")

def process_yuanta_report():
    if not os.path.exists(SOURCE_EXCEL):
        print(f"❌ 找不到原始檔案: {SOURCE_EXCEL}")
        return

    holdings = []
    latest_date = ""
    start_parsing = False

    # 讀取元大原始檔案 (通常是 ANSI 或 UTF-8)
    with open(SOURCE_EXCEL, "r", encoding="utf-8-sig") as f:
        reader = list(csv.reader(f))
        
        # 1. 抓取第一行的日期 (例如: 2026/03/04)
        first_line = ",".join(reader[0])
        date_match = re.search(r"(\d{4}/\d{2}/\d{2})", first_line)
        if date_match:
            latest_date = date_match.group(1).replace("/", "-")
        
        # 2. 抓取財務數據 (淨資產與現金)
        total_asset = 0
        cash_sum = 0
        for row in reader:
            line = ",".join(row)
            if "Fund Net Asset Value (NTD)" in line:
                total_asset = float(row[1])
            if "現金" in line and "NTD$" in line:
                cash_sum += float(re.sub(r'[^\d.]', '', row[1]))

        # 寫入財務項
        holdings.append({"date": latest_date, "code": "TOTAL_ASSET", "name": "淨資產", "shares": total_asset, "pct": 100.0})
        holdings.append({"date": latest_date, "code": "CASH", "name": "現金及其他", "shares": cash_sum, "pct": round((cash_sum/total_asset)*100, 3) if total_asset else 0})

        # 3. 抓取持股清單
        for row in reader:
            if not row: continue
            # 當偵測到這行標題時，開始記錄後面的資料
            if "商品代碼" in row and "商品名稱" in row:
                start_parsing = True
                continue
            
            if start_parsing and len(row) >= 4:
                code = row[0].strip()
                name = row[1].strip()
                shares = row[2].strip()
                pct = row[3].strip()
                
                if name:
                    holdings.append({
                        "date": latest_date,
                        "code": code if code else "----",
                        "name": name,
                        "shares": float(shares),
                        "pct": float(pct)
                    })

    if holdings and latest_date:
        save_to_history(holdings, latest_date)

def save_to_history(new_data, latest_date):
    existing_rows = []
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, "r", encoding="utf-8") as f:
            existing_rows = [r for r in csv.DictReader(f) if r["date"] != latest_date]

    fieldnames = ["date", "code", "name", "shares", "pct"]
    with open(HISTORY_CSV, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing_rows)
        writer.writerows(new_data)
    print(f"✅ 成功！已將 {latest_date} 的資料存入 {HISTORY_CSV}")
    print(f"📦 總計擷取 {len(new_data)-2} 檔持股")

if __name__ == "__main__":
    process_yuanta_report()
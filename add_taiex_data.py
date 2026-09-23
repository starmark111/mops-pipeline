import csv
import os
import requests
from datetime import datetime, timedelta

# --- 設定 ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_CSV = os.path.join(BASE_DIR, "00981A", "00981A_history.csv")
OUTPUT_CSV = os.path.join(BASE_DIR, "00981A", "00981A_history_clean.csv")

def get_yahoo_data_final(start_date_str, end_date_str):
    """從 Yahoo Finance 抓取並計算最準確的 4 項指標 (移除成交量)"""
    try:
        start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
        # 往前多抓 15 天確保有足夠的緩衝計算昨收
        start_ts = int((start_dt - timedelta(days=15)).timestamp())
        end_ts = int(datetime.strptime(end_date_str, "%Y-%m-%d").timestamp()) + 86400
        
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?period1={start_ts}&period2={end_ts}&interval=1d"
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        
        res = requests.get(url, headers=headers, timeout=15)
        data = res.json()
        
        result = data.get("chart", {}).get("result", [])[0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        
        closes = quote.get("close", [])
        highs = quote.get("high", [])
        lows = quote.get("low", [])

        history_map = {}
        for i in range(len(timestamps)):
            if i == 0 or closes[i] is None or closes[i-1] is None:
                continue
                
            dt_key = datetime.fromtimestamp(timestamps[i]).strftime("%Y-%m-%d")
            prev_close = closes[i-1]
            curr_close = closes[i]
            
            # 1. 漲跌點數 (點)
            diff_point = curr_close - prev_close
            
            # 2. 漲跌百分比 (%)
            diff_pct = (diff_point / prev_close) * 100
            
            # 3. 振幅百分比 (%) = (最高-最低) / 前日收盤 * 100
            amp_pct = ((highs[i] - lows[i]) / prev_close) * 100
            
            history_map[dt_key] = {
                "index": f"{curr_close:.2f}",
                "change": f"{'+' if diff_point > 0 else ''}{diff_point:.2f}",
                "change_pct": f"{'+' if diff_pct > 0 else ''}{diff_pct:.2f}",
                "amp_pct": f"{amp_pct:.2f}"
            }
            
        return history_map
    except Exception as e:
        print(f"❌ 數據讀取失敗: {e}")
        return {}

def main():
    if not os.path.exists(INPUT_CSV):
        print("❌ 找不到原始檔案 00981A_history.csv")
        return

    # 1. 讀取持股資料
    with open(INPUT_CSV, 'r', encoding='utf-8') as f:
        reader = list(csv.DictReader(f))
    
    dates = [row['date'] for row in reader]
    taiex_map = get_yahoo_data_final(min(dates), max(dates))

    # 2. 寫入精簡後的新欄位
    new_fields = ["date", "name", "pct", "大盤指數", "漲跌", "漲跌(%)", "振幅(%)"]
    
    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=new_fields)
        writer.writeheader()
        
        for row in reader:
            d = row['date']
            info = taiex_map.get(d, {})
            writer.writerow({
                "date": d,
                "name": row['name'],
                "pct": row['pct'],
                "大盤指數": info.get("index", "N/A"),
                "漲跌": info.get("change", "N/A"),
                "漲跌(%)": info.get("change_pct", "N/A"),
                "振幅(%)": info.get("amp_pct", "N/A")
            })

    print("-" * 30)
    print(f"✅ V6 精簡版完成！")
    print(f"📄 產出檔案: {os.path.basename(OUTPUT_CSV)}")
    print(f"📊 欄位已鎖定：指數、漲跌、漲跌(%)、振幅(%)")

if __name__ == "__main__":
    main()

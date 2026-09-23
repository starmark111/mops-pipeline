# 00981a_daily_ezmoney.py
# V11 - 使用 EZMoney 網頁結構擷取 00981A 持股資料（穩定版）

import os
import sys
import csv
from datetime import datetime
from typing import List, Dict, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError, Page

# ----------------------------------------------------------
# 基本設定
# ----------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "00981A_history.csv")
DATE_FORMAT = "%Y-%m-%d"

URL = "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=49YTW"

# ----------------------------------------------------------
# CSV 輔助函數
# ----------------------------------------------------------

def load_existing_dates() -> set:
    dates = set()
    if not os.path.exists(CSV_FILE):
        return dates

    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("date"):
                dates.add(r["date"])
    return dates


def rewrite_without_date(date_str: str):
    if not os.path.exists(CSV_FILE):
        return

    with open(CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r["date"] != date_str]

    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "code", "name", "shares", "pct"], extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def append_rows(rows: List[Dict]) -> bool:
    try:
        file_exists = os.path.exists(CSV_FILE)
        fieldnames = ["date", "code", "name", "shares", "pct"]
        
        with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')  # 加這行
            if not file_exists or os.path.getsize(CSV_FILE) == 0:
                writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return True
    except Exception as e:
        print(f"❌ 寫入 CSV 失敗: {e}", file=sys.stderr)
        return False

# ----------------------------------------------------------
# EZMoney 擷取邏輯
# ----------------------------------------------------------

def get_latest_date(page: Page) -> Optional[str]:
    """
    從 EZMoney 頁面抓取資料日期：
    <h5>資料日期:<span>2026/01/13</span></h5>
    """
    try:
        text = page.locator("#assetBody h5 span").inner_text().strip()
        return text.replace("/", "-")
    except Exception as e:
        print(f"❌ 無法取得資料日期: {e}", file=sys.stderr)
        return None
    

def scrape_holdings(page: Page, latest_date: str) -> List[Dict]:
    results: List[Dict] = []
    
    # 1. 抓股票部位 (這部分你已經穩定，維持原樣)
    tables = page.locator("#assetBody table").all()
    for table in tables:
        rows = table.locator("tbody tr").all()
        for r in rows:
            tds = r.locator("td").all()
            if len(tds) == 4:
                try:
                    code = tds[0].inner_text().strip()
                    if code.isdigit():
                        results.append({
                            "date": latest_date, "code": code, "name": tds[1].inner_text().strip(),
                            "shares": float(tds[2].inner_text().replace(",", "")),
                            "pct": float(tds[3].inner_text().replace("%", "")),
                            "type": "STOCK"
                        })
                except: continue

    # 2. 財務數據輔助處理
    def clean_num(text):
        if not text: return 0.0
        cleaned = text.replace("NTD", "").replace(",", "").replace("%", "").replace("\xa0", "").strip()
        try:
            return float(cleaned)
        except:
            return 0.0

    # 3. 遍歷所有表格單元格來尋找「淨資產」與其他財務項
    # 這是最穩定的策略：尋找包含關鍵字的 TD，抓取它後面的 TD
    all_tds = page.locator("td").all()
    
    found_asset = False
    for i, td in enumerate(all_tds):
        txt = td.inner_text().strip()
        
        # --- 處理淨資產 ---
        if "淨資產" in txt and not found_asset:
            # 嘗試抓取同一個 TD 裡的數字，或是下一個 TD 裡的數字
            import re
            content = td.inner_text()
            # 如果目前 TD 沒數字，檢查下一個 TD
            if not re.search(r'\d', content) and (i + 1) < len(all_tds):
                content = all_tds[i+1].inner_text()
            
            nums = re.findall(r'[\d,]{7,}', content) # 找長數字
            if nums:
                results.append({
                    "date": latest_date, "code": "TOTAL_ASSET", "name": "淨資產",
                    "shares": clean_num(nums[0]), "pct": 100.0, "type": "FIN"
                })
                found_asset = True
                print(f"✅ 成功抓取淨資產: {nums[0]}")

        # --- 處理現金 ---
        elif "現金" in txt or "銀行存款" in txt:
            if (i + 2) < len(all_tds):
                results.append({
                    "date": latest_date, "code": "CASH", "name": "現金及其他",
                    "shares": clean_num(all_tds[i+1].inner_text()),
                    "pct": clean_num(all_tds[i+2].inner_text()),
                    "type": "FIN"
                })

        # --- 處理應收付 ---
        elif "應收付" in txt:
            if (i + 2) < len(all_tds):
                results.append({
                    "date": latest_date, "code": "RECEIVABLE", "name": "應收付證券款",
                    "shares": clean_num(all_tds[i+1].inner_text()),
                    "pct": clean_num(all_tds[i+2].inner_text()),
                    "type": "FIN"
                })

    return results


# ----------------------------------------------------------
# 主程式
# ----------------------------------------------------------

if __name__ == "__main__":

    print("🚀 啟動 00981A Daily 吃豆腐神器 (V12)")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)

            latest_date = get_latest_date(page)
            if not latest_date:
                print("❌ 無法取得資料日期，程式終止", file=sys.stderr)
                sys.exit(1)

            print(f"📅 資料日期: {latest_date}")

            holdings = scrape_holdings(page, latest_date)
            browser.close()

    except PlaywrightTimeoutError:
        print("❌ 頁面載入逾時", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ 擷取過程發生錯誤: {e}", file=sys.stderr)
        sys.exit(1)

    if not holdings:
        print("❌ 未取得任何持股資料", file=sys.stderr)
        sys.exit(1)

    # ------------------------------------------------------
    # 同日更新邏輯（選項 C）
    # ------------------------------------------------------
    existing_dates = load_existing_dates()

    if latest_date in existing_dates:
        print(f"🔄 偵測到 {latest_date} 已存在，進行覆寫更新")
        rewrite_without_date(latest_date)
    else:
        print(f"➕ 新資料日期 {latest_date}，準備寫入")

    if not append_rows(holdings):
        sys.exit(1)

    # ------------------------------------------------------
    # 輸出摘要 (最上面的淨資產、CASH 水位、應收付證券款，這三個數字在compare會列出，這邊先註解起來，有需要可以再取消註解)
    # ------------------------------------------------------
    # 取得財務區數據
    # fin_data = {h["code"]: h for h in holdings if h["type"] == "FIN"}
    # total_asset = fin_data.get("TOTAL_ASSET", {"shares": 0})["shares"]
    # cash_info = fin_data.get("CASH", {"pct": 0.0, "shares": 0})

    # print("-" * 30)
    # # print(f"💰 淨資產: NTD {total_asset:,}")
    # # print(f"💰 CASH 水位: {cash_info['pct']:.3f}% (NTD {cash_info['shares']:,})")
    
    # if "RECEIVABLE" in fin_data:
    #     r = fin_data["RECEIVABLE"]
    #     print(f"🧾 應收付證券款: {r['pct']}% (NTD {r['shares']:,})")
    # print("-" * 30)

    # 僅針對股票進行排序 (過濾掉 FIN 類型)
    stock_only = [h for h in holdings if h["type"] == "STOCK"]
    # 🌟 修改處 1: 將 [:30] 改為 [:20] 
    top_ranking = sorted(stock_only, key=lambda x: x["pct"], reverse=True)[:30]

    # 🌟 修改處 2: 標題改為 TOP 30
    print(f"TOP 30 持股排行榜 (資料日期: {latest_date})：")
    print(f"{'名次':<4} {'代號':<8} {'名稱':<12} {'權重':<8}")
    
    # 🌟 修改處 3: 走訪 top_20 列表
    for i, h in enumerate(top_ranking, 1):
        print(f"{i:<4} {h['code']:<8} {h['name']:<12} {h['pct']:>6.2f}%")

    # print(f"\n✅ 成功寫入 {len(holdings)} 筆資料至 {CSV_FILE}")
    print(f"\n📌 資料來源: https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=49YTW")
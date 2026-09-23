import os
import csv
import requests

# ========= 設定 =========
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "three_big_investors.csv")

# 欄位（新增 margin_bal, margin_chg, foreign_oi, state）
HEADERS = ["date", "index", "change", "turnover_B",
           "self_buy", "self_hedge", "trust", "foreign",
           "margin_bal", "margin_chg", "foreign_oi", "state"]

# 狀態機門檻
TH_HOT = 8.0        # 🟡 20日融資增速 (%)
TH_DELEV = -1.5     # 🔴 單日融資減幅 (%)
TH_WASHED = -10.0   # 🟠 距高點回落 (%)
TH_REBOUND = 1.0    # 🟢 自近10日低點回升 (%)

# ========= 工具函數 =========
def to_billion(num_str):
    try:
        return round(float(num_str.replace(",", "")) / 1_000_000_000, 3)
    except:
        return 0.0

def load_history():
    if not os.path.isfile(CSV_FILE):
        return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def save_history(rows):
    """整檔重寫（自動補齊舊資料缺的新欄位）"""
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in HEADERS})

# ========= 資料抓取 =========
def fetch_twse_ui():
    """大盤指數/成交金額（原本的 Playwright 邏輯，不變）"""
    from playwright.sync_api import sync_playwright
    url = "https://www.twse.com.tw/zh/trading/historical/fmtqik.html"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=60000)
        page.wait_for_selector("table tbody tr", timeout=60000)
        rows = page.query_selector_all("table tbody tr")
        cells = rows[-1].query_selector_all("td")
        date_raw = cells[0].inner_text().strip()
        turnover_raw = cells[2].inner_text().strip()
        index_close_raw = cells[4].inner_text().strip()
        change_raw = cells[5].inner_text().strip()
        browser.close()
    return (date_raw,
            float(index_close_raw.replace(",", "")),
            float(change_raw.replace(",", "")),
            round(float(turnover_raw.replace(",", "")) / 1_000_000_000, 1))

def fetch_institutional():
    """三大法人（不變）"""
    url = "https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json"
    f_rows = requests.get(url, timeout=10).json()["data"]
    return (to_billion(f_rows[0][3]), to_billion(f_rows[1][3]),
            to_billion(f_rows[2][3]), to_billion(f_rows[3][3]))

def fetch_margin():
    """融資餘額（億）。回傳 (今日餘額, 前日餘額, 資料日期YYYYMMDD)"""
    url = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=MS"
    data = requests.get(url, timeout=10).json()
    for row in data["tables"][0]["data"]:
        if row[0].startswith("融資金額"):
            prev = round(float(row[4].replace(",", "")) / 100_000, 1)
            today = round(float(row[5].replace(",", "")) / 100_000, 1)
            return today, prev, data.get("date", "")
    raise ValueError("MI_MARGN 找不到融資金額列")

def fetch_foreign_futures(date_str):
    """外資台指期未平倉淨口數。失敗回傳 None（不影響其他訊息）"""
    try:
        r = requests.post(
            "https://www.taifex.com.tw/cht/3/futContractsDateDown",
            data={"queryType": "1", "goDay": "", "doQuery": "1",
                  "dateaddcnt": "", "queryDate": date_str,
                  "queryStartDate": date_str, "queryEndDate": date_str,
                  "commodityId": "TXF"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                     "Referer": "https://www.taifex.com.tw/cht/3/futContractsDate"},
            timeout=15)
        text = r.content.decode("cp950", errors="ignore")
        for line in text.splitlines():
            cols = [c.strip() for c in line.split(",")]
            if len(cols) >= 14 and "外資" in cols[2]:
                return int(cols[13].replace(",", ""))  # 多空未平倉口數淨額
    except Exception:
        pass
    return None

# ========= 狀態機 =========
def decide_state(prev_state, margins):
    """margins: 含今日的融資餘額 list（舊→新）。回傳 (state, reason)"""
    m = margins[-1]
    if len(margins) < 2:
        return "⚪", "資料累積中"
    daily_pct = (m / margins[-2] - 1) * 100
    peak = max(margins[-60:])
    dd_pct = (m / peak - 1) * 100
    chg20 = (m / margins[-21] - 1) * 100 if len(margins) >= 21 else None
    low10 = min(margins[-11:-1]) if len(margins) >= 11 else None

    if prev_state in ("⚪", "🟡", ""):
        if daily_pct <= TH_DELEV:
            return "🔴", f"單日 {daily_pct:+.1f}% ≤ {TH_DELEV}%，去槓桿"
        if chg20 is None:
            return "⚪", f"20日增速需累積資料({len(margins)-1}/20)"
        return "🟡", f"20日 {chg20:+.1f}%" + ("（>8% 過熱）" if chg20 >= TH_HOT else "（未過熱）")
    if prev_state == "🔴":
        if m >= peak:
            return "🟡", "融資重回前高，假摔解除"
        if dd_pct <= TH_WASHED:
            return "🟠", f"距高點 {dd_pct:+.1f}% ≤ -10%，可分批試單"
        return "🔴", f"距高點 {dd_pct:+.1f}%，去槓桿未結束"
    if prev_state == "🟠":
        if low10 and m >= low10 * (1 + TH_REBOUND / 100):
            return "🟢", f"自10日低點 {low10:,.0f} 億回升 {(m/low10-1)*100:+.1f}%，洗淨"
        if m >= peak:
            return "🟡", "融資重回前高"
        return "🟠", f"距高點 {dd_pct:+.1f}%，等待止跌回升"
    if prev_state == "🟢":
        if chg20 is not None and chg20 >= TH_HOT:
            return "🟡", f"20日 {chg20:+.1f}% 重新過熱，新循環"
        return "🟢", "低檔整理中"
    return "⚪", "未知狀態重置"

# ========= 主流程 =========
def fetch_data_and_save():
    date_raw, index_close, change_points, turnover_billion = fetch_twse_ui()
    self_buy, self_hedge, trust, foreign = fetch_institutional()

    parts = date_raw.split('/')
    ad_date = f"{int(parts[0])+1911}-{parts[1]}-{parts[2]}"

    margin_today, margin_prev, margin_date = fetch_margin()
    margin_chg = round(margin_today - margin_prev, 1)
    margin_stale = margin_date != ad_date.replace("-", "")
    stale_txt = "（⚠️ 尚未更新，為前日值）" if margin_stale else ""
    foreign_oi = fetch_foreign_futures(f"{int(parts[0])+1911}/{parts[1]}/{parts[2]}")

    # 歷史 + 狀態機
    history = [r for r in load_history() if r.get("date") != ad_date]  # 防重複執行
    margins = [float(r["margin_bal"]) for r in history if r.get("margin_bal")]
    prev_state = next((r["state"] for r in reversed(history) if r.get("state")), "")
    if margin_stale:
        state, reason = prev_state or "⚪", "融資尚未公布，沿用前日狀態"
    else:
        margins.append(margin_today)
        state, reason = decide_state(prev_state, margins)

    peak = max(margins[-60:])
    dd_pct = (margin_today / peak - 1) * 100
    chg20_txt = f"{(margin_today/margins[-21]-1)*100:+.1f}%" if len(margins) >= 21 else "N/A(資料未滿20日)"

    # 外資空單：日增減、5日增減、歷史分位（越高越空）
    oi_txt = "N/A（抓取失敗）"
    if foreign_oi is not None:
        ois = [int(float(r["foreign_oi"])) for r in history if r.get("foreign_oi")]
        parts_txt = []
        if ois:
            parts_txt.append(f"日 {foreign_oi-ois[-1]:+,}")
        if len(ois) >= 5:
            parts_txt.append(f"5日 {foreign_oi-ois[-5]:+,}")
        oi_txt = f"{foreign_oi:+,} 口" + (f"（{'｜'.join(parts_txt)}）" if parts_txt else "")

    history.append({
        "date": ad_date, "index": index_close, "change": change_points,
        "turnover_B": turnover_billion, "self_buy": self_buy,
        "self_hedge": self_hedge, "trust": trust, "foreign": foreign,
        "margin_bal": "" if margin_stale else margin_today,
        "margin_chg": "" if margin_stale else margin_chg,
        "foreign_oi": foreign_oi if foreign_oi is not None else "",
        "state": state,
    })
    save_history(history)

    # 乖離率（60日均線）與成交金額分位數（近一年），由 CSV 既有欄位計算
    idx = [float(r["index"]) for r in history if r.get("index")]
    tos = [float(r["turnover_B"]) for r in history if r.get("turnover_B")]
    if len(idx) >= 60:
        bias = (index_close / (sum(idx[-60:]) / 60) - 1) * 100
        bias_tag = "過熱" if bias >= 10 else "超跌" if bias <= -10 else "中性"
        bias_txt = f"{bias:+.1f}%（{bias_tag}）"
    else:
        bias_txt = f"N/A(資料 {len(idx)}/60 日)"
    win = tos[-120:]  # 半年

    to_pct = sum(1 for t in win if t <= turnover_billion) / len(win) * 100
    to_tag = "爆量" if to_pct >= 90 else "量縮" if to_pct <= 20 else "正常"
    to_txt = f"{to_pct:.0f}%（{to_tag}）"

    message = f"""
📊 台股盤後數據（{date_raw}）

大盤收盤指數: {index_close:,.2f}
漲跌點數: {change_points:+.2f} 點
今日成交金額: {turnover_billion:.1f} B

自營商(自行買賣): {self_buy:+.3f} B
自營商(避險): {self_hedge:+.3f} B
投信: {trust:+.3f} B
外資及陸資: {foreign:+.3f} B

融資餘額: {margin_today:,.1f} 億（{margin_chg:+,.1f} 億）｜距高點 {dd_pct:+.1f}%{stale_txt}
外資期貨空單: {oi_txt}
60日乖離率: {bias_txt}｜成交熱度: {to_txt}

狀態: {state} {reason}"""
    print(message)

if __name__ == "__main__":
    fetch_data_and_save()

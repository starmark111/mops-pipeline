import math
import requests
import yfinance as yf
from datetime import datetime

def fetch_tx_futures():
    """回傳 (price, pct_vs_ref, is_open)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Referer':    'https://mis.taifex.com.tw/',
        'Content-Type': 'application/json',
    }
    now_h = datetime.now().hour + datetime.now().minute / 60
    # 盤初試撮通常是08:45開始，夜盤是15:00開始。
    mkt   = '0' if 8 <= now_h < 15 else '1'
    try:
        resp = requests.post(
            'https://mis.taifex.com.tw/futures/api/getQuoteList',
            json={'SymbolType': 'F', 'MarketType': mkt, 'commodity_id': 'TX', 'currency': 'NTD'},
            headers=headers, timeout=8
        )
        for item in resp.json().get('RtData', {}).get('QuoteList', []):
            sid  = item.get('SymbolID', '')
            last = item.get('CLastPrice', '').replace(',', '')
            ref  = item.get('CRefPrice',  '').replace(',', '')
            opn  = item.get('COpenPrice', '').replace(',', '')
            
            if sid.startswith('TXF') and sid.endswith('-F') and last and last != '0' and ref and ref != '0':
                price   = float(last)
                ref_p   = float(ref)
                diff    = price - ref_p
                pct_ref = (price / ref_p - 1) if ref_p else 0
                is_open = bool(opn and opn != '0')
                return price, diff, pct_ref, is_open
    except Exception:
        pass
    
    # 若期交所沒資料，用 Yahoo ^TWII 大盤備援
    try:
        tk   = yf.Ticker("^TWII")
        info = tk.fast_info
        last = info.get('last_price') or info.get('lastPrice')
        prev = info.get('previous_close') or info.get('previousClose')
        if last and prev and prev != 0:
            return last, last - prev, (last / prev - 1), False
    except Exception:
        pass

    return None, None, None, False

def fetch_yf(sym):
    """回傳 (price, diff, pct_vs_prev) — 針對台股指數優化

    昨收不用 fast_info 的 previous_close：^TWII 等指數的日線常延遲，
    previous_close 會回到前天甚至更早的收盤，導致漲跌點/幅算錯。
    改由日線自行判斷：最後一根若是「今天」→ 昨收取倒數第二根，否則取最後一根。"""
    try:
        tk     = yf.Ticker(sym)
        closes = tk.history(period="10d").sort_index()['Close'].dropna()
        if len(closes) == 0:
            return None, None, None

        last = None
        try:
            info = tk.fast_info
            last = info.get('last_price') or info.get('lastPrice') or info.get('regularMarketPrice')
            last = float(last) if last and not math.isnan(float(last)) else None
        except Exception:
            pass
        if last is None:
            last = float(closes.iloc[-1])

        today = datetime.now().date()
        last_bar_is_current = (
            closes.index[-1].date() == today
            or abs(last - float(closes.iloc[-1])) < 1e-9
        )
        if last_bar_is_current and len(closes) >= 2:
            prev = float(closes.iloc[-2])
        else:
            prev = float(closes.iloc[-1])

        if not prev or math.isnan(prev):
            return last, None, None
        return last, last - prev, (last / prev - 1)
    except Exception:
        return None, None, None

def print_row(name, price, diff, pct, date_str=None):
    if price is None:
        print(f"{name}:數據暫時無法取得")
        return
        
    p_str = f"{price:,.2f}" if price < 1000 else f"{price:,.0f}"
    
    if diff is not None and pct is not None:
        d_str = f"{diff:+.2f}" if price < 1000 else f"{diff:+.0f}"
        pct_str = f"({d_str} / {pct*100:+.2f}%)"
    else:
        pct_str = "(無漲跌資料)"
    
    if date_str:
        print(f"{name}({date_str}):{p_str}{pct_str}")
    else:
        print(f"{name}:{p_str}{pct_str}")

def get_intraday_scan():
    now = datetime.now()
    print(f"盤中觀察({now.strftime('%H:%M')})")

    # 1. 台指期數據
    tx_price, tx_diff, tx_pct, is_open = fetch_tx_futures()
    session = "[盤中]" if is_open else "[盤前/後]"
    if tx_price is None:
        print("台指期:數據暫時無法取得")
    else:
        d_str = f"{tx_diff:+.0f}" if tx_diff is not None else "+0"
        print(f"台指期:{tx_price:,.0f}({d_str} / {tx_pct*100:+.2f}%) {session}")

    # 2. 國內現貨
    yf_local = [
        ('^TWII',   '台股大盤'),
        ('^TWOII',  '櫃買指數'),
        ('2330.TW', '台積電'),
    ]
    for sym, name in yf_local:
        p, d, pc = fetch_yf(sym)
        print_row(name, p, d, pc)

    print("-" * 20)

    # 3. 國際指數
    yf_intl = [
        ('^N225', '日股'),
        ('^KS11', '韓股'),
    ]
    for sym, name in yf_intl:
        try:
            h = yf.Ticker(sym).history(period="5d")
            if len(h) < 2:
                print(f"{name}:⚠️ 資料不足(可能休市)")
                continue
            d_str = h.index[-1].strftime('%m/%d')
            p = float(h['Close'].iloc[-1])
            prev = float(h['Close'].iloc[-2])
            diff = p - prev
            pc = (p / prev - 1)
            print_row(name, p, diff, pc, date_str=d_str)
        except Exception:
            print(f"{name}:數據暫時無法取得")

if __name__ == "__main__":
    get_intraday_scan()

import json
import math
import os
import sys
import time as _time
import yfinance as yf
import requests
from datetime import datetime, time as dtime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

TZ_TW = ZoneInfo('Asia/Taipei')
TZ_NY = ZoneInfo('America/New_York')


def now_tw():
    return datetime.now(TZ_TW)


# ── 時段判斷 ──────────────────────────────────────────────────────────

def is_us_market_open(now):
    """判斷美股是否正在交易（自動處理夏令/冬令）"""
    now_ny = now.astimezone(TZ_NY)
    return (now_ny.weekday() < 5 and
            dtime(9, 30) <= now_ny.time() < dtime(16, 0))


def is_us_post_close(now):
    """美股今日是否已收盤"""
    now_ny = now.astimezone(TZ_NY)
    return (now_ny.weekday() < 5 and now_ny.time() >= dtime(16, 0))


def get_session(now):
    """
    回傳當前時段：
      closed         - 休市（週六 05:00 後、週日 15:00 前）
      pre_market     - 盤前（週一至五 05:00–08:44）
      day            - 日盤（週一至五 08:45–13:44）
      post_day       - 盤後（週一至五 13:45–14:59）
      night_pre_us   - 夜盤 + 美股未開
      night_us_open  - 夜盤 + 美股交易中
      night_us_after - 夜盤 + 美股已收盤
    """
    wd = now.weekday()   # 0=Mon … 6=Sun
    h  = now.hour + now.minute / 60.0

    # 週六 05:00 後：休市
    if wd == 5 and h >= 5:
        return 'closed'

    # 週日 15:00 前：休市
    if wd == 6 and h < 15:
        return 'closed'

    # 夜盤時段：
    #   週一–週六 00:00–05:00（前一天夜盤延續；週六05:00後已被攔截）
    #   週一–週五 15:00–24:00
    #   週日 15:00–24:00
    in_night = (h < 5 and wd in [0, 1, 2, 3, 4, 5]) or \
               (h >= 15 and wd in [0, 1, 2, 3, 4, 6])

    if in_night:
        if is_us_market_open(now):   return 'night_us_open'
        if is_us_post_close(now):    return 'night_us_after'
        return 'night_pre_us'

    # 週一至週五日間
    if 5 <= h < 8.75:    return 'pre_market'
    if 8.75 <= h < 13.75: return 'day'
    return 'post_day'   # 13:45–14:59


# ── 資料抓取 ──────────────────────────────────────────────────────────

def get_tx_price(prefer_night=False):
    """
    抓台指期近月合約報價。
    prefer_night=True 優先抓夜盤（MarketType=1），否則優先日盤。
    回傳 (price, diff, pct, tag_str)，失敗回傳 (None, None, None, None)
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
               'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'}
    mkt_order = ['1', '0'] if prefer_night else ['0', '1']
    url = 'https://mis.taifex.com.tw/futures/api/getQuoteList'

    for mkt in mkt_order:
        try:
            body = {'SymbolType': 'F', 'MarketType': mkt,
                    'commodity_id': 'TX', 'currency': 'NTD'}
            resp = requests.post(url, json=body, headers=headers, timeout=8)
            data = resp.json()
            rt   = data.get('RtData') or {}
            for item in rt.get('QuoteList', []):
                sid = item.get('SymbolID', '')
                if not (sid.endswith('-F') or sid.endswith('-M')):
                    continue
                last_str = item.get('CLastPrice', '').replace(',', '')
                ref_str  = item.get('CRefPrice',  '').replace(',', '')
                try:
                    last = float(last_str)
                    ref  = float(ref_str) if ref_str else 0
                except ValueError:
                    continue
                if last <= 0:
                    continue
                ref  = ref if ref > 0 else last
                diff = last - ref
                pct  = (last / ref - 1) if ref else 0
                tag  = '夜盤' if mkt == '1' else '日盤'
                return last, diff, pct, tag
        except Exception:
            pass

    # 備援：yfinance
    try:
        h = yf.Ticker('TXF=F').history(period='5d')
        if len(h) >= 2:
            price = float(h['Close'].iloc[-1])
            prev  = float(h['Close'].iloc[-2])
            diff  = price - prev
            pct   = (price / prev - 1) if prev else 0
            return price, diff, pct, 'TXF=F'
    except Exception:
        pass

    return None, None, None, None


def fetch_yf(sym):
    """抓即時/最新收盤，回傳 (price, diff, pct)

    昨收不用 fast_info 的 previous_close：^TWII 等指數的日線常延遲，
    previous_close 會回到前天甚至更早的收盤，導致漲跌點/幅算錯
    （例：2026/06/11 大盤誤報 -1582 / -3.54%）。
    改由日線自行判斷：最後一根若是「今天」→ 昨收取倒數第二根，否則取最後一根。"""
    try:
        tk     = yf.Ticker(sym)
        closes = tk.history(period='10d').sort_index()['Close'].dropna()
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

        today = now_tw().date()
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
        pct = last / prev - 1
        if abs(pct) > 0.15:
            # 合理性檢查：台股有10%漲跌限制，>15% 必為 Yahoo 壞資料
            # → 退回純日線收盤計算；仍異常則只給價格不給漲跌
            l2, p2 = float(closes.iloc[-1]), float(closes.iloc[-2])
            if p2 and abs(l2 / p2 - 1) <= 0.15:
                return l2, l2 - p2, l2 / p2 - 1
            return last, None, None
        return last, last - prev, pct
    except Exception:
        return None, None, None


# ── 證交所 MIS 即時 API（指數用，比 Yahoo 穩定）──────────────────────
# 為什麼要有這段：Yahoo 的櫃買指數 ^TWOII 盤中很常回空值 → 之前常見
# 「櫃買指數:數據暫時無法取得」。大盤/個股走 Yahoo 還算穩，但櫃買必須換源。
MIS_URL = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
MIS_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36',
    'Referer': 'https://mis.twse.com.tw/stock/index.jsp',
    'Accept': 'application/json, text/javascript, */*; q=0.01',
}

DEBUG = False


def _dbg(*a):
    if DEBUG:
        print('[debug]', *a, file=sys.stderr)


def _numf(s):
    """數值轉換（允許負數，供漲跌用）。'-'/''/None → None"""
    try:
        return float(str(s).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def _num(s):
    """正數轉換（價格/指數用）。'23,456.78' → 23456.78；'-'/''/0 → None"""
    v = _numf(s)
    return v if (v is not None and v > 0) else None


def fetch_mis(chs, retries=3):
    """證交所 MIS 即時報價。chs 例：['tse_t00.tw','otc_o00.tw','tse_2330.tw']
    回傳 {ch: msg_dict}，失敗回 {}。"""
    params = {'ex_ch': '|'.join(chs), 'json': '1', 'delay': '0',
              '_': str(int(_time.time() * 1000))}
    for attempt in range(retries):
        try:
            r = requests.get(MIS_URL, params=params, headers=MIS_HEADERS, timeout=8)
            data = r.json()
            out = {}
            for m in data.get('msgArray', []):
                out[f"{m.get('ex','')}_{m.get('c','')}.tw"] = m
            if out:
                _dbg('MIS ok rtcode=', data.get('rtcode'), 'keys=', list(out))
                for k, m in out.items():
                    _dbg('  ', k, {kk: m.get(kk) for kk in
                                   ('n', 'z', 'y', 'o', 'h', 'l', 'v', 'tv', 'a', 't')})
                return out
            _dbg('MIS 回空 msgArray, rtcode=', data.get('rtcode'))
        except Exception as e:
            _dbg(f'MIS attempt {attempt+1} failed: {e!r}')
        _time.sleep(0.6)
    return {}


def mis_quote(m):
    """MIS msg → (price, diff, pct)。z 為 '-'（瞬間無成交）時依序退回其他欄位。"""
    if not m:
        return None, None, None
    price = _num(m.get('z'))
    if price is None:
        for k in ('pz', 'o', 'h', 'l'):
            price = _num(m.get(k))
            if price:
                break
    prev = _num(m.get('y'))
    if price is None:
        return None, None, None
    if not prev:
        return price, None, None
    return price, price - prev, price / prev - 1


def fetch_tpex_index():
    """櫃買官方 OpenAPI 日線（已實測可用）→ (close, diff, pct)。
    當 MIS 盤中無值（盤前/收盤後）時，比 Yahoo 的 ^TWOII 可靠。"""
    try:
        r = requests.get('https://www.tpex.org.tw/openapi/v1/tpex_index',
                         headers=MIS_HEADERS, timeout=10)
        arr = [x for x in r.json() if _num(x.get('Close'))]
        arr.sort(key=lambda x: str(x.get('Date')))
        if not arr:
            return None, None, None
        last = arr[-1]
        close = _num(last.get('Close'))
        chg = _numf(last.get('Change'))          # 可能為負，需用 _numf
        if chg is None:
            return close, None, None
        prev = close - chg
        return close, chg, (chg / prev if prev else None)
    except Exception as e:
        _dbg('TPEx index 失敗', repr(e))
        return None, None, None


def fetch_index(mis_ch, yf_sym, mis_cache=None):
    """指數/個股行情：MIS 官方優先 → 官方日線備援 → 最後才 yfinance。"""
    m = (mis_cache or {}).get(mis_ch)
    if m is None:
        m = fetch_mis([mis_ch]).get(mis_ch)
    p, d, pc = mis_quote(m)
    if p is not None:
        return p, d, pc
    if mis_ch == 'otc_o00.tw':               # 櫃買：改用 TPEx 官方，別用不穩的 ^TWOII
        _dbg('櫃買 MIS 無值 → 退 TPEx 官方日線')
        p, d, pc = fetch_tpex_index()
        if p is not None:
            return p, d, pc
    _dbg(f'{mis_ch} 無值 → 退 yfinance {yf_sym}')
    return fetch_yf(yf_sym)


# ── 大盤成交金額（盤中累計 + 全日預估）────────────────────────────────
# 註：盤中累計成交金額的官方端點欄位名各版本略有不同，故採「多來源嘗試」，
#     並在 --debug 模式印出實際回應，方便確認到底哪個可用。
def _roc_to_iso(s):
    """民國日期 '1150803' → '2026-08-03'"""
    s = str(s or '')
    if len(s) == 7 and s.isdigit():
        return f"{int(s[:3]) + 1911:04d}-{s[3:5]}-{s[5:7]}"
    return s


def fetch_twse_daily_stats():
    """證交所每日成交量值 FMTQIK（官方 OpenAPI，已實測可用）。
    回傳 [{'iso','value'(元),'volume'(股)}]，本月由舊到新。"""
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/FMTQIK',
                         headers=MIS_HEADERS, timeout=10)
        out = []
        for it in r.json():
            val = _num(it.get('TradeValue'))
            if val:
                out.append({'iso': _roc_to_iso(it.get('Date')),
                            'value': val,
                            'volume': _num(it.get('TradeVolume'))})
        out.sort(key=lambda x: x['iso'])
        _dbg(f'FMTQIK 取得 {len(out)} 日，最後一日 {out[-1]["iso"] if out else "-"}')
        return out
    except Exception as e:
        _dbg('FMTQIK 失敗', repr(e))
        return []


# ── 權值股回推大盤成交金額 ───────────────────────────────────────────
# 背景（皆實測）：MIS 指數不給成交量、getStatis 回 9999、FMTQIK 只有日收、
# Yahoo 的 ^TWII 分鐘量恆為 0 → 官方沒有現成的「盤中大盤成交金額」。
# 作法：抓昨日成交金額前 N 大個股（自動適應當下量能主力，不必維護成分股清單），
#      算出「這籃昨日佔大盤金額比例」，盤中用 MIS 查這籃即時累積量回推全市場。
# 精度：屬推估，早盤誤差較大、越近收盤越準；只適合判斷量能級距，不等同券商軟體數字。
BASKET_N = int(os.environ.get('MC_BASKET_N', '100'))
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cache')


def _cache_read(name, max_age_sec):
    p = os.path.join(CACHE_DIR, name)
    try:
        if _time.time() - os.path.getmtime(p) < max_age_sec:
            with open(p, encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _cache_write(name, obj):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, name), 'w', encoding='utf-8') as f:
            json.dump(obj, f)
    except Exception as e:
        _dbg('快取寫入失敗', repr(e))


def fetch_prev_stock_values():
    """昨日各上市個股成交金額 {代號: 金額}。官方 STOCK_DAY_ALL，快取 12 小時。"""
    cached = _cache_read('stock_day_all.json', 12 * 3600)
    if cached:
        _dbg(f'STOCK_DAY_ALL 用快取，{len(cached)} 檔')
        return cached
    try:
        r = requests.get('https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
                         headers=MIS_HEADERS, timeout=25)
        d = {}
        for it in r.json():
            code, val = it.get('Code'), _num(it.get('TradeValue'))
            if code and val:
                d[code] = val
        if d:
            _cache_write('stock_day_all.json', d)
            _dbg(f'STOCK_DAY_ALL 取得 {len(d)} 檔')
        return d
    except Exception as e:
        _dbg('STOCK_DAY_ALL 失敗', repr(e))
        return {}


def estimate_turnover_by_basket(now=None):
    """用權值股籃回推大盤盤中累計成交金額（元）。回傳 (金額, 說明) 或 (None, None)。"""
    prev_vals = fetch_prev_stock_values()
    hist = fetch_twse_daily_stats()
    if not prev_vals or not hist:
        return None, None
    prev_total = hist[-1]['value']

    top = sorted(prev_vals.items(), key=lambda kv: -kv[1])[:BASKET_N]
    basket_prev = sum(v for _, v in top)
    share = basket_prev / prev_total if prev_total else 0
    if not (0.2 < share < 1.0):            # 比例不合理就不用
        _dbg(f'籃子佔比異常 {share:.2%}，放棄回推')
        return None, None
    _dbg(f'籃子 {len(top)} 檔，昨日佔大盤 {share:.1%}')

    chs = [f'tse_{c}.tw' for c, _ in top]
    basket_now, got_n = 0.0, 0
    for i in range(0, len(chs), 40):       # 分批查，避免單次請求過長
        for ch, m in fetch_mis(chs[i:i + 40], retries=2).items():
            vol = _num(m.get('v'))         # 個股累積成交量（張）
            hi, lo = _num(m.get('h')), _num(m.get('l'))
            avg = (hi + lo) / 2 if (hi and lo) else (_num(m.get('z')) or _num(m.get('o')))
            if vol and avg:
                basket_now += vol * 1000 * avg
                got_n += 1
    _dbg(f'籃子即時取得 {got_n} 檔，金額 {basket_now/1e8:,.0f} 億')
    if got_n < len(chs) * 0.5 or basket_now <= 0:
        _dbg('籃子取得檔數不足，放棄')
        return None, None

    est = basket_now / share
    return est, f'{got_n}檔回推(昨佔{share*100:.0f}%)'


def fetch_intraday_turnover(mis_cache=None, now=None):
    """盤中大盤累計成交金額（元）。回傳 (金額, 來源說明) 或 (None, None)。

    背景（皆為實測結果）：
      · getStatis 端點回 rtcode 9999 → 不可用
      · FMTQIK 只有「日收」資料 → 盤中拿不到今日值
      · 盤前 MIS 的 t00 只有 y（昨收），盤中才會多出成交欄位
    因 MIS 對指數的成交欄位單位不一定（元／億元／股／張），這裡**自動判斷單位**：
    對每種解讀算出「依時間進度外推的全日金額」，只接受與前一交易日同量級
    （0.2~5 倍）者，避免單位猜錯而報出離譜數字。"""
    m = (mis_cache or {}).get('tse_t00.tw')
    if m is None:
        m = fetch_mis(['tse_t00.tw']).get('tse_t00.tw')
    if not m:
        return None, None
    _dbg('t00 欄位:', {k: v for k, v in m.items()
                      if k in ('z', 'y', 'm', 'r', 'v', 'tv', 'a', 's', 'o', 'h', 'l', 't')})

    hist = fetch_twse_daily_stats()
    prev_val = hist[-1]['value'] if hist else None
    prev_vol = hist[-1]['volume'] if hist else None
    per_share = (prev_val / prev_vol) if (prev_val and prev_vol) else None
    # 均價微調：昨日每股均價 × (今日指數/昨收指數)。
    # 當日大漲大跌時，成交均價會跟著移動，不調整會系統性低估/高估。
    # （近似值：指數為市值加權、成交均價為成交量加權，兩者不完全等比）
    if per_share:
        idx_now, idx_prev = _num(m.get('z')), _num(m.get('y'))
        if idx_now and idx_prev:
            adj = idx_now / idx_prev
            if 0.9 < adj < 1.1:                 # 只接受合理範圍，避免壞資料
                per_share *= adj
                _dbg(f'均價微調 ×{adj:.4f} → {per_share:.2f} 元/股')
    ratio = volume_progress(now or now_tw())

    def _accept(amt, label):
        """合理性檢查：換算後須為 10 億～5 兆，且外推全日與昨日同量級。"""
        if not (1e9 < amt < 5e12):
            return False
        if prev_val and ratio > 0.05:
            full = amt / ratio
            if not (0.2 * prev_val < full < 5 * prev_val):
                return False
        _dbg(f'採用解讀 {label}: {amt/1e8:,.0f} 億')
        return True

    # (1) MIS t00 的成交欄位。**實測 2026/08/25：`m` = 全市場累積成交量（千股／張）**
    #     （t00 的 m=8,727,116 對上昨日 FMTQIK 8,082,548,083 股＝8,082,548 千股，同量級；
    #      getStatis 帶 cookie 回傳的 tv 亦為同值，互相佐證）
    #     金額＝m × 1000 股 × 昨日每股均價；下面會自動挑出合理的單位解讀。
    for k in ('a', 'amt', 'val', 'm', 'v', 'tv'):
        raw = _num(m.get(k))
        if not raw:
            continue
        interps = [(raw, f'{k}＝元'), (raw * 1e8, f'{k}＝億元')]
        if per_share:
            interps += [(raw * per_share, f'{k}＝股×昨均價'),
                        (raw * 1000 * per_share, f'{k}＝張×昨均價')]
        for amt, label in interps:
            if _accept(amt, label):
                return amt, f'MIS.{label}'

    # (2) 權值股籃回推（目前的主力方案，來源為官方 MIS）
    est, note = estimate_turnover_by_basket(now)
    if est and _accept(est, f'權值股籃 {note}'):
        return est, f'BASKET.{note}'

    # (3) 備援：yfinance ^TWII 分鐘量 —— **實測無效，預設關閉**
    #     2026/08/25 11:28 盤中實測：26 根 5 分 K 的 Volume 全為 0。
    #     Yahoo 對台股「指數」不提供成交量，這條路是死的；留著只會白跑一次慢請求。
    #     （yfinance 為非官方爬蟲，本檔多處防呆註解都是被它咬過的紀錄。）
    if os.environ.get('MC_TRY_YF_VOLUME') == '1' and per_share:
        vol = fetch_intraday_volume_yf()
        if vol:
            for mult, unit in ((1, '股'), (1000, '張')):
                amt = vol * mult * per_share
                if _accept(amt, f'yf分鐘量({unit})×昨均價'):
                    return amt, f'yf.{unit}×昨均價'

    _dbg('無可用盤中成交來源（MIS 指數不給成交欄位、yf 指數量為 0）')
    return None, None


def fetch_intraday_volume_yf():
    """用 yfinance 抓 ^TWII 當日分鐘線的累計成交量（股）。"""
    try:
        h = yf.Ticker('^TWII').history(period='1d', interval='5m')
        if len(h) and 'Volume' in h:
            vol = float(h['Volume'].sum())
            _dbg(f'yf ^TWII 當日分鐘量加總 = {vol:,.0f}（{len(h)} 根）')
            return vol if vol > 0 else None
        _dbg('yf ^TWII 分鐘線無資料')
    except Exception as e:
        _dbg('yf 分鐘量失敗', repr(e))
    return None


def trading_progress(now):
    """台股 09:00–13:30 已交易『時間』比例（0~1），純時間、顯示用。"""
    h = now.hour + now.minute / 60.0
    if h <= 9:
        return 0.0
    if h >= 13.5:
        return 1.0
    return (h - 9) / 4.5


# 台股盤中「累計成交量佔全日比例」經驗曲線（券商軟體的預估量就是這樣算的）。
# 量能非均勻：開盤最重、中午最淡、13:25–13:30 尾盤集合競價再放大，
# 用純時間線性外推會在早盤高估、尾盤低估。
VOLUME_CURVE = [
    (9.00, 0.00), (9.25, 0.11), (9.50, 0.19), (10.00, 0.29), (10.50, 0.37),
    (11.00, 0.44), (11.50, 0.51), (12.00, 0.57), (12.50, 0.64), (13.00, 0.72),
    (13.25, 0.84), (13.50, 1.00),
]


def volume_progress(now):
    """回傳此刻『累計成交量應佔全日的比例』（依經驗曲線內插）。"""
    h = now.hour + now.minute / 60.0
    if h <= 9:
        return 0.0
    if h >= 13.5:
        return 1.0
    for i in range(1, len(VOLUME_CURVE)):
        h0, r0 = VOLUME_CURVE[i - 1]
        h1, r1 = VOLUME_CURVE[i]
        if h <= h1:
            return r0 + (r1 - r0) * (h - h0) / (h1 - h0)
    return 1.0


def fmt_amount(v):
    """成交金額 → 『x,xxx 億』"""
    return f"{v/1e8:,.0f} 億"


def show_turnover(now, mis_cache=None):
    """印出成交金額：今日實際/盤中累計＋全日預估；並附最近一日收盤金額當基準。"""
    hist = fetch_twse_daily_stats()
    today_iso = now.strftime('%Y-%m-%d')
    today_row = next((h for h in hist if h['iso'] == today_iso), None)

    # 收盤後 FMTQIK 會有今日實際值 → 直接用官方數字，最準
    if today_row:
        print(f"成交金額:{fmt_amount(today_row['value'])}（實際）")
        return

    ref = ""
    if hist:
        prev = hist[-1]
        ref = f"（昨 {fmt_amount(prev['value'])}）"

    amt, src = fetch_intraday_turnover(mis_cache)
    if amt is None:
        _dbg('盤中成交金額無來源')
        print(f"成交金額:盤中數據暫無{ref}")
        return

    _dbg('turnover source =', src)
    # 來源透明化：官方(MIS/證交所)直接給數字；yfinance 推估標「約」+「推估」
    official = bool(src and src.startswith('MIS'))
    vr = volume_progress(now)          # 依量能曲線，不是純時間
    if 0.05 < vr < 1:
        val = fmt_amount(amt / vr)
        if official:
            print(f"預估成交金額:{val}{ref}")
        else:
            print(f"預估成交金額:約 {val}（推估）{ref}")
    else:
        print(f"成交金額:{fmt_amount(amt)}{ref}")


def fmt_row(name, price, diff, pct, date_str=None):
    """格式化單行行情"""
    if price is None:
        return f"{name}:數據暫時無法取得"
    p_str   = f"{price:,.2f}" if price < 1000 else f"{price:,.0f}"
    d_str   = f"{diff:+.2f}"  if price < 1000 else f"{diff:+.0f}"
    pct_str = f"({d_str} / {pct*100:+.2f}%)" if diff is not None else ""
    prefix  = f"{name}({date_str})" if date_str else name
    return f"{prefix}:{p_str}{pct_str}"


def fmt_yf_hist(sym, name):
    """抓歷史收盤並格式化（含日期）"""
    try:
        h = yf.Ticker(sym).history(period='5d')
        if len(h) < 2:
            return f"{name}:⚠️ 資料不足"
        d_str = h.index[-1].strftime('%m/%d')
        p     = float(h['Close'].iloc[-1])
        prev  = float(h['Close'].iloc[-2])
        return fmt_row(name, p, p - prev, (p / prev - 1) if prev else 0, d_str)
    except Exception:
        return f"{name}:數據暫時無法取得"


# ── 各時段顯示 ────────────────────────────────────────────────────────

def show_pre_market(now):
    """盤前 05:00–08:44：夜盤收盤價 + 美股收盤 + 預估台股開盤 + 日韓（08:00後即時）"""
    print(f"Market Check【盤前】{now.strftime('%m/%d %H:%M')}")

    price, diff, pct, tag = get_tx_price(prefer_night=True)
    today = now.strftime('%m/%d')
    if price:
        d_str = f"{diff:+.0f}"
        print(f"台指期夜盤({today}):{price:,.0f}({d_str} / {pct*100:+.2f}%)")
        try:
            tw_h = yf.Ticker("^TWII").history(period="2d")
            if len(tw_h) >= 1:
                tw_close = float(tw_h['Close'].iloc[-1])
                print(f"👉預估台股開盤:{tw_close * pct:+,.0f} 點")
        except Exception:
            pass
    else:
        print("台指期:數據暫時無法取得")

    print("-" * 20)
    print("美股收盤")
    for sym, name in [("^IXIC", "NAS"), ("^SOX", "費半"), ("TSM", "TSM")]:
        print(fmt_yf_hist(sym, name))

    # 日韓：08:00 後已開盤，顯示即時；08:00 前顯示昨收
    print("-" * 20)
    h = now.hour + now.minute / 60.0
    if h >= 8.0:
        print("日韓開盤")
        for sym, name in [('^N225', '日股'), ('^KS11', '韓股')]:
            p, d, pc = fetch_yf(sym)
            print(fmt_row(name, p, d, pc))
    else:
        print("日韓昨收")
        for sym, name in [('^N225', '日股'), ('^KS11', '韓股')]:
            print(fmt_yf_hist(sym, name))


def show_day(now):
    """日盤 08:45–13:44：台指期[日盤] + 台股 + 日韓股"""
    print(f"Market Check【日盤】{now.strftime('%H:%M')}")

    price, diff, pct, tag = get_tx_price(prefer_night=False)
    today = now.strftime('%m/%d')
    if price:
        d_str = f"{diff:+.0f}"
        print(f"台指期({today}):{price:,.0f}({d_str} / {pct*100:+.2f}%) [{tag}]")
    else:
        print("台指期:數據暫時無法取得")

    mis = fetch_mis(['tse_t00.tw', 'otc_o00.tw', 'tse_2330.tw'])
    for ch, sym, name in [('tse_t00.tw', '^TWII', '台股大盤'),
                          ('otc_o00.tw', '^TWOII', '櫃買指數'),
                          ('tse_2330.tw', '2330.TW', '台積電')]:
        p, d, pc = fetch_index(ch, sym, mis)
        print(fmt_row(name, p, d, pc))
    show_turnover(now, mis)

    print("-" * 20)
    for sym, name in [('^N225', '日股'), ('^KS11', '韓股')]:
        print(fmt_yf_hist(sym, name))


def show_post_day(now):
    """盤後 13:45–14:59：台股今日收盤 + 台指期日盤收盤"""
    print(f"Market Check【盤後】{now.strftime('%H:%M')} 台股今日收盤")

    price, diff, pct, tag = get_tx_price(prefer_night=False)
    today = now.strftime('%m/%d')
    if price:
        d_str = f"{diff:+.0f}"
        print(f"台指期日盤({today}):{price:,.0f}({d_str} / {pct*100:+.2f}%)")
    else:
        print("台指期:數據暫時無法取得")

    print("-" * 20)
    mis = fetch_mis(['tse_t00.tw', 'otc_o00.tw', 'tse_2330.tw'])
    for ch, sym, name in [('tse_t00.tw', '^TWII', '台股大盤'),
                          ('otc_o00.tw', '^TWOII', '櫃買指數'),
                          ('tse_2330.tw', '2330.TW', '台積電')]:
        p, d, pc = fetch_index(ch, sym, mis)
        print(fmt_row(name, p, d, pc))
    show_turnover(now, mis)


def show_night_pre_us(now):
    """夜盤 + 美股未開：台指期[夜盤] + 日韓股"""
    now_ny = now.astimezone(TZ_NY)
    print(f"Market Check【夜盤】{now.strftime('%m/%d %H:%M')}（美股 {now_ny.strftime('%H:%M')} ET 未開盤）")

    price, diff, pct, tag = get_tx_price(prefer_night=True)
    today = now.strftime('%m/%d')
    if price:
        d_str = f"{diff:+.0f}"
        print(f"台指期({today}):{price:,.0f}({d_str} / {pct*100:+.2f}%) [{tag}]")
    else:
        print("台指期:數據暫時無法取得")

    print("-" * 20)
    for sym, name in [('^N225', '日股'), ('^KS11', '韓股')]:
        print(fmt_yf_hist(sym, name))


def show_night_us_open(now):
    """夜盤 + 美股交易中：台指期[夜盤] + 美股即時"""
    now_ny = now.astimezone(TZ_NY)
    print(f"Market Check【夜盤+美股】{now.strftime('%m/%d %H:%M')}（ET {now_ny.strftime('%H:%M')}）")

    price, diff, pct, tag = get_tx_price(prefer_night=True)
    today = now.strftime('%m/%d')
    if price:
        d_str = f"{diff:+.0f}"
        print(f"台指期({today}):{price:,.0f}({d_str} / {pct*100:+.2f}%) [{tag}]")
    else:
        print("台指期:數據暫時無法取得")

    print("-" * 20)
    print("美股即時")
    for sym, name in [("^IXIC", "NAS"), ("^SOX", "費半"), ("TSM", "TSM")]:
        p, d, pc = fetch_yf(sym)
        print(fmt_row(name, p, d, pc))


def show_night_us_after(now):
    """夜盤 + 美股已收盤：台指期夜盤 + 美股收盤 + 預估明日台股"""
    now_ny = now.astimezone(TZ_NY)
    print(f"Market Check【夜盤】{now.strftime('%m/%d %H:%M')}（美股已收盤 ET {now_ny.strftime('%H:%M')}）")

    price, diff, pct, tag = get_tx_price(prefer_night=True)
    today = now.strftime('%m/%d')
    if price:
        d_str = f"{diff:+.0f}"
        print(f"台指期({today}):{price:,.0f}({d_str} / {pct*100:+.2f}%) [{tag}]")
        try:
            tw_h = yf.Ticker("^TWII").history(period="2d")
            if len(tw_h) >= 1:
                tw_close = float(tw_h['Close'].iloc[-1])
                print(f"👉預估明日台股開盤:{tw_close * pct:+,.0f} 點")
        except Exception:
            pass
    else:
        print("台指期:數據暫時無法取得")

    print("-" * 20)
    print("美股收盤")
    for sym, name in [("^IXIC", "NAS"), ("^SOX", "費半"), ("TSM", "TSM")]:
        print(fmt_yf_hist(sym, name))


def show_closed(now):
    """休市：顯示提示 + 上週收盤參考"""
    print(f"Market Check【休市】{now.strftime('%m/%d %H:%M')} 台指期目前休市")
    print("-" * 20)
    print("上週收盤參考")
    for sym, name in [('^TWII', '台股大盤'), ('^TWOII', '櫃買'),
                      ('2330.TW', '台積電'), ("^IXIC", "NAS"), ("^SOX", "費半")]:
        print(fmt_yf_hist(sym, name))


# ── 入口 ──────────────────────────────────────────────────────────────

def probe():
    """診斷模式：不管現在幾點，直接測資料來源並印出原始欄位。
    用法：docker exec n8n /opt/venv/bin/python3 .../market_check.py --probe"""
    import json as _json
    print("=== 1. MIS 指數 API ===")
    try:
        params = {'ex_ch': 'tse_t00.tw|otc_o00.tw|tse_2330.tw',
                  'json': '1', 'delay': '0', '_': str(int(_time.time() * 1000))}
        r = requests.get(MIS_URL, params=params, headers=MIS_HEADERS, timeout=10)
        print("HTTP", r.status_code)
        data = r.json()
        print("rtcode =", data.get('rtcode'))
        for m in data.get('msgArray', []):
            print(f"\n--- {m.get('ex')}_{m.get('c')} ({m.get('n')}) 全部欄位 ---")
            print(_json.dumps(m, ensure_ascii=False, indent=1))
        if not data.get('msgArray'):
            print("msgArray 空！原始回應前 500 字：", r.text[:500])
    except Exception as e:
        print("MIS 失敗：", repr(e))

    print("\n=== 2. 本程式實際會用到的三個來源 ===")
    print("\n--- 昨日/今日成交金額 FMTQIK（已驗證可用）---")
    hist = fetch_twse_daily_stats()
    for h in hist[-3:]:
        print(f"  {h['iso']}  金額 {fmt_amount(h['value'])}  量 {h['volume']:,.0f} 股")

    print("\n--- 權值股籃回推（盤中主力方案）---")
    est, note = estimate_turnover_by_basket()
    if est:
        vr = volume_progress(now_tw())
        print(f"  籃子回推累計：{fmt_amount(est)}  {note}")
        if 0.05 < vr < 1:
            print(f"  量能進度 {vr*100:.0f}% → 預估全日 {fmt_amount(est / vr)}")
    else:
        print("  無值（非交易時段或來源失敗，開 --debug 看細節）")

    print("\n--- 盤中累計成交金額（總結果）---")
    amt, src = fetch_intraday_turnover()
    print(f"  結果：{fmt_amount(amt) if amt else '無值'}  來源：{src}")

    print("\n--- 櫃買 TPEx 官方日線（MIS 無值時的備援）---")
    print("  ", fetch_tpex_index())

    print("\n=== 2b. 盤中成交金額其他候選 ===")
    for url in (
        'https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX',
        f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?date={now_tw().strftime('%Y%m%d')}&response=json",
        'https://mis.twse.com.tw/stock/api/getStatis.jsp?ex=tse&i=t00',
    ):
        print(f"\n--- {url} ---")
        try:
            r = requests.get(url, headers=MIS_HEADERS, timeout=10)
            print("HTTP", r.status_code, "|", r.text.strip()[:300])
        except Exception as e:
            print("失敗：", repr(e))

    print("\n=== 3. getStatis 帶 session 重試（先取 cookie）===")
    try:
        s = requests.Session()
        s.headers.update(MIS_HEADERS)
        s.get('https://mis.twse.com.tw/stock/index.jsp', timeout=10)   # 取 JSESSIONID
        for u in ('https://mis.twse.com.tw/stock/api/getStatis.jsp?ex=tse&i=t00&json=1&delay=0',
                  'https://mis.twse.com.tw/stock/api/getStatis.jsp?ex=tse&i=1&json=1&delay=0'):
            rr = s.get(u + f'&_={int(_time.time()*1000)}', timeout=10)
            print(f"\n{u}\n → HTTP {rr.status_code}: {rr.text.strip()[:300]}")
    except Exception as e:
        print("session 版失敗：", repr(e))


def main():
    global DEBUG
    DEBUG = '--debug' in sys.argv
    if '--probe' in sys.argv:
        probe()
        return
    now     = now_tw()
    session = get_session(now)

    # --session xxx 可強制指定時段（測試用）
    for i, a in enumerate(sys.argv):
        if a == '--session' and i + 1 < len(sys.argv):
            session = sys.argv[i + 1]

    dispatch = {
        'pre_market':     show_pre_market,
        'day':            show_day,
        'post_day':       show_post_day,
        'night_pre_us':   show_night_pre_us,
        'night_us_open':  show_night_us_open,
        'night_us_after': show_night_us_after,
        'closed':         show_closed,
    }
    dispatch[session](now)


if __name__ == "__main__":
    main()

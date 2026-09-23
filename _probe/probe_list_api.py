#!/usr/bin/env python3
"""
MOPS 清單 API 對照探針（需在可直連 MOPS 的本機終端機執行，勿走沙箱代理）

用途：分辨 ezsearch_query 回「查無公告資料」是
  (A) 當天真的 0 筆        → 對照組（已知有公告日）會回 success + N 筆
  (B) 舊站 API 已失效/改版  → 對照組也回 82 bytes 查無公告資料
同時比對新版 API（mops.twse.com.tw/mops/api/*），作為遷移可行性依據。

執行：python3 probe_list_api.py
輸出：_probe/probe_result.json（raw 原文另存 _probe/raw/）
"""
import json, os, sys, time, datetime
import requests

DIR = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(DIR, "raw"); os.makedirs(RAW, exist_ok=True)

OLD_URL = "https://mopsov.twse.com.tw/mops/web/ezsearch_query"
OLD_HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Origin":  "https://mopsov.twse.com.tw",
    "Referer": "https://mopsov.twse.com.tw/mops/web/ezsearch",
}
NEW_URL = "https://mops.twse.com.tw/mops/api/t05st02"
NEW_HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Referer": "https://mops.twse.com.tw/",
    "Origin":  "https://mops.twse.com.tw",
}

# 對照組：已知有大量公告的日期 vs 疑似 0 筆的日期
# 08-07：程式註解實測 621 筆（success）
# 09-10：8月營收申報截止日，理應大量 F22
# 09-12：週六，理應真 0 筆
CASES = [
    ("20260807", "對照組-已知621筆"),
    ("20260910", "對照組-月營收截止日"),
    ("20260911", "平日"),
    ("20260912", "週六-理應真0筆"),
]
MARKETS = ("sii", "otc", "rotc")
ITEMS   = ("F22", "F27")

def probe_old(sess, date_ymd, market, item):
    tag = f"old_{item}_{market}_{date_ymd}"
    try:
        r = sess.post(OLD_URL, data={
            "step":"00","RADIO_CM":"1","TYPEK":market,"CO_MARKET":"","CO_ID":"",
            "PRO_ITEM":item,"SUBJECT":"","SDATE":date_ymd,"EDATE":date_ymd,
            "lang":"TW","AN":"",
        }, headers=OLD_HDR, timeout=25)
    except Exception as e:
        return {"tag":tag,"error":f"{type(e).__name__}: {e}"}
    open(os.path.join(RAW, tag+".txt"), "wb").write(r.content)
    out = {"tag":tag,"http":r.status_code,"bytes":len(r.content),
           "ctype":r.headers.get("Content-Type","")}
    try:
        j = json.loads(r.content.decode("utf-8-sig"))
        out["status"]  = j.get("status")
        out["message"] = j.get("message")
        out["rows"]    = len(j.get("data") or [])
    except Exception as e:
        out["parse_error"] = f"{type(e).__name__}: {e}"
        out["head"] = r.content[:200].decode("utf-8", "replace")
    return out

def probe_new(sess, date_ymd):
    d = datetime.datetime.strptime(date_ymd, "%Y%m%d")
    tag = f"new_t05st02_{date_ymd}"
    try:
        r = sess.post(NEW_URL, json={"year":str(d.year-1911),
                                     "month":str(d.month),"day":str(d.day)},
                      headers=NEW_HDR, timeout=30)
    except Exception as e:
        return {"tag":tag,"error":f"{type(e).__name__}: {e}"}
    open(os.path.join(RAW, tag+".json"), "wb").write(r.content)
    out = {"tag":tag,"http":r.status_code,"bytes":len(r.content)}
    try:
        j = r.json()
        out["code"] = j.get("code")
        rows = (j.get("result") or {}).get("data") or []
        out["rows"] = len(rows)
        # 統計主旨含「營業收入」「損益」的筆數，判斷新 API 是否涵蓋 F22/F27
        subs = [x[4] for x in rows if isinstance(x, list) and len(x) > 4]
        out["hit_月營收"] = sum(1 for s in subs if "營業收入" in s or "月營收" in s)
        out["hit_損益"]   = sum(1 for s in subs if "損益" in s)
        out["sample"]     = subs[:3]
    except Exception as e:
        out["parse_error"] = f"{type(e).__name__}: {e}"
    return out

def main():
    sess = requests.Session()
    res = {"probed_at": datetime.datetime.now().isoformat(timespec="seconds"),
           "old_ezsearch": [], "new_api": []}
    for date_ymd, note in CASES:
        for item in ITEMS:
            for mk in MARKETS:
                o = probe_old(sess, date_ymd, mk, item); o["note"] = note
                res["old_ezsearch"].append(o)
                print(json.dumps(o, ensure_ascii=False))
                time.sleep(1.0)
        n = probe_new(sess, date_ymd); n["note"] = note
        res["new_api"].append(n)
        print(json.dumps(n, ensure_ascii=False))
        time.sleep(1.0)
    p = os.path.join(DIR, "probe_result.json")
    json.dump(res, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n→ 已寫入", p, "／原文在", RAW)

if __name__ == "__main__":
    main()

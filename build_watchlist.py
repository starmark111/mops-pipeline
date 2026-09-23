#!/usr/bin/env python3
"""產生精簡關注清單 watchlist_active.csv。

從會變動的工作規劃主檔『關注股票列表.xlsx』的『原始資料』分頁，抽出
「研究員或實習生欄位有內容」的股票，寫成一個小而穩定的 CSV，給四支自動化
腳本（F22 / F27 / 重訊 / notify_memo）讀取。

另外會疊加 watchlist_extra.csv（手動維護、主檔以外的負責人，例如 Edison），
疊加的負責人會「加進」該股原本的負責人清單（不覆蓋）。這份疊加檔讓「主檔沒有的人」
也能長存，不會因為重新產生而消失。

輸出格式：
    stock_id,name,owners,extra
    - owners = 主檔研究員／實習生，合成「一組」（一則訊息），例：Leo／Ryan
    - extra  = 疊加檔的人（如 Edison），每人「各自獨立一組」（各自一則），多人用／隔開
    例：
    2382,廣達,Leo／Ryan,Edison      ← 會出「Leo／Ryan」一則 + 「Edison」一則
    2891,中信金,,Edison             ← 主檔無人，只出「Edison」一則

主檔更新後重跑一次即可；也可放進 n8n pipeline 最前面自動化。

用法：
    python3 build_watchlist.py
    python3 build_watchlist.py --src <主檔> --out <輸出CSV> --extra <疊加CSV>
"""
import os
import sys
import csv
import argparse
import datetime
import pandas as pd

_DIR          = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC   = os.path.join(_DIR, "關注股票列表.xlsx")
DEFAULT_OUT   = os.path.join(_DIR, "watchlist_active.csv")
DEFAULT_EXTRA = os.path.join(_DIR, "watchlist_extra.csv")
SHEET         = "原始資料"


def _norm(v) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() == "nan":
        return ""
    return s.rstrip("*").strip()


def _touch(merged: dict, order: list, code: str, name: str):
    key = code or name
    if key not in merged:
        merged[key] = {"code": code, "name": name, "owners": [], "extra": []}
        order.append(key)
    rec = merged[key]
    if not rec["name"] and name:
        rec["name"] = name
    if not rec["code"] and code:
        rec["code"] = code
    return rec


def _read_master(src: str):
    df = pd.read_excel(src, sheet_name=SHEET, dtype=str)
    rows = []
    for _, r in df.iterrows():
        code = _norm(r.get("股票代號"))
        name = _norm(r.get("股票名稱"))
        owners = [x for x in (_norm(r.get("研究員")), _norm(r.get("實習生"))) if x]
        if owners:
            rows.append((code, name, owners))
    return rows


def _read_extra(extra: str):
    """疊加檔。支援欄位：stock_id,name,owners（owners 用／隔開）；
    或舊式 researcher,intern 兩欄。回傳 [(code,name,[owners])]。"""
    if not extra or not os.path.exists(extra):
        return []
    rows = []
    with open(extra, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            code = _norm(r.get("stock_id"))
            name = _norm(r.get("name"))
            if r.get("owners") is not None:
                owners = [o.strip() for o in (r.get("owners") or "").split("／") if o.strip()]
            else:
                owners = [x for x in (_norm(r.get("researcher")), _norm(r.get("intern"))) if x]
            if owners:
                rows.append((code, name, owners))
    return rows


def build(src: str, out: str, extra: str) -> int:
    merged, order = {}, []
    # 主檔：研究員／實習生 合成「一組」（owners 欄）
    for code, name, owners in _read_master(src):
        if not owners:
            continue
        rec = _touch(merged, order, code, name)
        for o in owners:
            if o not in rec["owners"]:
                rec["owners"].append(o)
    # 疊加檔（如 Edison）：每人「各自獨立一組」（extra 欄），不併進主檔那一組
    for code, name, owners in _read_extra(extra):
        if not owners:
            continue
        rec = _touch(merged, order, code, name)
        for o in owners:
            if o not in rec["extra"] and o not in rec["owners"]:
                rec["extra"].append(o)
    recs = [merged[k] for k in order if (merged[k]["owners"] or merged[k]["extra"])]
    recs.sort(key=lambda d: ("／".join(d["owners"]), "／".join(d["extra"]), d["code"]))
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["stock_id", "name", "owners", "extra"])
        for d in recs:
            w.writerow([d["code"], d["name"], "／".join(d["owners"]), "／".join(d["extra"])])
    return len(recs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC, help=f"主檔 .xlsx（預設 {DEFAULT_SRC}）")
    ap.add_argument("--out", default=DEFAULT_OUT, help=f"輸出 CSV（預設 {DEFAULT_OUT}）")
    ap.add_argument("--extra", default=DEFAULT_EXTRA, help=f"疊加 CSV（預設 {DEFAULT_EXTRA}，可不存在）")
    args = ap.parse_args()
    if not os.path.exists(args.src):
        sys.stderr.write(f"❌ 找不到主檔：{args.src}\n")
        sys.exit(1)
    n = build(args.src, args.out, args.extra)
    extra_note = f"（含疊加 {args.extra}）" if os.path.exists(args.extra) else "（無疊加檔）"
    sys.stderr.write(f"✅ 產生 {args.out}：{n} 檔{extra_note}（{datetime.date.today()}）\n")

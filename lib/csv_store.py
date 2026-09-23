"""csv_store.py — CSV 安全讀寫共用模組

為什麼有這支：
  2026-08-09 monthly_revenue.csv 混入 NUL byte，csv.DictReader 直接 crash，
  整支腳本掛掉且完全無告警，事後才發現漏抓好幾天資料。
  事後檢查發現三支管線各寫各的，其中 F22 每次用 "w" 整份重寫 12MB（非原子），
  寫到一半被打斷就會同時失去新舊兩份資料 —— 這是最可能的損毀原因。

三道防線：
  1. 原子寫入：先寫暫存檔 → 寫完才 os.replace() 換上去。
     os.replace 在同一檔案系統上是原子操作，結果只有「完整的新檔」或
     「原封不動的舊檔」，不存在寫到一半的殘骸。
  2. NUL 檢查：讀寫都掃 NUL byte。寫入前發現就擋下並丟例外，
     不要寫進去等明天爆炸；讀取時發現則明確報錯，而非讓 csv 模組丟看不懂的錯。
  3. 欄位檢查：寫入前確認每列欄位與 fieldnames 一致，不對就擋下。

使用原則：
  - 這支模組**只管檔案安全，不管業務邏輯**。去重、排序、格式化都留在各管線。
  - 所有函數失敗時一律丟例外（CsvStoreError），不要靜默 return，
    避免重演「壞了但沒人知道」。
"""

import csv
import os
import shutil
import sys
import tempfile
from datetime import datetime

__all__ = [
    "CsvStoreError", "read_rows", "append_rows", "write_all",
    "has_nul", "clean_nul_inplace",
]

csv.field_size_limit(10 ** 7)   # 避免超長欄位觸發 _csv.Error


class CsvStoreError(Exception):
    """CSV 讀寫失敗。一律明確丟出，不靜默吞掉。"""


# =========================
# NUL byte 偵測與修復
# =========================
def has_nul(path) -> bool:
    """檔案是否含 NUL byte（大檔用分塊讀，不整份載入記憶體）。"""
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                return False
            if b"\x00" in chunk:
                return True


def clean_nul_inplace(path) -> str:
    """清除 NUL byte 並原子覆寫回原檔，回傳備份檔路徑。

    安全可逆：先備份 `檔名.bak-corrupt-YYYYMMDD-HHMMSS` 才動原檔。
    """
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}.bak-corrupt-{ts}"
    shutil.copy2(path, backup)
    with open(path, "rb") as f:
        data = f.read()
    _atomic_write_bytes(path, data.replace(b"\x00", b""))
    return backup


# =========================
# 內部：原子寫入
# =========================
def _atomic_write_bytes(path, data: bytes):
    """先寫同目錄暫存檔 → fsync → os.replace 換上去。

    暫存檔必須與目標同目錄，跨檔案系統的 replace 不是原子操作。
    """
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_csvstore_", suffix=".csv")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _rows_to_bytes(fieldnames, rows, include_header=True) -> bytes:
    import io
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    if include_header:
        w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


def _validate(fieldnames, rows):
    """寫入前檢查：欄位不得多出未宣告的鍵，值不得含 NUL。"""
    fset = set(fieldnames)
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise CsvStoreError(f"第 {i} 列不是 dict：{type(r).__name__}")
        extra = set(r) - fset
        if extra:
            raise CsvStoreError(f"第 {i} 列有未宣告欄位 {sorted(extra)}；"
                                f"若為新欄位請先更新 fieldnames")
        for k, v in r.items():
            if isinstance(v, str) and "\x00" in v:
                raise CsvStoreError(f"第 {i} 列欄位 {k} 含 NUL byte，已擋下不寫入")


# =========================
# 對外 API
# =========================
def read_rows(path, required_fields=None) -> list:
    """讀成 list[dict]。檔案不存在回 []（視為尚未建立，非錯誤）。

    NUL byte 或缺必要欄位一律丟 CsvStoreError，不靜默略過。
    """
    if not os.path.exists(path):
        return []
    if has_nul(path):
        raise CsvStoreError(
            f"{path} 含 NUL byte（檔案已損毀）。"
            f"可用 csv_store.clean_nul_inplace() 修復（會自動備份）")
    with open(path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if required_fields and rows:
        missing = set(required_fields) - set(rows[0])
        if missing:
            raise CsvStoreError(f"{path} 缺少欄位 {sorted(missing)}")
    return rows


def append_rows(path, fieldnames, rows) -> int:
    """附加寫入，回傳實際寫入筆數。檔案不存在則自動建立含表頭。

    附加本身是短寫入，風險遠低於整份重寫，因此直接 append 並 fsync；
    但仍會先做欄位/NUL 檢查，壞資料不進檔。
    """
    if not rows:
        return 0
    _validate(fieldnames, rows)
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    data = _rows_to_bytes(fieldnames, rows, include_header=new_file)
    with open(path, "ab") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return len(rows)


def write_all(path, fieldnames, rows, backup=True) -> int:
    """整份重寫（原子）。回傳寫入筆數。

    backup=True 時，覆寫前先留一份 `檔名.bak-YYYYMMDD-HHMMSS`。
    整份重寫是最危險的操作（F22 原本就是這樣寫 12MB），務必走這支。
    """
    _validate(fieldnames, rows)
    if backup and os.path.exists(path):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.bak-{ts}")
    _atomic_write_bytes(path, _rows_to_bytes(fieldnames, rows, include_header=True))
    return len(rows)


# =========================
# 自我檢查（python3 lib/csv_store.py 可直接跑）
# =========================
if __name__ == "__main__":
    import random
    tmpd = tempfile.mkdtemp()
    p = os.path.join(tmpd, "t.csv")
    fn = ["a", "b"]
    ok = True

    def chk(name, cond):
        global ok
        print(("  ✅ " if cond else "  ❌ ") + name)
        ok = ok and cond

    print("csv_store 自我檢查")
    chk("空檔讀取回 []", read_rows(p) == [])
    chk("append 建檔", append_rows(p, fn, [{"a": "1", "b": "2"}]) == 1)
    chk("append 再寫不重複表頭", append_rows(p, fn, [{"a": "3", "b": "4"}]) == 1
        and len(read_rows(p)) == 2)
    chk("write_all 原子覆寫", write_all(p, fn, [{"a": "9", "b": "9"}], backup=False) == 1
        and read_rows(p) == [{"a": "9", "b": "9"}])
    try:
        append_rows(p, fn, [{"a": "1", "b": "2", "c": "3"}]); chk("擋下未宣告欄位", False)
    except CsvStoreError:
        chk("擋下未宣告欄位", True)
    try:
        append_rows(p, fn, [{"a": "1\x00", "b": "2"}]); chk("擋下 NUL byte", False)
    except CsvStoreError:
        chk("擋下 NUL byte", True)
    with open(p, "ab") as f:
        f.write(b"\x00")
    chk("偵測到損毀檔", has_nul(p))
    try:
        read_rows(p); chk("讀損毀檔會報錯", False)
    except CsvStoreError:
        chk("讀損毀檔會報錯", True)
    bk = clean_nul_inplace(p)
    chk("修復後可讀且有備份", (not has_nul(p)) and os.path.exists(bk))
    shutil.rmtree(tmpd)
    print("結果：" + ("全部通過" if ok else "有失敗"))
    sys.exit(0 if ok else 1)

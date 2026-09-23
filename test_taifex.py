import requests

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      "Referer": "https://www.taifex.com.tw/cht/3/futContractsDate"}
D = "2026/07/21"

# 方法1: 下載CSV端點
r = requests.post("https://www.taifex.com.tw/cht/3/futContractsDateDown",
                  data={"queryType": "1", "goDay": "", "doQuery": "1",
                        "dateaddcnt": "", "queryDate": D,
                        "queryStartDate": D, "queryEndDate": D,
                        "commodityId": "TXF"},
                  headers=UA, timeout=15)
print("=== 方法1 futContractsDateDown ===")
print("status:", r.status_code, "| content-type:", r.headers.get("content-type"))
print(r.content[:500].decode("utf-8-sig", errors="replace"))

# 方法2: TAIFEX OpenAPI（免參數，回最新一日全商品）
r2 = requests.get(
    "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBySettlementDate",
    headers=UA, timeout=15)
print("\n=== 方法2 OpenAPI ===")
print("status:", r2.status_code, "| content-type:", r2.headers.get("content-type"))
print(r2.text[:500])

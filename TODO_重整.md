# MOPS 財報系統重整 · 待辦

更新日：2026-09-18

## 待討論（需與夥伴確認）

- [ ] **觀察清單那 5 檔例外**
      `watchlist_active.csv` 共 178 檔，規則應為「Monitor List + Edison + Ryan」= 173 檔。
      以下 5 檔為 Leo 或 Max 單獨掛名、不含 Ryan，不符規則：
      - 3306 鼎天（Leo）
      - 6980 鐳洋科技（Leo）
      - 2059 川湖（Leo／Sam）
      - 3580 友威科（Max）
      - 6425 易發（Max）
      要拿掉，還是規則改成包含他們？

- [ ] **watchlist_active.csv (178) vs 關注股票列表.xlsx (611) 對不上**
      CSV 為 2026-07-14 手動收斂，非自動產生。兩份各走各的，日後如何同步？

## 已決定

- 主資料庫改 SQLite（F22 + F27 合併）
- M99 不進資料庫，只發訊息
- 抽共用骨架：2 個 fetch adapter + 3 個 parser，其餘共用
- fetch 保持單純，頻率／驗證交上層
- Telegram 訊息格式維持現狀
- F27 每日排程改 03/09/15/21，與 F22 的 00/06/12/18 錯開

## 待實作

- [ ] schema.sql
- [ ] 匯入腳本（現有 63,970 列零損失驗證）
- [ ] fetch / parse / emit / audit 四層
- [ ] F27 補「查無公告資料」判斷與 FETCH_STATS
- [ ] F27 訊息拆開「累計 YoY」與「單季 QoQ」
- [ ] M99 訊息加 MoM / QoQ / YoY（月報配 MoM、季報配 QoQ、年報配 YoY）
- [ ] MoM / QoQ 加推算記號（目前無標示）

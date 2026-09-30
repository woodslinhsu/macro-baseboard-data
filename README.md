# 總經底圖資料抓取（GitHub Actions）

雲端沙盒擋掉 Yahoo／CNN，所以改由 GitHub Actions（網路不受限）每天抓一次資料，commit 成 `data/market.json`；沙盒裡的排程任務再從 `raw.githubusercontent.com`（沙盒可連）讀取，不必每次都靠 LLM 瀏覽網頁。

## 這個 repo 的內容

| 檔案 | 用途 |
|---|---|
| `fetch_market.py` | 抓 Yahoo（^GSPC ^DJI ^IXIC ^SOX ^VIX BZ=F ^TNX）、CNN 恐懼貪婪、NY Fed EFFR 與 30 天期聯邦基金期貨，寫出 `data/market.json` |
| `config.json` | 可調設定（見下） |
| `.github/workflows/fetch.yml` | 週一至週五 20:40 與 21:40 UTC（台北 04:40、05:40）各跑一次，涵蓋夏令／冬令收盤與 GitHub 排程延遲；也可手動執行 |

注意：`update_dash.py` **不在**這個 repo，它跟儀表板放在一起；這裡只負責產生資料。

## 設定步驟

1. 在 GitHub 建立一個 **Public** repo（例如 `macro-baseboard-data`）。必須是 public，`raw.githubusercontent.com` 才能免登入讀取。
2. 依相同路徑上傳檔案：`fetch_market.py`、`config.json`、`.github/workflows/fetch.yml`。
3. Settings → Actions → General → Workflow permissions → 選 **Read and write permissions** → Save。
4. 到 Actions 分頁 → 選 `fetch-market-data` → Run workflow，手動跑一次。
5. 成功後 repo 內會出現 `data/market.json`，內容有 `series`、`fg`、`fedwatch`、`errors`。
6. raw 網址格式：

   ```
   https://raw.githubusercontent.com/<帳號>/<repo>/main/data/market.json
   ```

7. 沙盒排程任務改用：

   ```
   python update_dash.py index.html --market https://raw.githubusercontent.com/<帳號>/<repo>/main/data/market.json
   ```

   `--market` 與 `--fetch` 不能同時使用。網址會自動附加 `?t=<時間戳>` 避開快取。

## 行為說明

- 各資料來源彼此獨立：某個來源失敗只會記在 `errors`，其餘照常寫出；`update_dash.py` 會把缺的項目與 `errors` 轉成 llm_tasks，由 LLM 補抓。
- 只要有至少一條指數／利率序列成功，程式結束碼為 0；全部失敗則為 1（Action 顯示紅燈），且不會覆蓋既有的 `market.json`。若只是部分失敗，資料照樣 commit、job 顯示綠燈；缺的項目記在 `errors`，由排程任務改用網頁補抓。
- 若 `market.json` 的 `fetched_at` 早於「應更新交易日」的美東 16:00 收盤，`update_dash.py` 會回報 `market.json stale`。以收盤時間判斷而非檔案時間長短，所以週一早上（台北）讀到週五抓的檔案是正常的。

## config.json 欄位

```json
{"fomc_meetings": ["2026-10-28", "2026-12-09"], "target_range": null}
```

- `fomc_meetings`：FOMC 利率決議日（會議最後一天）清單。程式只取「今天之後」的前兩場。
- `target_range`：目前聯邦基金利率目標區間，例如 `[3.75, 4.0]`；`null` 表示由 EFFR 推算（下緣 = floor(EFFR×4)/4，上緣 = 下緣 + 0.25）。升降息後若 EFFR 尚未反映，可手動填入。

### 新增 2027 年 FOMC 日期

Fed 公布 2027 年會議日程後，編輯 `config.json`，把決議日補進 `fomc_meetings`，例如：

```json
{"fomc_meetings": ["2026-10-28", "2026-12-09", "2027-01-27"], "target_range": null}
```

（上例 `2027-01-27` 只是示範格式，請以 Fed 官方公布的日期為準。）若清單中已沒有未來的會議，`fedwatch` 會變成 `null` 並在 `errors` 留下提示，儀表板改由 LLM 補。

## 關於 FedWatch 數字

這裡的 FedWatch 是用 30 天期聯邦基金期貨（ZQ，Yahoo 代號如 `ZQX26.CBT`）加上 NY Fed 的 EFFR，以簡化版 CME 方法推算，並非 CME 官方 FedWatch。機率為相對「目前區間」的**累積值**（第二場會議包含第一場的預期），且假設每次變動為 25bp。結果可能與 CME 官方工具相差數個百分點，僅供參考。

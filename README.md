# Bahamut Bumper for Pterodactyl

常駐於 Pterodactyl 的巴哈姆特自推程序。預設每天台北時間 `18:00` 在指定文章回覆「推」，確認新回覆成功後，才刪除前一天由同帳號發布的自推。

預設時間來自 2026-08-28 07:34 的滾動 24 小時截面：43 個非置頂主題中，27 個（62.8%）集中在 18:00–23:59，21 點是單小時峰值。18:00 能在晚間流量開始時回到頂部，並依該日推文量維持在首頁直到熱門時段結束。這只是單日樣本，之後可用 `BUMP_TIME` 調整。

## 安全與防呆

- 每次執行前讀取討論串；今天已有自推便不會再次發布。
- 新回覆無法唯一驗證時，絕不刪除舊回覆。
- 只刪除作者、日期和樓層都符合的「昨天自推」，不會刪首篇或他人文章。
- 重啟後若偵測到今天已推但昨天尚未刪，會接著完成清理。
- `data/`、Cookie 與登入狀態已加入 `.gitignore`；`storage_state.json` 等同登入憑證，勿公開或提交 Git。
- 子板規限制每日 `00:00–23:59` 只能自推一次；若你有多篇伺服器文章，不能讓其他文章同日自推。

## Pterodactyl 安裝

需要 Python 3.11 以上，以及能執行 Playwright Chromium 的映像。一般 Python 映像若缺少 Chromium 系統函式庫，請改用含 Playwright/Chromium 相依套件的映像，或請主機管理員加入相關套件。

安裝指令：

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Pterodactyl Startup Command：

```bash
python main.py
```

把 `.env.example` 中的值建立成 Pterodactyl Variables。程式直接讀取環境變數，不會自動載入 `.env`。

重要變數：

| 變數 | 預設值 | 說明 |
|---|---:|---|
| `BUMP_TIME` | `18:00` | 台北時間每日執行時間 |
| `HEADLESS` | `true` | Pterodactyl 應保持無介面模式 |
| `RUN_MISSED_ON_START` | `true` | 排定時間後重啟時立即補做；仍會先檢查今日是否已推 |
| `RETRY_MINUTES` | `10` | 失敗後等待分鐘數 |
| `MAX_RETRIES` | `3` | 當日最多嘗試次數 |
| `CHROMIUM_EXECUTABLE_PATH` | 空白 | 映像內已有 Chromium 時可指定完整路徑 |

## 登入狀態

不要把帳號密碼寫進設定檔。推薦在有桌面的電腦產生 Playwright storage state，再把檔案上傳到 Pterodactyl 的 `data/storage_state.json`：

```bash
pip install -r requirements.txt
python -m playwright install chromium
python main.py --login
```

瀏覽器開啟後自行登入巴哈，回到終端機按 Enter。將產生的 `data/storage_state.json` 上傳到伺服器相同位置。

也可以匯出 `gamer.com.tw` 的 Cookie JSON，上傳後在 Pterodactyl Console 執行一次：

```bash
python main.py --import-cookies cookies.json
```

成功後請刪除原始 `cookies.json`，只保留權限受限的 `data/storage_state.json`。

## 測試與單次執行

只檢查登入、今日自推與昨日待刪狀態，不改動網站：

```bash
python main.py --dry-run
```

立即執行一次後離開：

```bash
python main.py --once
```

沒有參數時才會進入 Pterodactyl 常駐模式。

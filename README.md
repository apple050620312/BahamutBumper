# Bahamut Bumper

在 Pterodactyl Generic Python Egg 執行的巴哈自推程式。一個帳號對應一篇自己的文章，預設每天台北時間 20:30 依序處理。程式確認當天回覆後，才會清理符合條件的舊回覆。

## 安裝

使用 Python 3.11 以上。將 `main.py`、`bahamut_bumper/`、`requirements.txt` 和 `.env.example` 上傳到伺服器根目錄，然後安裝依賴。

```sh
python -m pip install --user -r requirements.txt
```

Pterodactyl 的啟動指令設定為 `python main.py`。如果需要 ZIP，可從 Git 只封裝部署程式。

```sh
git archive --format=zip --output=BahamutBumper.zip HEAD main.py bahamut_bumper requirements.txt .env.example
```

## 設定帳號

將伺服器根目錄的 `.env.example` 複製成 `.env`，與 `main.py` 放在一起。範例已填入以下帳號清單路徑。

```dotenv
BAHAMUT_ACCOUNTS_FILE=data/accounts.json
```

程式啟動時會讀取 `.env`，每行一個 `名稱=值`。若同一項也在啟動環境中設定，以啟動環境為準。在 `data/accounts.json` 填入帳號與文章網址，每組各填一篇。

```json
[
  {
    "account": "alice123",
    "target_url": "https://forum.gamer.com.tw/C.php?bsn=18673&snA=205415"
  },
  {
    "account": "bob456",
    "target_url": "https://forum.gamer.com.tw/C.php?bsn=60&snA=12345"
  }
]
```

每個帳號的密碼各放一個純文字檔。檔名是 `bahamut_password.txt`，檔案內容只有密碼。以上例為準，請在這兩個位置建立檔案。

- `data/accounts/account-alice123/bahamut_password.txt`
- `data/accounts/account-bob456/bahamut_password.txt`

程式先讀密碼登入，驗證成功後會自動保存該帳號的 `cookies.json`。發文和刪文都使用這個已驗證的登入。

原本的單帳號部署可沿用 `BAHAMUT_ACCOUNT`、`BAHAMUT_TARGET_URL` 和 `data/bahamut_password.txt`。切換成帳號清單時，可在原帳號那一組指定 `"password_file": "bahamut_password.txt"` 和 `"cookie_file": "cookies.json"`。相對路徑以設定檔所在資料夾為準。

若板規要求同帳號一天只在其中一篇文章回覆，將需要一併檢查的文章列入該組的 `guard_urls`。單帳號模式使用 `BAHAMUT_GUARD_URLS`，以逗號分隔。

## Console 指令

| 指令 | 用途 |
|---|---|
| `check` | 驗證各帳號登入、文章擁有權和回覆表單 |
| `status` | 查看各帳號最近一次執行結果 |
| `once` | 立即執行正式發文與舊文清理 |
| `next` | 查看下次排程 |
| `login-test [帳號]` | 單獨測試密碼登入 |
| `help` | 查看全部指令 |

一般 Shell 可執行 `python main.py --check` 或 `python main.py --status`。`data/status.json` 記錄最近一次結果。某個帳號失敗時，其餘帳號仍會執行，排程只重試符合重試條件的帳號。

## 登入失敗

若登入 API 回傳 HTTP 403、429 或暫時無法連線，程式會先驗證已保存的 Cookie。論壇確認帳號與文章擁有權後才會繼續。403 或 429 且 Cookie 也無法使用時，該帳號會停止當天的自動重試。主機商可用日誌中的 HTTP 狀態與 Cloudflare Ray ID 協助排查。

需要補上備用 Cookie 時，可在瀏覽器登入後，將匯出的巴哈 Cookie 保存到該帳號資料夾的 `cookies.json`，再執行 `check`。若要直接使用匯出的 Cookie，可為該組設定 `"auto_mobile_login": false`。單帳號模式使用 `AUTO_MOBILE_LOGIN=false`。

## 常用設定

排程、回覆、重試和登入等完整設定都列在 [.env.example](.env.example)。

需要 Discord 通知時，把 Webhook URL 放在 `data/discord_webhook.txt`，並設定 `DISCORD_ATTENTION_USER_ID` 指定失敗時要通知的帳號。可用 Console 的 `test-notification` 確認。

## 專案結構

| 檔案 | 責任 |
|---|---|
| `main.py`、`bahamut_bumper/cli.py` | 啟動與 Console 指令 |
| `bahamut_bumper/config.py` | 服務設定與帳號設定 |
| `bahamut_bumper/scheduler.py` | 每日排程、逐帳號執行與重試 |
| `bahamut_bumper/workflow.py` | 每篇文章的檢查、發文與舊文清理 |
| `bahamut_bumper/auth.py`、`forum.py` | 登入、論壇請求與頁面解析 |
| `bahamut_bumper/state.py`、`notifications.py` | 狀態檔、執行鎖和 Discord 通知 |
| `tests/` | 離線測試 |

執行離線測試使用 `python -B -m unittest`。密碼、Cookie、Webhook 與最近執行結果集中在 `data/`，由 Git 忽略。

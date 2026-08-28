# Bahamut Bumper（Pterodactyl Generic Python Egg）

這是放進一般 Pterodactyl **Python Generic Egg** 就能執行的常駐程式，不需要 Chromium、Playwright 或自訂 Docker image。它使用匯出的巴哈 Cookie，以普通 HTTPS 請求完成：

1. 每天台北時間 `20:30` 檢查目標文章。
2. 當天尚未自推才回覆「推」。
3. 重新讀取文章，唯一驗證新樓層存在。
4. 再讀一次最新頁面與刪文 token，刪除昨天自己的回覆。
5. 再次確認舊樓層已消失，將結果寫入 `data/status.json`。

任何一步無法明確確認就停止；尤其新回覆沒有驗證成功時，絕不刪昨天的回覆。

## 為什麼選 20:30

分析板面頁 1–3、最近七天可見資料後，共整理出 72 個活躍主題、278 筆可見更新。首頁扣除置頂後約有 29 格；以「某時點之後更新過的不同主題數」估算被擠出首頁，而不是直接累加回覆數。

六個完整日循環（2026-08-21～08-26）的中位估算：

| 自推時間 | 首頁中位存活 | 下一次自推前不在首頁 |
|---|---:|---:|
| 18:00 | 約 18.9 小時 | 約 5.1 小時 |
| 20:00 | 約 22.0 小時 | 約 2.0 小時 |
| **20:30** | **約 22.2 小時** | **約 1.8 小時** |
| 21:00 | 約 22.0 小時 | 約 2.0 小時 |
| 22:00 | 約 21.1 小時 | 約 3.0 小時 |

因此 `20:30` 比 18:00 少犧牲隔日白天曝光，也沒有像 22:00 那樣錯過太多晚間流量。歷史上已刪除的推文無法回收，這個模型仍可能稍微高估存活時間。

## 從零安裝到 Generic Egg

建議 Python 3.11 以上。將這些檔案上傳到伺服器根目錄：

- `main.py`
- `requirements.txt`
- 依 `.env.example` 建立的 Pterodactyl Variables
- `data/cookies.json`（稍後建立；不要公開）

第一次在 Console 安裝：

```bash
python -m pip install --user -r requirements.txt
```

Pterodactyl Startup Command 設成：

```bash
python main.py
```

若 Egg 每次重裝都會清掉套件，也可使用：

```bash
python -m pip install --user -r requirements.txt && python main.py
```

程式直接讀取 Pterodactyl Variables，不會自行讀 `.env`。必要變數的預設值已列在 `.env.example`；建議至少明確設定 `BAHAMUT_ACCOUNT`、`BAHAMUT_TARGET_URL`、`BUMP_TIME`。

伺服器必須允許 DNS 與對 `https://forum.gamer.com.tw` 的連出 HTTPS。

## Cookie 從零設定

不要把巴哈帳號密碼交給腳本，也不要放進環境變數。先在自己的瀏覽器登入巴哈，再用可信任的 Cookie 匯出工具，只匯出 `gamer.com.tw` 網域 Cookie。

腳本接受三種 `data/cookies.json` 格式：

1. 常見瀏覽器擴充套件匯出的 Cookie JSON 陣列。
2. 含有頂層 `cookies` 陣列的 Playwright storage-state JSON。
3. 簡單的名稱和值物件，例如 `{"cookie_name":"value"}`。

也可將完整 `Cookie: name=value; ...` 純文字放入指定檔案。Cookie 等同登入憑證：不要貼到聊天、不要提交 Git、不要傳給他人；檔案已由 `.gitignore` 排除。

每次正常讀取巴哈時，程式會接收回應中的 `Set-Cookie`。只有頁面已確認登入帳號正確、而且回應確實含 Cookie 更新時，才會合併並原子寫回 `data/cookies.json`；第一次寫回前另存 `data/cookies.json.backup`。因此網站若採用活動式續期，新的 Cookie 會被保留下來，而登出頁不能覆寫有效憑證。即將到期不會通知；只有標示期限已過、正常讀頁後仍未收到續期 Cookie，或登入實際失效時才會要求人工處理。

啟動伺服器後，直接在 Pterodactyl Console 輸入只讀指令（不要加 `python main.py`）：

```text
check
```

成功時會看到 `"ok": true`、`"owner_verified": true`、`"reply_form": true`。這一步不發文、不刪文。

## 安全測試順序

### 1. 離線檢查

```bash
python -m unittest -v
python -m py_compile main.py test_main.py
python main.py --help
```

### 2. 登入與解析檢查（唯讀）

```text
check
```

### 3. 非伺服招生文章的完整往返測試

只能提供以下條件的 URL：

- 文章首篇作者是 `BAHAMUT_ACCOUNT`。
- 分類不是「伺服招生」（程式也會拒絕 `subbsn=18`）。
- 不是正式自推目標文章。

確認後在 Pterodactyl Console 輸入：

```text
live-test https://forum.gamer.com.tw/C.php?bsn=18673&snA=你的測試文章 CONFIRM
```

它會短暫公開一則帶時間戳的「自動化連線測試……」回覆，驗證後立即刪除。只有這項測試能端到端證明目前 Cookie、網站表單與刪文 token 都可用。若輸出未顯示 `"deleted": true`，立即開啟測試文章人工確認。

### 4. 正式單次執行

```text
once
```

這會真的對正式文章發文／刪除。Startup 維持 `python main.py`，程式會一邊等待每天 `20:30`，一邊從 stdin 接收 Console 指令。

查最近一次結果：

```text
status
```

其他 Console 指令：`next` 顯示下次排程、`help` 顯示完整說明、`stop` 安全停止。原有 `python main.py --check` 等參數仍可在一般 Shell 使用，但 Pterodactyl Console 不需要也不能這樣輸入。

## Discord 通知

將 Webhook URL 單獨放在 `data/discord_webhook.txt`；此檔位於已被 Git 忽略的 `data/`，不要把網址寫進程式或提交版本庫。也可以改用 Pterodactyl 的秘密環境變數 `DISCORD_WEBHOOK_URL`。

測試通知：

```text
test-notification
```

- 每日流程成功與完整往返測試成功：一般訊息，不標註使用者。
- Cookie 已過期且讀頁後仍未續期、登入失效、CAPTCHA 或每日流程最終失敗：標註 `DISCORD_ATTENTION_USER_ID`。
- 同一事件會寫入 `data/notification_state.json` 去重，避免重啟或重試反覆洗頻。
- `allowed_mentions` 明確限制為指定使用者，不允許訊息內容觸發 `@everyone` 或其他標註。

## 例外狀況如何處理

- **Cookie 過期、錯誤帳號或無權回覆**：停止，不發文；重新登入並匯出 Cookie。
- **網站主動續期 Cookie**：自動保存到原 Cookie 檔，不需人工處理。
- **CAPTCHA／Cloudflare 人機驗證**：停止，不繞過；用瀏覽器人工處理後更新 Cookie。
- **送出時斷線，結果不明**：不刪舊文。常駐模式稍後重試時會先重讀文章；若今日回覆已存在便不會重複發。
- **新回覆無法唯一驗證**：停止且不刪舊文。
- **昨天是一般對話而不是推文**：只有內容完全等於 `BAHAMUT_DELETE_MESSAGES` 其中一項才可能刪除；預設 `推,eee` 是為了接手目前既有的 `eee`，之後可改成只留 `推`。
- **刪文 token、作者、日期或文章編號不符**：停止且不刪。
- **刪文回應不明**：寫入失敗狀態；人工查看文章。下一次執行會重新讀頁，不會靠舊 token 猜測。
- **Pterodactyl 重啟**：若已過 20:30 且 `RUN_MISSED_ON_START=true`，立即補檢查；已有今日回覆就不重複發。
- **兩個程序同時啟動**：`data/daemon.lock` 只允許一個實例。
- **網站暫時故障**：每隔 `RETRY_MINUTES` 分鐘重試，最多 `MAX_RETRIES` 次。

## 已知限制

- 巴哈不是穩定公開 API；HTML、JavaScript 或 `post2.php` 參數改版時腳本會停止，需要更新解析器。
- 直接 HTTP 可能被網站新增的人機驗證阻擋，程式不會規避。
- Cookie 會過期，且 `--check` 通過只代表讀取、登入與表單解析正常；要證明寫入及刪除必須跑一次 `--live-test`。
- 程式只能檢查 `BAHAMUT_TARGET_URL` 與你列入 `BAHAMUT_GUARD_URLS` 的文章。若你另有伺服招生文卻沒列入，程式不知道同日是否已在別篇自推。
- 目前規則雖不再強制刪舊推文，腳本仍依你的要求執行「先發今日、驗證後刪昨日」。刪除候選必須是本人、昨天、非首篇，且文字完全符合 `BAHAMUT_DELETE_MESSAGES`；多個候選時會停下，不會猜哪則該刪。
- 被刪除的歷史回覆無法用板面資料完整重建，因此曝光估算不是保證。

## 重要環境變數

| 變數 | 預設值 | 說明 |
|---|---:|---|
| `BUMP_TIME` | `20:30` | `TZ` 時區的每日時間 |
| `TZ` | `Asia/Taipei` | 日界線與排程時區 |
| `BAHAMUT_COOKIE_FILE` | `data/cookies.json` | Cookie 檔位置 |
| `BAHAMUT_DELETE_MESSAGES` | `推,eee` | 允許刪除的昨日回覆文字 |
| `BAHAMUT_GUARD_URLS` | 空白 | 其他本人伺服招生文，逗號分隔 |
| `RUN_MISSED_ON_START` | `true` | 錯過時間後重啟是否立即補檢查 |
| `RETRY_MINUTES` | `10` | 失敗重試間隔 |
| `MAX_RETRIES` | `3` | 每輪最多嘗試次數 |
| `HTTP_TIMEOUT_SECONDS` | `30` | 單次 HTTP 逾時秒數 |
| `DISCORD_WEBHOOK_FILE` | `data/discord_webhook.txt` | Webhook 密鑰檔位置 |
| `DISCORD_ATTENTION_USER_ID` | `523114942434639873` | 只有需人工處理時才標註的使用者 |

請遵守巴哈板規與站規；自動化不能替你判斷所有人工互動或臨時公告。

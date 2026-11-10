# Motion Control Center

多台 Raspberry Pi + Arduino 控制系統的 **PC 端**：提供網頁介面和 API，統一管理與控制所有裝置的
X/Y/Z 滑台、旋轉、傾角。

## 系統架構

```
PC (Windows / Linux / Mac)
└── 控制中心 (Flask, port 5001)   ← 你在這裡
    ├── 網頁介面
    ├── REST API
    └── 背景狀態輪詢（每 5 秒）
         │  HTTP
         ├──────► Raspberry Pi #1 (Flask, port 5000) ──Serial──► Arduino
         └──────► Raspberry Pi #2 (Flask, port 5000) ──Serial──► Arduino
```

### 程式結構

```
app.py          HTTP 路由 + 輸入驗證
devices.py      裝置清單存取（data/pi_config.json：原子寫入、偵測手動修改、壞檔保護）
monitor.py      背景狀態輪詢 + 每台裝置同時只跑一個指令的鎖
pi_client.py    呼叫 Pi 的 HTTP（錯誤統一轉成 PiError，含該錯誤代表的裝置狀態）
templates/index.html   頁面骨架
static/style.css       樣式（ACQUA-Automation 的設計 token）
static/app.js          前端邏輯（非阻塞 UI，見下方設計備註）
```

## 功能

- 🌐 **網頁介面**：英文介面，一頁式版面——左邊裝置清單、右邊是選中裝置的指令面板。
  配色與排版沿用 ACQUA-Automation（主色 `#009B85` + 白，其他顏色只用在狀態上）
- 🎛️ **指令按鈕**：X/Y/Z 軸 Calibrate／Home／Position／Move，加上 Rotation、Tilt
- 💡 **LED Test**：L0–L3 快速按鈕（All off／Action／Status／All on），模擬版韌體限定，接新機時用來確認燈接對了
- ⌨️ **自訂指令欄**：不在按鈕清單裡的指令（例如韌體除錯用的指令）可以直接輸入送出，Enter 即送出
- 🏠 **全局控制**：Home all devices（每台依序 X→Y→Z→旋轉，裝置間並行）、Test all connections
- ✏️ **自訂顯示名稱**：點裝置面板上方的大標題就能改名（存到 `alternative_name`），隨時可改；原本的 `name` 仍會小字顯示在 IP 左邊
- 📡 **裝置管理**：新增、移除、測試連線
- 🚦 **三態狀態指示**：區分「正常」「Arduino 未接」「離線」（剛新增還沒檢測完時顯示 Checking）
- 🔄 **自動刷新**：後端每 5 秒在背景並行檢測，網頁讀快取，永遠不用等慢的 Pi；沒人開網頁 30 秒後自動停止輪詢
- ⚡ **非阻塞操作**：指令跑的時候只鎖那一台，其他裝置、改名、新增、測試都照常可用；
  執行中的指令會顯示經過秒數，結果記在下方的指令紀錄（每台保留最近 30 筆）

---

## 快速開始

### 方法 1：Docker（推薦）

```bash
cp .env.example .env      # 通常不用改任何值
docker compose up -d

docker compose logs -f    # 查看日誌
docker compose down       # 停止
```

### 方法 2：直接執行（適合開發）

```bash
pip install -r requirements.txt
python app.py
```

然後開瀏覽器：**http://localhost:5001**

> 直接執行時**不會讀 `.env` 檔**（那是 Docker Compose 的功能），設定要用環境變數帶入。
> 此時連接埠固定是 5001，`HOST_PORT` 只對 Docker 有效。
> 裝置清單會存在 `data/pi_config.json`。

---

## 設定

| 變數 (`.env`) | 說明 | 預設值 |
|---|---|---|
| `HOST_PORT` | 網頁 / API 連接埠（**僅 Docker 有效**） | `5001` |
| `CONFIG_FILE` | 裝置清單的存放位置 | `/app/data/pi_config.json` |
| `FLASK_DEBUG` | Flask debug 模式，**請保持 `0`** | `0` |

### ⚠️ 關於 `FLASK_DEBUG`

`FLASK_DEBUG=1` 會啟用 Werkzeug 互動式 debugger。因為服務綁在 `0.0.0.0`，**任何連得到這台 PC 的人都能透過瀏覽器執行任意程式碼**。只在本機開發時暫時開啟，正式環境務必保持 `0`。

---

## 裝置狀態

裝置清單與面板右上角的徽章有三種狀態，**分辨它們是排查問題最快的方法**：

| 徽章 | 意義 | 該做什麼 |
|---|---|---|
| 🟢 **Online** | Pi 正常，且 Arduino 確實有回應 | 正常，可以控制 |
| 🟡 **No Arduino** | Pi 連得到，但 Arduino 不通 | 查那台 Pi 的 USB 線、`SERIAL_PORT`、`BAUD_RATE`、韌體 |
| 🔴 **Offline** | 連不到 Pi 的 Flask | 查網路、IP、Pi 上的容器是否在跑 |

黃燈和紅燈的差別很重要：**黃燈代表網路完全沒問題，別再查網路了**，問題在那台 Pi 跟它的 Arduino 之間。

> **綠燈的門檻是「Arduino 真的回話了」**，不只是 Pi 的 Flask 有回應。
> 檢測時 Pi 會實際送一次 `?` 給 Arduino 並等回應，所以韌體沒燒錄、`BAUD_RATE` 設錯這類
> 「USB 插著、埠也開得起來、但 Arduino 從不回話」的情況會顯示黃燈而不是綠燈。
> 否則你會對著一個永遠不會亮的燈，查一個根本不存在的網路問題。

---

## 新增樹梅派

1. 點網頁上的 **「+ Add」**
2. 填入：
   - **Device ID**：唯一識別符，如 `pi-01`。**建議與該台 Pi `.env` 裡的 `PI_ID` 一致**，日後對日誌時才不會混淆
   - **Host or IP**：如 `192.168.1.100` 或 `raspberrypi.local`
   - **Port**：預設 `5000`
   - **Name**：裝置的正式名稱，留空就用 Device ID。建議跟 Device ID 取一樣，免得同一台有兩種寫法
3. 按 **「Add」**——會立刻測試連線並顯示狀態
4. 想要更好認的名字（例如 `Left rail`、`Mouth simulator`），直接點右邊面板上方的大標題改名即可，
   存在 `alternative_name`，不影響 Device ID 與 Name

### 名稱欄位說明

| 欄位 | 用途 | 能改嗎 |
|---|---|---|
| Device ID（JSON 的 key） | 唯一識別，API 網址用它（`/api/pi/pi-01/...`） | 新增後不能改 |
| `name` | 正式名稱 | 新增時設定 |
| `alternative_name` | 給人看的好記名稱，有填就取代 `name` 當大標題顯示；清空就退回 `name` | 隨時在網頁上改 |

> 從命令列新增含中文名稱的裝置時，請用網頁或 `curl.exe`。Windows PowerShell 5.1 的
> `Invoke-RestMethod` 不是用 UTF-8 送 body，中文會在送出前就變成 `???`，伺服器端救不回來。

---

## 資料存放

裝置清單存在 `data/pi_config.json`，由應用自動生成：

```json
{
  "pis": {
    "pi-01": {
      "host": "192.168.1.100",
      "port": 5000,
      "name": "pi-01",
      "alternative_name": "Left rail"
    }
  }
}
```

只存設定；連線狀態（online/offline）只放在記憶體，所以輪詢不會一直改寫這個檔案。
舊版檔案裡的 `status`、`brightness` 欄位會被忽略，下次存檔時自動拿掉。

- **可以直接手動編輯**：伺服器會偵測檔案修改時間，存檔後下一次刷新就會生效，不用重啟。
- **檔案壞掉（JSON 格式錯）時不會被覆蓋**：伺服器會把壞檔另存成 `pi_config.json.broken-<時間>`、
  記錄錯誤，並繼續用記憶體裡的清單。修好檔案後即自動重新讀取。

Docker 掛載的是整個 **`data/` 目錄**（不是單一檔案），所以容器重建也不會遺失。

> 為什麼掛目錄而不是檔案：bind-mount 一個**還不存在**的檔案時，Docker 會建出一個同名的**目錄**。
> 程式接著會寫入失敗，而錯誤被吞掉——結果是新增的裝置重啟後就消失，卻沒有任何錯誤訊息。
> `data/` 已列入 `.gitignore`，屬於執行期資料，不進版控。

---

## API 端點

網頁介面用的就是這組 API，也可以給外部程式呼叫。所有回應皆為 JSON，含 `status` 欄位（`success` / `error`）。

### `GET /api/pis`

取得所有裝置與**最後一次背景檢測的狀態**，直接從記憶體回傳，不會等 Pi。

```json
{ "status": "success", "pis": { "pi-01": { "host": "...", "status": "online", "running": null, ... } } }
```

- `status`：`online` / `no-arduino` / `offline` / `checking`（剛新增、還沒檢測完）
- `running`：這台目前正在跑的指令（例如 `"SXC"`），沒有則為 `null`

### `GET /api/pi/<pi_id>`

取得單一裝置，**當下即時檢測一次**（最多等 3 秒）。找不到回 `404`。

### `POST /api/pi/<pi_id>/command`

送指令給該台 Pi 的 Arduino，網頁上的每顆指令按鈕都是呼叫這支 API。指令字串直接對應韌體的
指令表（`SXC`／`SXH`／`SXM,500`／`RC`／`TM,45`……完整列表見 Pi 端 README）。

限制：
- 指令必須是**單行**、最多 64 字元。Pi 會在指令後面補 `
` 原樣寫進 Serial，如果指令裡夾著換行，
  會變成一次送出兩個指令、Pi 卻只等一個 `DONE`，之後的回應就全部錯位
- `timeout` 範圍 1–120 秒
- **同一台裝置同時只能跑一個指令**：前一個還沒結束時再送，會立即回 `409`（`"busy": true`），
  不會排隊卡在 Pi 的 Serial 鎖上

```json
{ "command": "SXM,500" }
```

可選的 `timeout`（秒）會原樣轉發給 Pi，Pi 再用它覆蓋等待 Arduino 回應的上限——校正、歸位這類
阻塞指令，真機低速模式單趟可能超過 30 秒，不夠長會在動作做完前就先逾時。網頁的指令按鈕已經照
指令類型帶好合理的 `timeout`，這裡列出來是給直接呼叫 API 的人參考：

```json
{ "command": "SXC", "timeout": 40 }
```

成功時原樣回傳 Pi 的 `/api/command` 回應在 `pi_response` 裡：

```json
{
  "status": "success",
  "pi_id": "pi-01",
  "command": "SXM,500",
  "pi_response": {
    "arduino_result": "DONE",
    "arduino_response": ["Calling move x", "Max Travel = 1000", "moving toward: 500", "DONE"]
  }
}
```

`arduino_result` 是 `"DONE"` 或 `"ERR"`（韌體看不懂這個指令，但連線本身正常）。想知道這次移動有
沒有真的執行，要掃描 `arduino_response` 裡的訊息行——超出行程時韌體仍以 `DONE` 收尾，只是中間會
多一行 `Exceeded Max travel.`（網頁的結果欄已經幫忙判斷並標成 **Out of range**）。

### `GET|POST /api/pi/<pi_id>/test`

測試連線（該台正在跑指令時回 `409`，因為 Serial 被佔用，測了也只會逾時）。回應含 `pi_status` 欄位（`online` / `no-arduino` / `offline`），失敗時也有，方便直接得知失敗原因：

```json
{ "status": "error", "pi_status": "no-arduino", "error": "SERIAL_DISCONNECTED" }
```

### `POST /api/pi`

新增裝置。

```json
{ "pi_id": "pi-03", "host": "192.168.1.102", "port": 5000, "name": "pi-03" }
```

可選的 `alternative_name` 也可以一起帶。

驗證規則：
- `pi_id`：1–32 字元，只能用英數字、`-`、`_`、`.`（會出現在網址裡）
- `host`：主機名稱或 IPv4，不能含 `http://` 或 `:port`
- `name`、`alternative_name`：最多 64 字元

新增後**立即回傳**，狀態是 `checking`，連線檢測在背景進行，網頁幾秒內會更新成實際狀態。

### `PATCH /api/pi/<pi_id>`

修改 `alternative_name`（網頁點大標題改名就是呼叫這支）。傳空字串代表清掉、退回顯示 `name`，上限 64 字。

```json
{ "alternative_name": "Left rail" }
```

回傳更新後的裝置：`{ "status": "success", "pi_id": "pi-01", "pi": { ... } }`

### `DELETE /api/pi/<pi_id>`

移除裝置。該台正在跑指令時回 `409`。

### 狀態碼一覽

| 狀態碼 | 意義 |
|---|---|
| `200` | 成功 |
| `400` | 請求有問題（JSON 無效、參數缺失、數值或連接埠格式錯、裝置 ID 重複） |
| `404` | 找不到該裝置（`/api/` 底下不存在的路徑也一律回 JSON） |
| `409` | 該裝置正在跑另一個指令 |
| `500` | 伺服器內部錯誤，或裝置清單存檔失敗（細節在伺服器 log） |
| `502` | 連不到 Pi，或 Pi 回報錯誤（含 Arduino 斷線） |
| `504` | 連線 Pi 逾時 |

`400` 會區分「body 不是 JSON 物件」和「JSON 合法但欄位沒填」，錯誤訊息會直說是哪一種。

錯誤回應裡如果有 `pi_status`，代表這次失敗讓伺服器更新了裝置狀態。Pi 回 `4xx`（例如 Pi 看不懂請求）
時不會改狀態——Pi 有回應，就不該被標成離線。

---

## 故障排除

### 裝置顯示 🔴 Offline

1. Pi 和 PC 在同一個網路嗎？`ping <pi_host>`
2. Pi 上的容器在跑嗎？`sudo docker compose ps`
3. 連接埠 5000 被防火牆擋了嗎？在 Pi 上：`sudo ufw allow 5000`
4. 裝置設定的 IP 對嗎？在 Pi 上用 `hostname -I` 確認

### 裝置顯示 🟡 No Arduino

網路是通的，問題在 Pi 與 Arduino 之間。到那台 Pi 上查：

```bash
sudo docker compose logs -f      # 看 Serial 的錯誤訊息
ls -la /dev/tty*                 # Arduino 真的在嗎
curl http://localhost:5000/api/ping
```

詳見 Pi 端 README 的「故障排除」。

### 燈號不亮，但顯示 🟢 Online（LED 模擬版韌體）

綠燈代表指令有送到 Arduino 並收到 `DONE`／`ERR`，所以問題多半在硬體接線，不是網路或 Pi：

1. 先按網頁上的 **LED Test**：`Action`(L1) 應該只有動作燈亮、`Status`(L2) 只有狀態燈亮、`All on`(L3) 兩顆
   全亮、`All off`(L0) 兩顆全暗。這組不牽涉軸控邏輯，能直接排除「哪顆燈根本沒接對」。
2. 動作燈 = **D9**、狀態燈 = **D10**，確認接線腳位、極性、限流電阻正確。
3. 按了 `SXM,<mm>` 等移動指令卻沒反應：先確認**有沒有先跑過該軸的校正**（`SXC`／`SYC`／`SZC`）——
   沒校正過的軸，移動指令一律回報超出行程，網頁結果欄會顯示 **Out of range**，這是正常行為不是接線問題。
4. 按了 Rotation 的按鈕或 Tilt 卻沒反應：目前韌體這幾個指令是空 stub，本來就不會動燈，
   詳見 Pi 端 README 的韌體已知行為說明。
5. 動作燈跑完**不會熄**，會停在代表目前位置的亮度；兩顆燈是所有軸共用的，只反映最後一個動過的軸。

### 網頁打不開

```bash
docker compose ps                # 容器在跑嗎
docker compose logs             # 有錯誤嗎
```

`HOST_PORT`（預設 5001）被別的程式佔用的話，改 `.env` 換一個埠。

### 改了程式或網頁，但畫面沒變

Dockerfile 是 `COPY . .`，`app.py` 和 `templates/` 是在 build 時**烤進 image** 的（只有 `data/` 是掛載的）。
改完要重新 build：

```bash
docker compose up -d --build
```

直接用 `python app.py` 跑時，非 debug 模式下 Flask 會快取模板，一樣要重啟程式。

---

## 設計備註

幾個不明顯但重要的實作決定：

- **狀態檢測在背景執行緒、並行**：以前每次 `/api/pis` 都當場逐台檢測，離線裝置會卡住 3 秒 timeout，
  首頁和每次刷新都要等；而且每開一個分頁就多一份對每台 Arduino 的 `?` 查詢。現在由 `monitor.py`
  在背景每 5 秒並行檢測一次，API 直接回記憶體裡的結果。沒有任何網頁在看（30 秒沒收到 `/api/pis`）
  就暫停輪詢。
- **正在跑指令的裝置不檢測**：Pi 回答 `/api/status` 時也要透過同一個 Serial 送 `?`，必須等指令的
  Serial 鎖。一個 40 秒的 Calibrate 跑到一半時去檢測，必定 3 秒逾時，裝置就會在指令執行中被誤判
  成 Offline。所以 `monitor.py` 同時管理「每台一個指令」的鎖，忙碌中的裝置跳過檢測；在檢測途中
  開始指令或收到更新的狀態時，那次檢測結果也會被丟棄。
- **設定檔是原子寫入**：先寫暫存檔再 `os.replace()`，並全程持鎖。先寫檔成功才更新記憶體，
  存檔失敗時 API 會回 500，而不是假裝成功、重啟後才發現裝置不見。
- **壞檔不會被預設值覆蓋**：舊版讀檔失敗就回傳預設清單，下一次存檔就把使用者的整份清單蓋掉。
  現在會另存壞檔並保留記憶體中的清單。
- **前端非阻塞**：所有請求都經過 `app.js` 的 `api()`——一定有逾時、不會拋例外、非 JSON 回應
  （例如 proxy 的 HTML 錯誤頁）也會轉成看得懂的訊息。指令只鎖它自己那台；5 秒刷新只更新變動的
  部分，不會把正在輸入的移動數值、自訂指令、改名框清掉；分頁在背景時暫停輪詢，切回來立刻刷新；
  確認視窗是頁面內的對話框，不用會卡住整個瀏覽器的 `window.confirm`。
- **前端不把資料塞進 inline `onclick`**：按鈕用 `data-*` 屬性加上單一事件委派，所有顯示文字都經過跳脫，
  裝置 ID 在後端也限制字元，避免名稱裡的引號變成可執行的 JS。
- **靜態檔網址帶版本號**：`?v=<啟動時間>`，重新 build 之後瀏覽器一定會拿新的 JS/CSS，
  不會用舊的快取去呼叫已經改過的 API。
- **`debug` 預設關閉**：理由見上方 `FLASK_DEBUG` 警告。
- **綠燈的判定包含 Arduino**：見上方「裝置狀態」。
- **`/api/pi/<id>/command` 的 `timeout` 是選填、會原樣轉發**：不帶的話用預設的 10 秒，對模擬版
  韌體夠用；但校正、歸位這類指令在真機低速模式單趟可能超過 30 秒，所以網頁按鈕會依指令類型帶上
  較長的 `timeout`。PC 呼叫 Pi 時，實際等待時間是 `timeout + 5` 秒緩衝——要讓 Pi 有機會先自己判定
  逾時、回傳明確的錯誤原因（`TIMEOUT_NO_TERMINAL` 等），而不是 PC 這邊的 `requests` 先斷線，把一個
  查得到原因的錯誤變成看不出所以然的連線失敗。

### 已知限制

- **必須是單一程序**：狀態快取和「每台一個指令」的鎖都在記憶體裡。目前用 Flask 內建伺服器
  （`threaded=True`）沒問題；如果日後改用 gunicorn，只能開 **1 個 worker**（用 `--threads` 增加並行），
  開多個 worker 會讓每個 worker 各自輪詢、各自上鎖，同一台裝置就可能同時收到兩個指令。
- **「每台一個指令」只管得到經過這台 PC 的指令**：有人直接呼叫 Pi 的 API 的話，PC 端不會知道。
- **Home all 是逐軸送出**：某一軸失敗就停止那一台的後續動作，其他裝置不受影響；結束時會顯示幾台成功、幾台失敗。
- **指令紀錄只存在瀏覽器分頁的記憶體裡**：重新整理就會清空。

---

## 相關文件

- **PC 控制中心（你在這裡）**：`./app.py`（結構見上方「程式結構」）
- **Raspberry Pi 端**：`../pi-arduino-connection-test-0717/`

## 許可證

MIT

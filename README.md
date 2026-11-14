# Motion Control Center

多台 Raspberry Pi + Arduino 控制系統的 **PC 端**：用一個網頁統一控制所有裝置的 X/Y/Z 滑台、旋轉、傾角，
並透過內建的 [Beszel](https://github.com/henrygd/beszel) 監看每台 Pi 的 CPU、記憶體、溫度等健康狀態。

**目錄**

1. [系統架構](#系統架構)
2. [快速開始](#快速開始)
3. [網頁操作](#網頁操作)
4. [Pi 健康監控（Beszel）](#pi-健康監控beszel)
5. [設定（.env）](#設定env)
6. [資料存放](#資料存放)
7. [API 端點](#api-端點)
8. [用 Ansible 部署多台 Pi](#用-ansible-部署多台-pi)
9. [測試](#測試)
10. [故障排除](#故障排除)
11. [設計備註與已知限制](#設計備註與已知限制)
12. [相關文件](#相關文件)

---

## 系統架構

```
PC (Windows / Linux / Mac) ── docker compose up -d 一次啟動兩個容器
├── Motion Control Center (Flask, port 5001)   ← 這個專案
│     ├── 網頁介面（Devices 頁 + Monitoring 頁）
│     ├── REST API
│     ├── 背景狀態輪詢（每 5 秒）
│     └── 背景健康同步 + 告警（每 10 秒）
└── Beszel hub (port 8090)                     ← 健康資料的收集與儲存（不需要打開它的網頁）
          │ HTTP（控制指令）                          ▲ WebSocket（agent 主動連進來）
          ├──────► Raspberry Pi #1 (Flask, port 5000) ──Serial──► Arduino
          │          └── beszel-agent（選用）─────────┘
          └──────► Raspberry Pi #2 (Flask, port 5000) ──Serial──► Arduino
                     └── beszel-agent（選用）
```

- 控制指令：PC → Pi（HTTP）→ Arduino（Serial）。Pi 端程式在 `../pi-arduino-connection-test-0717/`。
- 健康資料：每台 Pi 的 beszel-agent **主動**連到 PC 上的 hub，控制中心再從 hub 讀取，
  所有畫面（數值、圖表、容器、告警）都在控制中心裡，用同一套風格，不用登入或切換到 Beszel。

### 程式結構

```
app.py                  進入點（Docker 用 gunicorn app:app；開發用 python app.py）
mcc/                    應用程式本體
  __init__.py             create_app()：建立 Flask app、註冊路由、API token 檢查、錯誤處理
  config.py               所有環境變數設定（.env）集中在這裡
  services.py             所有路由共用的長駐物件（裝置清單、狀態監控、健康同步、告警）
  devices.py              裝置清單 data/pi_config.json（偵測手動修改、壞檔保護、停車位置）
  monitor.py              背景狀態輪詢 + 「每台裝置同時只跑一個指令」的鎖
  pi_client.py            呼叫 Pi 的 HTTP（錯誤統一轉成 PiError，附帶它代表的裝置狀態）
  beszel.py               Beszel：健康資料、歷史、容器、與裝置清單雙向同步、agent 設定
  alerts.py               告警規則、觸發 / 解除判斷、webhook 通知（data/alerts.json）
  storage.py              data/ 下 JSON 檔的原子寫入
  web.py                  路由共用：JSON 回應、請求解析、輸入驗證
  routes/                 HTTP 路由，依功能分成 Blueprint
    pages.py                首頁、/api/health
    devices.py              裝置清單：列出、新增、更新、改名 / 停車位置、移除
    commands.py             測試連線、指令表、送指令（含逾時決定）
    monitoring.py           健康監控：綁定 agent、歷史、容器、agent 設定
    alerts.py               告警規則與 webhook 測試
templates/index.html    頁面骨架與對話框
static/style.css        樣式（沿用 ACQUA-Automation 的設計：主色 #009B85 + 白），依區塊分段
static/js/              前端，依功能分檔、照順序載入、不需要 build
  core.js                 設定、共用狀態、api()、API token、通知、確認視窗
  devices.js              Devices 頁：裝置清單、控制面板、指令紀錄、新增 / 改名 / 移除
  health.js               裝置面板的 Health 區塊、綁定 agent、agent 設定說明
  charts.js               健康歷史圖表
  monitoring.js           Monitoring 頁、容器清單、告警規則
  park.js                 停車位置、Home & park、Home all devices
  main.js                 定時刷新、事件處理、啟動
tests/                  pytest（不需要 Pi 或 Beszel）
deploy/ansible/         一次部署所有 Pi 並註冊到這裡（見下方「用 Ansible 部署多台 Pi」）
docker-compose.yml      控制中心 + Beszel hub
```

---

## 快速開始

### 方法 1：Docker（推薦）

```bash
cp .env.example .env      # 至少要改 BESZEL_PASSWORD
docker compose up -d      # 控制中心 + Beszel hub 一起啟動

docker compose logs -f    # 查看日誌
docker compose down       # 停止
```

開瀏覽器：

- 控制中心：**http://\<PC 的 IP\>:5001**——日常只需要這個，健康數據與歷史圖表都在裡面
- Beszel 管理頁：http://\<PC 的 IP\>:8090（帳密是 `.env` 的 `BESZEL_EMAIL` / `BESZEL_PASSWORD`），
  只有要設定告警通知這類進階功能時才需要開

> 修改程式或網頁之後要 `docker compose up -d --build`，見[故障排除](#改了程式或網頁但畫面沒變)。

### 方法 2：直接執行（適合開發）

```bash
pip install -r requirements.txt
python app.py
```

- 直接執行時**不會讀 `.env`**（那是 Docker Compose 的功能），設定要用環境變數帶入；連接埠固定 5001。
- 不會啟動 Beszel hub；沒設 `BESZEL_URL` 時健康監控自動關閉，其他功能不受影響。

### 第一次部署檢查清單

1. PC：`.env` 設好 `BESZEL_PASSWORD`，`docker compose up -d`
2. Windows PC：防火牆開放 **8090** 輸入連線（Pi 的 agent 要連進來），見 [Pi 健康監控](#第一次設定)
3. **多台 Pi（推薦）**：用 [Ansible](#用-ansible-部署多台-pi) 一次部署——自動產生每台的 `.env`、啟用健康監控、
   註冊到這裡，第 4、5 步都不用做
4. 手動的話：每台 Pi 照 Pi 專案 README 部署，`.env` 的 `PI_ID` 設成跟這裡的 Device ID 一樣（例如 `pi-01`），
   再到網頁上 **+ Add** 新增
5. （手動時選用）網頁左側 **⚙** 取得 agent 設定，貼到每台 Pi 的 `.env` 啟用健康監控

> 裝置清單一開始是空的（舊版會預先放兩台寫死 IP 的範例裝置，換場域時會多出兩台不存在的 Pi）。

---

## 網頁操作

英文介面、一頁式版面（整頁不捲動，各區塊自己捲動）：

```
┌──┬─────────────┬───────────────────────────────────────────────┐
│▦ │ Devices  +Add│ floor c ✎                  Online  Test Remove │
│◔ │ ● floor c   │ pi-01 · 172.18.177.105:5000                    │
│⚙ │   CPU 12%   │ HEALTH  Agent CPU Memory … Uptime  History ▸   │
│  │ ● front 1 a │ PARKING  X 200 mm · Y 500 mm   Edit  Home&park │
│  │             │ CONTROLS                                       │
│  │             │ [X Axis] [Y Axis] [Z Axis] [Rotation]          │
│  │             │ [Tilt] [LED Test]                              │
│  │ Home all    │ Custom command ................... [Send]      │
│● │ Test all    │ 指令紀錄（最近 30 筆，最新在上）                │
└──┴─────────────┴───────────────────────────────────────────────┘
```

| 區塊 | 功能 |
|---|---|
| 左側欄 | **▦** Devices 頁、**◔** Monitoring 頁（紅色數字 = 目前的告警數）、**⚙** 在 Pi 上啟用健康監控的說明、底部圓點 = 與控制中心伺服器的連線 |
| 裝置清單 | 點一台切換右邊面板；顯示狀態徽章、IP、CPU／溫度、⚠ 告警數；正在跑指令時圓點變成轉圈 |
| Home all devices | 每台先歸位（X → Y → Z → 旋轉），再移動到**那台的停車位置**（有設定的話）；裝置之間並行，某一步失敗就停止那台，結束時顯示幾台完成／失敗。見[歸位與停車位置](#歸位與停車位置) |
| Test all connections | 測試所有裝置，結束時顯示「幾台 online」 |
| 面板標題 | 點大標題直接改名（Enter 或點旁邊儲存、Esc 取消、清空 = 恢復原名）；下方小字是正式名稱、位址、Pi 程式版本 |
| Health | 目前的 CPU／記憶體／磁碟／溫度；**History** 開歷史圖表，見 [Pi 健康監控](#pi-健康監控beszel) |
| Parking | 這台的停車位置；**Edit** 設定、**Home & park** 只對這台執行歸位 + 停車 |
| Controls | X/Y/Z 軸 Calibrate／Home／Position／Move、Rotation、Tilt、LED Test；Move 欄位按 Enter 即送出。**按鈕、每個指令的逾時、Home all 的順序都來自那台 Pi 的指令表**（Pi 專案的 `commands.py`），所以永遠跟它的韌體一致 |
| Custom command | 按鈕清單以外的韌體指令（例如除錯用），Enter 即送出；逾時用那台 Pi 的 `COMMAND_TIMEOUT` |
| 指令紀錄 | 每台各自保留最近 30 筆；執行中顯示經過秒數；結果為 **Done** / **Out of range**（超出行程、沒有實際移動）/ **Unknown command**（韌體回 ERR）/ 錯誤原因 |

### 歸位與停車位置

**歸位（Home）** 由韌體定義：每一軸往極限開關移動，碰到後就是該軸的 0 點，所以每台都一樣、不用設定。
順序寫在 Pi 專案的 `commands.py`（`HOME_SEQUENCE`，目前是 `SXH → SYH → SZH → RH`）。
傾角沒有歸位指令；歸位前要先校正過。

**停車位置（Parking）** 是每台各自的待命位置，在裝置面板的 Parking 列按 **Edit** 設定：

- 每個可移動的軸一格（X / Y / Z mm、Rotation / Tilt 角度），欄位從那台 Pi 的指令表產生
- 留空 = 那一軸停在歸位的位置；**Clear all** 清除全部
- 設定後，**Home all devices** 和 **Home & park** 都會：先跑完歸位，再依序移動到停車位置
  （例如 `SXH → SYH → SZH → RH → SXM,200 → SYM,500 → RM,90`）
- 存在 `data/pi_config.json` 的 `park`（以韌體的移動指令為鍵，例如 `{"SXM": 200, "RM": 90}`）；
  Ansible 重新部署（`PUT /api/pi/<id>`）不會清掉它

### Monitoring 頁

左側欄 **◔** 切換到 Monitoring 頁，一次看所有 Pi：

- **Active alerts**：目前觸發中的告警，最上方紅框
- **每台一張卡片**：控制連線狀態、agent 狀態、CPU／記憶體／磁碟／溫度長條（顏色跟著告警門檻）、負載、運行時間；
  **History** 開歷史圖表、**Containers** 看那台 Pi 上每個 Docker 容器的狀態、CPU、記憶體
- **Unlinked agents**：回報中但沒綁定裝置的 Pi，按 **Add as device** 加入
- **Recent alert events**：最近的觸發 / 解除紀錄
- 右上 **Alert rules** 設定告警，見[告警](#告警)

網址加上 `#health` 會直接打開 Monitoring 頁；`#device=pi-01` 直接打開那台裝置。

### 非阻塞操作

- 指令執行時**只鎖那一台**的按鈕，其他裝置、切換、改名、新增、測試都照常可用
- 5 秒自動刷新只更新有變的部分，不會清掉正在輸入的數值或改名框
- 跟伺服器斷線／恢復時會跳出提示；分頁在背景時暫停輪詢，切回來立刻刷新
- 移除裝置的確認是頁面內的對話框；同一台裝置被另一個分頁佔用時會顯示 *Busy*

### 裝置狀態

| 徽章 | 意義 | 該做什麼 |
|---|---|---|
| 🟢 **Online** | Pi 正常，且 Arduino 確實有回應 | 正常，可以控制 |
| 🟢 **Busy** | Pi 正常，正在跑一個指令——可能是別的分頁、Home all，或**直接從 Pi 本機網頁 / 腳本送的** | 等它跑完；按鈕會鎖住，清單上顯示是哪個指令 |
| 🟡 **No Arduino** | Pi 連得到，但 Arduino 不通 | 查那台 Pi 的 USB 線、`SERIAL_PORT`、`BAUD_RATE`、韌體 |
| 🔴 **Offline** | 連不到 Pi 的 Flask | 查網路、IP、Pi 上的容器是否在跑 |
| ⚪ **Checking** | 剛新增，第一次檢測還沒完成 | 等幾秒 |

**黃燈代表網路完全沒問題，別再查網路了**，問題在那台 Pi 跟它的 Arduino 之間。

> 綠燈的門檻是「Arduino 真的回話了」：檢測時 Pi 會實際送一次 `?` 給 Arduino 並等回應，所以韌體沒燒錄、
> `BAUD_RATE` 設錯這類「USB 插著但 Arduino 從不回話」的情況會顯示黃燈，而不是誤導人的綠燈。

### 新增裝置

1. 點 **+ Add**，填入：
   - **Device ID**：唯一識別，例如 `pi-01`（英數字、`-`、`_`、`.`，最多 32 字）。
     **要跟那台 Pi `.env` 的 `PI_ID` 一樣**——健康監控靠它對應，對日誌也不會混淆
   - **Host or IP**：例如 `192.168.1.100` 或 `raspberrypi.local`（不要加 `http://` 或 `:port`）
   - **Port**：預設 `5000`
   - **Name**：正式名稱，留空就用 Device ID（建議一樣，免得同一台有兩種寫法）
2. 按 **Add**：立刻加入清單並切換過去，連線檢測在背景進行（先顯示 Checking，幾秒內更新）
3. 想要更好認的名字（例如 `Left rail`），點面板上方的大標題改名即可

### 三種名稱

| 欄位 | 顯示位置 | 用途 | 能改嗎 |
|---|---|---|---|
| Device ID（JSON 的 key） | API 網址 | 唯一識別（`/api/pi/pi-01/...`），對應 Pi 的 `PI_ID` | 新增後不能改 |
| `name` | 大標題下方小字（IP 左邊） | 正式名稱 | 新增時設定 |
| `alternative_name` | 大標題、裝置清單 | 好記的名稱；清空就退回顯示 `name` | 隨時在網頁上改 |

> 從命令列新增含中文名稱的裝置時，請用網頁或 `curl.exe`。Windows PowerShell 5.1 的 `Invoke-RestMethod`
> 不是用 UTF-8 送 body，中文在送出前就會變成 `???`，伺服器端救不回來。

---

## Pi 健康監控（Beszel）

[Beszel](https://github.com/henrygd/beszel) 是輕量的主機監控工具：

- **hub**（資料庫）跑在這台 PC，已寫在本專案 `docker-compose.yml`，跟控制中心一起啟動。
  控制中心直接讀它的資料，**所有畫面都在控制中心裡**；Beszel 自己的網頁（8090）只有維護時才需要
- **agent**（約 4 MB）每台 Pi 一個，已寫在 Pi 專案的 `docker-compose.yml`，**預設不啟動**

控制中心每 10 秒從 hub 讀一次資料：

- 裝置清單每台下面多一行 `CPU 12% · 48°C`
- 裝置面板多一排 **Health**：Agent（Up/Down）、CPU、Memory、Disk、Temp、Load、Uptime。
  超過門檻變黃／紅：CPU 80/95%、記憶體與磁碟 85/95%、溫度 70/80°C。Agent 斷線時顯示的是最後一次收到的數值
- **History** 在控制中心裡開歷史圖表（CPU、記憶體、磁碟、溫度、負載），可切換 1h / 12h / 24h / 1w / 30d，
  滑鼠移到任一張圖上，所有圖會一起顯示同一時間點的數值；開著時每分鐘自動更新。
  資料由控制中心用 `.env` 的帳密向 hub 取得，**不用另外登入 Beszel**。
  中間有斷掉的地方 = 那段時間 Pi 或 agent 沒有回報
- **Containers**（Monitoring 頁）：那台 Pi 上每個 Docker 容器的狀態、健康檢查、CPU、記憶體
- **告警**：見下方[告警](#告警)
- 名稱、新增、刪除兩邊會同步，見下方「與裝置清單同步」

### 告警

在 Monitoring 頁右上 **Alert rules** 設定，存在 `data/alerts.json`：

| 規則 | 預設 |
|---|---|
| Monitoring agent down（agent 停止回報） | 開 |
| CPU ≥ | 90% |
| Memory ≥ | 90% |
| Disk ≥ | 90% |
| Temperature ≥ | 80°C |

- 條件要**連續成立約 20 秒**（兩次同步）才觸發，避免瞬間尖峰誤報；條件消失就自動解除
- 觸發中的告警顯示在 Monitoring 頁最上方、那台的卡片、裝置清單（⚠）和左側欄的紅色數字
- **Webhook URL**（選填）：觸發與解除時送出 `{"text": "…"}`，Teams / Slack 的 incoming webhook 都吃這個格式；
  **Send test** 可以先試送一則
- agent 斷線時不判斷 CPU 等數值（那是斷線前的舊資料）
- 觸發 / 解除紀錄只存在記憶體（最近 200 筆），重啟控制中心會清空；規則本身會保存

### 第一次設定

1. **PC**：`.env` 設好 `BESZEL_PASSWORD`，`docker compose up -d`。hub 第一次啟動時會用
   `BESZEL_EMAIL` / `BESZEL_PASSWORD` 自動建立管理員，不需要在網頁上註冊。
2. **Windows 防火牆**開放 8090（系統管理員 PowerShell）：
   ```powershell
   New-NetFirewallRule -DisplayName "Beszel hub 8090" -Direction Inbound -Protocol TCP -LocalPort 8090 -Action Allow
   ```
3. **用 PC 的區網 IP 開控制中心**（例如 `http://172.18.177.10:5001`，不要用 localhost），點左側 **⚙**，
   按 **Copy** 複製那四行。也可以直接開 `http://<PC IP>:5001/#monitoring`。
   - 用 localhost 開的話，給 Pi 的 `HUB_URL` 會指向 Pi 自己，視窗會顯示警告。
     或者在 `.env` 設 `BESZEL_PUBLIC_URL` 固定網址。
4. **每台 Pi**：把四行貼到 Pi 專案的 `.env`，確認 `PI_ID` 等於這裡的 Device ID，然後
   `git pull && docker compose up -d`。幾秒內就會出現 Health。

之後新增 Pi 只要重複第 4 步——agent 用的是 hub 的**永久 universal token**，連進來時自動註冊，
不需要在 Beszel 網頁上手動新增系統。

### 與裝置清單同步

`data/pi_config.json` 是**主清單**，每台裝置可以綁定一個 Beszel 系統。控制中心在背景每 10 秒同步一次
（在控制中心做了變更時會立刻同步），兩邊都可以改：

| 你做的事 | 結果 |
|---|---|
| Pi 的 agent 第一次連上 | 自動綁定到名稱相符的裝置（見下方比對規則），並把 Beszel 系統名稱改成裝置的顯示名稱 |
| 控制中心改名 | Beszel 的系統名稱跟著改 |
| Beszel 網頁改系統名稱 | 控制中心的 `alternative_name` 跟著改（改回跟 `name` 一樣時清空） |
| 兩邊同時改名 | 以控制中心為準 |
| 控制中心刪除裝置 | 確認視窗有「Also remove it from Beszel」可勾選，勾了就連同 Beszel 的系統與歷史一起刪 |
| Beszel 網頁刪除系統 | 控制中心解除綁定；之後同名的 agent 再連上時會重新自動綁定 |
| Health 區塊按 **Unlink** | 解除綁定，且**不再自動綁定**（`beszel_system_id` 設成 `none`）；要恢復按 **Auto-link** 或手動 **Link** |

綁定後是用 Beszel 的系統 ID 記住的，所以**之後兩邊怎麼改名都不會斷**。

**自動綁定的比對規則**（只在還沒綁定時用，取第一個符合、且還沒被別台綁走的系統）：

1. Beszel 系統名稱 == **Device ID**（Pi 的 compose 把 agent 名稱設成 `PI_ID`）
2. Beszel 系統名稱 == `name` 或 `alternative_name`
3. Beszel 記錄的主機 IP == 裝置的 host

> 第 3 條只是備援：hub 跑在 Docker Desktop 裡時，看到的來源位址是 Docker 閘道（實測 `192.168.250.1`），
> 不是 Pi 的真實 IP。所以**請讓 Pi 的 `PI_ID` 等於這裡的 Device ID**。

**沒綁定到任何裝置的 agent** 會列在裝置清單下方的 **Unlinked agents**：

- 按 **Add** → 開啟新增裝置視窗，Device ID 自動帶入 agent 名稱，填上 Pi 的 IP 就會建立並綁定
- 或到某台裝置的 Health 區塊，從下拉選單選擇 agent 再按 **Link**（同一個 agent 只能綁一台，綁到新裝置時會從舊裝置移走）

> 刪除裝置時就算勾了從 Beszel 移除，**只要那台 Pi 的 agent 還在跑，幾秒後就會重新註冊**並出現在 Unlinked agents。
> 要永久移除，請在那台 Pi 的 `.env` 拿掉 `COMPOSE_PROFILES=monitoring` 後 `docker compose up -d --remove-orphans`。

### 注意事項

- ⚠ **帳密只在 hub 第一次啟動時生效**（`beszel_data/` 還是空的時候）。之後改 `.env` 不會改到既有帳號，
  控制中心會登入失敗（Health 顯示 *Beszel login failed*）。要改密碼：先在 Beszel 網頁改，再同步改 `.env`
  並 `docker compose up -d`。
- hub 的資料（帳號、系統、歷史數據）在 `beszel_data/`，已列入 `.gitignore`。
- **⚙** 給的 `BESZEL_TOKEN` 能讓任何 agent 註冊進 hub。控制中心本身沒有登入機制（連得到的人本來就能控制機台），
  所以沒有更暴露，但不要把它貼到公開的地方。
- 不想用健康監控：刪掉 `docker-compose.yml` 裡的 `beszel` 服務與 `depends_on`，並把 `BESZEL_URL` 設成空字串，
  網頁的 Health 區塊與側欄按鈕會自動隱藏。

---

## 設定（.env）

| 變數 | 說明 | 預設值 |
|---|---|---|
| `HOST_PORT` | 控制中心網頁 / API 連接埠（僅 Docker） | `5001` |
| `CONFIG_FILE` | 裝置清單的位置（容器內路徑） | `/app/data/pi_config.json` |
| `FLASK_DEBUG` | Flask debug 模式（只影響 `python app.py`），**請保持 `0`** | `0` |
| `API_TOKEN` | 選填。設定後，所有會改變東西的 API（和 agent 設定）都要帶 `Authorization: Bearer <API_TOKEN>`；網頁第一次操作時會要求輸入並記在瀏覽器裡 | 空（不驗證） |
| `DEFAULT_COMMAND_TIMEOUT` / `MAX_COMMAND_TIMEOUT` | 指令逾時預設值與上限（秒）。每台 Pi 會在 `/api/status` 回報自己的值並以 Pi 為準，這兩個只在還沒成功檢測過那台時使用 | `130` / `300` |
| `DOCKER_SUBNET` | Docker 內部網路的子網段，跟實體 LAN 撞網段時才需要改 | `192.168.250.0/24` |
| `BESZEL_EMAIL` | Beszel 管理員帳號；控制中心也用它讀健康資料 | `admin@example.com` |
| `BESZEL_PASSWORD` | Beszel 管理員密碼，**必填**（沒填 `docker compose up` 會直接報錯） | — |
| `BESZEL_PORT` | Beszel 網頁連接埠 | `8090` |
| `BESZEL_PUBLIC_URL` | 選填。Pi 與瀏覽器連到 hub 的網址，例如 `http://172.18.177.10:8090`；留空 = 用你開控制中心網頁時的主機位址 | 空 |

`docker-compose.yml` 另外設定 `BESZEL_URL=http://beszel:8090`（控制中心在容器網路裡連 hub 的位址），通常不用改。

> ⚠ **`FLASK_DEBUG=1` 會啟用 Werkzeug 互動式 debugger**。服務綁在 `0.0.0.0`，任何連得到這台 PC 的人都能
> 透過瀏覽器執行任意程式碼。只在本機開發時暫時開啟。

---

## 資料存放

| 位置 | 內容 | 版控 |
|---|---|---|
| `data/pi_config.json` | 裝置清單（含停車位置、Beszel 綁定） | ✗（`.gitignore`） |
| `data/alerts.json` | 告警規則與 webhook URL（第一次在網頁上儲存時產生） | ✗（`.gitignore`） |
| `beszel_data/` | Beszel 帳號、系統、歷史數據 | ✗（`.gitignore`） |

`data/pi_config.json` 第一次啟動時自動產生：

```json
{
  "pis": {
    "pi-01": {
      "host": "192.168.1.100",
      "port": 5000,
      "name": "pi-01",
      "alternative_name": "Left rail",
      "beszel_system_id": "6vybygh6a8h4829",
      "beszel_name": "Left rail",
      "park": { "SXM": 200, "SYM": 500, "RM": 90 }
    }
  }
}
```

- `beszel_system_id`：綁定的 Beszel 系統 ID；`""` = 還沒綁定（可自動綁定）、`"none"` = 手動解除綁定（不再自動綁定）
- `beszel_name`：上次同步時兩邊一致的名稱，用來判斷之後是哪一邊改了名，**不要手動改**
- `park`：停車位置，鍵是韌體的移動指令（`SXM` / `SYM` / `SZM` / `RM` / `TM`），沒列出的軸停在歸位位置
- **只存設定**：連線狀態只放在記憶體，輪詢不會一直改寫檔案。舊版的 `status`、`brightness` 欄位會被忽略，下次存檔時自動拿掉。
- **可以直接手動編輯**：伺服器偵測到檔案修改時間改變就重新讀取，不用重啟。
- **壞掉時不會被覆蓋**：JSON 格式錯誤時，伺服器把壞檔另存成 `pi_config.json.broken-<時間>`、記錄錯誤，
  繼續用記憶體裡的清單；修好檔案後自動重新讀取。

> Docker 掛載的是整個 `data/` **目錄**而不是單一檔案：bind-mount 一個還不存在的檔案時，Docker 會建出同名的
> **目錄**，程式寫入失敗——新增的裝置重啟後就消失，卻沒有任何錯誤訊息。

---

## API 端點

網頁用的就是這組 API，也可以給外部程式呼叫。所有回應皆為 JSON，含 `status`（`success` / `error`），
錯誤時另有 `error` 說明原因。

| 方法 | 路徑 | 用途 |
|---|---|---|
| `GET` | `/api/health` | 服務存活檢查（Docker healthcheck / Ansible），回報是否需要 token |
| `GET` | `/api/pis` | 所有裝置 + 狀態 + 健康資料（從記憶體回傳，不等 Pi） |
| `GET` | `/api/pi/<id>` | 單一裝置，當下即時檢測（最多等 8 秒） |
| `POST` | `/api/pi` | 新增裝置（可直接綁定 Beszel 系統） |
| `PUT` | `/api/pi/<id>` | 建立或更新裝置（部署工具用，可重複執行） |
| `GET` | `/api/pi/<id>/commands` | 那台 Pi 的指令表（按鈕、逾時、Home all 順序） |
| `PATCH` | `/api/pi/<id>` | 修改 `alternative_name` 和 / 或停車位置 `park` |
| `DELETE` | `/api/pi/<id>` | 移除裝置（`?beszel=1` 連同 Beszel 系統一起刪） |
| `POST` | `/api/pi/<id>/beszel` | 綁定 / 解除綁定 Beszel 系統 |
| `GET` | `/api/pi/<id>/health/history` | 健康數據歷史（網頁的 History 圖表） |
| `GET` | `/api/pi/<id>/containers` | 那台 Pi 上的 Docker 容器與資源用量 |
| `GET`/`POST` | `/api/pi/<id>/test` | 測試連線（請 Arduino 回應一次） |
| `POST` | `/api/pi/<id>/command` | 送韌體指令 |
| `GET` | `/api/beszel` | 健康監控狀態 + 未綁定的 agent |
| `GET` | `/api/beszel/agent-env` | 產生 Pi `.env` 的 agent 設定 |
| `GET` | `/api/alerts` | 告警規則、觸發中的告警、最近的觸發 / 解除紀錄 |
| `PUT` | `/api/alerts` | 儲存告警規則 / webhook URL |
| `POST` | `/api/alerts/test` | 對 webhook 送一則測試訊息 |

### `GET /api/pis`

```json
{ "status": "success", "pis": { "pi-01": {
    "host": "172.18.177.105", "port": 5000, "name": "pi-01", "alternative_name": "floor c",
    "status": "online", "running": null, "checked_ago": 2.1,
    "app_version": "main@1a2b3c4", "rig_name": "Ceiling Rig Front",
    "command_timeout": 130, "max_command_timeout": 300,
    "health": { "status": "up", "cpu": 12.3, "mem": 41.0, "disk": 23.5, "temp": 48.2,
                "uptime": 86400, "load": 0.42, "agent_version": "0.21.1", "system_id": "..." } } } }
```

- `status`：`online` / `busy` / `no-arduino` / `offline` / `checking`（剛新增、還沒檢測完）
- `running`：這台正在跑的指令（例如 `"SXC"`），包含不是從這裡送的（Pi 本機網頁、腳本）；沒有則為 `null`
- `checked_ago`：距離上次檢測幾秒。只要有任何 API 呼叫（網頁、腳本、Ansible），背景每 5 秒檢測一次；
  30 秒沒有任何 API 呼叫就暫停，所以這個數字變大代表狀態可能過時了
- `app_version`、`rig_name`、`command_timeout`、`max_command_timeout`：那台 Pi 上次回報的資訊
- `health`：綁定的 Beszel 系統的健康資料；沒有綁定或沒開健康監控時為 `null`。`temp` 沒有感測器時為 `null`
- 回應最外層另有 `beszel`（同 `GET /api/beszel`），網頁每 5 秒用它更新 Unlinked agents

### `POST /api/pi`

```json
{ "pi_id": "pi-03", "host": "192.168.1.102", "port": 5000, "name": "pi-03", "alternative_name": "" }
```

- `pi_id`：1–32 字元，只能用英數字、`-`、`_`、`.`（會出現在網址裡）
- `host`：主機名稱或 IPv4，不能含 `http://` 或 `:port`；`port` 1–65535
- `name`、`alternative_name`：最多 64 字元，皆可省略
- `beszel_system_id`（選填）：直接綁定到這個 Beszel 系統（網頁的 Unlinked agents → **Add** 就是用這個）；
  系統不存在或已經綁給別台時回 `400`
- **立即回傳**，`status` 為 `checking`，連線檢測在背景進行

### `PUT /api/pi/<id>`

部署工具（Ansible）用：裝置不存在就建立（回 `201`），存在就更新（回 `200`）。可以重複執行。

```json
{ "host": "192.168.1.100", "port": 5000, "name": "pi-01" }
```

- 只會改 body 裡有的欄位：網頁上設的 `alternative_name`、Beszel 綁定都會保留
- 驗證規則同 `POST /api/pi`；回應含 `created`（`true` = 這次新建的）

### `GET /api/pi/<id>/commands`

轉發那台 Pi 的 `/api/commands`（格式見 Pi 專案 README），網頁用它產生按鈕。Pi 連不到時回 `502` / `504`。

### `PATCH /api/pi/<id>`

```json
{ "alternative_name": "Left rail", "park": { "SXM": 200, "RM": 90 } }
```

兩個欄位都可以單獨送。回傳更新後的裝置：`{ "status": "success", "pi_id": "pi-01", "pi": { ... } }`

- `alternative_name`：空字串代表清掉（退回顯示 `name`），上限 64 字
- `park`：整組取代；值為 `null` 的軸會被拿掉，`{}` 清除全部。值不是數字時回 `400` 並指出是哪一軸

### `DELETE /api/pi/<id>`

移除裝置。該台正在跑指令時回 `409`。

- `?beszel=1`：同時刪除綁定的 Beszel 系統（含歷史資料）。**先刪 Beszel**，失敗就回 `502` 且不刪裝置，
  可以重試或不勾選再刪一次。回應的 `beszel_deleted` 表示 Beszel 那邊有沒有刪掉

### `POST /api/pi/<id>/beszel`

```json
{ "system_id": "6vybygh6a8h4829" }
```

| `system_id` | 效果 |
|---|---|
| 系統 ID | 綁定到這個系統（如果已經綁給別台，會從那台移走）；下次同步會把系統名稱改成這台的顯示名稱 |
| `null` | 解除綁定，且不再自動綁定 |
| `""` | 解除綁定，恢復自動綁定 |

系統 ID 不存在時回 `404`。

### `GET|POST /api/pi/<id>/test`

請 Arduino 回應一次。回應一定含 `pi_status`（成功或失敗都有），方便直接得知原因：

```json
{ "status": "error", "pi_status": "no-arduino", "error": "SERIAL_DISCONNECTED" }
```

該台正在跑指令時回 `409`（不論指令是從這裡還是從 Pi 本機送的）。

### `POST /api/pi/<id>/command`

指令字串直接對應韌體指令表（`SXC`、`SXH`、`SXM,500`、`RC`、`TM,45`……完整列表見 Pi 端 README）。

```json
{ "command": "SXC", "timeout": 40 }
```

- `timeout`（選填，秒）：等待 Arduino 回應的上限。**不帶就用那台 Pi 回報的 `COMMAND_TIMEOUT`**（預設 130），
  上限是 Pi 回報的 `MAX_COMMAND_TIMEOUT`（預設 300）。PC 一定會把確定的數字轉發給 Pi，並且多等 5 秒，
  兩邊的逾時永遠一致
- 指令必須是**單行**、最多 64 字元（原因見[設計備註](#設計備註)）
- **同一台同時只能跑一個指令**：前一個還沒結束時再送，立即回 `409`（`"busy": true`）。
  Pi 本身也會檢查，所以從 Pi 本機網頁或腳本送的指令一樣擋得住

成功時 Pi 的回應原樣放在 `pi_response`：

```json
{
  "status": "success", "pi_id": "pi-01", "command": "SXM,500",
  "pi_response": {
    "arduino_result": "DONE",
    "arduino_response": ["Calling move x", "Max Travel = 1000", "moving toward: 500", "DONE"]
  }
}
```

- `arduino_result`：`"DONE"`，或 `"ERR"`（韌體看不懂這個指令，但連線本身正常）
- ⚠ **超出行程時韌體仍以 `DONE` 收尾**，只是中間多一行 `Exceeded Max travel.`。要知道有沒有真的移動，
  必須掃描 `arduino_response`（網頁已經幫忙判斷並標成 **Out of range**）

### `GET /api/beszel`

```json
{ "status": "success", "beszel": {
    "enabled": true, "connected": true, "error": null, "systems": 3,
    "unlinked": [ { "system_id": "h6jr5of5mg7mc7z", "name": "pi-03", "host": "...", "status": "up" } ] } }
```

`unlinked`：Beszel 裡有、但沒有綁定到任何裝置的系統。

### `GET /api/pi/<id>/health/history`

`?range=1h`（預設）/ `12h` / `24h` / `1w` / `30d`。對應 Beszel 存的平均值：1h 每 1 分鐘一筆、12h 每 10 分鐘、
24h 每 20 分鐘、1w 每 2 小時、30d 每 8 小時。

```json
{ "status": "success", "pi_id": "pi-01", "range": "1h", "ranges": ["1h", "12h", "24h", "1w", "30d"],
  "from": 1767320000, "to": 1767323600, "interval": 60,
  "points": [ { "t": 1767323045, "cpu": 12.5, "mem": 40.1, "disk": 7.7, "temp": 51.2, "load": 0.42 } ] }
```

- 時間都是 Unix timestamp；`from` / `to` 是整個區間，`points` 由舊到新
- `temp` 取最熱的感測器，沒有感測器時為 `null`
- `404`：裝置不存在，或沒有綁定 agent；`400`：range 不對或沒開健康監控；`502`：連不到 hub

### `GET /api/pi/<id>/containers`

```json
{ "status": "success", "pi_id": "pi-01", "containers": [
    { "name": "pi-arduino-controller", "image": "ghcr.io/…", "status": "Up 2 hours", "health": "healthy",
      "cpu": 3.1, "memory": 48.0, "updated": 1767323045 } ] }
```

依 CPU 由高到低；`memory` 單位 MB；`health` 是 Docker 健康檢查（`healthy` / `unhealthy` / `starting`，沒設定時為 `null`）。
錯誤碼同 health/history。

### `GET /api/alerts` / `PUT /api/alerts`

```json
{ "rules": { "agent_down": { "enabled": true },
             "cpu":  { "enabled": true, "threshold": 90 }, "mem":  { "enabled": true, "threshold": 90 },
             "disk": { "enabled": true, "threshold": 90 }, "temp": { "enabled": true, "threshold": 80 } },
  "webhook_url": "https://…" }
```

`PUT` 送上面的格式（兩個欄位都可以單獨送；門檻 1–100，溫度 1–150）。`GET` 另外回傳：

- `active`：觸發中的告警 `[{ device_id, rule, text, value, since }]`（`GET /api/pis` 也會附上 `alerts`）
- `events`：最近的觸發 / 解除紀錄（最新在前，最多 200 筆，只在記憶體）
- `webhook_error`：上一次 webhook 送失敗的原因

`POST /api/alerts/test`：送一則測試訊息到目前的 webhook URL；沒設定時回 `400`，送失敗回 `502`。

### `GET /api/beszel/agent-env`

回傳給 Pi `.env` 的四行（`env`：`COMPOSE_PROFILES`、`BESZEL_HUB_URL`、`BESZEL_KEY`、`BESZEL_TOKEN`），
另外也分開回傳 `hub_url`、`key`、`token` 給 Ansible 使用。有設 `API_TOKEN` 時需要 token（內含 Beszel 註冊 token）。
第一次呼叫時會在 hub 建立永久 universal token。頁面是用 localhost 開的話，`warning` 會提醒 `HUB_URL` 不能用。

### 狀態碼

| 狀態碼 | 意義 |
|---|---|
| `200` | 成功 |
| `201` | `PUT /api/pi/<id>` 新建了裝置 |
| `400` | 請求有問題：body 不是 JSON 物件、欄位缺失或格式錯、裝置 ID 重複（錯誤訊息會說是哪一種） |
| `401` | 有設 `API_TOKEN`，但請求沒帶或帶錯 `Authorization: Bearer <API_TOKEN>` |
| `404` | 找不到該裝置；`/api/` 底下不存在的路徑也一律回 JSON |
| `409` | 該裝置正在跑另一個指令（從這裡或從 Pi 本機送的） |
| `500` | 伺服器內部錯誤，或裝置清單存檔失敗（細節在伺服器 log） |
| `502` | 連不到 Pi、Pi 回報錯誤（含 Arduino 斷線），或連不到 Beszel |
| `504` | 連線 Pi 逾時 |

錯誤回應裡有 `pi_status` 時，代表這次失敗讓伺服器更新了裝置狀態。Pi 回 `4xx`（例如 Pi 看不懂請求）時不改狀態——
Pi 有回應，就不該被標成離線。

---

## 用 Ansible 部署多台 Pi

`deploy/ansible/` 對 inventory 裡的每台 Pi：裝 Docker → 取得程式碼或拉 image → 自動找 Arduino 的序列埠 →
產生 `.env`（含健康監控設定，從這裡的 `/api/beszel/agent-env` 取得）→ 啟動 → 等健康檢查 →
`PUT /api/pi/<id>` 註冊到這裡。可以重複執行，沒有變更時不會動到任何東西。

```bash
cd deploy/ansible
cp inventory.example.yml inventory.yml && nano inventory.yml
ansible-playbook site.yml
```

第一次設定（裝 WSL / Ansible、SSH key、建立 inventory、部署、疑難排解）照 [`deploy/ansible/TUTORIAL.md`](deploy/ansible/TUTORIAL.md) 一步步做；
指令與變數速查見 [`deploy/ansible/README.md`](deploy/ansible/README.md)。

---

## 測試

```bash
pip install -r requirements.txt pytest
python -m pytest
```

不需要 Pi 或 Beszel hub：涵蓋裝置清單 upsert、逾時決定方式、Pi 的 busy / 409 對應、指令表轉發、健康歷史資料轉換、
容器清單、停車位置、告警觸發 / 解除 / webhook、API token。
Pi 端的序列埠協定測試在 Pi 專案裡。

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

也可以直接開那台 Pi 的本機網頁 `http://<Pi IP>:5000/` 看 Arduino 狀態與最近錯誤。詳見 Pi 端 README 的「故障排除」。

### 燈號不亮，但顯示 🟢 Online（LED 模擬版韌體）

綠燈代表指令有送到 Arduino 並收到 `DONE`／`ERR`，問題多半在硬體接線：

1. 按 **LED Test**：`Action`(L1) 只有動作燈亮、`Status`(L2) 只有狀態燈亮、`All on`(L3) 兩顆全亮、`All off`(L0) 兩顆全暗。
   這組不牽涉軸控邏輯，能直接排除「哪顆燈根本沒接對」
2. 動作燈 = **D9**、狀態燈 = **D10**，確認接線腳位、極性、限流電阻
3. 按了 Move 卻沒反應：**先跑過該軸的 Calibrate 了嗎？** 沒校正過的軸，移動一律回報超出行程，
   紀錄會顯示 **Out of range**，這是正常行為
4. Rotation、Tilt 沒反應：目前韌體這幾個指令是空 stub，本來就不會動燈（見 Pi 端 README）
5. 動作燈跑完**不會熄**，會停在代表目前位置的亮度；兩顆燈所有軸共用，只反映最後一個動過的軸

### 指令顯示 Busy

那台裝置正在跑另一個指令（可能是別的分頁、Home all，或有人在 Pi 本機網頁上操作）。等它結束即可；
清單上那台的圓點會轉圈，面板上的按鈕也會鎖住。

### Controls 顯示「Cannot load the controls from this Pi」

那台 Pi 的程式是舊版，還沒有 `/api/commands`。更新那台 Pi（`git pull && docker compose up -d --build`，
或重跑 Ansible）即可。

### 網頁跳出「API token required」

控制中心的 `.env` 設了 `API_TOKEN`。輸入同一個值，瀏覽器會記住；輸入錯誤時下次操作會再問一次。

### Health 顯示「Not linked to a monitoring agent」

這台被手動 **Unlink** 過，不會自動綁定。從下拉選單選 agent 按 **Link**，或按 **Auto-link** 恢復自動綁定。

### 同一台 Pi 在 Unlinked agents 一直出現

刪除裝置時勾了「從 Beszel 移除」，但那台 Pi 的 agent 還在跑，所以又重新註冊了。按 **Add** 重新加回來，
或在那台 Pi 上停用 agent（`.env` 拿掉 `COMPOSE_PROFILES=monitoring`，再 `docker compose up -d --remove-orphans`）。

### Health 顯示「No monitoring agent found」

1. 那台 Pi 的 `.env` 有 `COMPOSE_PROFILES=monitoring` 嗎？`docker compose ps` 應該看得到 `beszel-agent`
2. `docker compose logs beszel-agent` 要看到 `WebSocket connected`。連不上的話在 Pi 上
   `curl http://<PC IP>:8090` 試試——多半是 Windows 防火牆沒開 8090，或 `BESZEL_HUB_URL` 寫成 localhost
3. 連上了還是沒對應到：看裝置清單下方的 **Unlinked agents** 有沒有它——有的話，在 Health 區塊的下拉選單選它按
   **Link** 即可（之後就會一直記住）。根本原因通常是 Pi 的 `PI_ID` 跟這裡的 Device ID 不一樣

### Health 顯示「Beszel login failed」

`.env` 的 `BESZEL_EMAIL` / `BESZEL_PASSWORD` 跟 hub 裡的帳號不一致，多半是 hub 建好之後才改了 `.env`
（帳密只在第一次啟動時生效，見 [注意事項](#注意事項)）。Beszel 網頁也會因此登不進去。

**不刪資料、把 hub 帳號改成 `.env` 的值**（PC 上執行，指令要跑幾十秒）：

```bash
# 1. 用 .env 的帳密建立 / 更新 hub 的超級管理員（Git Bash；PowerShell 不需要 MSYS_NO_PATHCONV=1）
set -a; . ./.env; set +a
MSYS_NO_PATHCONV=1 docker exec beszel /beszel superuser upsert "$BESZEL_EMAIL" "$BESZEL_PASSWORD"
```

2. 開 `http://localhost:8090/_/`，用剛才的帳密登入 PocketBase 管理後台 → **users** →
   點那個 admin 使用者，把 email 和 password 改成 `.env` 的值 → Save。
3. 回到 `http://localhost:8090` 用 `.env` 的帳密登入；控制中心 10 秒內會自動重新連上。

**如果還沒有任何 Pi 接上、不在乎歷史資料**，也可以直接重建：`docker compose down`、刪掉 `beszel_data/`、
`docker compose up -d`。hub 會用 `.env` 的帳密重新建立帳號——但 `KEY` 與 token 會換新，已經設定過的 Pi
要重新貼一次 **⚙** 給的四行。

### 網頁打不開

```bash
docker compose ps                # 容器在跑嗎
docker compose logs              # 有錯誤嗎
```

`HOST_PORT`（預設 5001）或 `BESZEL_PORT`（預設 8090）被別的程式佔用的話，改 `.env` 換一個埠。

### 改了程式或網頁，但畫面沒變

Dockerfile 是 `COPY . .`，程式、`templates/`、`static/` 是 build 時**烤進 image** 的（只有 `data/` 是掛載的）：

```bash
docker compose up -d --build
```

直接用 `python app.py` 跑時，非 debug 模式下 Flask 會快取模板，一樣要重啟。（Docker 裡是 gunicorn。）
JS/CSS 網址帶有版本號，重啟後瀏覽器一定會拿到新檔案，不需要清快取。

---

## 設計備註與已知限制

### 設計備註

- **狀態檢測在背景、每台各自獨立**：`/api/pis` 直接回記憶體裡的結果，頁面永遠不用等離線 Pi 的逾時；
  多開幾個分頁也不會多送查詢給 Arduino。每台的檢測互不等待——一台離線、要等滿 8 秒逾時，
  不會拖慢其他台的 5 秒更新。30 秒沒有任何 API 呼叫就暫停輪詢。
- **Busy 由 Pi 回報**：Pi 有指令在跑時，`/api/status` 不碰序列埠、直接回 `busy` 和指令名稱，所以從 Pi 本機網頁
  送的長指令也不會讓這裡誤判成 Offline。這裡送出的指令期間則完全跳過檢測；檢測途中開始指令或收到更新的狀態時，
  那次結果也會被丟棄。
- **逾時以 Pi 為準，而且一定明確轉發**：以前 PC 沒帶 timeout 時只等 15 秒，Pi 卻預設等 130 秒——PC 先放棄、
  把裝置標成 Offline、放掉指令鎖，下一個指令再卡在 Pi 上。現在 PC 從 `/api/status` 讀 Pi 的
  `COMMAND_TIMEOUT` / `MAX_COMMAND_TIMEOUT`，每次都把確定的數字轉發給 Pi，自己多等 5 秒。
- **指令表由 Pi 提供**：按鈕、逾時、Home all 順序來自每台 Pi 的 `/api/commands`，PC 與 Pi 本機網頁不再各自
  維護一份；不同 Pi 燒了不同韌體也能各自顯示正確的按鈕。
- **指令限制單行**：Pi 會在指令後面補換行字元、原樣寫進 Serial。指令裡夾著換行的話會一次送出兩個指令，
  Pi 卻只等一個 `DONE`，之後的回應就全部錯位。
- **`timeout + 5` 秒緩衝**：PC 等 Pi 的時間比轉發給 Pi 的 `timeout` 多 5 秒，讓 Pi 有機會先回報明確的錯誤
  （`TIMEOUT_NO_TERMINAL` 等），而不是 PC 先斷線，變成看不出原因的連線失敗。
- **設定檔原子寫入**：先寫暫存檔再 `os.replace()`，全程持鎖；先寫檔成功才更新記憶體，存檔失敗時 API 回 500，
  不會假裝成功、重啟後才發現裝置不見。
- **壞檔不會被預設值覆蓋**：舊版讀檔失敗就回傳預設清單，下一次存檔就把整份清單蓋掉；現在會另存壞檔並保留記憶體中的清單。
- **綠燈包含 Arduino**：見[裝置狀態](#裝置狀態)。
- **後端分層**：`mcc/routes/` 只處理 HTTP（解析、驗證、回應），邏輯在 `devices` / `monitor` / `beszel` / `alerts`
  模組，共用物件集中在 `services.py`；`create_app()` 組裝全部。測試直接替換 `services` 裡的物件，不用啟動背景執行緒。
- **前端分檔、不需要 build**：`static/js/` 依功能分成 7 個一般 script，照順序載入、共用全域範圍；只有 `main.js` 在載入時執行。
- **健康監控全部原生**：數值、圖表、容器、告警都由控制中心向 Beszel 取資料自己畫，風格一致、不用登入 Beszel；
  Beszel 只負責收集與儲存。
- **告警要連續兩次成立才觸發**：一次 CPU 尖峰不會吵人；agent 斷線時不判斷數值（那是斷線前的舊資料）。
- **停車位置跟著指令表走**：以韌體的移動指令（`SXM`…）為鍵、欄位從 Pi 的指令表產生，韌體加了新軸不用改控制中心。
- **前端非阻塞**：所有請求經過 `core.js` 的 `api()`——一定有逾時、不拋例外、非 JSON 回應（例如 proxy 的 HTML 錯誤頁）
  也轉成看得懂的訊息。
- **前端不用 inline `onclick`**：按鈕用 `data-*` 屬性 + 單一事件委派，顯示文字一律跳脫，後端也限制裝置 ID 字元，
  避免名稱裡的引號變成可執行的 JS。
- **靜態檔網址帶版本號**（`?v=<啟動時間>`）：重新 build 後瀏覽器一定拿新的 JS/CSS，不會用舊快取呼叫已經改過的 API。
- **兩邊同步用「上次一致的名稱」判斷誰改了名**：`beszel_name` 記錄上次同步時兩邊一致的名稱。
  只有 Beszel 那邊跟它不同 → Beszel 改的，拉回來；控制中心這邊不同 → 推過去；兩邊都不同 → 控制中心為準。
  不這樣做的話，每次同步都分不出「哪邊是新的」，名稱會被來回覆蓋。
- **綁定用 Beszel 系統 ID，不用名稱**：名稱只在第一次自動綁定時用來比對，之後兩邊改名才不會斷線。
- **Beszel agent 用 universal token 自動註冊**：新增 Pi 不必在 Beszel 網頁上手動操作；token 設為永久，
  hub 重啟後仍有效（預設的臨時 token 一小時就失效）。
- **`debug` 預設關閉**：見[設定](#設定env)的警告。

### 已知限制

- **必須是單一程序**：狀態快取和指令鎖都在記憶體裡。Docker 裡用 gunicorn **1 個 worker** + 16 threads；
  不要調高 worker 數，多個 worker 會各自上鎖、各自輪詢。
- **指令紀錄只存在瀏覽器分頁的記憶體裡**：重新整理就會清空。告警的觸發 / 解除紀錄只存在伺服器記憶體，重啟會清空。
- **Home all / Home & park 由瀏覽器依序送出**：執行途中關掉或重新整理頁面，剩下的步驟就不會送。
- **`API_TOKEN` 只是共用密碼**：沒有使用者帳號，也沒有 HTTPS；讀取類的 API（狀態、健康資料）不需要 token。
  請只在受信任的區網使用。Pi 的 API 本身沒有驗證。
- **Ansible 不會刪除裝置**：從 inventory 拿掉的 Pi 要在網頁上 Remove。

---

## 相關文件

- **Raspberry Pi 端**：`../pi-arduino-connection-test-0717/`——Pi 的部署、韌體指令表、Pi API、
  beszel-agent 啟用方式，以及單台 Pi 的本機網頁（`http://<Pi IP>:5000/`）
- **Beszel**：https://github.com/henrygd/beszel ・ 文件 https://beszel.dev

## 許可證

MIT

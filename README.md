# PC LED 控制中心

多台 Raspberry Pi + Arduino LED 控制系統的 **PC 端**：提供網頁介面和 API，統一管理與控制所有裝置。

## 系統架構

```
PC (Windows / Linux / Mac)
└── 控制中心 (Flask, port 5001)   ← 你在這裡
    ├── 網頁介面
    └── REST API
         │  HTTP
         ├──────► Raspberry Pi #1 (Flask, port 5000) ──Serial──► Arduino
         └──────► Raspberry Pi #2 (Flask, port 5000) ──Serial──► Arduino
```

## 功能

- 🌐 **網頁介面**：一頁掌握所有裝置，即時控制
- 🎚️ **亮度控制**：0–255 滑桿（防抖，不會塞爆序列埠）＋一鍵開關
- 📡 **裝置管理**：新增、移除、測試連線
- 🚦 **三態狀態指示**：區分「正常」「Arduino 未接」「離線」
- 🔄 **自動刷新**：每 5 秒並行檢測所有裝置

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

每張卡片右上角的徽章有三種狀態，**分辨它們是排查問題最快的方法**：

| 徽章 | 意義 | 該做什麼 |
|---|---|---|
| 🟢 **連接中** | Pi 正常，且 Arduino 確實有回應 | 正常，可以控制 |
| 🟡 **Arduino 未接** | Pi 連得到，但 Arduino 不通 | 查那台 Pi 的 USB 線、`SERIAL_PORT`、`BAUD_RATE`、韌體 |
| 🔴 **離線** | 連不到 Pi 的 Flask | 查網路、IP、Pi 上的容器是否在跑 |

黃燈和紅燈的差別很重要：**黃燈代表網路完全沒問題，別再查網路了**，問題在那台 Pi 跟它的 Arduino 之間。

> **綠燈的門檻是「Arduino 真的回話了」**，不只是 Pi 的 Flask 有回應。
> 檢測時會實際送一次 `PING` 並等回應，所以韌體沒燒錄、`BAUD_RATE` 設錯這類
> 「USB 插著、埠也開得起來、但 Arduino 從不回話」的情況會顯示黃燈而不是綠燈。
> 否則你會對著一個永遠不會亮的燈，查一個根本不存在的網路問題。

---

## 新增樹梅派

1. 點網頁上的 **「➕ 新增裝置」**
2. 填入：
   - **裝置 ID**：唯一識別符，如 `pi-001`。**建議與該台 Pi `.env` 裡的 `PI_ID` 一致**，日後對日誌時才不會混淆
   - **主機名稱或 IP**：如 `192.168.1.100` 或 `raspberrypi.local`
   - **連接埠**：預設 `5000`
   - **顯示名稱**：如 `樓下客廳`
3. 按 **「新增」**——會立刻測試連線並顯示狀態

---

## 資料存放

裝置清單存在 `data/pi_config.json`，由應用自動生成：

```json
{
  "pis": {
    "pi-001": {
      "host": "192.168.1.100",
      "port": 5000,
      "name": "樹梅派 #1",
      "brightness": 0,
      "status": "offline"
    }
  }
}
```

Docker 掛載的是整個 **`data/` 目錄**（不是單一檔案），所以容器重建也不會遺失。

> 為什麼掛目錄而不是檔案：bind-mount 一個**還不存在**的檔案時，Docker 會建出一個同名的**目錄**。
> 程式接著會寫入失敗，而錯誤被吞掉——結果是新增的裝置重啟後就消失，卻沒有任何錯誤訊息。
> `data/` 已列入 `.gitignore`，屬於執行期資料，不進版控。

---

## API 端點

網頁介面用的就是這組 API，也可以給外部程式呼叫。所有回應皆為 JSON，含 `status` 欄位（`success` / `error`）。

### `GET /api/pis`

取得所有裝置。**會先並行檢測所有裝置的連線狀態**再回傳。

```json
{ "status": "success", "pis": { "pi-001": { "host": "...", "status": "online", ... } } }
```

`status` 為 `online` / `no-arduino` / `offline`。

### `GET /api/pi/<pi_id>`

取得單一裝置（同樣會先檢測）。找不到回 `404`。

### `POST /api/pi/<pi_id>/brightness`

```json
{ "brightness": 128 }
```

數值會被夾在 0–255。

```json
{ "status": "success", "pi_id": "pi-001", "brightness": 128, "pi_response": { ... } }
```

### `POST /api/pi/<pi_id>/command`

送自訂指令給該台 Pi 的 Arduino。

```json
{ "command": "SET:100" }
```

### `GET|POST /api/pi/<pi_id>/test`

測試連線。回應含 `pi_status` 欄位（`online` / `no-arduino` / `offline`），失敗時也有，方便直接得知失敗原因：

```json
{ "status": "error", "pi_status": "no-arduino", "error": "SERIAL_DISCONNECTED" }
```

### `POST /api/pi`

新增裝置。

```json
{ "pi_id": "pi-003", "host": "192.168.1.102", "port": 5000, "name": "新房間" }
```

### `DELETE /api/pi/<pi_id>`

移除裝置。

### 狀態碼一覽

| 狀態碼 | 意義 |
|---|---|
| `200` | 成功 |
| `400` | 請求有問題（JSON 無效、參數缺失、數值或連接埠格式錯、裝置 ID 重複） |
| `404` | 找不到該裝置 |
| `502` | 連不到 Pi，或 Pi 回報錯誤（含 Arduino 斷線） |
| `504` | 連線 Pi 逾時 |

`400` 會區分「JSON 本身無效」和「JSON 合法但欄位沒填」，錯誤訊息會直說是哪一種。

---

## 故障排除

### 裝置顯示 🔴 離線

1. Pi 和 PC 在同一個網路嗎？`ping <pi_host>`
2. Pi 上的容器在跑嗎？`sudo docker compose ps`
3. 連接埠 5000 被防火牆擋了嗎？在 Pi 上：`sudo ufw allow 5000`
4. 裝置設定的 IP 對嗎？在 Pi 上用 `hostname -I` 確認

### 裝置顯示 🟡 Arduino 未接

網路是通的，問題在 Pi 與 Arduino 之間。到那台 Pi 上查：

```bash
sudo docker compose logs -f      # 看 Serial 的錯誤訊息
ls -la /dev/tty*                 # Arduino 真的在嗎
curl http://localhost:5000/api/ping
```

詳見 Pi 端 README 的「故障排除」。

### 燈號不亮，但顯示 🟢 連接中

綠燈代表指令有送到 Arduino 並收到回應，所以問題多半在硬體或韌體：

1. LED 接的是 **PWM 腳位**嗎？（範例韌體用的是 pin 9）
2. LED 極性、限流電阻正確嗎？
3. 韌體的 `analogWrite` 用的腳位跟實際接線一致嗎？

### 網頁打不開

```bash
docker compose ps                # 容器在跑嗎
docker compose logs             # 有錯誤嗎
```

`HOST_PORT`（預設 5001）被別的程式佔用的話，改 `.env` 換一個埠。

---

## 設計備註

幾個不明顯但重要的實作決定：

- **狀態檢測是並行的**：逐台檢測時，每台離線裝置都會卡住 3 秒的 timeout。5 台裝置（3 台離線）要等約 9 秒，比前端 5 秒的刷新間隔還久——請求會越堆越多，裝置一多就整個卡死。改成並行後同樣情境實測 3.04 秒。
- **設定檔是原子寫入**：先寫暫存檔再 `os.replace()`，並全程持鎖。多執行緒同時寫容易產生半截的 JSON，一旦寫壞，整份裝置清單就沒了。
- **`debug` 預設關閉**：理由見上方 `FLASK_DEBUG` 警告。
- **綠燈的判定包含 Arduino**：見上方「裝置狀態」。

---

## 相關文件

- **PC 控制中心（你在這裡）**：`./app.py`
- **Raspberry Pi 端**：`../pi-arduino-connection-test-0717/`

## 許可證

MIT

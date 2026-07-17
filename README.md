# PC LED 控制中心

這是一個多機樹梅派 Arduino LED 控制系統的 **PC 端** Flask 應用。

## 功能

- 🌐 **網頁介面**：即時查看和控制多台樹梅派上的 LED 燈號
- 🎚️ **亮度滑桿**：精細控制每台裝置的 LED 亮度 (0-255)
- ⚡ **快速按鈕**：一鍵開關燈號
- 📡 **裝置管理**：輕鬆新增、移除和測試樹梅派連接
- 🔄 **自動狀態刷新**：每 5 秒檢測裝置連線狀態
- 📊 **連接狀態指示**：即時顯示每台裝置的連線狀態

## 系統架構

```
PC (Windows/Linux/Mac)
├── PC Flask App (port 5001)  ← 你在這裡
│   ├── Web UI
│   └── API
│
└── 網路連接到
    ├── Raspberry Pi #1 (Flask on port 5000)
    │   └── Arduino (通過 Serial)
    └── Raspberry Pi #2 (Flask on port 5000)
        └── Arduino (通過 Serial)
```

## 快速開始

### 方法 1：直接運行 (適合開發)

```bash
# 安裝依賴
pip install -r requirements.txt

# 運行
python app.py
```

然後訪問：`http://localhost:5001`

### 方法 2：使用 Docker

```bash
# 構建並運行
docker-compose up -d

# 查看日誌
docker-compose logs -f

# 停止
docker-compose down
```

## 配置文件

應用會自動生成 `pi_config.json` 來儲存 Raspberry Pi 的資訊：

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

### 如何新增樹梅派

1. 點擊網頁上的 **「➕ 新增裝置」** 按鈕
2. 填入以下資訊：
   - **裝置 ID**：唯一識別符，如 `pi-001`
   - **主機名稱或 IP**：如 `192.168.1.100` 或 `raspberrypi.local`
   - **連接埠**：預設 `5000`
   - **顯示名稱**：如 `樓下客廳`
3. 點擊 **「新增」**

## API 端點

### 獲取所有裝置
```
GET /api/pis
```

### 獲取特定裝置
```
GET /api/pi/<pi_id>
```

### 設定亮度
```
POST /api/pi/<pi_id>/brightness
{
  "brightness": 128
}
```

### 測試連接
```
GET /api/pi/<pi_id>/test
POST /api/pi/<pi_id>/test
```

### 發送自訂命令
```
POST /api/pi/<pi_id>/command
{
  "command": "SET:100"
}
```

### 新增裝置
```
POST /api/pi
{
  "pi_id": "pi-003",
  "host": "192.168.1.102",
  "port": 5000,
  "name": "新房間"
}
```

### 移除裝置
```
DELETE /api/pi/<pi_id>
```

## 故障排除

### 無法連接到樹梅派

1. 確保樹梅派和 PC 在同一個網路
2. 檢查樹梅派上的 Flask 應用是否正在運行
3. 測試 ping：`ping <pi_host>`
4. 確認連接埠 5000 未被防火牆阻擋

### 燈號不亮

1. 檢查樹梅派上的 Arduino 連接是否正確
2. 在樹梅派的終端測試：`curl http://localhost:5000/api/ping`
3. 查看樹梅派的 Flask 日誌

## 相關文件

- **Raspberry Pi 端**：`../pi-arduino-connection-test-0717/app.py`
- **PC 端 (你在這裡)**：`./app.py`

## 許可證

MIT

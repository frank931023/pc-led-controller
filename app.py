"""
PC 端 Flask 應用 - 控制多台 Raspberry Pi 上的 Arduino 燈號

這個應用提供：
1. 網頁介面：顯示所有連接的 Pi，並提供滑桿/按鈕控制燈號
2. API 端點：供外部應用調用
3. Pi 管理：新增、移除、測試 Pi 連接
"""

from concurrent.futures import ThreadPoolExecutor
from flask import Flask, render_template, request, jsonify
import copy
import json
import os
import tempfile
import threading
import requests

app = Flask(__name__)

# 儲存 Pi 的配置檔。預設放在 data/ 目錄下，Docker 掛載整個目錄而非單一檔案
# ——bind-mount 一個不存在的檔案時，Docker 會建出同名「目錄」，讓存檔靜默失敗。
CONFIG_FILE = os.getenv('CONFIG_FILE', os.path.join('data', 'pi_config.json'))

# load/save 非原子操作，多執行緒同時寫會寫壞 JSON
_config_lock = threading.RLock()

# 檢測 Pi 連線的 timeout（秒）
STATUS_TIMEOUT = 3
COMMAND_TIMEOUT = 10
# 呼叫端可用 timeout 欄位覆蓋 COMMAND_TIMEOUT（校正類指令在真機上可能需要拉到 30 秒以上），
# 這裡另外加一段緩衝給網路來回，避免 Pi 那邊都還沒判定逾時，PC 端的 requests 就先斷線。
COMMAND_TIMEOUT_BUFFER = 5

# 預設配置
DEFAULT_CONFIG = {
    "pis": {
        "pi-01": {
            "host": "172.18.177.105",
            "port": 5000,
            "name": "pi-01",
            "status": "offline"
        },
        "pi-02": {
            "host": "172.18.177.104",
            "port": 5000,
            "name": "pi-02",
            "status": "offline"
        }
    }
}


def load_config():
    """從檔案讀取 Pi 配置"""
    with _config_lock:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                config.setdefault('pis', {})
                return config
            except Exception as e:
                print(f"Error loading config: {e}")
        # 回傳複本，否則呼叫端的修改會污染 DEFAULT_CONFIG
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """儲存 Pi 配置到檔案（先寫暫存檔再 rename，避免寫到一半壞掉）"""
    with _config_lock:
        try:
            directory = os.path.dirname(os.path.abspath(CONFIG_FILE))
            os.makedirs(directory, exist_ok=True)

            fd, tmp_path = tempfile.mkstemp(dir=directory, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, CONFIG_FILE)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
        except Exception as e:
            print(f"Error saving config: {e}")


def set_pi_status(pi_id, status):
    """更新單一 Pi 的狀態並存檔（read-modify-write 全程持鎖）"""
    with _config_lock:
        config = load_config()
        if pi_id not in config['pis']:
            return None
        config['pis'][pi_id]['status'] = status
        save_config(config)
        return config['pis'][pi_id]


def get_pi_url(pi_info, endpoint):
    """組合 Pi 的 API URL"""
    return f"http://{pi_info['host']}:{pi_info['port']}{endpoint}"


def probe_pi(pi_info):
    """
    檢測一台 Pi 是否可用。

    只有 Flask 有回應「而且」Arduino 也接著，才算 online——Flask 活著但 Arduino
    被拔掉時燈一樣不會亮，顯示綠燈只會讓人白白 debug。
    """
    try:
        resp = requests.get(get_pi_url(pi_info, '/api/status'), timeout=STATUS_TIMEOUT)
        if resp.status_code != 200:
            return "offline"
        return "online" if resp.json().get('arduino_status') == 'connected' else "no-arduino"
    except Exception as e:
        print(f"Connection test failed for {pi_info.get('host')}: {e}")
        return "offline"


def update_pi_status():
    """更新所有 Pi 的連線狀態（並行檢測，避免離線裝置把首頁卡住）"""
    config = load_config()
    pi_ids = list(config['pis'].keys())
    if not pi_ids:
        return config

    with ThreadPoolExecutor(max_workers=min(len(pi_ids), 16)) as pool:
        statuses = list(pool.map(lambda pid: probe_pi(config['pis'][pid]), pi_ids))

    with _config_lock:
        config = load_config()
        for pi_id, status in zip(pi_ids, statuses):
            if pi_id in config['pis']:
                config['pis'][pi_id]['status'] = status
        save_config(config)
        return config


# ===== 網頁路由 =====

@app.route('/')
def index():
    """主頁 - 顯示所有 Pi 和控制介面"""
    config = update_pi_status()
    return render_template('index.html', pis=config['pis'])


# ===== API 路由 =====

@app.route('/api/pis', methods=['GET'])
def get_pis():
    """取得所有 Pi 的資訊"""
    config = update_pi_status()
    return jsonify({
        "status": "success",
        "pis": config['pis']
    })


@app.route('/api/pi/<pi_id>', methods=['GET'])
def get_pi(pi_id):
    """取得特定 Pi 的資訊"""
    config = load_config()
    if pi_id not in config['pis']:
        return jsonify({"status": "error", "error": "Pi not found"}), 404

    pi = set_pi_status(pi_id, probe_pi(config['pis'][pi_id]))
    if pi is None:
        return jsonify({"status": "error", "error": "Pi not found"}), 404

    return jsonify({
        "status": "success",
        "pi": pi
    })


@app.route('/api/pi/<pi_id>/command', methods=['POST'])
def send_command(pi_id):
    """發送自訂命令到 Pi。可選的 timeout 欄位（秒）會轉發給 Pi，校正類指令需要較長時間"""
    config = load_config()
    if pi_id not in config['pis']:
        return jsonify({"status": "error", "error": "Pi not found"}), 404

    data = json_body()
    if data is None:
        return jsonify(BAD_JSON[0]), BAD_JSON[1]

    cmd = str(data.get('command', '')).strip()
    if not cmd:
        return jsonify({"status": "error", "error": "Missing command"}), 400

    payload = {"command": cmd}
    request_timeout = COMMAND_TIMEOUT

    if 'timeout' in data and data.get('timeout') not in (None, ''):
        try:
            client_timeout = float(data.get('timeout'))
        except (TypeError, ValueError):
            return jsonify({
                "status": "error",
                "error": f"Invalid timeout value: {data.get('timeout')!r}"
            }), 400
        payload['timeout'] = client_timeout
        request_timeout = client_timeout + COMMAND_TIMEOUT_BUFFER

    url = get_pi_url(config['pis'][pi_id], '/api/command')

    try:
        resp = requests.post(url, json=payload, timeout=request_timeout)
    except requests.exceptions.Timeout:
        set_pi_status(pi_id, "offline")
        return jsonify({"status": "error", "error": "Connection timeout"}), 504
    except requests.exceptions.RequestException as e:
        set_pi_status(pi_id, "offline")
        return jsonify({"status": "error", "error": f"Connection failed: {e}"}), 502

    if resp.status_code != 200:
        set_pi_status(pi_id, "no-arduino" if resp.status_code == 503 else "offline")
        return jsonify({
            "status": "error",
            "error": pi_error(resp, "Failed to send command")
        }), 502

    set_pi_status(pi_id, "online")
    return jsonify({
        "status": "success",
        "pi_id": pi_id,
        "command": cmd,
        "pi_response": safe_json(resp)
    })


@app.route('/api/pi/<pi_id>/test', methods=['GET', 'POST'])
def test_pi(pi_id):
    """測試與 Pi 的連接"""
    config = load_config()
    if pi_id not in config['pis']:
        return jsonify({"status": "error", "error": "Pi not found"}), 404

    url = get_pi_url(config['pis'][pi_id], '/api/ping')

    try:
        resp = requests.get(url, timeout=STATUS_TIMEOUT)
    except requests.exceptions.RequestException as e:
        set_pi_status(pi_id, "offline")
        return jsonify({
            "status": "error",
            "pi_status": "offline",
            "error": f"Connection failed: {e}"
        }), 502

    if resp.status_code != 200:
        pi_status = "no-arduino" if resp.status_code == 503 else "offline"
        set_pi_status(pi_id, pi_status)
        return jsonify({
            "status": "error",
            "pi_status": pi_status,
            "error": pi_error(resp, "Pi responded with error")
        }), 502

    set_pi_status(pi_id, "online")
    return jsonify({
        "status": "success",
        "pi_id": pi_id,
        "pi_status": "online",
        "message": "Connection successful",
        "pi_response": safe_json(resp)
    })


@app.route('/api/pi', methods=['POST'])
def add_pi():
    """新增一台 Pi"""
    data = json_body()
    if data is None:
        return jsonify(BAD_JSON[0]), BAD_JSON[1]

    pi_id = str(data.get('pi_id', '')).strip()
    host = str(data.get('host', '')).strip()
    name = str(data.get('name', '') or pi_id).strip()

    if not pi_id or not host:
        return jsonify({"status": "error", "error": "Missing pi_id or host"}), 400

    try:
        port = int(data.get('port', 5000))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": f"Invalid port: {data.get('port')!r}"}), 400

    if not 1 <= port <= 65535:
        return jsonify({"status": "error", "error": f"Port out of range: {port}"}), 400

    pi_info = {
        "host": host,
        "port": port,
        "name": name,
        "status": "offline"
    }

    with _config_lock:
        config = load_config()
        if pi_id in config['pis']:
            return jsonify({
                "status": "error",
                "error": f"Pi '{pi_id}' already exists"
            }), 400
        config['pis'][pi_id] = pi_info
        save_config(config)

    # 測試連接
    pi = set_pi_status(pi_id, probe_pi(pi_info))

    return jsonify({
        "status": "success",
        "message": f"Pi '{pi_id}' added successfully",
        "pi": pi or pi_info
    })


@app.route('/api/pi/<pi_id>', methods=['DELETE'])
def delete_pi(pi_id):
    """移除一台 Pi"""
    with _config_lock:
        config = load_config()
        if pi_id not in config['pis']:
            return jsonify({"status": "error", "error": "Pi not found"}), 404
        del config['pis'][pi_id]
        save_config(config)

    return jsonify({
        "status": "success",
        "message": f"Pi '{pi_id}' deleted"
    })


def json_body():
    """
    取得請求的 JSON body。

    回傳 None 代表 body 缺少或不是合法 JSON——要跟「JSON 合法但欄位沒填」分開回報，
    否則編碼壞掉的請求會得到「Missing pi_id or host」這種找錯方向的訊息。
    """
    return request.get_json(silent=True)


BAD_JSON = ({"status": "error", "error": "Invalid or missing JSON body"}, 400)


def safe_json(resp):
    """Pi 理論上都回 JSON，但真壞掉時不該讓 PC 端也跟著 500"""
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:200]}


def pi_error(resp, fallback):
    return safe_json(resp).get('error', fallback)


if __name__ == '__main__':
    # 初始化配置檔
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)

    # debug=True 會把 Werkzeug debugger 開放給整個區域網路（可遠端執行程式碼），
    # 所以預設關閉；本機開發要用時再設 FLASK_DEBUG=1。
    debug = os.getenv('FLASK_DEBUG', '0').lower() in ('1', 'true', 'yes')
    print(f"Starting PC LED Controller (debug={debug})...")
    app.run(host='0.0.0.0', port=5001, debug=debug)

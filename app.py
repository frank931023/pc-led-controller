"""
PC 端 Flask 應用 - 控制多台 Raspberry Pi 上的 Arduino 燈號

這個應用提供：
1. 網頁介面：顯示所有連接的 Pi，並提供滑桿/按鈕控制燈號
2. API 端點：供外部應用調用
3. Pi 管理：新增、移除、測試 Pi 連接
"""

from flask import Flask, render_template, request, jsonify
import requests
import json
import os
from pathlib import Path

app = Flask(__name__)

# 儲存 Pi 的配置檔
CONFIG_FILE = 'pi_config.json'

# 預設配置
DEFAULT_CONFIG = {
    "pis": {
        "pi-001": {
            "host": "172.18.177.105",
            "port": 5000,
            "name": "pi-01",
            "brightness": 0,
            "status": "offline"
        },
        "pi-002": {
            "host": "172.18.177.104",
            "port": 5000,
            "name": "pi-02",
            "brightness": 0,
            "status": "offline"
        }
    }
}


def load_config():
    """從檔案讀取 Pi 配置"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG


def save_config(config):
    """儲存 Pi 配置到檔案"""
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving config: {e}")


def get_pi_url(pi_id, endpoint):
    """組合 Pi 的 API URL"""
    config = load_config()
    if pi_id not in config['pis']:
        return None
    
    pi_info = config['pis'][pi_id]
    return f"http://{pi_info['host']}:{pi_info['port']}{endpoint}"


def test_pi_connection(pi_id):
    """測試連接到某台 Pi"""
    url = get_pi_url(pi_id, '/api/status')
    if not url:
        return False
    
    try:
        resp = requests.get(url, timeout=3)
        return resp.status_code == 200
    except Exception as e:
        print(f"Connection test failed for {pi_id}: {e}")
        return False


def update_pi_status():
    """更新所有 Pi 的連線狀態"""
    config = load_config()
    for pi_id in config['pis']:
        status = "online" if test_pi_connection(pi_id) else "offline"
        config['pis'][pi_id]['status'] = status
    save_config(config)


# ===== 網頁路由 =====

@app.route('/')
def index():
    """主頁 - 顯示所有 Pi 和控制介面"""
    update_pi_status()
    config = load_config()
    return render_template('index.html', pis=config['pis'])


# ===== API 路由 =====

@app.route('/api/pis', methods=['GET'])
def get_pis():
    """取得所有 Pi 的資訊"""
    update_pi_status()
    config = load_config()
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
    
    # 測試連接
    is_online = test_pi_connection(pi_id)
    config['pis'][pi_id]['status'] = "online" if is_online else "offline"
    save_config(config)
    
    return jsonify({
        "status": "success",
        "pi": config['pis'][pi_id]
    })


@app.route('/api/pi/<pi_id>/brightness', methods=['POST'])
def set_brightness(pi_id):
    """設定特定 Pi 上的 LED 亮度"""
    try:
        config = load_config()
        if pi_id not in config['pis']:
            return jsonify({"status": "error", "error": "Pi not found"}), 404
        
        data = request.json
        brightness = int(data.get('brightness', 0))
        brightness = max(0, min(255, brightness))
        
        # 發送指令到 Pi
        url = get_pi_url(pi_id, '/api/set_brightness')
        resp = requests.post(url, json={"value": brightness}, timeout=5)
        
        if resp.status_code == 200:
            config['pis'][pi_id]['brightness'] = brightness
            config['pis'][pi_id]['status'] = "online"
            save_config(config)
            return jsonify({
                "status": "success",
                "pi_id": pi_id,
                "brightness": brightness,
                "pi_response": resp.json()
            })
        else:
            config['pis'][pi_id]['status'] = "offline"
            save_config(config)
            return jsonify({
                "status": "error",
                "error": "Failed to set brightness on Pi"
            }), 500
            
    except requests.exceptions.Timeout:
        config = load_config()
        config['pis'][pi_id]['status'] = "offline"
        save_config(config)
        return jsonify({
            "status": "error",
            "error": "Connection timeout"
        }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 400


@app.route('/api/pi/<pi_id>/command', methods=['POST'])
def send_command(pi_id):
    """發送自訂命令到 Pi"""
    try:
        config = load_config()
        if pi_id not in config['pis']:
            return jsonify({"status": "error", "error": "Pi not found"}), 404
        
        data = request.json
        cmd = data.get('command', '')
        
        if not cmd:
            return jsonify({"status": "error", "error": "Missing command"}), 400
        
        url = get_pi_url(pi_id, '/api/command')
        resp = requests.post(url, json={"command": cmd}, timeout=5)
        
        if resp.status_code == 200:
            config['pis'][pi_id]['status'] = "online"
            save_config(config)
            return jsonify({
                "status": "success",
                "pi_id": pi_id,
                "command": cmd,
                "pi_response": resp.json()
            })
        else:
            config['pis'][pi_id]['status'] = "offline"
            save_config(config)
            return jsonify({
                "status": "error",
                "error": "Failed to send command"
            }), 500
            
    except requests.exceptions.Timeout:
        config = load_config()
        config['pis'][pi_id]['status'] = "offline"
        save_config(config)
        return jsonify({"status": "error", "error": "Connection timeout"}), 500
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 400


@app.route('/api/pi/<pi_id>/test', methods=['GET', 'POST'])
def test_pi(pi_id):
    """測試與 Pi 的連接"""
    config = load_config()
    if pi_id not in config['pis']:
        return jsonify({"status": "error", "error": "Pi not found"}), 404
    
    try:
        url = get_pi_url(pi_id, '/api/ping')
        resp = requests.get(url, timeout=3)
        
        if resp.status_code == 200:
            config['pis'][pi_id]['status'] = "online"
            save_config(config)
            return jsonify({
                "status": "success",
                "pi_id": pi_id,
                "message": "Connection successful",
                "pi_response": resp.json()
            })
        else:
            config['pis'][pi_id]['status'] = "offline"
            save_config(config)
            return jsonify({
                "status": "error",
                "error": "Pi responded with error"
            }), 500
    except Exception as e:
        config['pis'][pi_id]['status'] = "offline"
        save_config(config)
        return jsonify({
            "status": "error",
            "error": f"Connection failed: {str(e)}"
        }), 500


@app.route('/api/pi', methods=['POST'])
def add_pi():
    """新增一台 Pi"""
    try:
        data = request.json
        pi_id = data.get('pi_id', '').strip()
        host = data.get('host', '').strip()
        port = int(data.get('port', 5000))
        name = data.get('name', pi_id).strip()
        
        if not pi_id or not host:
            return jsonify({
                "status": "error",
                "error": "Missing pi_id or host"
            }), 400
        
        config = load_config()
        
        if pi_id in config['pis']:
            return jsonify({
                "status": "error",
                "error": f"Pi '{pi_id}' already exists"
            }), 400
        
        config['pis'][pi_id] = {
            "host": host,
            "port": port,
            "name": name,
            "brightness": 0,
            "status": "offline"
        }
        save_config(config)
        
        # 測試連接
        is_online = test_pi_connection(pi_id)
        config['pis'][pi_id]['status'] = "online" if is_online else "offline"
        save_config(config)
        
        return jsonify({
            "status": "success",
            "message": f"Pi '{pi_id}' added successfully",
            "pi": config['pis'][pi_id]
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 400


@app.route('/api/pi/<pi_id>', methods=['DELETE'])
def delete_pi(pi_id):
    """移除一台 Pi"""
    config = load_config()
    if pi_id not in config['pis']:
        return jsonify({
            "status": "error",
            "error": "Pi not found"
        }), 404
    
    del config['pis'][pi_id]
    save_config(config)
    
    return jsonify({
        "status": "success",
        "message": f"Pi '{pi_id}' deleted"
    })


if __name__ == '__main__':
    # 初始化配置檔
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
    
    # 啟動 Flask
    print("Starting PC LED Controller...")
    app.run(host='0.0.0.0', port=5001, debug=True)

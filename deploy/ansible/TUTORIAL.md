# 從零開始：用 Ansible 部署所有 Pi

這份教學帶你從一台什麼都沒裝的 Windows 電腦和全新的 Raspberry Pi 開始，做到**一行指令部署 / 更新所有 Pi**。
照順序做，每一步最後都有「✅ 確認」，確認過再往下。

> 只想查指令和變數？看同目錄的 [README.md](README.md)。

---

## 目錄

0. [先搞懂整體在做什麼](#0-先搞懂整體在做什麼)
1. [把 Pi 程式推上 GitHub](#1-把-pi-程式推上-github)
2. [啟動 PC 控制中心](#2-啟動-pc-控制中心)
3. [準備每一台 Pi](#3-準備每一台-pi)
4. [在 Windows 裝 WSL 和 Ansible](#4-在-windows-裝-wsl-和-ansible)
5. [設定 SSH key（免密碼登入每台 Pi）](#5-設定-ssh-key免密碼登入每台-pi)
6. [建立 inventory 與設定檔](#6-建立-inventory-與設定檔)
7. [部署前檢查](#7-部署前檢查)
8. [第一次部署](#8-第一次部署)
9. [日常操作](#9-日常操作)
10. [（選用）用 CI 建好的 image，部署更快](#10-選用用-ci-建好的-image部署更快)
11. [疑難排解](#11-疑難排解)
12. [完成檢查清單](#12-完成檢查清單)

---

## 0. 先搞懂整體在做什麼

```
你的 Windows 電腦
 ├─ WSL (Ubuntu) ── Ansible ──SSH──▶ pi-01 ─ Arduino Uno
 │                         ──SSH──▶ pi-02 ─ Arduino Uno
 │                         ──SSH──▶ ...   ─ Arduino Mega
 │                                    │
 │                                    └─ git clone ◀── GitHub（Pi 程式）
 └─ Docker ── PC 控制中心 :5001 ◀─────── Ansible 登記每台 Pi
            └ Beszel hub   :8090 ◀────── 每台 Pi 的監控 agent
```

Ansible 對**每一台 Pi** 做的事：

1. 沒有 Docker 就裝 Docker、裝 git
2. 從 GitHub 抓 Pi 程式到 `/opt/pi-arduino-controller`
3. 自動找出 Arduino 的序列埠
4. 產生 `.env`（`PI_ID` = 你在 inventory 取的名字）
5. `docker compose up` 啟動
6. 等 Pi 服務起來，把它登記到 PC 控制中心

**你需要準備：**

| 東西 | 說明 |
|---|---|
| Windows 電腦 | 跑 PC 控制中心（Docker Desktop）和 Ansible（WSL） |
| N 台 Raspberry Pi + microSD | 每台 Pi 接 **一台** Arduino（Uno 或 Mega 都可以） |
| 同一個區網 | 電腦和所有 Pi 互相連得到 |
| GitHub 帳號 | Pi 程式要在 GitHub 上，Pi 才抓得到 |

先準備一張表，邊做邊填（後面會一直用到）：

| inventory 名稱 | IP | Arduino |
|---|---|---|
| pi-01 | | Uno |
| pi-02 | | Uno |
| … | | |
| pi-07 | | Mega |

> **名稱很重要**：inventory 名稱 = Pi 的 `PI_ID` = 控制中心的 Device ID = Beszel 裡的名稱。
> 取好就不要再改。建議用 `pi-01`、`pi-02` … 這種格式。

---

## 1. 把 Pi 程式推上 GitHub

Ansible 是讓每台 Pi **自己從 GitHub 下載**程式，不是從你的電腦複製過去。
所以 Pi 程式（`pi-arduino-connection-test-0717`）有任何改動，都要先 push：

```powershell
cd "D:\frank\Logitech Intern\pi-arduino-connection-test-0717"
git status          # 看有哪些改動
git add -A
git commit -m "說明這次改了什麼"
git push
```

PC 專案（`pc-led-controller`，Ansible 檔案在這裡）是在你電腦本機執行的，不 push 也能用，但建議一起 commit 保存。

✅ **確認**：瀏覽器打開 `https://github.com/frank931023/pi-arduino-connection-test-0717`，最新 commit 是你剛推的。
repo 必須是 **public**（目前是），否則 Pi 沒有權限下載。

---

## 2. 啟動 PC 控制中心

### 2.1 找出 PC 的區網 IP

PowerShell：

```powershell
ipconfig
```

找你**連到 Pi 那個網路**的網卡（乙太網路或 Wi-Fi）的 `IPv4 位址`，例如 `172.18.177.10`。
**不要**用 `vEthernet (WSL)`、`Docker` 開頭的那些。

> 建議到路由器把這個 IP 設成固定（DHCP 保留），不然 PC 的 IP 一變，所有 Pi 的監控都要重新部署。

### 2.2 啟動

```powershell
cd "D:\frank\Logitech Intern\pc-led-controller"
copy .env.example .env      # 第一次才需要；之後打開 .env 確認 BESZEL_PASSWORD 有填
docker compose up -d --build
```

✅ **確認**：

- 瀏覽器打開 `http://<PC IP>:5001`（用 IP，不要用 localhost）看得到控制中心
- 打開 `http://<PC IP>:5001/api/health` 看到 `"status": "success"`

> 打不開？Windows 防火牆可能擋了 5001 / 8090。第一次執行 Docker 時跳出的防火牆視窗要按「允許」，
> 或到「Windows Defender 防火牆 → 允許應用程式」勾選 Docker Desktop 的私人網路。

---

## 3. 準備每一台 Pi

### 3.1 燒錄系統（每張 microSD 都做一次）

1. 下載並安裝 [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
2. 選擇裝置 → 你的 Pi 型號；作業系統 → **Raspberry Pi OS Lite (64-bit)**（不需要桌面）
3. 按「下一步」→「編輯設定」(OS customisation)：
   - **主機名稱**：跟 inventory 名稱一樣，例如 `pi-01`（不一樣也能用，但一樣比較好認）
   - **使用者名稱**：`pi`，密碼自己設（之後只在步驟 5 用一次）
   - **Wi-Fi**：用 Wi-Fi 才要填；能接網路線更穩定
   - **服務** 分頁：勾選 **啟用 SSH** → 使用密碼登入
4. 寫入，插回 Pi，接上 Arduino 和電源

### 3.2 找出每台 Pi 的 IP

任一種方式：

- 路由器管理頁面的「已連線裝置」清單
- PowerShell：`ping pi-01.local`（主機名稱 + `.local`），回應裡就是 IP

**強烈建議**在路由器把每台 Pi 設成固定 IP（DHCP 保留），否則 Pi 重開機換了 IP，控制中心就找不到它。

把 IP 填進步驟 0 的表。

### 3.3 第一次登入（每台都要）

PowerShell 或 WSL 都可以：

```bash
ssh pi@172.18.177.105
```

第一次會問 `Are you sure you want to continue connecting?` → 輸入 `yes`，再輸入密碼。

登入後執行：

```bash
# Raspberry Pi OS trixie 一定要裝，否則 Docker 容器連不到網路（舊版裝了也無害）
sudo apt update && sudo apt install -y iptables

# 看 Arduino 有沒有被認到（Uno / Mega 原廠板通常是 ttyACM0，副廠 CH340 板是 ttyUSB0）
ls -l /dev/serial/by-id/

exit
```

✅ **確認**：每台 Pi 都能 SSH 登入，`/dev/serial/by-id/` 底下有一個 Arduino 開頭的裝置。
沒接 Arduino 也能繼續，部署會成功，只是控制中心顯示 **No Arduino**。

---

## 4. 在 Windows 裝 WSL 和 Ansible

Ansible 不能直接在 Windows 上跑，要在 WSL（Windows 內建的 Linux）裡跑。

### 4.1 安裝 WSL（已經有就跳過）

以**系統管理員**身分開 PowerShell：

```powershell
wsl --install -d Ubuntu
```

完成後**重開機**。開機後會自動跳出 Ubuntu 視窗，要你設定 Linux 的使用者名稱和密碼（跟 Windows 無關，自己記得）。

之後要進 WSL：開始選單搜尋「Ubuntu」，或在 PowerShell 輸入 `wsl`。

### 4.2 安裝 Ansible

以下都在 **WSL (Ubuntu)** 裡執行：

```bash
sudo apt update
sudo apt install -y pipx openssh-client curl
pipx ensurepath
```

**關掉 Ubuntu 視窗再重開一次**（讓 PATH 生效），然後：

```bash
pipx install ansible-core
```

✅ **確認**：

```bash
ansible --version
```

看到 `ansible [core 2.1x.x]`（2.15 以上都可以）。

---

## 5. 設定 SSH key（免密碼登入每台 Pi）

Ansible 要能**不輸入密碼**就登入每台 Pi。做法是產生一組金鑰，把公鑰放到每台 Pi 上。

### 5.1 產生金鑰（只做一次）

WSL 裡：

```bash
ssh-keygen -t ed25519 -C "pi-deploy"
```

三個問題都直接按 Enter（存在預設位置 `~/.ssh/id_ed25519`、不設密碼）。

> 金鑰要放在 WSL 的家目錄（`~/.ssh`），**不要**放在 `/mnt/c` 或 `/mnt/d` 底下，
> 那裡的檔案權限太寬，SSH 會拒絕使用。

### 5.2 把公鑰複製到每台 Pi

一台一台：

```bash
ssh-copy-id pi@172.18.177.105
```

或一次全部（IP 換成你的）：

```bash
for ip in 172.18.177.105 172.18.177.104 172.18.177.106; do
  ssh-copy-id pi@$ip
done
```

每台會問一次 `yes` 和 Pi 的密碼，這是**最後一次**需要輸入 Pi 密碼。

✅ **確認**：

```bash
ssh pi@172.18.177.105 hostname
```

**不用輸入密碼**就印出主機名稱（例如 `pi-01`）。每台都試一次。

---

## 6. 建立 inventory 與設定檔

### 6.1 讓 Ansible 找到設定檔

WSL 可以直接存取 D 槽（在 `/mnt/d`），但 Ansible 會因為「這個資料夾所有人都能寫入」而**拒絕讀取**裡面的 `ansible.cfg`。
解法是明確告訴它設定檔在哪。在 WSL 裡執行一次：

```bash
cat >> ~/.bashrc <<'EOF'

# Pi 部署
export ANSIBLE_CONFIG="/mnt/d/frank/Logitech Intern/pc-led-controller/deploy/ansible/ansible.cfg"
alias pideploy='cd "/mnt/d/frank/Logitech Intern/pc-led-controller/deploy/ansible"'
EOF
source ~/.bashrc
```

之後在 WSL 輸入 `pideploy` 就會進到 Ansible 資料夾。

### 6.2 建立 inventory（裝置清單）

```bash
pideploy
cp inventory.example.yml inventory.yml
nano inventory.yml
```

改成你的 Pi（7 台的範例）：

```yaml
all:
  children:
    pis:
      vars:
        ansible_user: pi
      hosts:
        pi-01:
          ansible_host: 172.18.177.105
        pi-02:
          ansible_host: 172.18.177.104
        pi-03:
          ansible_host: 172.18.177.106
        pi-04:
          ansible_host: 172.18.177.107
        pi-05:
          ansible_host: 172.18.177.108
        pi-06:
          ansible_host: 172.18.177.109
        pi-07:
          ansible_host: 172.18.177.110
```

注意事項：

- **縮排要用空白，不能用 Tab**，而且要對齊（YAML 很在意縮排）
- 每台 Pi 只接一台 Arduino 時**不用填** `serial_port`，會自動偵測
- 某台 Pi 接了不只一個 USB 序列裝置，才需要指定，用 `/dev/serial/by-id/` 的完整路徑（步驟 3.3 看到的那個）：
  ```yaml
        pi-07:
          ansible_host: 172.18.177.110
          serial_port: /dev/serial/by-id/usb-Arduino__www.arduino.cc__0042_xxxx-if00
  ```

nano 存檔：`Ctrl+O` → Enter；離開：`Ctrl+X`。

> `inventory.yml` 已列入 `.gitignore`，不會被 commit（裡面有 IP）。

### 6.3 設定控制中心位址

```bash
nano group_vars/all.yml
```

至少確認這一行是**步驟 2.1 查到的 PC IP**：

```yaml
pc_url: http://172.18.177.10:5001
```

⚠ 不能寫 `localhost` 或 `127.0.0.1`：Pi 的監控 agent 要靠這個位址連回 PC。

其他設定通常不用改：

| 變數 | 預設 | 什麼時候改 |
|---|---|---|
| `pi_repo_version` | `main` | 要部署特定 branch / tag |
| `pi_image` | `""`（在 Pi 上 build） | 見步驟 10 |
| `command_timeout` | `130` | 校正 / 歸位需要更久 |
| `baud_rate` | `115200` | 韌體 `Serial.begin()` 改了 |
| `monitoring` | `true` | 不要 Beszel 健康監控就改 `false` |
| `pc_api_token` | `""` | 控制中心 `.env` 有設 `API_TOKEN` 時（建議用 `-e` 帶，不要寫進檔案） |

---

## 7. 部署前檢查

都在 WSL 的 `pideploy` 資料夾裡：

```bash
# 1. Ansible 有讀到設定和 inventory 嗎？
ansible-inventory --graph
```
應該列出 `@pis` 底下所有 Pi。看到 `[WARNING]: No inventory was parsed` → 回頭檢查 6.1 的 `ANSIBLE_CONFIG`。

```bash
# 2. 每台 Pi 都連得到嗎？
ansible all -m ping
```
每台都要是綠色的 `SUCCESS` 和 `"ping": "pong"`。

```bash
# 3. WSL 連得到控制中心嗎？
curl http://172.18.177.10:5001/api/health
```
要看到 `"status": "success"`。

```bash
# 4. （monitoring: true 時）控制中心能給出 Beszel 設定嗎？
curl http://172.18.177.10:5001/api/beszel/agent-env
```
要看到 `key`、`token` 有值，而且 `warning` 是 `null`。

✅ **確認**：四項都通過。

---

## 8. 第一次部署

### 8.1 先部署一台試試

```bash
ansible-playbook site.yml --limit pi-01
```

第一次比較久（要在 Pi 上裝 Docker、build image），**可能要好幾分鐘**，畫面停在某一步是正常的。

成功的話最後會看到：

```
TASK [Report] ****
ok: [pi-01] =>
  msg: 'pi-01: online · version main@abc1234 · serial /dev/serial/by-id/usb-Arduino...'

PLAY RECAP ****
pi-01 : ok=16  changed=6  unreachable=0  failed=0  skipped=3 ...
```

- `failed=0`、`unreachable=0` 就是成功
- 狀態可能是 `online`（Arduino 有回應）或 `no_arduino`（沒接或埠不對）

### 8.2 部署全部

```bash
ansible-playbook site.yml
```

所有 Pi 會**同時**部署。想一台一台來（前一台成功才換下一台）：

```bash
ansible-playbook site.yml -e pi_batch_size=1
```

### 8.3 檢查結果

- 控制中心 `http://<PC IP>:5001`：每台 Pi 都出現了，不用手動 + Add
- 點進一台：看得到 Controls 按鈕、App version、序列埠
- 每台 Pi 自己的頁面 `http://<Pi IP>:5000`：標題是 `pi-01` 這類名稱（不是 `pi-001`）
- 開了監控的話，Health 區塊幾十秒後會有 CPU / 記憶體 / 溫度

### 8.4 再跑一次確認

```bash
ansible-playbook site.yml
```

這次應該全部是 `changed=0`，代表沒有變更、容器也沒有重啟。**任何時候重跑都是安全的。**

---

## 9. 日常操作

每次都先在 WSL 輸入 `pideploy`。

| 想做的事 | 做法 |
|---|---|
| **更新 Pi 程式** | Pi 專案 `git push` → `ansible-playbook site.yml` |
| **新增一台 Pi** | 做步驟 3、5.2 → inventory 加一行 → `ansible-playbook site.yml --limit pi-08` |
| **只處理某一台** | `ansible-playbook site.yml --limit pi-03` |
| **處理幾台** | `ansible-playbook site.yml --limit pi-01,pi-03` |
| **改 timeout / baud rate** | 改 `group_vars/all.yml` → `ansible-playbook site.yml`（不要直接改 Pi 上的 `.env`，下次會被覆蓋） |
| **某台用不同設定** | 在 inventory 該台底下加，例如 `command_timeout: 200` |
| **部署特定版本** | `ansible-playbook site.yml -e pi_repo_version=v1.2`（tag、branch、commit 都行） |
| **拿掉一台 Pi** | 從 inventory 刪掉 → 到控制中心網頁按 **Remove**（Ansible 不會自動刪） |
| **關掉健康監控** | `ansible-playbook site.yml -e monitoring=false` |
| **控制中心有 API_TOKEN** | 指令最後加 `-e pc_api_token=你的token` |
| **看某台 Pi 的 log** | `ssh pi@<IP> "cd /opt/pi-arduino-controller && sudo docker compose logs --tail 50 arduino-controller"` |
| **對所有 Pi 下同一個指令** | `ansible all -a "uptime"`、`ansible all -b -a "reboot"` |
| **先預覽會改什麼** | `ansible-playbook site.yml --check --diff`（部分步驟在 check 模式會略過，僅供參考） |

---

## 10. （選用）用 CI 建好的 image，部署更快

預設每台 Pi 都要自己 build image，慢而且每台都做一次。
Pi 專案已經有 GitHub Actions（`.github/workflows/image.yml`），push 後會自動 build 好 image 放到 GHCR：

1. push 之後到 GitHub repo 的 **Actions** 分頁，等 workflow 變綠色
2. 到 GitHub 個人頁 → **Packages** → `pi-arduino-connection-test-0717` → **Package settings** → **Change visibility** → **Public**
   （不設 public 的話，每台 Pi 都要先 `docker login ghcr.io`）
3. 改 `group_vars/all.yml`：
   ```yaml
   pi_image: ghcr.io/frank931023/pi-arduino-connection-test-0717:main
   ```
4. `ansible-playbook site.yml`

之後的更新流程：push → 等 Actions 完成 → `ansible-playbook site.yml`。

---

## 11. 疑難排解

| 症狀 | 原因與解法 |
|---|---|
| `ansible: command not found` | 步驟 4.2 的 `pipx ensurepath` 後沒重開視窗。關掉 Ubuntu 重開 |
| `[WARNING]: Ansible is being run in a world writable directory ... ignoring it as an ansible.cfg source` | 沒設 `ANSIBLE_CONFIG`，見 6.1。設了還出現就 `source ~/.bashrc` 或重開視窗 |
| `No inventory was parsed` / `no hosts matched` | 同上；或 `inventory.yml` 不存在、縮排錯了。用 `ansible-inventory --graph` 檢查 |
| `UNREACHABLE! ... Permission denied (publickey,password)` | SSH key 沒裝到那台 Pi，重做 5.2 |
| `UNREACHABLE! ... Connection timed out` / `No route to host` | IP 錯了、Pi 沒開機或不在同一個網路。先 `ping <IP>` |
| `Missing sudo password` | 那台 Pi 的帳號 sudo 需要密碼。指令加 `-K`，會問一次 sudo 密碼 |
| `Check that the control center is reachable` 失敗 | WSL 連不到 `pc_url`。確認 `pc_url` 的 IP、控制中心有在跑、Windows 防火牆允許 5001 |
| `Fail early when the control center needs a token` | 控制中心有設 `API_TOKEN`，加 `-e pc_api_token=...` |
| `Refuse a hub URL the Pis cannot reach` | `pc_url` 寫了 localhost / 127.0.0.1，改成 PC 的區網 IP |
| 卡在 `Install Docker` 很久 | 正常，第一次要下載安裝。超過 15 分鐘才需要懷疑網路 |
| `Start the containers` 失敗，訊息有 `network` / `iptables` | trixie 沒裝 iptables，做 3.3 的 `sudo apt install -y iptables` 後重開 Pi 再跑 |
| 部署成功但狀態是 `no_arduino` | Arduino 沒接、或埠不對。`ssh pi@<IP> ls -l /dev/serial/by-id/` 看看，必要時在 inventory 設 `serial_port`，再跑一次 |
| 狀態 `online` 但控制中心沒有 Controls 按鈕 | 那台 Pi 跑的是舊版程式：確認 Pi 專案已 push，再跑一次 `site.yml` |
| Pi 頁面標題是 `pi-001` | 那台不是用 Ansible 部署的（`.env` 沒有 `PI_ID`）。加進 inventory 再跑一次 |
| Health 一直沒資料 | `curl <pc_url>/api/beszel/agent-env` 檢查；PC 的 Windows 防火牆要允許 8090 |
| 每次跑都 `changed` | 回報給維護者；正常重跑應該是 `changed=0` |

需要看更多細節：任何指令後面加 `-v`（或 `-vvv` 看 SSH 細節）。

---

## 12. 完成檢查清單

- [ ] Pi 專案最新的改動已 push 到 GitHub
- [ ] 控制中心 `http://<PC IP>:5001` 打得開
- [ ] 每台 Pi 已燒錄、開 SSH、固定 IP、裝好 iptables
- [ ] WSL 裡 `ansible --version` 正常
- [ ] WSL 裡 `ssh pi@<每台 IP> hostname` 不用密碼
- [ ] `~/.bashrc` 有 `ANSIBLE_CONFIG` 和 `pideploy`
- [ ] `inventory.yml` 列了所有 Pi，`group_vars/all.yml` 的 `pc_url` 是 PC 區網 IP
- [ ] `ansible all -m ping` 全部 SUCCESS
- [ ] `ansible-playbook site.yml` 全部 `failed=0`
- [ ] 控制中心看得到所有 Pi，再跑一次是 `changed=0`

# Ansible：一次部署所有 Pi

> **第一次用？** 照 [TUTORIAL.md](TUTORIAL.md) 從安裝 WSL / Ansible、設定 SSH key 一步步做到部署完成。
> 這份 README 是給已經會用的人查指令和變數。

`site.yml` 會對 `inventory.yml` 裡的每台 Pi 做完整部署，並把它註冊到 Motion Control Center：

1. 確認控制中心連得到；開了健康監控時，向它取得 Beszel agent 設定
2. 沒有 Docker 就安裝（官方腳本）、安裝 git
3. 取得 Pi 程式碼（`pi_repo_url` 的 `pi_repo_version`）
4. 找出 Arduino：`/dev/serial/by-id` 底下只有一個裝置就用它，否則用 `serial_port` 或 `/dev/ttyUSB0`
5. 產生 Pi 的 `.env`（`PI_ID` = inventory 的主機名稱）
6. 有設 `pi_image` 就 `docker compose pull`，否則在 Pi 上 build；然後 `docker compose up -d`
7. 等 Pi 的 `/api/health` 回應
8. `PUT /api/pi/<id>` 註冊到控制中心（已存在就只更新 host / port / name，網頁上改的別名和 Beszel 綁定會保留）
9. 印出控制中心看到的狀態、版本、序列埠

**可以重複執行**：沒有變更時每一步都是 `ok`、`changed=0`，不會重啟容器。
新增 Pi = 在 inventory 加一行再跑一次；更新程式 = 再跑一次。

## 需求

- **控制端**：Linux 或 macOS，裝 `ansible-core`（2.15 以上，已用 2.19 實測）。
  Windows 不能直接當 Ansible 控制端，請在 WSL 裡執行：
  ```bash
  # WSL (Ubuntu)
  sudo apt install -y pipx && pipx install ansible-core
  ```
- **每台 Pi**：Raspberry Pi OS / Debian，能用 SSH 登入（建議 key 登入：`ssh-copy-id pi@<IP>`），帳號有 sudo
- 控制端連得到每台 Pi 的 SSH，也連得到 PC 控制中心的 `5001`

> Raspberry Pi OS trixie 第一次要先 `sudo apt install -y iptables`，否則 Docker 容器連不出網路
> （見 Pi 專案 README「步驟 1」與故障排除）。

## 使用

```bash
cd deploy/ansible
cp inventory.example.yml inventory.yml     # 填入每台 Pi 的 IP
nano group_vars/all.yml                    # 至少確認 pc_url（用 PC 的區網 IP，不要 localhost）

ansible all -m ping                        # 先確認每台都連得到
ansible-playbook site.yml                  # 部署全部
```

常用變化：

```bash
ansible-playbook site.yml --limit pi-03            # 只部署一台
ansible-playbook site.yml -e pi_batch_size=1       # 滾動更新：一次一台，前一台成功才換下一台
ansible-playbook site.yml -e pi_repo_version=v1.2  # 部署指定版本（tag / branch / commit）
ansible-playbook site.yml -e monitoring=false      # 停用並移除所有 Pi 的健康監控 agent
ansible-playbook site.yml -e pc_api_token=xxxx     # 控制中心有設 API_TOKEN 時
```

## 設定

| 變數 | 位置 | 說明 |
|---|---|---|
| `pc_url` | `group_vars/all.yml` | 控制中心網址（PC 的區網 IP）。給 Pi 的 Beszel hub 網址也是從它推出來的 |
| `pc_api_token` | `-e` 或 ansible-vault | 控制中心 `.env` 的 `API_TOKEN`，沒設就留空 |
| `pi_repo_url` / `pi_repo_version` | `group_vars/all.yml` | Pi 程式碼來源與版本 |
| `pi_image` | `group_vars/all.yml` | CI 建好的 image（例如 `ghcr.io/frank931023/pi-arduino-connection-test-0717:main`）；空 = 在 Pi 上 build |
| `pi_dir` | `group_vars/all.yml` | Pi 上的安裝位置，預設 `/opt/pi-arduino-controller` |
| `monitoring` | `group_vars/all.yml` | `true` = 啟動 Beszel agent；`false` = 移除 |
| `serial_port` | inventory（每台） | Arduino 的序列埠；不填就自動偵測 |
| `pi_port` / `pi_name` / `register_host` | inventory（每台） | API 埠、控制中心顯示名稱、控制中心連這台 Pi 用的位址 |
| `command_timeout` / `max_command_timeout` / `baud_rate` | `group_vars/all.yml` | 寫進每台 Pi 的 `.env` |

**inventory 是唯一的裝置清單**：主機名稱同時是 Pi 的 `PI_ID`、控制中心的 Device ID、Beszel agent 的名稱，
三者永遠一致。網頁上的 **+ Add** 仍然可以用（臨時加一台測試用），但正式的 Pi 請加在 inventory。

> Ansible 不會刪除控制中心裡的裝置。從 inventory 拿掉一台 Pi 之後，請在網頁上 **Remove**。

## 注意事項

- 每台 Pi 的 `.env` 由 Ansible 產生，**手動修改會在下次執行時被覆蓋**——改 inventory 或 `group_vars`
- `inventory.yml` 已列入 `.gitignore`（含 IP），`inventory.example.yml` 才進版控
- `pc_api_token`、Beszel token 都是秘密：用 `-e` 帶入或 `ansible-vault encrypt_string`，不要 commit 明碼

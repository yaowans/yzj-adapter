# 非 Docker 部署指南（systemd）

在 Ubuntu 22.04 上以 systemd 服务方式部署 yzj-adapter。

---

## 1. 系统要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Ubuntu 20.04+ / Debian 11+ / CentOS 8+ |
| Python | 3.11+ |
| 内存 | ≥ 256MB 可用 |
| 网络 | 能访问 Hermes API 和云之家服务器 |

---

## 2. 安装 Python 3.11

### Ubuntu 22.04

```bash
apt update
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.11 python3.11-venv python3.11-dev
```

### 验证

```bash
python3.11 --version
# Python 3.11.x
```

---

## 3. 创建项目目录

```bash
mkdir -p /opt/yzj-adapter
cd /opt/yzj-adapter
```

将以下文件放入 `/opt/yzj-adapter/`：

- `app.py`
- `requirements.txt`

---

## 4. 创建虚拟环境并安装依赖

```bash
cd /opt/yzj-adapter

python3.11 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

deactivate
```

**requirements.txt 内容：**

```txt
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
websockets==13.1
cachetools==5.5.0
```

验证安装：

```bash
/opt/yzj-adapter/.venv/bin/python -c "import fastapi, httpx, websockets, cachetools; print('依赖安装成功')"
```

---

## 5. 配置环境变量

创建 `/opt/yzj-adapter/.env`：

```bash
cat > /opt/yzj-adapter/.env <<'EOF'
# ─── 云之家配置 ───
YZJ_SEND_MSG_URL=https://www.yunzhijia.com/gateway/robot/webhook/send?yzjtype=12&yzjtoken=YOUR_YZJTOKEN
YZJ_SECRET=YOUR_YZJ_SECRET

# ─── Hermes API 配置 ───
HERMES_API_BASE=http://127.0.0.1:8642
HERMES_API_KEY=YOUR_HERMES_API_KEY
HERMES_MODEL=hermes-agent

# ─── 超时配置 ───
REQUEST_TIMEOUT=120
SEND_TIMEOUT=15

# ─── 会话配置 ───
MAX_HISTORY=10

# ─── WebSocket 配置 ───
ENABLE_WEBSOCKET=true
WS_RECONNECT_SECONDS=5

# ─── 机器人行为 ───
BOT_SYSTEM_PROMPT=你是接入云之家群组的 Hermes 助手。请用简洁中文回答，优先直接解决问题。

# ─── 日志 ───
LOG_LEVEL=INFO
EOF
```

设置权限（仅 root 可读，因为含密钥）：

```bash
chmod 600 /opt/yzj-adapter/.env
chown root:root /opt/yzj-adapter/.env
```

---

## 6. 创建 systemd 服务

### 6.1 创建 service 文件

```bash
cat > /etc/systemd/system/yzj-adapter.service <<'EOF'
[Unit]
Description=Hermes Yunzhijia Adapter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/yzj-adapter

# 从 .env 文件加载环境变量
EnvironmentFile=/opt/yzj-adapter/.env

ExecStart=/opt/yzj-adapter/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8080

# 自动重启
Restart=always
RestartSec=5
StartLimitBurst=5
StartLimitIntervalSec=60

# 日志输出到 journalctl
StandardOutput=journal
StandardError=journal
SyslogIdentifier=yzj-adapter

[Install]
WantedBy=multi-user.target
EOF
```

### 6.2 如果不想用 .env 文件

可以把 `EnvironmentFile` 行替换成多行 `Environment`：

```ini
Environment="YZJ_SEND_MSG_URL=https://www.yunzhijia.com/gateway/robot/webhook/send?yzjtype=12&yzjtoken=YOUR_YZJTOKEN"
Environment="YZJ_SECRET=YOUR_YZJ_SECRET"
Environment="HERMES_API_BASE=http://127.0.0.1:8642"
Environment="HERMES_API_KEY=YOUR_HERMES_API_KEY"
Environment="HERMES_MODEL=hermes-agent"
Environment="REQUEST_TIMEOUT=120"
Environment="SEND_TIMEOUT=15"
Environment="MAX_HISTORY=10"
Environment="ENABLE_WEBSOCKET=true"
Environment="WS_RECONNECT_SECONDS=5"
Environment="BOT_SYSTEM_PROMPT=你是接入云之家群组的 Hermes 助手。请用简洁中文回答，优先直接解决问题。"
Environment="LOG_LEVEL=INFO"
```

---

## 7. 启动服务

```bash
systemctl daemon-reload
systemctl enable yzj-adapter
systemctl start yzj-adapter
systemctl status yzj-adapter
```

期望输出：

```
● yzj-adapter.service - Hermes Yunzhijia Adapter
     Loaded: loaded (/etc/systemd/system/yzj-adapter.service; enabled)
     Active: active (running)
```

---

## 8. 验证部署

### 8.1 健康检查

```bash
curl -s http://127.0.0.1:8080/health
```

期望返回：

```json
{
  "status": "ok",
  "websocket_enabled": true,
  "websocket_url": "wss://www.yunzhijia.com/xuntong/websocket?yzjtoken=xxx",
  "hermes_api_base": "http://127.0.0.1:8642",
  "model": "hermes-agent"
}
```

### 8.2 检查路由注册

```bash
curl -s http://127.0.0.1:8080/openapi.json | python3 -m json.tool | grep yunzhijia
```

应包含 `/yunzhijia/webhook`。

### 8.3 模拟 webhook 请求

```bash
curl -X POST http://127.0.0.1:8080/yunzhijia/webhook \
  -H "Content-Type: application/json" \
  -d '{
    "robotId": "BOT-test",
    "robotName": "测试机器人",
    "operatorOpenid": "test-openid",
    "operatorName": "测试用户",
    "time": "1234567890",
    "msgId": "test-msg-001",
    "content": "你好",
    "type": 2
  }'
```

期望立即返回：

```json
{
  "success": true,
  "data": {
    "type": 2,
    "content": "已收到，正在思考中。"
  }
}
```

---

## 9. 日志管理

### 查看实时日志

```bash
journalctl -u yzj-adapter -f
```

### 查看最近 200 行

```bash
journalctl -u yzj-adapter -n 200 --no-pager
```

### 按时间过滤

```bash
journalctl -u yzj-adapter --since "2026-04-16 00:00:00" --until "2026-04-16 01:00:00"
```

### 只看错误

```bash
journalctl -u yzj-adapter -p err --no-pager
```

### 日志持久化

默认 journalctl 日志可能在重启后丢失。如需持久化：

```bash
mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald
```

---

## 10. 常用运维命令

```bash
# 启动
systemctl start yzj-adapter

# 停止
systemctl stop yzj-adapter

# 重启
systemctl restart yzj-adapter

# 查看状态
systemctl status yzj-adapter

# 修改 .env 后重载
systemctl restart yzj-adapter

# 修改 service 文件后重载
systemctl daemon-reload
systemctl restart yzj-adapter

# 更新 app.py 后重启
systemctl restart yzj-adapter

# 更新依赖后重启
cd /opt/yzj-adapter
source .venv/bin/activate
pip install -r requirements.txt
deactivate
systemctl restart yzj-adapter

# 开机自启
systemctl enable yzj-adapter

# 取消开机自启
systemctl disable yzj-adapter
```

---

## 11. 防火墙配置

### ufw

```bash
ufw allow 8080/tcp comment "yzj-adapter"
ufw reload
```

### iptables

```bash
iptables -A INPUT -p tcp --dport 8080 -j ACCEPT
```

> ⚠️ 生产环境建议通过 Nginx/Caddy 反向代理，不要直接暴露端口。

---

## 12. Nginx 反向代理（推荐）

### 12.1 安装 Nginx

```bash
apt install -y nginx
```

### 12.2 配置

创建 `/etc/nginx/sites-available/yzj-adapter`：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Hermes 慢查询需要较长超时
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }
}
```

### 12.3 启用

```bash
ln -s /etc/nginx/sites-available/yzj-adapter /etc/nginx/sites-enabled/
nginx -t
systemctl reload nginx
```

### 12.4 HTTPS（Let's Encrypt）

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

完成后云之家 webhook 地址改为：

```
https://your-domain.com/yunzhijia/webhook
```

---

## 13. 故障排查

### 服务启动失败

```bash
journalctl -u yzj-adapter -n 50 --no-pager
```

常见原因：
1. Python 虚拟环境路径不对
2. `.env` 文件不存在或格式有误
3. 端口被占用

### 端口被占用

```bash
ss -tlnp | grep 8080
```

解决：修改 systemd service 文件中的 `--port` 参数，或杀掉占用进程。

### Hermes 连接失败

```bash
curl -s http://127.0.0.1:8642/v1/models \
  -H "Authorization: Bearer YOUR_HERMES_API_KEY"
```

### websocket 连不上

```bash
journalctl -u yzj-adapter --no-pager | grep -i websocket
```

确认 `YZJ_SEND_MSG_URL` 格式正确，`yzjtoken` 有效。

### 签名校验失败

```bash
journalctl -u yzj-adapter --no-pager | grep -i "invalid sign"
```

确认 `YZJ_SECRET` 与云之家平台一致。

---

## 14. Docker vs 非 Docker 对比

| 项目 | Docker | systemd |
|---|---|---|
| 部署难度 | 低 | 中 |
| 依赖管理 | 镜像内置 | 需手动 venv |
| 日志查看 | `docker compose logs` | `journalctl` |
| 重启策略 | `restart: unless-stopped` | `Restart=always` |
| 资源隔离 | 容器级 | 进程级 |
| 更新方式 | `docker compose up --build` | 替换文件 + `systemctl restart` |
| 配置管理 | docker-compose.yml | `.env` + service 文件 |
| 适合场景 | 快速部署、测试 | 生产环境、精细控制 |

---

## 15. 完整部署检查清单

- [ ] Python 3.11+ 已安装
- [ ] 虚拟环境已创建，依赖已安装
- [ ] `app.py` 和 `requirements.txt` 已放入 `/opt/yzj-adapter/`
- [ ] `.env` 文件已配置所有必填变量
- [ ] `.env` 权限已设为 600
- [ ] systemd service 文件已创建
- [ ] `systemctl daemon-reload` 已执行
- [ ] `systemctl enable yzj-adapter` 已执行
- [ ] `systemctl start yzj-adapter` 已执行
- [ ] `curl http://127.0.0.1:8080/health` 返回 ok
- [ ] `/yunzhijia/webhook` 路由已注册
- [ ] 云之家 webhook 地址已配置为 `http://IP:8080/yunzhijia/webhook`
- [ ] 群内 `@机器人` 测试通过
- [ ] 防火墙已放行端口
- [ ] （可选）Nginx 反向代理已配置
- [ ] （可选）HTTPS 证书已配置

---

## 16. 从 Docker 迁移到 systemd

如果你之前用 Docker 部署，想切换到 systemd：

```bash
# 1. 停止 Docker 容器
cd /root/yzj-adapter
docker compose down

# 2. 复制文件到 /opt
mkdir -p /opt/yzj-adapter
cp /root/yzj-adapter/app.py /opt/yzj-adapter/
cp /root/yzj-adapter/requirements.txt /opt/yzj-adapter/

# 3. 创建虚拟环境 + 安装依赖
cd /opt/yzj-adapter
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
deactivate

# 4. 从 docker-compose.yml 提取环境变量写入 .env
# 手动创建 /opt/yzj-adapter/.env（参考第 5 节）

# 5. 创建 systemd 服务并启动（参考第 6~7 节）
```

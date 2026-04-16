# yzj-adapter — Hermes 云之家群机器人适配器

将 [Hermes Agent](https://hermes-agent.nousresearch.com/) 接入云之家群聊，让群成员通过 `@机器人` 与 Hermes 对话。

---

## 架构

```
┌──────────────┐
│  云之家群聊   │  用户 @大龙虾 你好
└──────┬───────┘
       │ 消息推送（双通道）
       ├─ webhook 回调 ──────────┐
       └─ websocket 推送 ────────┤
                                 ▼
                        ┌─────────────────┐
                        │   yzj-adapter   │
                        │   (FastAPI)     │
                        │                 │
                        │  • 签名校验     │
                        │  • 消息去重     │
                        │  • 会话管理     │
                        │  • @机器人清洗  │
                        └────────┬────────┘
                                 │ POST /v1/chat/completions
                                 ▼
                        ┌─────────────────┐
                        │  Hermes API     │
                        │  :8642          │
                        └────────┬────────┘
                                 │ 回复
                                 ▼
                        ┌─────────────────┐
                        │  sendMsgUrl     │
                        │  异步回发云之家  │
                        └─────────────────┘
```

### 设计要点

| 要点 | 说明 |
|---|---|
| **同步快返，异步慢处理** | webhook 立即返回"已收到"，Hermes 响应通过 sendMsgUrl 异步回发，避免云之家 3 秒超时 |
| **双入口** | 同时支持 webhook 回调和 websocket 推送，按 msgId 去重 |
| **协议转换** | 适配器负责验签、解析、会话映射、调用 Hermes、回发结果，Hermes 侧无需改动 |
| **websocket 解包** | 云之家 websocket 消息的真实 payload 在 `payload["msg"]` 内，适配器自动解包 |

---

## 文件结构

```
yzj-adapter/
├── app.py              # 主程序（FastAPI）
├── requirements.txt    # Python 依赖
├── Dockerfile          # Docker 镜像定义
├── docker-compose.yml  # Docker Compose 编排
└── README.md           # 本文件
```

---

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `YZJ_SEND_MSG_URL` | ✅ | — | 云之家主动发消息 URL（含 yzjtoken） |
| `YZJ_SECRET` | ✅ | — | 云之家签名密钥（HMAC-SHA1/SHA256） |
| `HERMES_API_BASE` | ✅ | `http://127.0.0.1:8642` | Hermes API Server 地址 |
| `HERMES_API_KEY` | ✅ | — | Hermes API Key（Bearer Token） |
| `HERMES_MODEL` | | `hermes-agent` | 模型名，可通过 GET /v1/models 查询 |
| `REQUEST_TIMEOUT` | | `20` | 调 Hermes 超时（秒），建议 120 |
| `SEND_TIMEOUT` | | `10` | 回发云之家超时（秒） |
| `MAX_HISTORY` | | `12` | 单会话保留历史轮数 |
| `ENABLE_WEBSOCKET` | | `true` | 是否启用 websocket |
| `WS_RECONNECT_SECONDS` | | `5` | websocket 断线重连间隔（秒） |
| `BOT_SYSTEM_PROMPT` | | 见下方 | 系统提示词 |
| `LOG_LEVEL` | | `INFO` | DEBUG / INFO / WARNING / ERROR |

默认系统提示词：

```
你是接入云之家群组的 Hermes 助手。请用简洁中文回答，优先直接解决问题。
```

---

## API 端点

### GET /health

```bash
curl http://127.0.0.1:8081/health
```

响应：

```json
{
  "status": "ok",
  "websocket_enabled": true,
  "websocket_url": "wss://www.yunzhijia.com/xuntong/websocket?yzjtoken=xxx",
  "hermes_api_base": "http://154.201.73.253:8642",
  "model": "hermes-agent"
}
```

### POST /yunzhijia/webhook

云之家 webhook 回调入口。

请求头：

| Header | 说明 |
|---|---|
| `sign` | 签名（配了 YZJ_SECRET 时必传） |
| `sessionid` | 会话 ID（可选） |

请求体：云之家消息 JSON，包含 `robotId`、`robotName`、`operatorOpenid`、`operatorName`、`time`、`msgId`、`content`、`type` 等字段。

立即响应：

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

## 消息流转

```
1. 用户在群里发：@大龙虾 你好
2. 云之家通过 webhook / websocket 推送到 yzj-adapter
3. yzj-adapter 立即返回 JSON 确认（webhook 路径）
4. 后台异步调用 Hermes API
5. Hermes 返回回答
6. yzj-adapter 通过 sendMsgUrl 回发到群里
```

---

## 云之家 websocket 消息结构

云之家 websocket 推送的真实消息体嵌套在 `msg` 字段内：

```json
{
  "msg": {
    "eid": "10109",
    "groupType": 2,
    "msgType": 2,
    "robotName": "大龙虾",
    "groupId": "69b0c026e4b0286ef85f3f47",
    "msgId": "69df5f0be4b0e2effbc00347",
    "robotId": "BOT-69b3e1b3e4b025b4019ef897",
    "type": 2,
    "content": "@大龙虾 你好",
    "operatorName": "用户A",
    "operatorOpenid": "xxx",
    "time": 1776246539306
  },
  "level": 0,
  "cmd": "directPush",
  "type": "robotMessage"
}
```

适配器的 `extract_ws_payload()` 会自动解包 `msg` 层，提取出真实业务 payload。

---

## 签名校验

适配器对 webhook 请求做签名校验：

1. 将 `robotId, robotName, operatorOpenid, operatorName, time, msgId, content` 用逗号拼接
2. 用 `YZJ_SECRET` 分别计算 HMAC-SHA1 和 HMAC-SHA256 的 Base64 签名
3. 与请求头 `sign` 比对，任一匹配即通过

> 云之家文档对签名算法存在歧义（文档写 SHA256，参考实现用 SHA1），适配器同时兼容两种。

---

## Python 依赖

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
httpx==0.27.2
websockets==13.1
cachetools==5.5.0
```

系统要求：Python 3.11+

---

## Docker 部署

### 1. 准备文件

```bash
mkdir -p /root/yzj-adapter && cd /root/yzj-adapter
# 放入 app.py、requirements.txt、Dockerfile、docker-compose.yml
```

### 2. Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

### 3. docker-compose.yml

```yaml
services:
  yzj-adapter:
    build: .
    container_name: yzj-adapter
    restart: unless-stopped
    ports:
      - "8081:8080"
    environment:
      # ── 云之家 ──
      YZJ_SEND_MSG_URL: "https://www.yunzhijia.com/gateway/robot/webhook/send?yzjtype=12&yzjtoken=YOUR_YZJTOKEN"
      YZJ_SECRET: "YOUR_YZJ_SECRET"

      # ── Hermes API ──
      HERMES_API_BASE: "http://154.201.73.253:8642"
      HERMES_API_KEY: "YOUR_HERMES_API_KEY"
      HERMES_MODEL: "hermes-agent"

      # ── 超时 ──
      REQUEST_TIMEOUT: "120"
      SEND_TIMEOUT: "15"

      # ── 会话 ──
      MAX_HISTORY: "10"

      # ── WebSocket ──
      ENABLE_WEBSOCKET: "true"
      WS_RECONNECT_SECONDS: "5"

      # ── 机器人 ──
      BOT_SYSTEM_PROMPT: "你是接入云之家群组的 Hermes 助手。请用简洁中文回答，优先直接解决问题。"

      # ── 日志 ──
      LOG_LEVEL: "INFO"
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "3"
```

> 修改上面 4 个占位符为真实值：`YOUR_YZJTOKEN`、`YOUR_YZJ_SECRET`、`YOUR_HERMES_API_KEY`。

### 4. 启动

```bash
docker compose up -d --build
```

### 5. 验证

```bash
# 健康检查
curl -s http://127.0.0.1:8081/health

# 路由注册
curl -s http://127.0.0.1:8081/openapi.json | python3 -m json.tool | grep yunzhijia
```

### 6. 常用命令

```bash
# 查看日志
docker compose logs -f yzj-adapter
docker compose logs --tail=120 yzj-adapter

# 重建
docker compose up -d --build

# 无缓存重建（路由丢失时用）
docker compose build --no-cache && docker compose up -d

# 停止
docker compose down
```

---

## 非 Docker 部署（systemd）

详见 [DEPLOY_SYSTEMD.md](./DEPLOY_SYSTEMD.md)。

简要步骤：

```bash
# 1. 安装 Python 3.11
apt update && apt install -y python3.11 python3.11-venv

# 2. 创建项目
mkdir -p /opt/yzj-adapter && cd /opt/yzj-adapter

# 3. 虚拟环境 + 依赖
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && deactivate

# 4. 配置环境变量
cp .env.example .env && vim .env

# 5. 创建 systemd 服务并启动
cp yzj-adapter.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now yzj-adapter
```

---

## Nginx HTTPS 反向代理（可选）

```bash
apt install -y nginx
```

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
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }
}
```

启用 + HTTPS：

```bash
ln -s /etc/nginx/sites-available/yzj-adapter /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Let's Encrypt
apt install -y certbot python3-certbot-nginx
certbot --nginx -d your-domain.com
```

完成后云之家 webhook 地址改为 `https://your-domain.com/yunzhijia/webhook`。

---

## 云之家侧配置

| 项目 | 说明 | 对应环境变量 |
|---|---|---|
| Webhook 回调地址 | `http://服务器IP:8081/yunzhijia/webhook` | — |
| sendMsgUrl | 云之家提供的主动发消息 URL | `YZJ_SEND_MSG_URL` |
| Secret | 云之家分配的签名密钥 | `YZJ_SECRET` |
| 机器人名称 | 如 "大龙虾" | — |
| robotId | 如 BOT-xxx | — |

---

## 日志关键信息

正常运行日志：

```text
yzj-adapter 启动中
WebSocket 连接地址: wss://...
云之家 WebSocket 已连接
原始 websocket 消息: {msg: {...}}
标准化 websocket payload: {msgId: ..., content: "...", type: 2}
收到 websocket 消息 msgId=69df...
HTTP Request: POST .../v1/chat/completions "HTTP/1.1 200 OK"
HTTP Request: POST .../gateway/robot/webhook/send?... "HTTP/1.1 200 OK"
异步回复成功 msgId=69df... source=websocket
```

---

## 常见问题

### webhook 404

```bash
curl -s http://127.0.0.1:8081/openapi.json | python3 -m json.tool | grep yunzhijia
```

没有路由则重建：`docker compose build --no-cache && docker compose up -d`

### Hermes 超时（httpx.ReadTimeout）

- 增大 `REQUEST_TIMEOUT` 到 120
- 减少 `MAX_HISTORY` 到 6~8
- 对天气/新闻类问题可接入专用 API 跳过 Hermes

### 端口冲突

改 docker-compose.yml 外部端口：`"8082:8080"`

### websocket 消息解析异常

适配器 `extract_ws_payload()` 已自动解包 `msg` 层。

### 签名校验失败（401）

确认 `YZJ_SECRET` 与云之家平台配置一致。适配器同时支持 SHA1 和 SHA256。

---

## 后续优化方向

- [ ] Redis 替代内存缓存（去重 + 会话）
- [ ] 天气/新闻查询走专用 API
- [ ] 消息类型扩展（图片、卡片、文件）
- [ ] 并发限流与任务队列
- [ ] Prometheus 监控
- [ ] Secret / API Key 定期轮换
- [ ] @机器人 前缀自动清洗
- [ ] 多群支持与群白名单

---

## Docker vs 非 Docker

| 项目 | Docker | systemd |
|---|---|---|
| 部署难度 | 低 | 中 |
| 依赖管理 | 镜像内置 | 手动 venv |
| 日志 | docker compose logs | journalctl |
| 重启策略 | restart: unless-stopped | Restart=always |
| 更新 | docker compose up --build | 替换文件 + systemctl restart |
| 隔离 | 容器级 | 进程级 |

---

## License

内部使用。

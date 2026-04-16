import os
import json
import hmac
import base64
import hashlib
import asyncio
import logging
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse, parse_qs

import httpx
import websockets
from fastapi import FastAPI, Request, Header
from fastapi.responses import JSONResponse
from cachetools import TTLCache

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("yzj-adapter")

app = FastAPI(title="Hermes Yunzhijia Adapter")

YZJ_SEND_MSG_URL = os.getenv("YZJ_SEND_MSG_URL", "").strip()
YZJ_SECRET = os.getenv("YZJ_SECRET", "").strip()
HERMES_API_BASE = os.getenv("HERMES_API_BASE", "http://127.0.0.1:8642").rstrip("/")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "").strip()
HERMES_MODEL = os.getenv("HERMES_MODEL", "hermes-agent")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "20"))
SEND_TIMEOUT = float(os.getenv("SEND_TIMEOUT", "10"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "12"))
BOT_SYSTEM_PROMPT = os.getenv(
    "BOT_SYSTEM_PROMPT",
    "你是接入云之家群组的 Hermes 助手。请用简洁中文回答，优先直接解决问题。"
)
ENABLE_WEBSOCKET = os.getenv("ENABLE_WEBSOCKET", "true").lower() == "true"
WS_RECONNECT_SECONDS = int(os.getenv("WS_RECONNECT_SECONDS", "5"))

dedup_cache = TTLCache(maxsize=10000, ttl=600)
session_cache: TTLCache = TTLCache(maxsize=5000, ttl=1800)


def build_signature_string(payload: Dict[str, Any]) -> str:
    fields = [
        str(payload.get("robotId", "")),
        str(payload.get("robotName", "")),
        str(payload.get("operatorOpenid", "")),
        str(payload.get("operatorName", "")),
        str(payload.get("time", "")),
        str(payload.get("msgId", "")),
        str(payload.get("content", "")),
    ]
    return ",".join(fields)


def sign_hmac(secret: str, message: str, algo: str = "sha1") -> str:
    digestmod = hashlib.sha1 if algo.lower() == "sha1" else hashlib.sha256
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), digestmod).digest()
    return base64.b64encode(digest).decode("utf-8")


def verify_sign(payload: Dict[str, Any], incoming_sign: Optional[str]) -> bool:
    if not YZJ_SECRET:
        return True
    if not incoming_sign:
        return False
    sign_str = build_signature_string(payload)
    expect_sha1 = sign_hmac(YZJ_SECRET, sign_str, "sha1")
    expect_sha256 = sign_hmac(YZJ_SECRET, sign_str, "sha256")
    return (
        hmac.compare_digest(incoming_sign, expect_sha1)
        or hmac.compare_digest(incoming_sign, expect_sha256)
    )


def derive_ws_url(send_msg_url: str) -> str:
    parsed = urlparse(send_msg_url)
    qs = parse_qs(parsed.query)
    yzjtoken = qs.get("yzjtoken", [""])[0]
    if not yzjtoken:
        raise ValueError("YZJ_SEND_MSG_URL 中缺少 yzjtoken")
    return f"wss://{parsed.netloc}/xuntong/websocket?yzjtoken={yzjtoken}"


def normalize_text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "我收到消息了，但内容为空。"
    return text[:4000]


def get_session_key(session_id: Optional[str], payload: Dict[str, Any]) -> str:
    robot_id = str(payload.get("robotId", "bot"))
    if session_id:
        return f"yzj:{robot_id}:{session_id}"
    return f"yzj:{robot_id}:{payload.get('operatorOpenid', 'user')}"


def get_history(session_key: str) -> List[Dict[str, str]]:
    return session_cache.get(session_key, [])


def append_history(session_key: str, role: str, content: str):
    history = session_cache.get(session_key, [])
    history.append({"role": role, "content": content})
    session_cache[session_key] = history[-MAX_HISTORY:]


def build_msg_id(payload: Dict[str, Any], source: str) -> str:
    msg_id = str(payload.get("msgId", "")).strip()
    if msg_id:
        return msg_id
    return f"{source}:{payload.get('time','')}:{payload.get('operatorOpenid','')}"


def try_mark_dedup(msg_id: str) -> bool:
    if msg_id in dedup_cache:
        return False
    dedup_cache[msg_id] = True
    return True


def extract_text_content(raw_content: Any) -> str:
    if raw_content is None:
        return ""
    if isinstance(raw_content, str):
        return raw_content.strip()
    if isinstance(raw_content, dict):
        for key in ("text", "content", "body", "value", "title"):
            val = raw_content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return json.dumps(raw_content, ensure_ascii=False)
    if isinstance(raw_content, list):
        parts = []
        for item in raw_content:
            t = extract_text_content(item)
            if t:
                parts.append(t)
        return " ".join(parts).strip()
    return str(raw_content).strip()


async def call_hermes(session_key: str, user_text: str) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {HERMES_API_KEY}",
    }
    messages = [{"role": "system", "content": BOT_SYSTEM_PROMPT}]
    messages.extend(get_history(session_key))
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": HERMES_MODEL,
        "messages": messages,
        "temperature": 0.3,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        resp = await client.post(
            f"{HERMES_API_BASE}/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()

    answer = data["choices"][0]["message"]["content"].strip()
    append_history(session_key, "user", user_text)
    append_history(session_key, "assistant", answer)
    return answer


async def send_yzj_text(content: str):
    payload = {"msgtype": 2, "content": normalize_text(content)}
    async with httpx.AsyncClient(timeout=SEND_TIMEOUT) as client:
        resp = await client.post(YZJ_SEND_MSG_URL, json=payload)
        resp.raise_for_status()
        return resp.text


def webhook_ok(content: str):
    return JSONResponse({
        "success": True,
        "data": {
            "type": 2,
            "content": normalize_text(content)
        }
    })


def extract_ws_payload(raw_json: Any) -> Optional[Dict[str, Any]]:
    logger.info("原始 websocket 消息: %s", raw_json)

    if not isinstance(raw_json, dict):
        return None

    payload = raw_json
    if isinstance(payload.get("data"), dict):
        payload = payload["data"]
    if isinstance(payload.get("msg"), dict):
        payload = payload["msg"]

    logger.info("标准化 websocket payload: %s", payload)
    return payload if isinstance(payload, dict) else None


async def process_message_async(payload: Dict[str, Any], session_id: Optional[str], source: str):
    msg_id = build_msg_id(payload, source)
    msg_type = payload.get("type")
    content = extract_text_content(payload.get("content"))

    try:
        if not content:
            logger.info("空内容消息已忽略 msgId=%s type=%s payload=%s", msg_id, msg_type, payload)
            await send_yzj_text("暂不支持该消息类型，请发送文本内容。")
            return

        session_key = get_session_key(session_id, payload)
        answer = await call_hermes(session_key, content)
        await send_yzj_text(answer)
        logger.info("异步回复成功 msgId=%s source=%s", msg_id, source)

    except httpx.ReadTimeout as e:
        logger.exception("调用 Hermes 超时 msgId=%s source=%s error=%s payload=%s", msg_id, source, e, payload)
        try:
            await send_yzj_text("这条问题处理超时了，通常是因为需要联网查询或推理较久，请稍后重试。")
        except Exception:
            logger.exception("发送超时提示也失败 msgId=%s", msg_id)

    except Exception as e:
        logger.exception("异步处理失败 msgId=%s source=%s error=%s payload=%s", msg_id, source, e, payload)
        try:
            await send_yzj_text("处理这条消息时发生错误，请稍后再试。")
        except Exception:
            logger.exception("发送失败兜底消息也失败 msgId=%s", msg_id)


@app.get("/health")
async def health():
    ws_url = None
    if YZJ_SEND_MSG_URL:
        try:
            ws_url = derive_ws_url(YZJ_SEND_MSG_URL)
        except Exception as e:
            ws_url = f"error: {e}"
    return {
        "status": "ok",
        "websocket_enabled": ENABLE_WEBSOCKET,
        "websocket_url": ws_url,
        "hermes_api_base": HERMES_API_BASE,
        "model": HERMES_MODEL,
    }


@app.post("/yunzhijia/webhook")
async def yunzhijia_webhook(
    request: Request,
    sign: Optional[str] = Header(default=None),
    sessionid: Optional[str] = Header(default=None),
):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"success": False, "message": "invalid json"})

    msg_id = build_msg_id(payload, "webhook")
    logger.info("收到云之家 webhook msgId=%s payload=%s", msg_id, payload)

    if not verify_sign(payload, sign):
        return JSONResponse(status_code=401, content={"success": False, "message": "invalid sign"})

    if not try_mark_dedup(msg_id):
        return webhook_ok("消息已接收。")

    asyncio.create_task(process_message_async(payload=payload, session_id=sessionid, source="webhook"))
    return webhook_ok("已收到，正在思考中。")


async def websocket_loop():
    if not ENABLE_WEBSOCKET:
        return
    if not YZJ_SEND_MSG_URL:
        return

    ws_url = derive_ws_url(YZJ_SEND_MSG_URL)
    logger.info("WebSocket 连接地址: %s", ws_url)

    while True:
        try:
            async with websockets.connect(
                ws_url,
                ping_interval=20,
                ping_timeout=20,
                max_size=4 * 1024 * 1024,
            ) as ws:
                logger.info("云之家 WebSocket 已连接")

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                    except Exception:
                        logger.warning("收到非 JSON websocket 消息")
                        continue

                    payload = extract_ws_payload(data)
                    if not isinstance(payload, dict):
                        continue

                    if "content" not in payload and "msgId" not in payload and "type" not in payload:
                        continue

                    msg_id = build_msg_id(payload, "websocket")
                    logger.info("收到 websocket 消息 msgId=%s", msg_id)

                    if not try_mark_dedup(msg_id):
                        logger.info("WebSocket 重复消息已忽略 msgId=%s", msg_id)
                        continue

                    asyncio.create_task(
                        process_message_async(
                            payload=payload,
                            session_id=payload.get("sessionId"),
                            source="websocket",
                        )
                    )

        except Exception as e:
            logger.exception("WebSocket 断开，%s 秒后重连: %s", WS_RECONNECT_SECONDS, e)
            await asyncio.sleep(WS_RECONNECT_SECONDS)


@app.on_event("startup")
async def startup_event():
    logger.info("yzj-adapter 启动中")
    if ENABLE_WEBSOCKET:
        asyncio.create_task(websocket_loop())

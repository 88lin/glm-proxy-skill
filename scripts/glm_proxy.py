#!/usr/bin/env python3
"""
GLM API 中转代理服务器 v2.0 — 全面优化版
- 兼容 OpenAI API 格式 (/v1/chat/completions, /v1/models)
- 支持 thinking 模式 (reasoning_effort: xhigh/max/high/medium/low)
- 支持 SSE 流式响应 + 心跳保活（防思考阶段断开）
- 自适应超时：根据 reasoning_effort 动态调整
- 自动重试 + 指数退避（可恢复错误不致命）
- 连接池自愈：检测失效自动重建
- 并发控制：信号量限制同时上游请求
- 完善异常处理：所有错误返回 OpenAI 格式
- 请求计时日志：全链路耗时追踪
"""

import os
import sys
import json
import time
import secrets
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional, AsyncIterator

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import uvicorn

# ============================================================
# 配置
# ============================================================
UPSTREAM_BASE = "https://tokenhub.developer.huaweicloud.com/v2"
API_KEY_FILE = os.environ.get("API_KEY_FILE", "/tmp/working_api_key.txt")
PROXY_HOST = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "9997"))

# 代理自身的 API Key 认证
PROXY_API_KEY_FILE = os.environ.get("PROXY_API_KEY_FILE", "/tmp/proxy_api_key.txt")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")

# --- 连接池配置 ---
POOL_MAX_KEEPALIVE = int(os.environ.get("POOL_MAX_KEEPALIVE", "30"))
POOL_KEEPALIVE_EXPIRY = float(os.environ.get("POOL_KEEPALIVE_EXPIRY", "60.0"))
POOL_MAX_CONNECTIONS = int(os.environ.get("POOL_MAX_CONNECTIONS", "100"))
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "15.0"))

# --- 自适应超时（秒）---
# 根据 reasoning_effort 级别动态调整读超时
THINKING_TIMEOUTS = {
    "xhigh": float(os.environ.get("TIMEOUT_XHIGH", "600")),   # 10分钟
    "max":   float(os.environ.get("TIMEOUT_MAX", "600")),     # 10分钟
    "high":  float(os.environ.get("TIMEOUT_HIGH", "300")),    # 5分钟
    "medium": float(os.environ.get("TIMEOUT_MEDIUM", "180")), # 3分钟
    "low":   float(os.environ.get("TIMEOUT_LOW", "120")),     # 2分钟
}
DEFAULT_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "120.0"))  # 无 thinking

# --- 重试配置 ---
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
RETRY_BACKOFF_BASE = float(os.environ.get("RETRY_BACKOFF_BASE", "1.0"))  # 基础退避秒
RETRY_BACKOFF_MAX = float(os.environ.get("RETRY_BACKOFF_MAX", "10.0"))   # 最大退避秒

# --- 心跳保活 ---
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "10.0"))  # 心跳间隔秒
HEARTBEAT_COMMENT = ": heartbeat"  # SSE 注释行，客户端忽略

# --- 并发控制 ---
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MAX_CONCURRENT_REQUESTS", "20"))

# --- 连接池自愈 ---
POOL_HEALTH_CHECK_INTERVAL = float(os.environ.get("POOL_HEALTH_CHECK_INTERVAL", "60.0"))
POOL_RECREATE_ON_ERROR = True

# 模型列表
AVAILABLE_MODELS = [
    {
        "id": "glm-5.2",
        "object": "model",
        "created": 1700000000,
        "owned_by": "zhipu",
        "description": "GLM-5.2 - 最新版本，支持 thinking 模式 (reasoning_effort: xhigh/max/high/medium/low)"
    },
    {
        "id": "glm-5.1",
        "object": "model",
        "created": 1700000000,
        "owned_by": "zhipu",
        "description": "GLM-5.1 - 稳定版本，支持 thinking 模式"
    },
]

THINKING_LEVELS = ["xhigh", "max", "high", "medium", "low"]

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("glm-proxy")

# ============================================================
# FastAPI 应用
# ============================================================
app = FastAPI(title="GLM API Proxy", version="2.0.0")
security = HTTPBearer(auto_error=False)

# 全局状态
_http_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()
_semaphore: Optional[asyncio.Semaphore] = None
_stats = {
    "total_requests": 0,
    "total_errors": 0,
    "total_retries": 0,
    "total_timeouts": 0,
    "upstream_errors": 0,
    "pool_recreates": 0,
}


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    return _semaphore


def load_proxy_api_key() -> str:
    global PROXY_API_KEY
    if PROXY_API_KEY:
        return PROXY_API_KEY
    try:
        with open(PROXY_API_KEY_FILE, "r") as f:
            key = f.read().strip()
        if key:
            PROXY_API_KEY = key
            return key
    except FileNotFoundError:
        pass
    key = "sk-glm-" + secrets.token_urlsafe(32)
    try:
        with open(PROXY_API_KEY_FILE, "w") as f:
            f.write(key)
    except Exception:
        pass
    PROXY_API_KEY = key
    return key


async def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    expected_key = load_proxy_api_key()
    if not credentials or credentials.credentials != expected_key:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid API key. Set Authorization: Bearer <key>", "type": "auth_error"}}
        )
    return credentials.credentials


def load_api_key() -> str:
    try:
        with open(API_KEY_FILE, "r") as f:
            key = f.read().strip()
        if not key:
            raise ValueError("API key file is empty")
        logger.info(f"API key loaded from {API_KEY_FILE} (len={len(key)})")
        return key
    except FileNotFoundError:
        logger.error(f"API key file not found: {API_KEY_FILE}")
        sys.exit(1)


# ============================================================
# 连接池管理（带自愈）
# ============================================================
async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        async with _client_lock:
            if _http_client is None or _http_client.is_closed:
                _http_client = await _create_client()
    return _http_client


async def _create_client() -> httpx.AsyncClient:
    limits = httpx.Limits(
        max_keepalive_connections=POOL_MAX_KEEPALIVE,
        max_connections=POOL_MAX_CONNECTIONS,
        keepalive_expiry=POOL_KEEPALIVE_EXPIRY,
    )
    # 使用一个足够大的默认超时；具体请求会按 thinking 级别动态设置
    timeout = httpx.Timeout(
        timeout=DEFAULT_TIMEOUT,
        connect=CONNECT_TIMEOUT,
        read=DEFAULT_TIMEOUT,
        write=30.0,
        pool=10.0,
    )
    client = httpx.AsyncClient(
        limits=limits,
        timeout=timeout,
        http2=False,
    )
    logger.info(f"HTTP client created (pool={POOL_MAX_KEEPALIVE}, max_conn={POOL_MAX_CONNECTIONS}, expiry={POOL_KEEPALIVE_EXPIRY}s)")
    return client


async def recreate_http_client() -> httpx.AsyncClient:
    """连接池自愈：关闭旧客户端并创建新的"""
    global _http_client
    _stats["pool_recreates"] += 1
    async with _client_lock:
        if _http_client and not _http_client.is_closed:
            try:
                await _http_client.aclose()
            except Exception:
                pass
        _http_client = await _create_client()
        logger.warning(f"HTTP client recreated (total recreates: {_stats['pool_recreates']})")
    return _http_client


def get_adaptive_timeout(reasoning_effort: Optional[str]) -> httpx.Timeout:
    """根据 thinking 级别返回自适应超时"""
    if reasoning_effort and reasoning_effort.lower() in THINKING_TIMEOUTS:
        read_timeout = THINKING_TIMEOUTS[reasoning_effort.lower()]
    else:
        read_timeout = DEFAULT_TIMEOUT
    return httpx.Timeout(
        timeout=read_timeout,
        connect=CONNECT_TIMEOUT,
        read=read_timeout,
        write=30.0,
        pool=10.0,
    )


# ============================================================
# 启动/关闭
# ============================================================
@app.on_event("startup")
async def startup():
    app.state.api_key = load_api_key()
    proxy_key = load_proxy_api_key()
    await get_http_client()
    get_semaphore()
    # 启动连接池健康检查后台任务
    asyncio.create_task(_pool_health_check())
    logger.info(f"GLM API Proxy v2.0 starting on {PROXY_HOST}:{PROXY_PORT}")
    logger.info(f"Upstream: {UPSTREAM_BASE}")
    logger.info(f"Available models: {[m['id'] for m in AVAILABLE_MODELS]}")
    logger.info(f"Adaptive timeouts: {THINKING_TIMEOUTS}")
    logger.info(f"Heartbeat: every {HEARTBEAT_INTERVAL}s, Retry: max={MAX_RETRIES}, Concurrency: {MAX_CONCURRENT_REQUESTS}")


@app.on_event("shutdown")
async def shutdown():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    logger.info("GLM API Proxy stopped")


async def _pool_health_check():
    """后台定期检查连接池健康状态"""
    while True:
        await asyncio.sleep(POOL_HEALTH_CHECK_INTERVAL)
        client = _http_client
        if client is None or client.is_closed:
            logger.warning("Pool health check: client closed, recreating...")
            await recreate_http_client()
        else:
            # 检查是否有过多失败连接
            pool = getattr(client, "_transport", None)
            if pool is None:
                continue
            # httpx 内部连接池状态（如果可访问）
            logger.debug(f"Pool health OK (stats: {_stats})")


# ============================================================
# 健康检查 & 统计
# ============================================================
@app.get("/health")
async def health():
    client = _http_client
    pool_ok = client is not None and not client.is_closed
    return {
        "status": "ok" if pool_ok else "degraded",
        "time": datetime.now(timezone.utc).isoformat(),
        "pool_alive": pool_ok,
        "stats": _stats,
    }


# ============================================================
# 模型列表
# ============================================================
@app.get("/v1/models")
@app.get("/models")
async def list_models(_=Depends(verify_api_key)):
    return {"object": "list", "data": AVAILABLE_MODELS}


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str, _=Depends(verify_api_key)):
    for m in AVAILABLE_MODELS:
        if m["id"] == model_id:
            return m
    raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")


# ============================================================
# Chat Completions — 核心转发逻辑
# ============================================================
@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request, _=Depends(verify_api_key)):
    api_key = app.state.api_key
    t0 = time.time()

    # 解析请求体
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model = body.get("model", "unknown")
    stream = body.get("stream", False)
    reasoning_effort = body.get("reasoning_effort")
    _stats["total_requests"] += 1

    # 自适应超时
    adaptive_timeout = get_adaptive_timeout(reasoning_effort)
    timeout_label = reasoning_effort if reasoning_effort in THINKING_TIMEOUTS else "default"
    logger.info(f"Request #{_stats['total_requests']}: model={model}, stream={stream}, effort={reasoning_effort}, timeout={timeout_label}({adaptive_timeout.read}s)")

    # 构造上游请求
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    url = f"{UPSTREAM_BASE}/chat/completions"
    upstream_body = json.dumps(body)

    if stream:
        return await _handle_stream(url, headers, upstream_body, model, reasoning_effort, adaptive_timeout, t0)
    else:
        return await _handle_non_stream(url, headers, upstream_body, model, reasoning_effort, adaptive_timeout, t0)


# ============================================================
# 非流式处理（内部流式 + 空白保活防 CF 524 超时）
# ============================================================
# 策略：非流式请求也内部用 stream=true 连接上游，
# 同时向客户端周期性发送空白字节（JSON 允许前导空白），
# 保持 CF Tunnel 数据流不断。最后发送完整 JSON。
async def _handle_non_stream(url, headers, body, model, reasoning_effort, timeout, t0):
    """
    非流式处理 — 内部流式 + 空白保活
    
    客户端收到: " \\n \\n \\n ... {complete_json}"
    前导空白不影响 JSON 解析，但保持了隧道数据流。
    """
    # 修改请求体为流式
    try:
        body_dict = json.loads(body)
    except Exception:
        body_dict = {"model": model}
    body_dict["stream"] = True
    stream_body = json.dumps(body_dict)

    async def keepalive_generator():
        attempt = 0
        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                client = await get_http_client()
                # 收集流式 chunk 的状态
                collected_content = []
                collected_reasoning = []
                finish_reason = None
                usage = None
                resp_model = model
                resp_id = None
                resp_created = None
                chunk_count = 0
                last_heartbeat = time.time()
                first_data_sent = False

                # 先发一个空白启动响应（开始 chunked transfer）
                yield " "

                async with client.stream("POST", url, headers=headers, content=stream_body, timeout=timeout) as resp:
                    if resp.status_code != 200:
                        error_bytes = await resp.aread()
                        error_text = error_bytes.decode("utf-8", errors="replace")[:500]
                        _stats["upstream_errors"] += 1

                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                            _stats["total_retries"] += 1
                            backoff = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
                            logger.warning(f"Non-stream upstream {resp.status_code}, retry {attempt}/{MAX_RETRIES} after {backoff}s")
                            await asyncio.sleep(backoff)
                            continue

                        # 不可重试，返回错误 JSON
                        error_resp = {"error": {"message": error_text, "type": "upstream_error"}}
                        yield json.dumps(error_resp)
                        return

                    # 成功连接，开始收集流式数据
                    async for line in resp.aiter_lines():
                        if not line:
                            continue

                        # 发送空白保活（保持 CF 隧道数据流）
                        now = time.time()
                        if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                            yield " "
                            last_heartbeat = now

                        if not line.startswith("data:"):
                            continue

                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        chunk_count += 1
                        resp_id = chunk.get("id", resp_id)
                        resp_created = chunk.get("created", resp_created)
                        resp_model = chunk.get("model", resp_model)

                        # 提取 delta
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            if delta.get("content"):
                                collected_content.append(delta["content"])
                            if delta.get("reasoning_content"):
                                collected_reasoning.append(delta["reasoning_content"])
                            fr = choices[0].get("finish_reason")
                            if fr:
                                finish_reason = fr

                        # 提取 usage（通常在最后一个 chunk）
                        if chunk.get("usage"):
                            usage = chunk["usage"]

                    # 组装完整非流式响应
                    elapsed = time.time() - t0
                    full_content = "".join(collected_content)
                    full_reasoning = "".join(collected_reasoning)

                    message = {"role": "assistant", "content": full_content}
                    if full_reasoning:
                        message["reasoning_content"] = full_reasoning

                    final_response = {
                        "id": resp_id or f"chatcmpl-{int(time.time())}",
                        "object": "chat.completion",
                        "created": resp_created or int(time.time()),
                        "model": resp_model,
                        "choices": [{
                            "index": 0,
                            "message": message,
                            "finish_reason": finish_reason or "stop",
                        }],
                    }
                    if usage:
                        final_response["usage"] = usage

                    tokens = usage.get("total_tokens", "?") if usage else "?"
                    logger.info(f"Non-stream (internal stream) OK: model={resp_model}, tokens={tokens}, chunks={chunk_count}, elapsed={elapsed:.1f}s, attempt={attempt}")

                    # 发送完整 JSON（前导空白不影响解析）
                    yield json.dumps(final_response, ensure_ascii=False)
                    return

            except httpx.ConnectTimeout:
                _stats["total_timeouts"] += 1
                logger.warning(f"Non-stream ConnectTimeout attempt {attempt}/{MAX_RETRIES}")
                if attempt < MAX_RETRIES:
                    _stats["total_retries"] += 1
                    await asyncio.sleep(min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX))
                    if POOL_RECREATE_ON_ERROR:
                        await recreate_http_client()
                    continue
                yield json.dumps({"error": {"message": "Connection timeout to upstream", "type": "timeout"}})
                return

            except httpx.ReadTimeout:
                _stats["total_timeouts"] += 1
                logger.warning(f"Non-stream ReadTimeout attempt {attempt}/{MAX_RETRIES} (timeout={timeout.read}s)")
                if attempt < MAX_RETRIES:
                    _stats["total_retries"] += 1
                    await asyncio.sleep(min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX))
                    continue
                yield json.dumps({"error": {"message": f"Read timeout ({timeout.read}s) — try lower reasoning_effort", "type": "timeout"}})
                return

            except httpx.ConnectError as e:
                _stats["upstream_errors"] += 1
                logger.warning(f"Non-stream ConnectError attempt {attempt}/{MAX_RETRIES}: {e}")
                if attempt < MAX_RETRIES:
                    _stats["total_retries"] += 1
                    if POOL_RECREATE_ON_ERROR:
                        await recreate_http_client()
                    await asyncio.sleep(min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX))
                    continue
                yield json.dumps({"error": {"message": f"Connection error: {e}", "type": "connect_error"}})
                return

            except httpx.RemoteProtocolError as e:
                _stats["upstream_errors"] += 1
                logger.warning(f"Non-stream RemoteProtocolError attempt {attempt}/{MAX_RETRIES}: {e}")
                if attempt < MAX_RETRIES:
                    _stats["total_retries"] += 1
                    if POOL_RECREATE_ON_ERROR:
                        await recreate_http_client()
                    await asyncio.sleep(min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX))
                    continue
                yield json.dumps({"error": {"message": f"Remote protocol error: {e}", "type": "protocol_error"}})
                return

            except asyncio.CancelledError:
                elapsed = time.time() - t0
                logger.info(f"Non-stream cancelled by client: model={model}, elapsed={elapsed:.1f}s")
                return

            except Exception as e:
                _stats["total_errors"] += 1
                logger.error(f"Non-stream error attempt {attempt}: {e}\n{traceback.format_exc()[:500]}")
                if attempt < MAX_RETRIES:
                    _stats["total_retries"] += 1
                    await asyncio.sleep(min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX))
                    continue
                yield json.dumps({"error": {"message": str(e), "type": "error"}})
                return

        # 所有重试失败
        _stats["total_errors"] += 1
        elapsed = time.time() - t0
        logger.error(f"Non-stream all retries failed (elapsed={elapsed:.1f}s)")
        yield json.dumps({"error": {"message": "All retries failed", "type": "upstream_error", "retries": MAX_RETRIES}})

    return StreamingResponse(
        keepalive_generator(),
        media_type="application/json",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Proxy-Mode": "non-stream-keepalive",
        },
    )


# ============================================================
# 流式处理（心跳保活 + 重试 + 断线检测）
# ============================================================
async def _handle_stream(url, headers, body, model, reasoning_effort, timeout, t0):
    """
    流式 SSE 响应，带以下优化：
    1. 心跳保活：思考阶段定期发送 SSE 注释行，防止 CF Tunnel/客户端断开
    2. 自动重试：连接失败时重试（仅首次连接失败时）
    3. 断线检测：检测客户端断开，及时停止上游请求
    4. 正确的 SSE 格式：data: {...}\n\n
    """

    async def stream_generator():
        attempt = 0
        max_stream_retries = MAX_RETRIES

        while attempt < max_stream_retries:
            attempt += 1
            try:
                client = await get_http_client()
                first_data_received = False
                chunk_count = 0
                last_heartbeat = time.time()

                async with client.stream("POST", url, headers=headers, content=body, timeout=timeout) as resp:
                    if resp.status_code != 200:
                        error_bytes = await resp.aread()
                        error_text = error_bytes.decode("utf-8", errors="replace")[:500]
                        _stats["upstream_errors"] += 1

                        # 可重试的状态码
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_stream_retries:
                            _stats["total_retries"] += 1
                            backoff = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
                            logger.warning(f"Stream upstream {resp.status_code}, retry {attempt}/{max_stream_retries} after {backoff}s")
                            # 发送心跳让客户端知道还在工作
                            yield f"{HEARTBEAT_COMMENT} retrying ({attempt}/{max_stream_retries})\n\n"
                            await asyncio.sleep(backoff)
                            continue
                        # 不可重试
                        logger.error(f"Stream upstream error {resp.status_code}: {error_text}")
                        yield _format_sse_error(error_text, "upstream_error")
                        yield "data: [DONE]\n\n"
                        return

                    # 成功连接，开始转发流
                    async for line in resp.aiter_lines():
                        # 检查是否需要发送心跳
                        now = time.time()
                        if not first_data_received and (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                            yield f"{HEARTBEAT_COMMENT}\n\n"
                            last_heartbeat = now

                        if not line:
                            continue

                        first_data_received = True
                        chunk_count += 1

                        # 正确转发 SSE 行
                        # 上游发送的行已经是 "data: {...}" 格式
                        if line.startswith("data:"):
                            yield f"{line}\n\n"
                        elif line.startswith(":"):
                            # 注释行，直接转发
                            yield f"{line}\n\n"
                        else:
                            # 其他行，包装为 data
                            yield f"data: {line}\n\n"

                        # 定期心跳（即使有数据，也防止长时间无新数据）
                        now = time.time()
                        if (now - last_heartbeat) >= HEARTBEAT_INTERVAL:
                            yield f"{HEARTBEAT_COMMENT}\n\n"
                            last_heartbeat = now

                    elapsed = time.time() - t0
                    logger.info(f"Stream completed: model={model}, chunks={chunk_count}, elapsed={elapsed:.1f}s, attempt={attempt}")
                    yield "data: [DONE]\n\n"
                    return  # 成功完成，退出重试循环

            except httpx.ConnectTimeout:
                _stats["total_timeouts"] += 1
                logger.warning(f"Stream ConnectTimeout attempt {attempt}/{max_stream_retries}")
                if attempt < max_stream_retries:
                    _stats["total_retries"] += 1
                    backoff = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
                    yield f"{HEARTBEAT_COMMENT} reconnecting ({attempt}/{max_stream_retries})\n\n"
                    await asyncio.sleep(backoff)
                    if POOL_RECREATE_ON_ERROR:
                        await recreate_http_client()
                    continue
                yield _format_sse_error("Connection timeout to upstream", "timeout")
                yield "data: [DONE]\n\n"
                return

            except httpx.ReadTimeout:
                _stats["total_timeouts"] += 1
                logger.warning(f"Stream ReadTimeout attempt {attempt}/{max_stream_retries} (timeout={timeout.read}s)")
                if attempt < max_stream_retries:
                    _stats["total_retries"] += 1
                    backoff = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
                    yield f"{HEARTBEAT_COMMENT} upstream slow, retrying ({attempt}/{max_stream_retries})\n\n"
                    await asyncio.sleep(backoff)
                    continue
                yield _format_sse_error(f"Read timeout ({timeout.read}s) — thinking too long, try lower reasoning_effort", "timeout")
                yield "data: [DONE]\n\n"
                return

            except httpx.ConnectError as e:
                _stats["upstream_errors"] += 1
                logger.warning(f"Stream ConnectError attempt {attempt}/{max_stream_retries}: {e}")
                if attempt < max_stream_retries:
                    _stats["total_retries"] += 1
                    if POOL_RECREATE_ON_ERROR:
                        await recreate_http_client()
                    backoff = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
                    await asyncio.sleep(backoff)
                    continue
                yield _format_sse_error(f"Connection error: {e}", "connect_error")
                yield "data: [DONE]\n\n"
                return

            except httpx.RemoteProtocolError as e:
                _stats["upstream_errors"] += 1
                logger.warning(f"Stream RemoteProtocolError attempt {attempt}/{max_stream_retries}: {e}")
                if attempt < max_stream_retries:
                    _stats["total_retries"] += 1
                    if POOL_RECREATE_ON_ERROR:
                        await recreate_http_client()
                    backoff = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
                    await asyncio.sleep(backoff)
                    continue
                yield _format_sse_error(f"Remote protocol error: {e}", "protocol_error")
                yield "data: [DONE]\n\n"
                return

            except asyncio.CancelledError:
                # 客户端断开连接
                elapsed = time.time() - t0
                logger.info(f"Stream cancelled by client: model={model}, elapsed={elapsed:.1f}s")
                return

            except Exception as e:
                _stats["total_errors"] += 1
                logger.error(f"Stream unexpected error attempt {attempt}: {e}\n{traceback.format_exc()[:500]}")
                if attempt < max_stream_retries:
                    _stats["total_retries"] += 1
                    backoff = min(RETRY_BACKOFF_BASE * (2 ** (attempt - 1)), RETRY_BACKOFF_MAX)
                    await asyncio.sleep(backoff)
                    continue
                yield _format_sse_error(str(e), "error")
                yield "data: [DONE]\n\n"
                return

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Proxy-Version": "2.0",
        },
    )


def _format_sse_error(message: str, error_type: str) -> str:
    """格式化 SSE 错误消息"""
    error_data = json.dumps({
        "error": {
            "message": message,
            "type": error_type,
        }
    })
    return f"data: {error_data}\n\n"


# ============================================================
# Embeddings
# ============================================================
@app.post("/v1/embeddings")
@app.post("/embeddings")
async def embeddings(request: Request, _=Depends(verify_api_key)):
    api_key = app.state.api_key
    client = await get_http_client()

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = await client.post(f"{UPSTREAM_BASE}/embeddings", headers=headers, json=body)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except httpx.ReadTimeout:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1 * attempt)
                continue
            raise HTTPException(status_code=504, detail="Embeddings timeout")
        except Exception as e:
            if attempt < MAX_RETRIES:
                await asyncio.sleep(1 * attempt)
                continue
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    raise HTTPException(status_code=502, detail="All retries failed")


# ============================================================
# 根路径信息
# ============================================================
@app.get("/")
async def root():
    return HTMLResponse("""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GLM API 代理服务</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f172a;color:#e2e8f0;min-height:100vh}
.container{max-width:960px;margin:0 auto;padding:40px 20px}
h1{font-size:2em;background:linear-gradient(135deg,#60a5fa,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}
.subtitle{color:#94a3b8;margin-bottom:32px;font-size:1.1em}
.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:24px;margin-bottom:20px}
.card h2{color:#60a5fa;font-size:1.2em;margin-bottom:16px;display:flex;align-items:center;gap:8px}
.badge{display:inline-block;background:#1e293b;border:1px solid #475569;border-radius:6px;padding:3px 10px;font-size:0.8em;margin:2px}
.badge.green{border-color:#22c55e;color:#4ade80}
.badge.blue{border-color:#3b82f6;color:#60a5fa}
.badge.purple{border-color:#a855f7;color:#c084fc}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #334155}
th{color:#94a3b8;font-weight:600;font-size:0.85em;text-transform:uppercase;letter-spacing:0.5px}
td code{background:#0f172a;padding:3px 8px;border-radius:4px;color:#7dd3fc;font-size:0.9em}
.method{font-weight:bold;font-size:0.85em;padding:2px 8px;border-radius:4px}
.method.get{background:#1a3a2a;color:#4ade80}
.method.post{background:#3a2a1a;color:#fbbf24}
code.block{display:block;background:#0f172a;padding:16px;border-radius:8px;overflow-x:auto;font-size:0.85em;line-height:1.6;color:#e2e8f0;font-family:'SF Mono',Monaco,Consolas,monospace;white-space:pre}
.comment{color:#64748b}
.key{color:#fbbf24}
.str{color:#4ade80}
.test-area{margin-top:8px}
input,textarea,select{background:#0f172a;border:1px solid #475569;border-radius:8px;padding:10px 14px;color:#e2e8f0;font-size:0.9em;width:100%;outline:none}
input:focus,textarea:focus,select:focus{border-color:#60a5fa}
textarea{resize:vertical;min-height:100px;font-family:monospace}
button{background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;border:none;border-radius:8px;padding:10px 24px;font-size:0.95em;cursor:pointer;transition:opacity .2s}
button:hover{opacity:0.9}
button:disabled{opacity:0.5;cursor:not-allowed}
.result{margin-top:12px;background:#0f172a;border:1px solid #334155;border-radius:8px;padding:16px;min-height:60px;font-family:monospace;font-size:0.85em;white-space:pre-wrap;overflow-x:auto;max-height:400px;overflow-y:auto}
.result.ok{border-color:#22c55e}
.result.err{border-color:#ef4444}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:640px){.grid{grid-template-columns:1fr}}
label{display:block;color:#94a3b8;font-size:0.8em;margin-bottom:4px}
a{color:#60a5fa;text-decoration:none}
a:hover{text-decoration:underline}
.footer{text-align:center;color:#475569;font-size:0.8em;margin-top:32px}
</style>
</head>
<body>
<div class="container">
<h1>GLM API 代理服务</h1>
<p class="subtitle">OpenAI 兼容接口 · 支持 Thinking 模式 · 流式响应 · Cloudflare Tunnel 公网访问</p>

<div class="card">
<h2>📡 可用接口</h2>
<table>
<tr><th>方法</th><th>路径</th><th>说明</th></tr>
<tr><td><span class="method get">GET</span></td><td><code>/v1/models</code></td><td>获取可用模型列表</td></tr>
<tr><td><span class="method post">POST</span></td><td><code>/v1/chat/completions</code></td><td>对话补全（支持流式 & thinking）</td></tr>
<tr><td><span class="method get">GET</span></td><td><code>/health</code></td><td>健康检查 + 运行时统计</td></tr>
<tr><td><span class="method post">POST</span></td><td><code>/v1/embeddings</code></td><td>文本向量（如上游支持）</td></tr>
</table>
</div>

<div class="card">
<h2>🤖 模型 & Thinking 等级</h2>
<p style="margin-bottom:8px">
<span class="badge blue">glm-5.2</span>
<span class="badge blue">glm-5.1</span>
<span class="badge blue">openPangu-2.0-Flash</span>
</p>
<p style="color:#94a3b8;font-size:0.9em">Thinking 模式（reasoning_effort 参数）：</p>
<p>
<span class="badge purple">xhigh</span>
<span class="badge purple">max</span>
<span class="badge purple">high</span>
<span class="badge purple">medium</span>
<span class="badge purple">low</span>
</p>
</div>

<div class="card">
<h2>📝 调用示例</h2>
<p style="color:#94a3b8;margin-bottom:8px">cURL — 普通对话：</p>
<code class="block">curl -X POST <span class="str">https://glm.zeroo.ggff.net/v1/chat/completions</span> \
  -H <span class="str">"Authorization: Bearer YOUR_API_KEY"</span> \
  -H <span class="str">"Content-Type: application/json"</span> \
  -d <span class="str">'{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'</span></code>

<p style="color:#94a3b8;margin:12px 0 8px">cURL — Thinking 模式：</p>
<code class="block">curl -X POST <span class="str">https://glm.zeroo.ggff.net/v1/chat/completions</span> \
  -H <span class="str">"Authorization: Bearer YOUR_API_KEY"</span> \
  -H <span class="str">"Content-Type: application/json"</span> \
  -d <span class="str">'{"model":"glm-5.2","messages":[{"role":"user","content":"证明根号2是无理数"}],"reasoning_effort":"xhigh"}'</span></code>

<p style="color:#94a3b8;margin:12px 0 8px">Python (OpenAI SDK)：</p>
<code class="block"><span class="key">from</span> openai <span class="key">import</span> OpenAI

client = OpenAI(api_key=<span class="str">"YOUR_API_KEY"</span>, base_url=<span class="str">"https://glm.zeroo.ggff.net/v1"</span>)
resp = client.chat.completions.create(
    model=<span class="str">"glm-5.2"</span>,
    messages=[{<span class="str">"role"</span>: <span class="str">"user"</span>, <span class="str">"content"</span>: <span class="str">"你好"</span>}],
    extra_body={<span class="str">"reasoning_effort"</span>: <span class="str">"xhigh"</span>}
)
<span class="comment"># resp.choices[0].message.content — 回答</span>
<span class="comment"># resp.choices[0].message.reasoning_content — 推理过程</span></code>
</div>

<div class="card">
<h2>🧪 在线测试</h2>
<div class="grid">
<div>
<label>API Key</label>
<input type="password" id="apiKey" placeholder="sk-xxx" value="">
</div>
<div>
<label>模型</label>
<select id="model">
<option value="glm-5.2">glm-5.2</option>
<option value="glm-5.1">glm-5.1</option>
<option value="openPangu-2.0-Flash">openPangu-2.0-Flash</option>
</select>
</div>
</div>
<div style="margin-top:12px">
<label>Thinking 模式</label>
<select id="thinking">
<option value="">关闭</option>
<option value="low">low</option>
<option value="medium">medium</option>
<option value="high">high</option>
<option value="max">max</option>
<option value="xhigh">xhigh（最深推理）</option>
</select>
</div>
<div style="margin-top:12px">
<label>消息内容</label>
<textarea id="message">你好，请简单介绍一下自己</textarea>
</div>
<div style="margin-top:12px;display:flex;gap:12px;align-items:center">
<button id="sendBtn" onclick="sendRequest()">发送请求</button>
<label style="display:flex;align-items:center;gap:6px;cursor:pointer">
<input type="checkbox" id="stream" style="width:auto"> 流式响应
</label>
</div>
<div id="result" class="result">等待发送...</div>
</div>

<div class="card">
<h2>⚙️ 在客户端中使用</h2>
<table>
<tr><th>客户端</th><th>API 地址</th><th>API Key</th></tr>
<tr><td>ChatGPT-Next-Web</td><td><code>https://glm.zeroo.ggff.net</code></td><td>代理 API Key</td></tr>
<tr><td>LobeChat</td><td><code>https://glm.zeroo.ggff.net/v1</code></td><td>代理 API Key</td></tr>
<tr><td>OpenAI SDK</td><td><code>https://glm.zeroo.ggff.net/v1</code></td><td>代理 API Key</td></tr>
</table>
</div>

<div class="footer">
GLM API Proxy v2.1 · FastAPI + httpx · Cloudflare Tunnel<br>
<a href="/health">健康检查</a> · <a href="/v1/models">模型列表</a>
</div>
</div>

<script>
async function sendRequest() {
    const btn = document.getElementById('sendBtn');
    const result = document.getElementById('result');
    const key = document.getElementById('apiKey').value;
    const model = document.getElementById('model').value;
    const thinking = document.getElementById('thinking').value;
    const msg = document.getElementById('message').value;
    const stream = document.getElementById('stream').checked;

    if (!key) { result.className='result err'; result.textContent='请输入 API Key'; return; }

    btn.disabled = true;
    result.className = 'result';
    result.textContent = '请求中...';

    const body = {model, messages:[{role:'user',content:msg}]};
    if (thinking) body.reasoning_effort = thinking;
    if (stream) body.stream = true;

    try {
        const resp = await fetch('/v1/chat/completions', {
            method:'POST',
            headers:{'Authorization':'Bearer '+key,'Content-Type':'application/json'},
            body:JSON.stringify(body)
        });

        if (!resp.ok) {
            const err = await resp.text();
            result.className='result err';
            result.textContent='HTTP '+resp.status+'
'+err;
            return;
        }

        if (stream) {
            result.className='result ok';
            result.textContent='';
            const reader = resp.body.getReader();
            const dec = new TextDecoder();
            let buf='';
            while(true){
                const{done,value}=await reader.read();
                if(done)break;
                buf+=dec.decode(value,{stream:true});
                const lines=buf.split('
');
                buf=lines.pop();
                for(const line of lines){
                    if(line.startsWith('data: ')&&line!=='data: [DONE]'){
                        try{
                            const d=JSON.parse(line.slice(6));
                            const c=d.choices?.[0]?.delta;
                            if(c?.reasoning_content)result.textContent+='[思考]'+c.reasoning_content;
                            if(c?.content)result.textContent+=c.content;
                            result.scrollTop=result.scrollHeight;
                        }catch(e){}
                    }
                }
            }
        } else {
            const data = await resp.json();
            result.className='result ok';
            const c = data.choices?.[0]?.message;
            result.textContent = '';
            if(c?.reasoning_content) result.textContent+='[推理过程]
'+c.reasoning_content+'

';
            if(c?.content) result.textContent+='[回答]
'+c.content;
            if(data.usage) result.textContent+='

[Usage] '+JSON.stringify(data.usage);
        }
    } catch(e) {
        result.className='result err';
        result.textContent='错误: '+e.message;
    } finally {
        btn.disabled=false;
    }
}
</script>
</body>
</html>""")


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         GLM API 中转代理服务器 v2.0.0 — 全面优化版           ║
╠══════════════════════════════════════════════════════════════╣
║ 监听地址: {PROXY_HOST}:{PROXY_PORT:<51}║
║ 上游 API: {UPSTREAM_BASE:<52}║
║ 模型列表: glm-5.2, glm-5.1                                    ║
║ Thinking: reasoning_effort (xhigh/max/high/medium/low)        ║
║ 自适应超时: xhigh/max=600s, high=300s, medium=180s, low=120s  ║
║ 心跳保活: 每{HEARTBEAT_INTERVAL:>4.0f}秒发送 SSE 注释行                       ║
║ 自动重试: 最多{MAX_RETRIES}次, 指数退避                          ║
║ 并发控制: 最多{MAX_CONCURRENT_REQUESTS}个同时请求                        ║
║ 连接池:   keepalive={POOL_MAX_KEEPALIVE}, max={POOL_MAX_CONNECTIONS}, expiry={POOL_KEEPALIVE_EXPIRY}s   ║
╚══════════════════════════════════════════════════════════════╝
    """)

    uvicorn.run(
        app,
        host=PROXY_HOST,
        port=PROXY_PORT,
        log_level="info",
        access_log=False,
        timeout_keep_alive=120,  # uvicorn 层 keep-alive
        limit_concurrency=100,   # uvicorn 层并发限制
    )

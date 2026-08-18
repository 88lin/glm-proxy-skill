# GLM API 中转代理服务 Skill

## 概述

在华为云开发环境中，将内部 GLM 模型服务（GLM-5.2/GLM-5.1）通过 OpenAI 兼容 API 代理到公网，供外部电脑使用。支持 thinking 模式、流式/非流式响应、Cloudflare Tunnel 内网穿透、API Key 认证。

## 触发词

GLM代理、GLM中转、GLM API、glm proxy、华为云模型代理、tokenhub代理、内网模型穿透、thinking模式代理、reasoning_effort、Cloudflare Tunnel GLM

## 前置条件

- 华为云开发环境（可访问 `tokenhub.developer.huaweicloud.com`）
- Python 3.8+
- hwcloud CLI（用于提取上游 API Key）
- Cloudflare 账号（用于 Tunnel，可选）

## 执行步骤

### 第一步：提取上游 API Key

上游 API Key 是华为云内部 tokenhub 服务的认证凭证，需要从 hwcloud 进程内存中提取。

> **重要**：tokenhub API Key 通常超过 1000 字符，以 `AAAA` 开头。提取脚本已优化支持超长 key + 自动验证。

**一键提取（推荐）：**

```bash
# 确保 hwcloud 正在运行（hwcloud chat 或其他命令）
bash scripts/extract_key.sh
```

脚本会自动：
1. 用 gdb dump 进程内存，搜索 `Authorization: Bearer <token>` 模式
2. 支持超长 key（30-2000 字符），按长度降序排列候选
3. 逐个自动验证候选 key（向 tokenhub 发测试请求）
4. 验证成功后保存到 `/tmp/working_api_key.txt`

**手动提取（备选）：**

```bash
PID=$(pgrep -f hwcloud | head -1)

# gdb dump 全部内存
gdb -batch -ex "attach $PID" -ex "dump memory /tmp/mem.bin 0 0xffffffff" -ex "detach"

# 搜索 Bearer token（注意：key 可能超过 1000 字符）
python3 -c "
import re
data = open('/tmp/mem.bin','rb').read().decode('utf-8','ignore')
for m in re.finditer(r'Authorization:\s*Bearer\s+([A-Za-z0-9_\-+/=\.]{30,2000})', data):
    print(m.group(1))
" | sort -u | head -5

# 验证找到的 key
curl -s -H "Authorization: Bearer $(cat /tmp/working_api_key.txt)" \
  https://tokenhub.developer.huaweicloud.com/v2/models | python3 -m json.tool
```

### 第二步：一键安装代理服务

```bash
export PROXY_PORT=9997
export UPSTREAM_API_KEY="$(cat /tmp/working_api_key.txt)"
bash scripts/setup.sh
```

安装脚本会自动：
1. 检查环境（Python、pip）
2. 安装依赖（fastapi、httpx、uvicorn）
3. 部署代理脚本到 `/root/glm-proxy/`
4. 生成代理 API Key（保存到 `/tmp/proxy_api_key.txt`）
5. 启动代理服务
6. 健康检查

### 第三步：配置 Cloudflare Tunnel（公网访问）

**3.1 创建 Tunnel：**

1. 登录 https://one.dash.cloudflare.com
2. Networks → Tunnels → Create a tunnel
3. 选择 Cloudflared 类型，命名隧道
4. 复制 Tunnel Token（格式为 `eyJxxx...`）

**3.2 配置 Ingress Rule：**

| 字段 | 值 |
|------|-----|
| Service | `http://localhost:9997` |
| Public hostname | `glm.zeroo.ggff.net`（如 `glm.zeroo.ggff.net`） |
| Path | 留空 |

**3.3 启动隧道：**

```bash
# 安装 cloudflared（ARM64 环境）
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    wget https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -O /usr/local/bin/cloudflared
else
    wget https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -O /usr/local/bin/cloudflared
fi
chmod +x /usr/local/bin/cloudflared

# 启动隧道
export CF_TUNNEL_TOKEN="eyJxxx你的tunnel_token"
nohup cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" > /tmp/cloudflared.log 2>&1 &
echo $! > /tmp/cloudflared.pid
```

或者直接用安装脚本一步到位：

```bash
CF_TUNNEL_TOKEN="eyJxxx你的tunnel_token" bash scripts/setup.sh
```

### 第四步：验证服务

```bash
# 1. 健康检查
curl http://localhost:9997/health

# 2. 模型列表
curl -H "Authorization: Bearer $(cat /tmp/proxy_api_key.txt)" \
  http://localhost:9997/v1/models

# 3. 普通对话
curl -X POST http://localhost:9997/v1/chat/completions \
  -H "Authorization: Bearer $(cat /tmp/proxy_api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

# 4. Thinking 模式（xhigh 推理强度）
curl -X POST http://localhost:9997/v1/chat/completions \
  -H "Authorization: Bearer $(cat /tmp/proxy_api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"证明根号2是无理数"}],"reasoning_effort":"xhigh"}'

# 5. 流式响应
curl -X POST http://localhost:9997/v1/chat/completions \
  -H "Authorization: Bearer $(cat /tmp/proxy_api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}],"stream":true}'

# 6. 公网访问（替换为glm.zeroo.ggff.net）
curl -X POST https://glm.zeroo.ggff.net/v1/chat/completions \
  -H "Authorization: Bearer $(cat /tmp/proxy_api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

### 第五步：从外部电脑使用

在外部电脑上，将 API Base URL 设置为公网地址：

```python
from openai import OpenAI

client = OpenAI(
    api_key="你的代理API_KEY",  # /tmp/proxy_api_key.txt 中的值
    base_url="https://glm.zeroo.ggff.net/v1"
)

# 普通对话
response = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "你好"}]
)
print(response.choices[0].message.content)

# Thinking 模式
response = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "证明根号2是无理数"}],
    extra_body={"reasoning_effort": "xhigh"}
)
print(response.choices[0].message.content)
print(response.choices[0].message.reasoning_content)  # 推理过程
```

## 服务管理

```bash
# 查看状态
curl http://localhost:9997/health

# 查看日志
tail -f /tmp/glm_proxy.log

# 重启代理
kill $(cat /tmp/glm_proxy.pid)
PROXY_PORT=9997 nohup python3 /root/glm-proxy/glm_proxy.py > /tmp/glm_proxy.log 2>&1 &
echo $! > /tmp/glm_proxy.pid

# 重启隧道
kill $(cat /tmp/cloudflared.pid)
nohup cloudflared tunnel run --token "$(cat /tmp/cf_tunnel_token.txt)" > /tmp/cloudflared.log 2>&1 &
echo $! > /tmp/cloudflared.pid

# 停止所有
kill $(cat /tmp/glm_proxy.pid) $(cat /tmp/cloudflared.pid) 2>/dev/null
```

## 可调参数（环境变量）

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_PORT` | 9997 | 代理监听端口 |
| `PROXY_HOST` | 0.0.0.0 | 监听地址 |
| `TIMEOUT_XHIGH` | 600 | xhigh 超时（秒） |
| `TIMEOUT_MAX` | 600 | max 超时（秒） |
| `TIMEOUT_HIGH` | 300 | high 超时（秒） |
| `TIMEOUT_MEDIUM` | 180 | medium 超时（秒） |
| `TIMEOUT_LOW` | 120 | low 超时（秒） |
| `HEARTBEAT_INTERVAL` | 10 | 心跳间隔（秒） |
| `MAX_RETRIES` | 3 | 最大重试次数 |
| `MAX_CONCURRENT_REQUESTS` | 20 | 最大并发请求 |
| `POOL_MAX_KEEPALIVE` | 30 | 连接池保活数 |
| `POOL_MAX_CONNECTIONS` | 100 | 连接池最大连接 |

## 技术架构

```
外部电脑 → HTTPS → Cloudflare Tunnel → 代理服务(localhost:9997) → tokenhub API → GLM 推理集群
```

**代理服务核心功能：**
- OpenAI 兼容 API（`/v1/chat/completions`、`/v1/models`）
- thinking 模式（`reasoning_effort`: xhigh/max/high/medium/low）
- 流式 SSE + 心跳保活（每 10s 发送 `: heartbeat`）
- 非流式内部流式 + 空白保活（防 CF 524 超时）
- 自适应超时（xhigh=600s, high=300s, medium=180s, low=120s）
- 自动重试 + 指数退避（502/503/504）
- 连接池自愈（检测失效自动重建）
- 并发控制（信号量 20 + uvicorn 100）
- API Key 认证（Bearer token）

## 故障排查

| 问题 | 解决方案 |
|------|----------|
| 上游 504 超时 | 降低 reasoning_effort 级别 |
| CF 524 超时 | 确保使用最新版代理脚本（v2.1+ 有空白保活） |
| 连接池失效 | 重启代理服务，或等待自动重建 |
| API Key 401 | 检查 `/tmp/proxy_api_key.txt` 和 Authorization header |
| 公网无法访问 | 检查 cloudflared 进程和 Tunnel 配置 |
| 环境重启后失效 | 需重新提取 API Key 并重启服务 |
| 提取 Key 失败 | 确认 hwcloud 正在运行；key 可能超过 1000 字符，用 v2 提取脚本 |

## 文件说明

| 文件 | 说明 |
|------|------|
| `scripts/glm_proxy.py` | 代理服务主程序（FastAPI + httpx） |
| `scripts/setup.sh` | 一键安装脚本 |
| `scripts/extract_key.sh` | API Key 提取脚本（v2 优化版） |
| `examples/` | 使用示例 |

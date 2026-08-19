# GLM API 中转代理服务

> 将华为云内部 GLM 模型（GLM-5.2 / GLM-5.1）通过 OpenAI 兼容 API 代理到公网，支持 thinking 模式、流式响应、Cloudflare Tunnel 穿透。

## 🚀 快速开始（复制粘贴给 AI）

把以下内容复制给 AI，替换 `eyJxxx` 为你的 Cloudflare Tunnel Token：

```
请帮我搭建 GLM API 中转代理服务：

1. 从 hwcloud 进程内存提取 tokenhub API Key，保存到 /tmp/working_api_key.txt
2. 运行 bash scripts/setup.sh 安装代理服务（端口 9997）
3. 配置 Cloudflare Tunnel：
   - Tunnel Token: eyJxxx（替换为你的 token）
   - Ingress: http://localhost:9997 → glm.zeroo.ggff.net
4. 启动隧道并验证公网访问
5. 输出最终的 API Base URL 和 API Key

参考脚本目录中的 SKILL.md 获取详细步骤。
```

## 📋 完整搭建步骤

### 1. 提取上游 API Key

在华为云开发环境中：

```bash
# 启动 hwcloud chat（后台）
hwcloud chat &

# 运行提取脚本
bash scripts/extract_key.sh
```

或手动提取：

```bash
# 找到 hwcloud 进程并扫描内存
PID=$(pgrep -f hwcloud | head -1)
python3 -c "
import re
pid = $PID
with open(f'/proc/{pid}/maps') as f:
    for line in f:
        parts = line.split()
        if len(parts) < 6 or 'r' not in parts[1]: continue
        s, e = [int(x,16) for x in parts[0].split('-')]
        if e-s > 50*1024*1024: continue
        try:
            with open(f'/proc/{pid}/mem','rb') as m:
                m.seek(s); d = m.read(e-s).decode('utf-8','ignore')
            for t in re.finditer(r'Bearer\s+([A-Za-z0-9_\-]{32,128})', d):
                print(t.group(1))
        except: continue
" | sort -u
```

将找到的 Key 保存：
```bash
echo '你的KEY' > /tmp/working_api_key.txt
```

### 2. 安装并启动代理

```bash
# 一键安装
bash scripts/setup.sh

# 或指定端口和上游 Key
UPSTREAM_API_KEY="你的KEY" PROXY_PORT=9997 bash scripts/setup.sh
```

安装完成后，代理 API Key 在 `/tmp/proxy_api_key.txt`。

### 3. 配置 Cloudflare Tunnel

1. 登录 [Cloudflare Zero Trust](https://one.dash.cloudflare.com)
2. Networks → Tunnels → Create a tunnel
3. 复制 Tunnel Token（`eyJxxx...` 格式）
4. 配置 Ingress Rule：
   - Service: `http://localhost:9997`
   - Public hostname: `glm.zeroo.ggff.net`

```bash
# 安装 cloudflared
ARCH=$(uname -m)
CF_ARCH=$([ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ] && echo "arm64" || echo "amd64")
wget "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -O /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# 启动隧道
nohup cloudflared tunnel run --token "eyJxxx你的token" > /tmp/cloudflared.log 2>&1 &
echo $! > /tmp/cloudflared.pid
```

### 4. 验证

```bash
# 本地验证
curl http://localhost:9997/health

curl -X POST http://localhost:9997/v1/chat/completions \
  -H "Authorization: Bearer $(cat /tmp/proxy_api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

# 公网验证（替换域名）
curl -X POST https://glm.zeroo.ggff.net/v1/chat/completions \
  -H "Authorization: Bearer $(cat /tmp/proxy_api_key.txt)" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'
```

## 📖 从外部电脑使用

### Python (OpenAI SDK)

```python
from openai import OpenAI

client = OpenAI(
    api_key="你的代理API_KEY",       # /tmp/proxy_api_key.txt 的内容
    base_url="https://glm.zeroo.ggff.net/v1"
)

# 普通对话
resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "你好"}]
)
print(resp.choices[0].message.content)

# Thinking 模式（深度推理）
resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "证明根号2是无理数"}],
    extra_body={"reasoning_effort": "xhigh"}
)
print(resp.choices[0].message.content)
print("---推理过程---")
print(resp.choices[0].message.reasoning_content)

# 流式响应
for chunk in client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "写一首诗"}],
    stream=True
):
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### cURL

```bash
# 普通对话
curl -X POST https://glm.zeroo.ggff.net/v1/chat/completions \
  -H "Authorization: Bearer 你的API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

# Thinking 模式
curl -X POST https://glm.zeroo.ggff.net/v1/chat/completions \
  -H "Authorization: Bearer 你的API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"证明根号2是无理数"}],"reasoning_effort":"xhigh"}'

# 流式
curl -X POST https://glm.zeroo.ggff.net/v1/chat/completions \
  -H "Authorization: Bearer 你的API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 在 ChatGPT-Next-Web / LobeChat 等客户端中使用

| 设置项 | 值 |
|--------|-----|
| API 地址 | `https://glm.zeroo.ggff.net` |
| API Key | `/tmp/proxy_api_key.txt` 的内容 |
| 模型 | `glm-5.2` 或 `glm-5.1` |

## 🔧 服务管理

```bash
# 查看状态
curl http://localhost:9997/health | python3 -m json.tool

# 查看日志
tail -f /tmp/glm_proxy.log

# 重启代理
kill $(cat /tmp/glm_proxy.pid); sleep 1
PROXY_PORT=9997 nohup python3 /root/glm-proxy/glm_proxy.py > /tmp/glm_proxy.log 2>&1 &
echo $! > /tmp/glm_proxy.pid

# 重启隧道
kill $(cat /tmp/cloudflared.pid); sleep 1
nohup cloudflared tunnel run --token "$(cat /tmp/cf_tunnel_token.txt)" > /tmp/cloudflared.log 2>&1 &
echo $! > /tmp/cloudflared.pid
```

## 🌟 功能特性

- ✅ **OpenAI 兼容 API** — `/v1/chat/completions`、`/v1/models`
- ✅ **Thinking 模式** — `reasoning_effort`: xhigh / max / high / medium / low
- ✅ **流式 SSE** — 实时输出 + 心跳保活（每 10s）
- ✅ **非流式保活** — 内部流式 + 空白保活，防 CF 524 超时
- ✅ **自适应超时** — xhigh=600s, high=300s, medium=180s, low=120s
- ✅ **自动重试** — 最多 3 次，指数退避
- ✅ **连接池自愈** — 检测失效自动重建
- ✅ **并发控制** — 信号量 20 + uvicorn 100
- ✅ **API Key 认证** — Bearer token 保护
- ✅ **Cloudflare Tunnel** — 公网 HTTPS 内网穿透

## 📁 文件结构

```
glm-proxy-skill/
├── SKILL.md              # AI 执行指令（给 AI 看的）
├── README.md             # 使用说明（给人看的）
├── scripts/
│   ├── glm_proxy.py      # 代理服务主程序
│   ├── setup.sh          # 一键安装脚本
│   └── extract_key.sh    # API Key 提取脚本
└── examples/
    ├── curl_test.sh      # cURL 测试示例
    └── python_example.py # Python 使用示例
```

## ⚠️ 注意事项

1. **上游 API Key 需在华为云环境内提取** — tokenhub 仅内网可访问
2. **环境重启后需重新提取 Key** — Key 可能会过期
3. **Cloudflare 免费版有 100s 边缘超时** — 代理已通过空白保活解决此问题
4. **代理端口需与 Tunnel Ingress 一致** — 默认 9997
5. **GitHub 镜像 `ghfast.top`** — 用于在中国加速下载 cloudflared

## 🔗 下一步：设置自动备份

GLM 代理搭好后，建议立即设置自动备份，这样容器销毁重建后可以一键恢复：

→ **[devenv-chat-backup-skill](https://github.com/88lin/devenv-chat-backup-skill)** — 聊天历史 + GLM Proxy + 配置自动备份到 GitHub

设置完成后，以后容器销毁重建只需一条命令恢复全部（代理 + Key + 聊天历史 + 保活）。

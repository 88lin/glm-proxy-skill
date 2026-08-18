#!/bin/bash
# ============================================================
# GLM API 代理服务 — 一键安装脚本 v2.2
# 优化：跳过已安装依赖、支持固定 API Key、加速部署
# 用法: bash setup.sh
# ============================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step()  { echo -e "${BLUE}[STEP]${NC} $1"; }

GITHUB_MIRROR="https://ghfast.top"
PROXY_PORT="${PROXY_PORT:-9997}"
INSTALL_DIR="${INSTALL_DIR:-/root/glm-proxy}"
UPSTREAM_API_KEY="${UPSTREAM_API_KEY:-}"
CF_TUNNEL_TOKEN="${CF_TUNNEL_TOKEN:-}"
PROXY_API_KEY="${PROXY_API_KEY:-}"  # 支持固定 API Key

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║      GLM API 中转代理服务 — 一键安装脚本 v2.2              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ============================================================
# STEP 1: 检查环境
# ============================================================
step "1/6 检查运行环境..."

if ! command -v python3 &>/dev/null; then
    error "未找到 python3，请先安装 Python 3.8+"
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Python: $PY_VER, 架构: $(uname -m)"

if ! command -v pip3 &>/dev/null; then
    warn "未找到 pip3，正在安装..."
    python3 -m ensurepip --upgrade 2>/dev/null || apt-get update && apt-get install -y python3-pip
fi

info "环境检查通过 ✓"

# ============================================================
# STEP 2: 安装 Python 依赖（跳过已安装的）
# ============================================================
step "2/6 安装 Python 依赖..."

NEED_INSTALL=0
for pkg in fastapi httpx uvicorn; do
    if ! python3 -c "import $pkg" 2>/dev/null; then
        NEED_INSTALL=1
        break
    fi
done

if [ "$NEED_INSTALL" = "1" ]; then
    PIP_INDEX="${PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
    pip3 install fastapi httpx uvicorn -q -i "$PIP_INDEX" 2>/dev/null || {
        warn "清华镜像失败，使用默认源..."
        pip3 install fastapi httpx uvicorn -q
    }
    info "Python 依赖安装完成 ✓"
else
    info "Python 依赖已安装，跳过 ✓"
fi

# ============================================================
# STEP 3: 部署代理脚本
# ============================================================
step "3/6 部署代理脚本..."

mkdir -p "$INSTALL_DIR"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -f "$SCRIPT_DIR/glm_proxy.py" ]; then
    cp "$SCRIPT_DIR/glm_proxy.py" "$INSTALL_DIR/glm_proxy.py"
    info "代理脚本已复制到 $INSTALL_DIR"
else
    error "未找到 glm_proxy.py"
    exit 1
fi

info "代理脚本部署完成 ✓"

# ============================================================
# STEP 4: 配置 API Key
# ============================================================
step "4/6 配置 API Key..."

# 上游 API Key
if [ -n "$UPSTREAM_API_KEY" ]; then
    echo "$UPSTREAM_API_KEY" > /tmp/working_api_key.txt
    info "上游 API Key 已配置（环境变量）"
elif [ -f /tmp/working_api_key.txt ]; then
    info "上游 API Key 已存在（/tmp/working_api_key.txt）"
else
    echo ""
    warn "需要上游 API Key（从华为云 hwcloud 内部模型服务获取）"
    echo ""
    echo "  提取方法：运行 bash scripts/extract_key.sh"
    echo ""
    read -p "  请粘贴上游 API Key（或按 Enter 跳过）: " INPUT_KEY
    if [ -n "$INPUT_KEY" ]; then
        echo "$INPUT_KEY" > /tmp/working_api_key.txt
        info "上游 API Key 已保存"
    else
        warn "跳过上游 API Key 配置"
    fi
fi

# 代理 API Key（支持固定或自动生成）
if [ -n "$PROXY_API_KEY" ]; then
    echo "$PROXY_API_KEY" > /tmp/proxy_api_key.txt
    info "代理 API Key 已固定: ${PROXY_API_KEY:0:20}..."
elif [ -f /tmp/proxy_api_key.txt ]; then
    info "代理 API Key 已存在: $(cat /tmp/proxy_api_key.txt | head -c 20)..."
else
    PROXY_KEY="sk-glm-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    echo "$PROXY_KEY" > /tmp/proxy_api_key.txt
    info "代理 API Key 已生成: ${PROXY_KEY:0:20}..."
fi

info "API Key 配置完成 ✓"

# ============================================================
# STEP 5: 启动代理服务
# ============================================================
step "5/6 启动代理服务..."

# 停止已有实例
if [ -f /tmp/glm_proxy.pid ]; then
    OLD_PID=$(cat /tmp/glm_proxy.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null
        sleep 2
        info "已停止旧实例 (PID: $OLD_PID)"
    fi
fi

PROXY_PORT="$PROXY_PORT" nohup python3 "$INSTALL_DIR/glm_proxy.py" > /tmp/glm_proxy.log 2>&1 &
echo $! > /tmp/glm_proxy.pid
sleep 3

if kill -0 "$(cat /tmp/glm_proxy.pid)" 2>/dev/null; then
    info "代理服务已启动 (PID: $(cat /tmp/glm_proxy.pid), 端口: $PROXY_PORT)"
else
    error "代理服务启动失败，查看日志："
    tail -20 /tmp/glm_proxy.log
    exit 1
fi

# 健康检查
if curl -s "http://127.0.0.1:$PROXY_PORT/health" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if d['status']=='ok' else 1)" 2>/dev/null; then
    info "健康检查通过 ✓"
else
    warn "健康检查未通过，服务可能还在启动中"
fi

# ============================================================
# STEP 6: Cloudflare Tunnel（可选）
# ============================================================
step "6/6 Cloudflare Tunnel 配置（可选）..."

if [ -n "$CF_TUNNEL_TOKEN" ]; then
    if ! command -v cloudflared &>/dev/null; then
        info "安装 cloudflared..."
        ARCH=$(uname -m)
        if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
            CF_ARCH="arm64"
        else
            CF_ARCH="amd64"
        fi
        wget -q "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -O /usr/local/bin/cloudflared 2>/dev/null || \
        wget -q "${GITHUB_MIRROR}/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -O /usr/local/bin/cloudflared
        chmod +x /usr/local/bin/cloudflared
    else
        info "cloudflared 已安装，跳过"
    fi
    
    if [ -f /tmp/cloudflared.pid ]; then
        kill "$(cat /tmp/cloudflared.pid)" 2>/dev/null || true
        sleep 1
    fi
    
    echo "$CF_TUNNEL_TOKEN" > /tmp/cf_tunnel_token.txt
    nohup cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" > /tmp/cloudflared.log 2>&1 &
    echo $! > /tmp/cloudflared.pid
    sleep 3
    
    if kill -0 "$(cat /tmp/cloudflared.pid)" 2>/dev/null; then
        info "Cloudflare Tunnel 已启动 (PID: $(cat /tmp/cloudflared.pid))"
    else
        warn "Cloudflare Tunnel 启动失败，查看 /tmp/cloudflared.log"
    fi
else
    warn "未提供 CF_TUNNEL_TOKEN，跳过隧道配置"
    echo ""
    echo "  如需公网访问，请："
    echo "  1. 在 https://one.dash.cloudflare.com 创建 Tunnel"
    echo "  2. 配置 ingress 指向 http://localhost:$PROXY_PORT"
    echo "  3. 重新运行: CF_TUNNEL_TOKEN=eyJxxx... bash setup.sh"
fi

# ============================================================
# 完成
# ============================================================
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║                    ✅ 安装完成！                             ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo -e "║  代理地址:     http://localhost:${PROXY_PORT}/v1"
echo -e "║  代理 API Key: $(cat /tmp/proxy_api_key.txt | head -c 40)..."
echo "║  健康检查:     curl http://localhost:${PROXY_PORT}/health"
echo "║  日志:         /tmp/glm_proxy.log"
echo "║                                                              ║"
echo "║  使用示例:                                                   ║"
echo "║  curl -X POST http://localhost:${PROXY_PORT}/v1/chat/completions \\"
echo "║    -H 'Authorization: Bearer \$(cat /tmp/proxy_api_key.txt)' \\"
echo "║    -H 'Content-Type: application/json' \\"
echo "║    -d '{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'"
echo "║                                                              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

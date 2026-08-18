#!/bin/bash
# ============================================================
# 从华为云 hwcloud 二进制中提取 tokenhub API Key
# 用法: bash extract_key.sh
# ============================================================
set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step()  { echo -e "${BLUE}[STEP]${NC} $1"; }

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     从 hwcloud 提取 tokenhub API Key                        ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ============================================================
# 方法 1: 从已运行的 hwcloud 进程内存提取
# ============================================================
step "方法 1: 从 hwcloud 进程内存提取..."

HWCloud_PIDS=$(pgrep -f "hwcloud" 2>/dev/null || true)

if [ -n "$HWCloud_PIDS" ]; then
    info "找到 hwcloud 进程: $HWCloud_PIDS"
    
    for PID in $HWCloud_PIDS; do
        info "扫描进程 $PID 的内存..."
        
        # 从 /proc/PID/maps 和 /proc/PID/mem 提取字符串
        # 查找类似 API key 的模式（长 hex 或 base64 字符串）
        if [ -r "/proc/$PID/maps" ] && [ -r "/proc/$PID/mem" ]; then
            # 提取可读内存区域中的字符串，搜索 API key 模式
            python3 -c "
import re, sys, os

pid = $PID
found_keys = set()

try:
    with open(f'/proc/{pid}/maps', 'r') as maps:
        for line in maps:
            parts = line.split()
            if len(parts) < 6:
                continue
            perms = parts[1]
            if 'r' not in perms:
                continue
            addr_range = parts[0].split('-')
            start = int(addr_range[0], 16)
            end = int(addr_range[1], 16)
            
            # 跳过过大的区域
            if end - start > 50 * 1024 * 1024:
                continue
            
            try:
                with open(f'/proc/{pid}/mem', 'rb') as mem:
                    mem.seek(start)
                    data = mem.read(end - start)
            except Exception:
                continue
            
            # 搜索 API key 模式
            # tokenhub key 通常以特定前缀开头或是很长的 hex/base64 字符串
            text = data.decode('utf-8', errors='ignore')
            
            # 模式 1: Bearer token
            for m in re.finditer(r'Bearer\s+([A-Za-z0-9_\-]{32,128})', text):
                found_keys.add(m.group(1))
            
            # 模式 2: api_key / authorization 字段
            for m in re.finditer(r'(?:api[_-]?key|authorization|token)[\"\s:=]+([A-Za-z0-9_\-]{32,128})', text, re.I):
                found_keys.add(m.group(1))
            
            # 模式 3: sk- 前缀
            for m in re.finditer(r'(sk-[A-Za-z0-9_\-]{20,100})', text):
                found_keys.add(m.group(1))

except Exception as e:
    print(f'Error: {e}', file=sys.stderr)

for k in sorted(found_keys, key=len, reverse=True):
    print(k)
" 2>/dev/null | while read -r KEY; do
                if [ -n "$KEY" ] && [ ${#KEY} -ge 32 ]; then
                    info "找到候选 Key: ${KEY:0:20}... (长度: ${#KEY})"
                    echo "$KEY" > /tmp/working_api_key.txt
                    info "已保存到 /tmp/working_api_key.txt"
                    exit 0
                fi
            done
        fi
    done
    warn "进程内存中未找到明确的 API Key"
else
    warn "未找到运行中的 hwcloud 进程"
fi

# ============================================================
# 方法 2: 从 hwcloud 二进制文件中搜索
# ============================================================
step "方法 2: 从 hwcloud 二进制文件搜索..."

HWCloud_BIN=$(which hwcloud 2>/dev/null || find / -name "hwcloud" -type f 2>/dev/null | head -1 || true)

if [ -n "$HWCloud_BIN" ]; then
    info "找到 hwcloud: $HWCloud_BIN"
    
    # 搜索二进制中的字符串
    strings "$HWCloud_BIN" 2>/dev/null | grep -E '^[A-Za-z0-9_\-]{32,128}$' | sort -u | while read -r KEY; do
        # 尝试验证 key 是否有效
        if curl -s -o /dev/null -w "%{http_code}" \
            -H "Authorization: Bearer $KEY" \
            "https://tokenhub.developer.huaweicloud.com/v2/models" 2>/dev/null | grep -q "200"; then
            info "验证成功！Key: ${KEY:0:20}..."
            echo "$KEY" > /tmp/working_api_key.txt
            info "已保存到 /tmp/working_api_key.txt"
            exit 0
        fi
    done
    warn "二进制中未找到有效 Key"
else
    warn "未找到 hwcloud 二进制"
fi

# ============================================================
# 方法 3: 通过 hwcloud chat 获取
# ============================================================
step "方法 3: 通过 hwcloud chat 获取..."

if command -v hwcloud &>/dev/null; then
    info "尝试通过 hwcloud chat 启动会话..."
    warn "请在新终端中运行: hwcloud chat"
    warn "然后重新运行此脚本（方法 1 会扫描新进程）"
else
    warn "hwcloud 命令不可用"
fi

# ============================================================
# 方法 4: 手动输入
# ============================================================
step "方法 4: 手动输入..."
echo ""
read -p "请粘贴 tokenhub API Key（或按 Ctrl+C 退出）: " MANUAL_KEY

if [ -n "$MANUAL_KEY" ]; then
    echo "$MANUAL_KEY" > /tmp/working_api_key.txt
    info "已保存到 /tmp/working_api_key.txt"
    
    # 验证
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $MANUAL_KEY" \
        "https://tokenhub.developer.huaweicloud.com/v2/models" 2>/dev/null || echo "000")
    
    if [ "$HTTP_CODE" = "200" ]; then
        info "✅ API Key 验证成功！"
    else
        warn "API Key 验证返回 HTTP $HTTP_CODE（可能在此环境外无法验证）"
    fi
else
    error "未提供 API Key"
    exit 1
fi

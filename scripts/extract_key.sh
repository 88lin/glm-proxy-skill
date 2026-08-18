#!/bin/bash
# ============================================================
# 从华为云 hwcloud 进程内存提取 tokenhub API Key（优化版 v2）
# 改进：gdb 快速提取 + 自动验证 + 支持超长 key（1000+ 字符）
# 用法: bash extract_key.sh
# ============================================================
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }
step()  { echo -e "${BLUE}[STEP]${NC} $1"; }

API_BASE="https://tokenhub.developer.huaweicloud.com/v2"
KEY_FILE="/tmp/working_api_key.txt"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     从 hwcloud 提取 tokenhub API Key (v2 优化版)           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# --- 验证函数 ---
verify_key() {
    local key="$1"
    local resp
    resp=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: Bearer $key" \
        -H "Content-Type: application/json" \
        -d '{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}],"max_tokens":5}' \
        "$API_BASE/chat/completions" 2>/dev/null || echo "000")
    [ "$resp" = "200" ]
}

# --- 方法 1: gdb 提取（最快最可靠）---
step "方法 1: gdb 内存搜索（推荐，最快）..."

HWCloud_PIDS=$(pgrep -f "hwcloud" 2>/dev/null || true)

if [ -n "$HWCloud_PIDS" ] && command -v gdb &>/dev/null; then
    for PID in $HWCloud_PIDS; do
        info "用 gdb 扫描进程 $PID ..."
        
        # gdb 批量 dump 内存，搜索 "Authorization: Bearer " 后的长字符串
        # 支持最长 2000 字符的 key
        gdb -batch -ex "attach $PID" \
            -ex "dump memory /tmp/_mem_dump.bin 0 0xffffffff" \
            -ex "detach" 2>/dev/null

        if [ -f /tmp/_mem_dump.bin ]; then
            info "内存已 dump ($(du -h /tmp/_mem_dump.bin | cut -f1))，搜索 Bearer token..."
            
            # 搜索 "Authorization: Bearer " 后的 token（支持超长 key）
            python3 -c "
import re, sys

with open('/tmp/_mem_dump.bin', 'rb') as f:
    data = f.read()

text = data.decode('utf-8', errors='ignore')

# 模式 1: Authorization: Bearer <token>（最精确，支持 30-2000 字符）
candidates = set()
for m in re.finditer(r'Authorization:\s*Bearer\s+([A-Za-z0-9_\-+/=\.]{30,2000})', text):
    candidates.add(m.group(1).strip())

# 模式 2: bearer <token>（小写变体）
for m in re.finditer(r'bearer\s+([A-Za-z0-9_\-+/=\.]{30,2000})', text, re.I):
    candidates.add(m.group(1).strip())

# 模式 3: 以 AAAA 开头的超长 token（tokenhub key 特征）
for m in re.finditer(r'(AAAA[A-Za-z0-9_\-+/=\.]{50,2000})', text):
    candidates.add(m.group(1).strip())

# 按长度降序输出（长 key 更可能是真正的 API key）
for k in sorted(candidates, key=len, reverse=True):
    print(k)
" 2>/dev/null | while read -r KEY; do
                if [ -n "$KEY" ] && [ ${#KEY} -ge 30 ]; then
                    info "候选 Key: ${KEY:0:30}... (长度: ${#KEY})"
                    if verify_key "$KEY"; then
                        info "✅ 验证成功！"
                        echo "$KEY" > "$KEY_FILE"
                        info "已保存到 $KEY_FILE"
                        rm -f /tmp/_mem_dump.bin
                        exit 0
                    else
                        warn "验证失败，继续尝试..."
                    fi
                fi
            done
            rm -f /tmp/_mem_dump.bin
        fi
    done
    warn "gdb 方法未找到有效 Key"
else
    [ -z "$HWCloud_PIDS" ] && warn "未找到 hwcloud 进程"
    ! command -v gdb &>/dev/null && warn "gdb 未安装"
fi

# --- 方法 2: /proc/PID/mem 直接读取（gdb 不可用时）---
step "方法 2: /proc/PID/mem 直接搜索..."

if [ -n "$HWCloud_PIDS" ]; then
    for PID in $HWCloud_PIDS; do
        info "扫描进程 $PID 内存区域..."
        
        python3 -c "
import re, sys

pid = $PID
candidates = set()

try:
    with open(f'/proc/{pid}/maps', 'r') as maps:
        for line in maps:
            parts = line.split()
            if len(parts) < 6 or 'r' not in parts[1]:
                continue
            start, end = [int(x, 16) for x in parts[0].split('-')]
            if end - start > 100 * 1024 * 1024:
                continue
            try:
                with open(f'/proc/{pid}/mem', 'rb') as mem:
                    mem.seek(start)
                    data = mem.read(end - start)
            except Exception:
                continue
            text = data.decode('utf-8', errors='ignore')
            # Authorization: Bearer <token>（支持超长 key）
            for m in re.finditer(r'Authorization:\s*Bearer\s+([A-Za-z0-9_\-+/=\.]{30,2000})', text):
                candidates.add(m.group(1).strip())
            # AAAA 开头的超长 token
            for m in re.finditer(r'(AAAA[A-Za-z0-9_\-+/=\.]{50,2000})', text):
                candidates.add(m.group(1).strip())
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)

for k in sorted(candidates, key=len, reverse=True):
    print(k)
" 2>/dev/null | while read -r KEY; do
                if [ -n "$KEY" ] && [ ${#KEY} -ge 30 ]; then
                    info "候选 Key: ${KEY:0:30}... (长度: ${#KEY})"
                    if verify_key "$KEY"; then
                        info "✅ 验证成功！"
                        echo "$KEY" > "$KEY_FILE"
                        info "已保存到 $KEY_FILE"
                        exit 0
                    else
                        warn "验证失败，继续..."
                    fi
                fi
            done
    done
    warn "/proc/mem 方法未找到有效 Key"
fi

# --- 方法 3: 手动输入 ---
step "方法 3: 手动输入..."
echo ""
read -p "请粘贴 tokenhub API Key（或按 Ctrl+C 退出）: " MANUAL_KEY

if [ -n "$MANUAL_KEY" ]; then
    echo "$MANUAL_KEY" > "$KEY_FILE"
    info "已保存到 $KEY_FILE"
    if verify_key "$MANUAL_KEY"; then
        info "✅ API Key 验证成功！"
    else
        warn "API Key 验证失败（可能在此环境外无法验证）"
    fi
else
    error "未提供 API Key"
    exit 1
fi

#!/bin/bash
# GLM API 代理服务 — cURL 测试脚本
# 用法: bash curl_test.sh [公网域名]
# 示例: bash curl_test.sh glm.yourdomain.com

BASE_URL="${1:-http://localhost:9000}"
API_KEY=$(cat /tmp/proxy_api_key.txt 2>/dev/null || echo "YOUR_API_KEY")

echo "测试地址: $BASE_URL"
echo "API Key: ${API_KEY:0:20}..."
echo ""

# 1. 健康检查
echo "=== 1. 健康检查 ==="
curl -s "$BASE_URL/health" | python3 -m json.tool
echo ""

# 2. 模型列表
echo "=== 2. 模型列表 ==="
curl -s -H "Authorization: Bearer $API_KEY" "$BASE_URL/v1/models" | python3 -m json.tool
echo ""

# 3. 普通对话
echo "=== 3. 普通对话 ==="
curl -s -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好，请简单介绍一下自己"}]}' | python3 -m json.tool
echo ""

# 4. Thinking 模式
echo "=== 4. Thinking 模式 (xhigh) ==="
curl -s --max-time 120 -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"1+1等于几？简短回答"}],"reasoning_effort":"xhigh"}' | python3 -m json.tool
echo ""

# 5. 流式响应
echo "=== 5. 流式响应 ==="
curl -s -N -X POST "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"写一首关于春天的诗"}],"stream":true}' | head -20
echo "..."
echo ""

echo "=== 测试完成 ==="

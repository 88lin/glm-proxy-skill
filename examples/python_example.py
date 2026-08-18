#!/usr/bin/env python3
"""
GLM API 代理服务 — Python 使用示例
用法: python3 python_example.py
"""

from openai import OpenAI

# ============================================================
# 配置（替换为你的实际值）
# ============================================================
API_KEY = "你的代理API_KEY"           # /tmp/proxy_api_key.txt 的内容
BASE_URL = "https://glm.zeroo.ggff.net/v1"  # 你的公网地址

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

# ============================================================
# 1. 模型列表
# ============================================================
print("=== 模型列表 ===")
models = client.models.list()
for m in models.data:
    print(f"  {m.id} — {getattr(m, 'description', '')}")
print()

# ============================================================
# 2. 普通对话
# ============================================================
print("=== 普通对话 ===")
resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "你好，请简单介绍一下自己"}]
)
print(resp.choices[0].message.content)
print()

# ============================================================
# 3. Thinking 模式（深度推理）
# ============================================================
print("=== Thinking 模式 (xhigh) ===")
resp = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "证明根号2是无理数"}],
    extra_body={"reasoning_effort": "xhigh"}  # xhigh/max/high/medium/low
)
print("【回答】")
print(resp.choices[0].message.content)
print()
if hasattr(resp.choices[0].message, 'reasoning_content') and resp.choices[0].message.reasoning_content:
    print("【推理过程】")
    print(resp.choices[0].message.reasoning_content[:500] + "...")
print()

# ============================================================
# 4. 流式响应
# ============================================================
print("=== 流式响应 ===")
stream = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "写一首关于春天的诗"}],
    stream=True
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        print(delta.content, end="", flush=True)
print()
print()

# ============================================================
# 5. 流式 + Thinking
# ============================================================
print("=== 流式 + Thinking (high) ===")
stream = client.chat.completions.create(
    model="glm-5.2",
    messages=[{"role": "user", "content": "解释量子纠缠"}],
    stream=True,
    extra_body={"reasoning_effort": "high"}
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
        print(f"\033[90m{delta.reasoning_content}\033[0m", end="", flush=True)  # 灰色显示推理
    if delta.content:
        print(delta.content, end="", flush=True)
print()

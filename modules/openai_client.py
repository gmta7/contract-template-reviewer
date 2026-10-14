"""
OpenAI 兼容客户端工厂。

openai<1.55 与 httpx>=0.28 组合时，SDK 会向 httpx.Client 传入已移除的 proxies 参数，
导致 TypeError。通过显式传入 http_client 可绕过该问题。
"""
from typing import Optional

import httpx

from config import LLM_API_KEY, LLM_BASE_URL


def create_openai_client(
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
):
    """创建 OpenAI 兼容客户端（DeepSeek 等 OpenAI 兼容接口通用）。"""
    from openai import OpenAI

    key = api_key or LLM_API_KEY
    url = base_url or LLM_BASE_URL
    http_client = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))
    return OpenAI(api_key=key, base_url=url, http_client=http_client)

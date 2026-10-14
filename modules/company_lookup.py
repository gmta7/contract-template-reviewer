"""
乙方公司成立日期查询模块

查询策略（依次尝试，成功即返回）：
1. LLM 从自身知识库中提取成立日期（快，但可能不准确，附注说明来源）
2. 通过 HTTP 搜索国家企业信用信息公示系统 (gsxt.gov.cn)，LLM 解析结果
3. 返回 None，提示人工核查

结果附带 confidence 字段：
  - "high"   : 通过权威数据源（工商系统）查到
  - "medium" : LLM 知识，应人工二次验证
  - "none"   : 未能查到
"""
import json
import re
from typing import Optional, Tuple

import config


def _ask_llm_for_establishment(company_name: str) -> Tuple[Optional[str], str]:
    """
    让 LLM 从训练知识中提取公司成立日期。
    返回 (日期字符串或None, 置信度说明)
    """
    if not config.LLM_API_KEY:
        return None, "未配置 LLM API Key"

    try:
        from modules.openai_client import create_openai_client
        client = create_openai_client()
        prompt = (
            f'请从你的知识库中查找中国企业「{company_name}」的工商注册成立日期。\n'
            f'只输出以下 JSON，不要输出任何其他内容：\n'
            f'{{"establishment_date": "YYYY-MM-DD 或 null", "confidence": "high/medium/low", "note": "备注说明"}}\n'
            f'如果不确定，establishment_date 填 null，confidence 填 low。'
        )
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        date_val = data.get("establishment_date")
        if date_val and date_val != "null":
            note = data.get("note", "")
            confidence = data.get("confidence", "medium")
            return date_val, f"LLM知识库（置信度:{confidence}）{f'，{note}' if note else ''}"
        return None, "LLM 知识库中未找到该公司成立日期"
    except Exception as e:
        return None, f"LLM 查询失败：{e}"


def _search_gsxt(company_name: str) -> Tuple[Optional[str], str]:
    """
    通过百度搜索国家企业信用信息公示系统，尝试获取成立日期。
    返回 (日期字符串或None, 来源说明)
    """
    try:
        import urllib.request
        import urllib.parse

        query = f'{company_name} 成立日期 site:gsxt.gov.cn'
        url = f"https://www.baidu.com/s?wd={urllib.parse.quote(query)}&rn=5"

        req = urllib.request.Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        })
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # 提取搜索结果摘要文字
        snippets = re.findall(r'<span class="content-right_2s-H4">(.*?)</span>', html)
        if not snippets:
            # 兼容不同百度返回格式
            snippets = re.findall(r'<div[^>]*class="c-abstract[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)

        snippet_text = " ".join(re.sub(r'<[^>]+>', '', s) for s in snippets[:5])

        if not snippet_text.strip():
            return None, "搜索结果为空"

        # 用 LLM 从搜索结果摘要中提取成立日期
        if not config.LLM_API_KEY:
            return None, "未配置 LLM API Key，无法解析搜索结果"

        from modules.openai_client import create_openai_client
        client = create_openai_client()
        prompt = (
            f'以下是关于「{company_name}」的搜索结果摘要：\n{snippet_text[:2000]}\n\n'
            f'请从中提取该公司的工商注册成立日期。\n'
            f'只输出 JSON：{{"establishment_date": "YYYY-MM-DD 或 null"}}\n'
            f'若无法确认，填 null。'
        )
        resp = client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        date_val = data.get("establishment_date")
        if date_val and date_val != "null":
            return date_val, "搜索引擎+LLM解析（建议人工在工商系统二次确认）"
        return None, "搜索结果中未找到明确的成立日期"

    except Exception as e:
        return None, f"搜索查询失败：{e}"


def lookup_establishment_date(company_name: str) -> dict:
    """
    查询公司成立日期，返回结构化结果。

    Returns:
        {
            "company_name": str,
            "establishment_date": "YYYY-MM-DD" | None,
            "confidence": "high" | "medium" | "none",
            "source": str,
        }
    """
    if not company_name or not company_name.strip():
        return {
            "company_name": company_name,
            "establishment_date": None,
            "confidence": "none",
            "source": "公司名称为空",
        }

    name = company_name.strip()

    # 策略一：LLM 知识库
    date_val, source = _ask_llm_for_establishment(name)
    if date_val:
        return {
            "company_name": name,
            "establishment_date": date_val,
            "confidence": "medium",
            "source": source,
        }

    # 策略二：网络搜索
    date_val, source = _search_gsxt(name)
    if date_val:
        return {
            "company_name": name,
            "establishment_date": date_val,
            "confidence": "medium",
            "source": source,
        }

    return {
        "company_name": name,
        "establishment_date": None,
        "confidence": "none",
        "source": f"自动查询未找到结果（{source}），请在 gsxt.gov.cn 人工核实",
    }

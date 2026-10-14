"""
LLM 辅助分析模块

仅在方案二（文档比对）置信度不足时调用。
使用 OpenAI 兼容接口，支持私有部署（通过 LLM_BASE_URL 配置）。
"""
import json
from typing import Optional

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


SYSTEM_PROMPT = """你是一名专业法务助理，专门负责审核甲方标准合同模版的回签版本。
你的任务是：
1. 识别乙方填写的内容（合同期限、金额、主体名称等）
2. 发现乙方对模版正文进行的任何修改（无论增删改）
3. 评估风险等级

输出严格遵循以下 JSON 格式，不输出任何其他内容：
{
  "filled_fields": [
    {"field_name": "字段名称", "value": "填写值", "location": "所在条款或位置描述"}
  ],
  "tampered_sections": [
    {
      "original": "模版原文",
      "modified": "修改后文字",
      "location": "所在条款",
      "severity": "high | medium | low",
      "reason": "风险说明"
    }
  ],
  "missing_sections": [
    {"content": "缺失的模版内容", "location": "所在条款"}
  ],
  "added_sections": [
    {"content": "新增内容", "location": "位置"}
  ],
  "summary": "整体审核摘要（2-3句话）",
  "risk_level": "high | medium | low",
  "recommendation": "建议处理方式"
}"""


def analyze_with_llm(
    template_text: str,
    contract_text: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """
    使用 LLM 分析模版与回签合同的差异。

    Args:
        template_text: 模版全文（段落用换行连接）
        contract_text: 回签合同全文
        api_key: 可选，覆盖 config 中的值
        base_url: 可选，覆盖 config 中的值
        model: 可选，覆盖 config 中的值

    Returns:
        解析后的 JSON dict，失败时返回含 error 键的 dict
    """
    from modules.openai_client import create_openai_client

    key = api_key or LLM_API_KEY
    url = base_url or LLM_BASE_URL
    mdl = model or LLM_MODEL

    if not key:
        return {"error": "未配置 LLM API Key，请在设置中填写或设置 OPENAI_API_KEY 环境变量"}

    client = create_openai_client(api_key=key, base_url=url)

    user_message = f"""【合同模版】
{template_text}

【乙方回签合同】
{contract_text}

请按要求分析并输出 JSON。"""

    try:
        response = client.chat.completions.create(
            model=mdl,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = response.choices[0].message.content
        return json.loads(raw)
    except Exception as e:
        return {"error": f"LLM 调用失败：{e}"}

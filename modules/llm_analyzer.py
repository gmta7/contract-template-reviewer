"""
LLM 辅助分析模块

仅在方案二（文档比对）置信度不足时调用。
使用 OpenAI 兼容接口，支持私有部署（通过 LLM_BASE_URL 配置）。
"""
import json
from typing import Optional

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


TEMPLATE_SYSTEM_PROMPT = """你是一名专业法务助理，专门负责审核甲方标准合同模版的回签版本。
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


NON_TEMPLATE_SYSTEM_PROMPT = """你是一名具有中国大陆商事合同审查经验的高级法务。
你的任务是对一份没有标准模版可供比对的合同进行全面初审。本次输出仅作为法务初审意见，不代替最终人工审核。

审查目标：
1. 判断合同主体、交易结构、权利义务和履行流程是否清晰、完整、可执行。
2. 识别不利条款、责任失衡、表述歧义、条款冲突、关键内容缺失和合规风险。
3. 给出具体、可执行的修改建议；必要时提供可直接使用的建议条款。

审查范围至少包括：
1. 合同主体、签约资格、授权及收付款主体是否一致。
2. 交易内容、交付范围、质量标准、履行方式和交付时间。
3. 验收标准、期限、异议程序和默示验收。
4. 金额、计价、税费、发票、付款条件、退款和结算依据。
5. 生效、期限、续约、自动续期和提前终止。
6. 单方解释权、单方变更权及权利义务是否明显失衡。
7. 违约金、赔偿范围、责任上限、免责及无限责任。
8. 解除条件、通知期限、终止结算、资料返还和持续义务。
9. 原有知识产权、履约成果、素材授权和第三方侵权责任。
10. 保密、个人信息、数据安全、数据使用和对外披露。
11. 资质、权利来源和合法合规承诺。
12. 转让、分包、不可抗力、通知送达和争议解决。
13. 正文、附件、订单等文件的效力顺序。
14. 主体、金额、日期、期限、付款方式等是否前后矛盾。
15. 根据合同类型应当关注的其他行业合规事项。

审查原则：
1. 只能依据合同原文，不得虚构未载明的事实、法律规定或交易背景。
2. 每个风险点必须引用合同原文，并注明条款或位置。
3. 无法确定的信息标记为“需人工确认”，不得自行推断。
4. 不要因为合同未包含与交易无关的条款而机械提示风险。
5. 区分“不利条款”“表述不清”“前后冲突”“关键条款缺失”和“合规风险”。
6. 修改建议必须具体；建议增删或替换时，尽量提供可直接使用的条款文本。
7. 对金额、日期、比例、付款周期、违约金和责任上限等关键信息进行交叉核对。
8. 无法判断我方身份时进行中立审查，并将立场相关事项列入需确认事项。
9. 不得为了凑数生成风险点；没有实质风险时，risk_points 返回空数组。

风险等级：
- high：可能导致合同无效、重大付款损失、无限或显著过重责任、核心权利丧失、严重合规责任，或争议后难以救济。
- medium：可能影响履约、验收、结算、解除、举证或增加争议成本，签署前应修改或确认。
- low：主要是文字、格式或一般完整性问题，不直接造成重大损失，但建议优化。

输出严格遵循以下 JSON 格式，不输出 Markdown、注释或任何其他内容：
{
  "contract_type": "合同类型",
  "our_party": "识别到的我方名称及甲方/乙方身份；无法判断则写未明确",
  "counterparty": "交易对方名称；无法识别则写未识别",
  "transaction_summary": "交易内容、期限、金额和主要履行方式概括",
  "risk_level": "high | medium | low",
  "summary": "整体审查结论，不超过三句话",
  "risk_points": [
    {
      "id": 1,
      "category": "主体/履约/验收/付款/期限/违约/解除/知识产权/保密与数据/争议解决/其他",
      "issue_type": "不利条款/表述不清/前后冲突/关键条款缺失/合规风险",
      "title": "风险标题",
      "location": "条款编号或具体位置",
      "clause_excerpt": "对应合同原文；属于缺失条款时写未约定",
      "risk_level": "high | medium | low",
      "reason": "风险原因及不利影响",
      "potential_consequence": "可能产生的实际后果",
      "recommendation": "具体处理建议",
      "suggested_revision": "建议修改后的条款文字；不适用时为空字符串"
    }
  ],
  "internal_conflicts": [
    {
      "locations": ["冲突位置一", "冲突位置二"],
      "description": "前后不一致的具体内容",
      "recommendation": "统一方式"
    }
  ],
  "items_to_confirm": [
    {
      "item": "需要业务或法务确认的事项",
      "reason": "无法直接从合同判断的原因"
    }
  ],
  "recommendation": "签署建议及前置条件"
}"""

# 保留旧名称，兼容可能直接导入 SYSTEM_PROMPT 的调用方。
SYSTEM_PROMPT = TEMPLATE_SYSTEM_PROMPT


def analyze_with_llm(
    template_text: str,
    contract_text: str,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    review_mode: str = "template",
) -> dict:
    """
    使用 LLM 分析模版与回签合同的差异。

    Args:
        template_text: 模版全文（段落用换行连接）
        contract_text: 回签合同全文
        api_key: 可选，覆盖 config 中的值
        base_url: 可选，覆盖 config 中的值
        model: 可选，覆盖 config 中的值
        review_mode: template=模版比对；non_template=无模版全文风险初审

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

    if review_mode == "template":
        system_prompt = TEMPLATE_SYSTEM_PROMPT
        user_message = f"""【合同模版】
{template_text}

【乙方回签合同】
{contract_text}

请按要求分析并输出 JSON。"""
    elif review_mode == "non_template":
        system_prompt = NON_TEMPLATE_SYSTEM_PROMPT
        user_message = f"""【审查立场】
我方名称及身份：未指定。请根据合同内容尽力识别；无法可靠判断时进行中立审查。

【合同全文】
{contract_text}

请严格按照系统要求完成非模版合同初审，并只输出 JSON。"""
    else:
        return {"error": f"不支持的 LLM 审核模式：{review_mode}"}

    try:
        response = client.chat.completions.create(
            model=mdl,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = response.choices[0].message.content
        return json.loads(raw)
    except Exception as e:
        return {"error": f"LLM 调用失败：{e}"}

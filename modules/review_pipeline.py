"""
合同审核核心流水线（共享模块）

`dingtalk/approval_handler.py`（生产钉钉审批流程）和 `test_dingtalk_flow.py`
（本地测试网页）都调用这里的同一套逻辑，避免两处代码各自维护一份、
修复效果不同步（改了测试工具但生产环境没同步生效）的问题。

对外主要入口：
- find_best_template()        模版匹配
- extract_contract_meta()     合同关键信息提取（正则兜底 + 关键词 LLM，不做硬截断）
- run_review()                 执行完整审核流程，返回 ReviewOutcome
- format_comment()             把审核结果格式化为钉钉评论文本（不做 [:5] 截断）
- generate_word_report_file()  生成完整版 Word 报告（供截断兜底/附件上传使用）
"""
import difflib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import List, Optional, Tuple

import config as app_config
from modules.extractor import extract_text
from modules.differ import compute_diff, DiffResult
from modules.date_checker import check_dates, DateCheckResult
from modules.email_checker import check_emails, EmailCheckResult
from modules.company_lookup import lookup_establishment_date
from modules.llm_analyzer import analyze_with_llm

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_MATCH_THRESHOLD = 0.60
_QUICK_MATCH_PARA_COUNT = 100

# 评论文本安全长度阈值：钉钉评论字段长度限制目前没有官方公开的精确值，
# 这里保守取值，超出部分会被换成"详见附件报告"的提示，而不是硬截断丢信息。
COMMENT_SAFE_LENGTH = 5000


# ─────────────────────────────────────────────
# 1. 模版匹配
# ─────────────────────────────────────────────
def find_best_template(
    contract_paragraphs: List[str],
    templates_dir: Optional[str] = None,
    threshold: float = DEFAULT_TEMPLATE_MATCH_THRESHOLD,
) -> Optional[Tuple[str, List[str], float]]:
    """
    在模版库中寻找与回签合同最相似的模版。
    返回 (模版路径, 模版段落列表, 相似度)；未找到符合阈值的模版则返回 None。
    """
    templates_dir = templates_dir or app_config.TEMPLATES_DIR
    tmpl_dir = Path(templates_dir)
    if not tmpl_dir.exists():
        logger.warning("模版目录不存在: %s", templates_dir)
        return None

    c_text = " ".join(contract_paragraphs[:_QUICK_MATCH_PARA_COUNT])
    best_score = 0.0
    best_path = None
    best_paras: List[str] = []

    for tmpl_path in tmpl_dir.rglob("*"):
        if tmpl_path.suffix.lower() not in (".docx", ".pdf"):
            continue
        try:
            tmpl_paras = extract_text(str(tmpl_path))
        except Exception:
            continue
        if not tmpl_paras:
            continue

        t_text = " ".join(tmpl_paras[:_QUICK_MATCH_PARA_COUNT])
        score = difflib.SequenceMatcher(None, t_text, c_text, autojunk=False).ratio()
        if score > best_score:
            best_score, best_path, best_paras = score, tmpl_path, tmpl_paras

    if best_score >= threshold and best_path:
        return str(best_path), best_paras, best_score
    return None


# ─────────────────────────────────────────────
# 2. 关键信息提取（正则兜底 + 关键词 LLM，不做硬截断）
# ─────────────────────────────────────────────
_PARTY_LINE_PATTERNS = {
    "party_a": re.compile(r"^\s*甲\s*方(?:名称|全称)?(?:\s*[（(][^）)]{0,30}[）)])?\s*[：:]\s*(.+)$"),
    "party_b": re.compile(r"^\s*乙\s*方(?:名称|全称)?(?:\s*[（(][^）)]{0,30}[）)])?\s*[：:]\s*(.+)$"),
}

# 用于挑选"值得连同文档开头一起发给 LLM"的关键段落，避免因位置靠后被截断丢弃
_META_KEYWORDS = ("甲方", "乙方", "签订", "签署", "生效", "终止", "期限", "合同编号", "金额", "日期")

# 元信息字段全集，缺失时统一置 None（而不是拼进一句人类可读的占位文本），
# 以免污染下游 check_dates 等需要真实日期值的逻辑
_META_FIELDS = (
    "party_a", "party_b", "signing_date", "effective_date",
    "expiry_date", "contract_amount", "contract_type",
)


def _regex_extract_parties(paragraphs: List[str]) -> dict:
    """
    在全部段落（不截断）中用正则直接匹配"甲方：xxx" / "乙方：xxx" 这类行，
    作为不依赖 LLM 的兜底/校验数据源。免费、确定性强，不受篇幅位置影响。
    """
    result: dict = {}
    for p in paragraphs:
        text = p.strip()
        for key, pat in _PARTY_LINE_PATTERNS.items():
            if key in result:
                continue
            m = pat.match(text)
            if not m:
                continue
            val = m.group(1).strip().strip("：:，,。 ")
            # 排除仍是空白占位符本身的情况（如"甲方：___"未真正填写）
            if val and not re.fullmatch(r"[_＿\s]*", val):
                result[key] = val
    return result


# 数字与"年/月/日"之间允许任意空白（含全角空格），兼容常见的排版留白，
# 如"自  2026  年  5  月  15  日开始至  2027  年  5  月  15  日截止"
_DATE_NUM = r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?"

# 起止日期中间常插入"（含当日）""（含）"这类括号补充说明，必须允许跳过，
# 否则"自X日起（含当日）至Y日止"会因为括号卡在"起"和"至"之间而整条匹配失败
_PAREN_NOTE = r"(?:\s*[（(][^）)]{0,20}[）)])?"

# "合作期限：自X起至Y止"这类期限句式，"自/由"前缀设为可选，
# 以兼容"有效期：2026年5月15日至2027年5月15日"这种没有"自"字的写法；
# "至/到"是范围连接词，作为必需的强锚点，避免误配不相关的两个日期
_PERIOD_RE = re.compile(
    r"(?:[自由]\s*)?" + _DATE_NUM + r"\s*(?:起|开始)?" + _PAREN_NOTE + r"\s*"
    r"(?:至|到|——|—|-|~)\s*" + _DATE_NUM + r"\s*(?:止|截止|结束|为止)?"
)

# "签订日期：X" / "本合同于X签订" 等常见签订日期句式
_SIGNING_RE = re.compile(
    r"(?:签订日期|签署日期|签约日期|订立日期)\s*[：:]?\s*" + _DATE_NUM
)
_SIGNING_INLINE_RE = re.compile(
    r"于\s*" + _DATE_NUM + r"\s*(?:签订|签署|签约)"
)

# 落款（签字盖章栏）里的日期行，如「日期：  2026年9月1日」。
# 中文合同大多不写"签订日期"字样，只在甲乙双方盖章处留一个"日期："
_SIGN_DATE_RE = re.compile(
    r"(?:签署日期|签字日期|签章日期|签订日期|日\s*期)\s*[：:]?\s*" + _DATE_NUM
)

# 附件标题。落款日期只在附件之前找，避免误取附件（服务清单、报价单等）里的日期。
# 限定为短段落，正文里"详见附件一"这种句子不会以"附件"开头，不受影响
_ATTACHMENT_HEAD_RE = re.compile(r"^附\s*件\s*[一二三四五六七八九十\d]*\s*[：:、．.]?")
_ATTACHMENT_HEAD_MAX_LEN = 30

# 落款区标记：签章栏里那种只有"甲方：""乙方：""（盖章）"的独立短行
_SIGN_MARKER_RE = re.compile(
    r"^(?:[甲乙]\s*方\s*[：:]?"
    r"|[（(]\s*(?:盖章|公章|签章|签字|签字并盖章|盖章并签字)\s*[）)]"
    r"|(?:盖章|公章|签章)\s*[：:]?)$"
)

# 找不到落款标记时，退而只在正文最后这么多段里找日期
_SIGN_TAIL_PARAS = 15


def _body_end_index(paragraphs: List[str]) -> int:
    """返回正文结束位置（第一个附件标题所在下标），没有附件则返回段落总数"""
    for i, para in enumerate(paragraphs):
        text = para.strip()
        if len(text) <= _ATTACHMENT_HEAD_MAX_LEN and _ATTACHMENT_HEAD_RE.match(text):
            return i
    return len(paragraphs)


def _signature_region(paragraphs: List[str]) -> List[str]:
    """
    截出落款区段落：正文（附件之前）里最后一个"甲方/乙方/（盖章）"独立短行之后的部分。

    不能在整篇正文里搜"日期："——条款里的付款日期、结算日期、送达日期都会命中。
    找不到落款标记时退回正文末尾若干段，落款总在正文最后。
    """
    body = paragraphs[:_body_end_index(paragraphs)]
    last_marker = -1
    for i, para in enumerate(body):
        if _SIGN_MARKER_RE.match(para.strip()):
            last_marker = i
    if last_marker >= 0:
        return body[last_marker:]
    return body[-_SIGN_TAIL_PARAS:]


def _find_signing_date_in_body(paragraphs: List[str]) -> Optional[str]:
    """
    在落款区查找签署日期。

    中文合同的落款通常长这样，位于正文最后、附件之前：
        甲方：
        （盖章）
        日期：  2026年9月1日
    甲乙双方各有一个日期行，这里取最后一个（通常是后签的一方，也即合同实际
    成立之日）。日期留空未填时匹配不到，由上层回退为提交审核的系统日期。
    """
    last = None
    for para in _signature_region(paragraphs):
        for m in _SIGN_DATE_RE.finditer(para):
            iso = _to_iso(m.group(1), m.group(2), m.group(3))
            if iso:
                last = iso
    return last


def _to_iso(y, m, d) -> Optional[str]:
    try:
        return _date(int(y), int(m), int(d)).isoformat()
    except ValueError:
        return None


def _regex_extract_dates(paragraphs: List[str]) -> dict:
    """
    在全文中用正则匹配常见的"自X年X月X日起至X年X月X日止"合作期限句式，
    以及"签订日期：X"句式，作为不依赖 LLM 的日期兜底提取。

    与 party 提取一样，这里全文扫描不做截断；数字与年/月/日之间允许
    任意空白，避免因为排版留白（如"2026  年  5  月  15  日"）而提取失败。
    """
    result: dict = {}
    full_text = "\n".join(paragraphs)

    m = _PERIOD_RE.search(full_text)
    if m:
        eff = _to_iso(m.group(1), m.group(2), m.group(3))
        exp = _to_iso(m.group(4), m.group(5), m.group(6))
        if eff:
            result["effective_date"] = eff
        if exp:
            result["expiry_date"] = exp

    # 优先用明确写了"签订日期"字样的句式，其次退回落款区的"日期："行
    m2 = _SIGNING_RE.search(full_text) or _SIGNING_INLINE_RE.search(full_text)
    if m2:
        sign = _to_iso(m2.group(1), m2.group(2), m2.group(3))
        if sign:
            result["signing_date"] = sign
    else:
        sign = _find_signing_date_in_body(paragraphs)
        if sign:
            result["signing_date"] = sign

    return result


def _build_llm_meta_context(paragraphs: List[str], max_chars: int = 6000) -> str:
    """
    构造发给 LLM 的合同文本片段：文档开头节选 + 全文中所有含关键词的段落。

    之前的实现直接截断 contract_text[:3000]，如果"甲方："信息所在段落
    在文档中出现得比较靠后（例如长合同、或表格排在后面），就会被完全排除在
    发给 LLM 的内容之外，导致 LLM 永远看不到这段文字。
    这里改为始终收纳全文中含关键词的段落，不受它们在文档中的位置影响。
    """
    full_text = "\n".join(paragraphs)
    head = full_text[:2000]
    keyword_paras = [p for p in paragraphs if any(kw in p for kw in _META_KEYWORDS)]
    keyword_block = "\n".join(keyword_paras[:60])
    combined = (
        f"【文档开头节选】\n{head}\n\n"
        f"【全文中包含关键字段的段落（不代表原文只有这些内容）】\n{keyword_block}"
    )
    return combined[:max_chars]


def _extract_meta_with_llm(paragraphs: List[str], api_key: Optional[str]) -> dict:
    """用 LLM 从合同关键片段中提取结构化关键信息"""
    if not api_key:
        return {}

    try:
        from modules.openai_client import create_openai_client
        client = create_openai_client(api_key=api_key, base_url=app_config.LLM_BASE_URL)
        context = _build_llm_meta_context(paragraphs)
        prompt = (
            "从以下合同文本片段中提取关键信息，以 JSON 格式输出，不要输出其他内容：\n"
            '{"party_a": "甲方全称", '
            '"party_b": "乙方全称", '
            '"signing_date": "合同落款日期YYYY-MM-DD或null", '
            '"effective_date": "合作期限起始日期YYYY-MM-DD或null", '
            '"expiry_date": "合作期限终止日期YYYY-MM-DD或null", '
            '"contract_amount": "合同金额（含单位）或null", '
            '"contract_type": "合同类型简述"}\n\n'
            "注意：文本片段已包含全文中所有含甲方/乙方/日期等关键词的段落，"
            "并不代表原合同只有这么多内容，请据此尽力提取。\n\n"
            f"合同文本片段：\n{context}"
        )
        resp = client.chat.completions.create(
            model=app_config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.warning("LLM 元信息提取失败: %s", e)
        return {}


def extract_contract_meta(paragraphs: List[str], api_key: Optional[str] = None) -> dict:
    """
    提取合同关键信息（甲方/乙方/落款日期/期限起始/期限终止/金额/合同类型）。

    两层策略：
    1. 正则快速提取（全文扫描，不截断）：甲方/乙方 + 合作期限/签订日期，
       作为不依赖 LLM 的兜底/校验数据源
    2. LLM 提取：文档开头 + 全文关键词段落拼接后交给 LLM，避免关键信息
       因为在文档靠后位置而被截断丢弃
    3. 合并：LLM 结果优先，为空时用正则结果兜底；两者都为空的字段统一置 None
       （由上层展示逻辑负责显示"未识别，请人工核对"，不污染日期解析等下游逻辑）
    """
    regex_result: dict = {}
    regex_result.update(_regex_extract_parties(paragraphs))
    regex_result.update(_regex_extract_dates(paragraphs))
    llm_result = _extract_meta_with_llm(paragraphs, api_key)

    merged: dict = dict(llm_result) if llm_result else {}
    for key, val in regex_result.items():
        existing = merged.get(key)
        if not existing or str(existing).strip().lower() in ("null", "none", ""):
            merged[key] = val

    for key in _META_FIELDS:
        val = merged.get(key)
        if not val or str(val).strip().lower() in ("null", "none"):
            merged[key] = None

    return merged


# ─────────────────────────────────────────────
# 3. 完整审核流程
# ─────────────────────────────────────────────
@dataclass
class ReviewOutcome:
    is_template: bool
    template_name: str
    template_path: str
    similarity: float
    diff_result: DiffResult
    meta: dict
    company_result: dict
    date_result: DateCheckResult
    llm_result: Optional[dict]
    comment_text: str
    has_critical: bool
    has_warnings: bool
    word_report_path: Optional[str] = None
    email_result: Optional[EmailCheckResult] = None


def run_review(
    contract_name: str,
    contract_paragraphs: List[str],
    llm_api_key: Optional[str] = None,
    template_match_threshold: float = DEFAULT_TEMPLATE_MATCH_THRESHOLD,
    templates_dir: Optional[str] = None,
    manual_meta: Optional[dict] = None,
    generate_report: bool = True,
) -> ReviewOutcome:
    """
    执行完整审核流程：
      提取关键信息 → 模版匹配 → 差异比对/LLM初审 → 乙方成立日期查询
      → 日期合规核查 → 生成评论文本 → （可选）生成 Word 报告

    manual_meta: 人工手填字段（优先于自动提取结果），如
                 {"party_b": "...", "signing_date": "2024-01-01"}
    """
    contract_full_text = "\n".join(contract_paragraphs)
    effective_key = llm_api_key or app_config.LLM_API_KEY

    meta = extract_contract_meta(contract_paragraphs, effective_key)
    if manual_meta:
        for k, v in manual_meta.items():
            if v:
                meta[k] = v

    # 不少合同落款处不写日期（或留空未填），此时按"提交合同审核系统的日期"为准。
    # 同时记下这是推定值，好让日期核查区分文案表述、并跳过对推定值做陈旧检查。
    signing_assumed = not meta.get("signing_date")
    if signing_assumed:
        meta["signing_date"] = _date.today().isoformat()
    meta["signing_date_source"] = "system" if signing_assumed else "document"

    tmpl_match = find_best_template(
        contract_paragraphs, templates_dir=templates_dir, threshold=template_match_threshold
    )
    is_template = tmpl_match is not None
    template_name = ""
    template_path = ""
    similarity = 0.0
    llm_result = None

    if is_template:
        template_path, tmpl_paras, similarity = tmpl_match
        template_name = Path(template_path).stem
        diff_result = compute_diff(tmpl_paras, contract_paragraphs)
        logger.info("匹配到模版 %s，相似度 %.1f%%", template_name, similarity * 100)
        if diff_result.needs_llm and effective_key:
            llm_result = analyze_with_llm(
                template_text="\n".join(tmpl_paras),
                contract_text=contract_full_text,
                api_key=effective_key,
                review_mode="template",
            )
    else:
        logger.info("未匹配到模版，走 LLM 全文初审")
        diff_result = DiffResult()
        if effective_key:
            llm_result = analyze_with_llm(
                template_text="",
                contract_text=contract_full_text,
                api_key=effective_key,
                review_mode="non_template",
            )

    # 邮箱核查与是否匹配模版无关：模版/非模版合同都要查跨主体串用
    email_result = check_emails(contract_paragraphs, meta.get("party_a"))

    party_b = meta.get("party_b") or ""
    # lookup_establishment_date 内部读取模块级全局 config.LLM_API_KEY，
    # 这里临时切换，用完立刻还原，避免多请求并发时互相污染。
    prev_key = app_config.LLM_API_KEY
    if effective_key:
        app_config.LLM_API_KEY = effective_key
    try:
        company_result = lookup_establishment_date(party_b)
    finally:
        app_config.LLM_API_KEY = prev_key

    date_result = check_dates(
        signing_date=meta.get("signing_date"),
        effective_date=meta.get("effective_date"),
        expiry_date=meta.get("expiry_date"),
        establishment_date=company_result.get("establishment_date"),
        party_b_name=party_b,
        signing_date_assumed=signing_assumed,
    )

    word_report_path = None
    if generate_report:
        try:
            word_report_path = generate_word_report_file(
                diff_result=diff_result,
                template_name=template_name or "（无匹配模版，AI全文初审）",
                contract_name=contract_name,
                llm_result=llm_result,
            )
        except Exception as e:
            logger.warning("生成 Word 报告失败（不影响评论文本）：%s", e)

    comment_text = format_comment(
        contract_name=contract_name,
        is_template=is_template,
        template_name=template_name,
        similarity=similarity,
        diff_result=diff_result,
        meta=meta,
        date_result=date_result,
        company_result=company_result,
        llm_result=llm_result,
        report_available=bool(word_report_path),
        email_result=email_result,
    )

    has_critical = (
        date_result.has_errors
        or diff_result.tampered_count > 0
        or bool(email_result and email_result.has_errors)
    )
    has_warnings = (
        date_result.has_warnings
        or bool(email_result and email_result.has_warnings)
        # 乙方成立日期查不到时仍需人工核实主体资格；提示文案只在「乙方主体核查」
        # 一节出现，但告警标志依然要立起来
        or not company_result.get("establishment_date")
        or diff_result.removed_count > 0
        or diff_result.added_count > 0
        or bool(llm_result and llm_result.get("risk_level") in ("high", "medium"))
    )

    return ReviewOutcome(
        is_template=is_template,
        template_name=template_name,
        template_path=template_path,
        similarity=similarity,
        diff_result=diff_result,
        meta=meta,
        company_result=company_result,
        date_result=date_result,
        llm_result=llm_result,
        comment_text=comment_text,
        has_critical=has_critical,
        has_warnings=has_warnings,
        word_report_path=word_report_path,
        email_result=email_result,
    )


# ─────────────────────────────────────────────
# 4. 评论文本格式化（不做 [:5] 截断，超长时提示查看附件）
# ─────────────────────────────────────────────
def _truncate_with_note(text: str, max_len: int, note: str) -> str:
    """若文本超过 max_len，从末尾截断并追加提示，尽量在换行处截断以保持可读性"""
    if len(text) <= max_len:
        return text
    cut = text.rfind("\n", 0, max_len - len(note) - 10)
    if cut <= 0:
        cut = max_len - len(note) - 10
    return text[:cut] + "\n\n" + note


def format_comment(
    contract_name: str,
    is_template: bool,
    template_name: str,
    similarity: float,
    diff_result: DiffResult,
    meta: dict,
    date_result: DateCheckResult,
    company_result: dict,
    llm_result: Optional[dict],
    max_comment_length: int = COMMENT_SAFE_LENGTH,
    report_available: bool = True,
    email_result: Optional[EmailCheckResult] = None,
) -> str:
    """
    将所有审核结果格式化为审批评论文本（纯文本，支持换行）。

    与旧版的区别：不再对修改条款 / LLM 风险条款做 [:5] 硬截断，全部列出；
    只有在拼出来的总文本超过安全长度时才做安全阀截断，并提示"详见附件报告"。
    """
    lines = []
    lines.append("=" * 45)
    lines.append("【AI 合同审核报告】")
    lines.append("=" * 45)

    # ── 基本信息 ──
    party_a = meta.get("party_a") or "（未识别，请人工核对）"
    party_b = meta.get("party_b") or "（未识别，请人工核对）"
    contract_type = meta.get("contract_type") or ""
    amount = meta.get("contract_amount") or ""
    lines.append(f"合同名称：{contract_name}")
    if contract_type:
        lines.append(f"合同类型：{contract_type}")
    lines.append(f"甲方：{party_a}")
    lines.append(f"乙方：{party_b}")
    if amount:
        lines.append(f"合同金额：{amount}")
    lines.append("")

    # ── 模版比对结果 ──
    if is_template:
        lines.append("▍模版比对")
        lines.append(f"  匹配模版：{template_name}（相似度 {similarity:.1%}）")
        if not any([diff_result.tampered_count, diff_result.removed_count, diff_result.added_count]):
            lines.append("  ✅ 模版条款未发现修改，仅有正常填写字段")
        else:
            if diff_result.tampered_count:
                lines.append(f"  🚨 疑似修改条款：{diff_result.tampered_count} 处")
            if diff_result.removed_count:
                lines.append(f"  ⚠️ 模版内容缺失：{diff_result.removed_count} 处")
            if diff_result.added_count:
                lines.append(f"  ➕ 模版外新增内容：{diff_result.added_count} 处")
        lines.append(f"  正常填写字段：{diff_result.filled_count} 处")

        # 修改详情：全部列出，不再只显示前5条
        tampered_items = [i for i in diff_result.items if i.type.value == "tampered"]
        if tampered_items:
            lines.append("")
            lines.append(f"  修改详情（共 {len(tampered_items)} 条）：")
            for n, item in enumerate(tampered_items, 1):
                lines.append(f"    {n}. 原文：{(item.template_text or '')[:120]}")
                lines.append(f"       改为：{(item.contract_text or '')[:120]}")

        removed_items = [i for i in diff_result.items if i.type.value == "removed"]
        if removed_items:
            lines.append("")
            lines.append(f"  内容缺失详情（共 {len(removed_items)} 条）：")
            for n, item in enumerate(removed_items, 1):
                lines.append(f"    {n}. {(item.template_text or '')[:120]}")

        added_items = [i for i in diff_result.items if i.type.value == "added"]
        if added_items:
            lines.append("")
            lines.append(f"  模版外新增详情（共 {len(added_items)} 条）：")
            for n, item in enumerate(added_items, 1):
                lines.append(f"    {n}. {(item.contract_text or '')[:120]}")
    else:
        lines.append("▍合同类型")
        lines.append("  ⚠️ 未匹配到模版，作为非模版合同进行 AI 初审")

    lines.append("")

    # ── LLM 初审结果（非模版合同 / 低置信度模版合同） ──
    if llm_result and "error" not in llm_result:
        risk_map = {"high": "高风险 🔴", "medium": "中等风险 🟡", "low": "低风险 🟢"}
        risk = llm_result.get("risk_level", "")
        lines.append("▍AI 初审结果")
        if risk:
            lines.append(f"  整体风险等级：{risk_map.get(risk, risk)}")
        summary = llm_result.get("summary", "")
        if summary:
            lines.append(f"  摘要：{summary}")
        risk_points = llm_result.get("risk_points", [])
        if risk_points:
            lines.append(f"  风险条款（共 {len(risk_points)} 条）：")
            for idx, rp in enumerate(risk_points, 1):
                if not isinstance(rp, dict):
                    continue
                level_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(
                    rp.get("risk_level", ""), "•"
                )
                title = rp.get("title") or f"风险事项 {idx}"
                lines.append(f"    {level_icon} {idx}. {title}")
                location = rp.get("location", "")
                if location:
                    lines.append(f"       位置：{location[:100]}")
                excerpt = rp.get("clause_excerpt", "")
                if excerpt:
                    lines.append(f"       原文：{excerpt[:180]}")
                reason = rp.get("reason", "")
                if reason:
                    lines.append(f"       原因：{reason[:200]}")
                advice = rp.get("recommendation", "")
                if advice:
                    lines.append(f"       建议：{advice[:200]}")
        rec = llm_result.get("recommendation", "")
        if rec:
            lines.append(f"  建议：{rec}")
        lines.append("")

    # ── 邮箱核查（仅模版合同） ──
    if email_result is not None:
        lines.append("▍邮箱核查")
        lines.append(email_result.to_text())
        lines.append("")

    # ── 日期核查 ──
    lines.append("▍日期核查")
    lines.append(date_result.to_text())
    lines.append("")

    # ── 乙方主体核查 ──
    lines.append("▍乙方主体核查")
    est_date = company_result.get("establishment_date")
    source = company_result.get("source", "")
    if est_date:
        lines.append(f"  成立日期：{est_date}（来源：{source}）")
    else:
        lines.append(f"  ⚠️ 未查到成立日期：{source}")
    lines.append("")

    # ── 综合结论 ──
    lines.append("▍综合结论")
    has_risk = (
        (is_template and (diff_result.tampered_count or diff_result.removed_count or diff_result.added_count))
        or date_result.has_errors
        or date_result.has_warnings
        or (email_result and (email_result.has_errors or email_result.has_warnings))
        or not company_result.get("establishment_date")
        or (llm_result and llm_result.get("risk_level") in ("high", "medium"))
    )
    if has_risk:
        lines.append("  ⚠️ 发现需关注的问题，请法务人员重点审查上述标注条目")
    else:
        lines.append("  ✅ 自动审查未发现明显问题，请法务最终确认后审批")

    lines.append("=" * 45)
    lines.append("（本报告由 AI 自动生成，仅供参考，最终以法务审核意见为准）")

    full_text = "\n".join(lines)

    if len(full_text) > max_comment_length:
        if report_available:
            note = "⚠️ 以上内容因篇幅过长已截断，完整逐条明细请查看附件《AI合同审核报告》Word 文档。"
        else:
            note = "⚠️ 以上内容因篇幅过长已截断，建议人工在系统中核对完整合同内容。"
        full_text = _truncate_with_note(full_text, max_comment_length, note)

    return full_text


# ─────────────────────────────────────────────
# 5. Word 报告生成（供截断兜底 / 附件上传使用）
# ─────────────────────────────────────────────
def generate_word_report_file(
    diff_result: DiffResult,
    template_name: str,
    contract_name: str,
    llm_result: Optional[dict] = None,
    output_path: Optional[str] = None,
) -> str:
    """生成完整版 Word 审核报告，返回文件路径"""
    from modules.reporter import generate_word_report

    if not output_path:
        tmp_dir = tempfile.mkdtemp(prefix="contract_report_")
        safe_name = re.sub(r'[\\/:*?"<>|]', "_", contract_name or "合同")[:60]
        output_path = os.path.join(tmp_dir, f"AI合同审核报告_{safe_name}.docx")

    generate_word_report(
        diff_result=diff_result,
        template_name=template_name,
        contract_name=contract_name,
        output_path=output_path,
        llm_result=llm_result,
    )
    return output_path

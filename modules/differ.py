"""
差异比对核心模块

算法说明：
1. 将模版和回签合同分别提取为段落列表
2. 对模版段落中的"空白指示符"（下划线、空括号等）做标准化处理
3. 使用 SequenceMatcher 对齐段落，得到 equal/replace/insert/delete 分块
4. 对判定为"replace"的分块，再用归一化文本二次比对（见 _diff_replace_block），
   先找出块内真正的插入/删除段落，避免"块内多一段导致后续全部错位配对"的问题
5. 对每处差异判断类型：
   - FILLED   : 模版段落含空白指示符 或 回签内容为"纯新增"（未删改模版原文） → 正常
   - TAMPERED : 回签内容删除/替换了模版原有文字，且不满足上述填写条件 → 告警（面向用户显示为"修改"）
   - ADDED    : 回签版本新增了模版中没有的段落 → 告警
   - REMOVED  : 模版段落在回签版本中消失 → 告警
   - UNCHANGED: 完全一致 → 正常
"""
import html
import re
import difflib
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple


# ─────────────────────────────────────────────
# 空白指示符模式（中文合同常见占位形式）
# ─────────────────────────────────────────────
_BLANK_PATTERNS = [
    r"_{3,}",              # 三条或以上下划线
    r"【\s*】",             # 空白中文方括号
    r"（\s*）",             # 空白中文圆括号
    r"\(\s*\)",            # 空白英文圆括号（仅括号内为空）
    r"\[\s*\]",            # 空白英文方括号
    r"〔\s*〕",             # 旧式空白括号
    r"年\s{1,8}月\s{1,8}日",   # 日期空白：年  月  日
    r"[：:]\s{4,}",        # 冒号后连续空格（字段值留空）
    r"（以下称?[""']?\s*[""']?）",   # 简称括号为空
    r"□\s*□",             # 复选框
]
_BLANK_RE = re.compile("|".join(_BLANK_PATTERNS))


def has_blank_indicator(text: str) -> bool:
    """判断段落是否含有空白指示符"""
    return bool(_BLANK_RE.search(text))


def _normalize_for_matching(text: str) -> str:
    """
    将空白指示符统一替换为占位符，用于相似度比较时消除"空白被填充"的干扰。
    同时折叠连续空白，去除首尾空格。
    """
    result = _BLANK_RE.sub("__BLANK__", text)
    result = re.sub(r"\s+", " ", result).strip()
    return result


def is_pure_addition(template_text: str, contract_text: str) -> bool:
    """
    判断回签文本相对模版是否为"纯新增"（只追加内容，未删除/替换模版原文）。

    填写行为：模版原有文字保持不变，只在空白处追加内容。
      如：「甲方：」→「甲方：北京XX公司」
          「邮箱后缀为，」→「邮箱后缀为 @xxx.com，」
    修改行为：删除或替换了模版原有文字。
      如：「付款周期为90日」→「付款周期为180日」

    安全阀（保守处理）：若插入内容包含中文句末标点（。！？；），
    可能是新增了整条条款，退回由人工核查。
    注意：不包含英文点号，以免误伤邮箱/网址（如 @xxx.com）。
    """
    t = re.sub(r"\s+", "", template_text)
    c = re.sub(r"\s+", "", contract_text)
    if t == c:
        return True
    if not t:
        return False

    matcher = difflib.SequenceMatcher(None, t, c, autojunk=False)
    inserted_chunks = []
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op in ("delete", "replace"):
            return False
        if op == "insert":
            inserted_chunks.append(c[j1:j2])

    inserted_text = "".join(inserted_chunks)
    if not inserted_text:
        return False
    # 只用中文句末标点做安全阀，不包含英文句号（.）
    if any(ch in "。！？；" for ch in inserted_text):
        return False
    return True


# ─────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────
class DiffType(str, Enum):
    UNCHANGED = "unchanged"
    FILLED    = "filled"
    TAMPERED  = "tampered"
    ADDED     = "added"
    REMOVED   = "removed"


@dataclass
class DiffItem:
    type: DiffType
    template_text: Optional[str]
    contract_text: Optional[str]
    line_index: int
    # HTML 高亮版本（仅 TAMPERED 时有值）
    template_html: str = ""
    contract_html: str = ""


@dataclass
class DiffResult:
    items: List[DiffItem] = field(default_factory=list)
    similarity: float = 0.0
    filled_count: int = 0
    tampered_count: int = 0
    added_count: int = 0
    removed_count: int = 0

    @property
    def alert_count(self) -> int:
        return self.tampered_count + self.added_count + self.removed_count

    @property
    def needs_llm(self) -> bool:
        """相似度过低，或存在大量告警时，建议启用 LLM 辅助分析"""
        return self.similarity < 0.5 or self.tampered_count > 5


# ─────────────────────────────────────────────
# 字符级高亮（用于 TAMPERED 段落显示）
# ─────────────────────────────────────────────
def _char_diff_html(old: str, new: str) -> Tuple[str, str]:
    """
    返回 (old_html, new_html)：对删除/修改/新增字符做 HTML 高亮标注。
    """
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    old_html = ""
    new_html = ""
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            escaped = html.escape(old[i1:i2])
            old_html += escaped
            new_html += escaped
        elif op == "replace":
            old_html += f'<span class="diff-del">{html.escape(old[i1:i2])}</span>'
            new_html += f'<span class="diff-ins">{html.escape(new[j1:j2])}</span>'
        elif op == "delete":
            old_html += f'<span class="diff-del">{html.escape(old[i1:i2])}</span>'
        elif op == "insert":
            new_html += f'<span class="diff-ins">{html.escape(new[j1:j2])}</span>'
    return old_html, new_html


def _classify_pair(t: str, c: str) -> DiffItem:
    """
    对一组"应当对应"的模版段落 t 与回签段落 c 做最终分类（FILLED 或 TAMPERED）。
    line_index 由调用方统一补齐，这里先置 0。
    """
    if has_blank_indicator(t) or is_pure_addition(t, c):
        return DiffItem(type=DiffType.FILLED, template_text=t, contract_text=c, line_index=0)

    old_html, new_html = _char_diff_html(t, c)
    return DiffItem(
        type=DiffType.TAMPERED,
        template_text=t,
        contract_text=c,
        line_index=0,
        template_html=old_html,
        contract_html=new_html,
    )


def _diff_replace_block(
    template_paragraphs: List[str],
    contract_paragraphs: List[str],
    t_norm_slice: List[str],
    c_norm_slice: List[str],
    i1: int,
    j1: int,
) -> List[DiffItem]:
    """
    对顶层比对判定为"replace"的一整块段落做二次比对，避免"整体错位"问题。

    问题背景：如果直接按下标顺序把模版段落和回签段落一一配对，
    一旦回签合同在块内某处多出/少了一个段落（例如多打一个冒号导致
    段落被拆成两段），后面所有配对都会依次错位一位。

    这里在块内部用归一化文本再跑一次 SequenceMatcher，先找出块内
    真正的 insert/delete（多出来的/缺失的段落），只把真正一一对应的
    段落交给 _classify_pair 判定 FILLED/TAMPERED，从而避免级联错位。
    """
    items: List[DiffItem] = []
    inner_matcher = difflib.SequenceMatcher(None, t_norm_slice, c_norm_slice, autojunk=False)

    for op, ni1, ni2, nj1, nj2 in inner_matcher.get_opcodes():
        if op == "equal":
            for k in range(ni2 - ni1):
                items.append(DiffItem(
                    type=DiffType.UNCHANGED,
                    template_text=template_paragraphs[i1 + ni1 + k],
                    contract_text=contract_paragraphs[j1 + nj1 + k],
                    line_index=0,
                ))

        elif op == "replace":
            t_lines = template_paragraphs[i1 + ni1:i1 + ni2]
            c_lines = contract_paragraphs[j1 + nj1:j1 + nj2]
            max_len = max(len(t_lines), len(c_lines))
            for k in range(max_len):
                t = t_lines[k] if k < len(t_lines) else None
                c = c_lines[k] if k < len(c_lines) else None
                if t is not None and c is not None:
                    items.append(_classify_pair(t, c))
                elif t is not None:
                    items.append(DiffItem(type=DiffType.REMOVED, template_text=t, contract_text=None, line_index=0))
                else:
                    items.append(DiffItem(type=DiffType.ADDED, template_text=None, contract_text=c, line_index=0))

        elif op == "delete":
            for k in range(ni2 - ni1):
                items.append(DiffItem(
                    type=DiffType.REMOVED,
                    template_text=template_paragraphs[i1 + ni1 + k],
                    contract_text=None,
                    line_index=0,
                ))

        elif op == "insert":
            for k in range(nj2 - nj1):
                items.append(DiffItem(
                    type=DiffType.ADDED,
                    template_text=None,
                    contract_text=contract_paragraphs[j1 + nj1 + k],
                    line_index=0,
                ))

    return items


# ─────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────
def compute_diff(
    template_paragraphs: List[str],
    contract_paragraphs: List[str],
) -> DiffResult:
    """
    比较模版段落列表和回签合同段落列表，返回 DiffResult。
    """
    result = DiffResult()

    t_norm = [_normalize_for_matching(p) for p in template_paragraphs]
    c_norm = [_normalize_for_matching(p) for p in contract_paragraphs]

    matcher = difflib.SequenceMatcher(None, t_norm, c_norm, autojunk=False)
    result.similarity = matcher.ratio()

    idx = 0

    def _tally_and_append(item: DiffItem) -> None:
        nonlocal idx
        item.line_index = idx
        if item.type == DiffType.FILLED:
            result.filled_count += 1
        elif item.type == DiffType.TAMPERED:
            result.tampered_count += 1
        elif item.type == DiffType.ADDED:
            result.added_count += 1
        elif item.type == DiffType.REMOVED:
            result.removed_count += 1
        result.items.append(item)
        idx += 1

    for op, i1, i2, j1, j2 in matcher.get_opcodes():

        if op == "equal":
            for k in range(i2 - i1):
                _tally_and_append(DiffItem(
                    type=DiffType.UNCHANGED,
                    template_text=template_paragraphs[i1 + k],
                    contract_text=contract_paragraphs[j1 + k],
                    line_index=0,
                ))

        elif op == "replace":
            block_items = _diff_replace_block(
                template_paragraphs, contract_paragraphs,
                t_norm[i1:i2], c_norm[j1:j2],
                i1, j1,
            )
            for item in block_items:
                _tally_and_append(item)

        elif op == "delete":
            for k in range(i2 - i1):
                _tally_and_append(DiffItem(
                    type=DiffType.REMOVED,
                    template_text=template_paragraphs[i1 + k],
                    contract_text=None,
                    line_index=0,
                ))

        elif op == "insert":
            for k in range(j2 - j1):
                _tally_and_append(DiffItem(
                    type=DiffType.ADDED,
                    template_text=None,
                    contract_text=contract_paragraphs[j1 + k],
                    line_index=0,
                ))

    return result

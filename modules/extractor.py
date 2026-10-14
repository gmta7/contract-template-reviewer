"""
文档文本提取模块
支持 .docx / .doc / .pdf 三种格式
"""
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple


_STANDALONE_COLON_RE = re.compile(r"^[：:]+$")


def _append_logical_paragraphs(paragraphs: List[str], text: str) -> None:
    """
    将一个 Word 段落统一拆成逻辑行。

    Enter 会生成独立段落，Shift+Enter 会在同一段落中生成换行；
    在进入差异比对前统一成相同的列表结构，避免后续内容错位。
    同时忽略误输入的独立冒号段落，但保留“甲方：”等正常字段。
    """
    for line in text.splitlines():
        line = line.strip()
        if line and not _STANDALONE_COLON_RE.fullmatch(line):
            paragraphs.append(line)


def _iter_block_items(parent):
    """
    按文档真实顺序遍历段落和表格（而不是先取完所有正文段落再取所有表格）。

    docx 的 body 是一段混排的 XML 树，直接用 doc.paragraphs / doc.tables
    会丢失两者的相对顺序（表格内容永远被排到所有正文段落之后），
    导致"甲方/乙方"这类常见于表格中的信息在长文档中被错误地挪到末尾，
    进而在只读取文档前 N 字的场景下（如 LLM 关键信息提取）彻底丢失。

    这里改为直接遍历 body 的 XML 子节点，按出现顺序依次产出
    Paragraph 或 Table 对象，与人眼看到的阅读顺序一致。
    """
    from docx.document import Document as _DocumentObj
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table, _Cell
    from docx.text.paragraph import Paragraph

    if isinstance(parent, _DocumentObj):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        parent_elm = parent

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)


def extract_from_docx(file_path: str) -> List[str]:
    """从 .docx 文件中按真实文档顺序提取段落文本（含表格内容，支持嵌套表格）"""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(file_path)
    paragraphs: List[str] = []

    def _walk(parent):
        for block in _iter_block_items(parent):
            if isinstance(block, Paragraph):
                _append_logical_paragraphs(paragraphs, block.text)
            elif isinstance(block, Table):
                for row in block.rows:
                    for cell in row.cells:
                        _walk(cell)

    _walk(doc)
    return paragraphs


def extract_from_pdf(file_path: str) -> List[str]:
    """从 PDF 文件中提取文本行（优先 pdfplumber，失败则用 PyMuPDF）"""
    paragraphs: List[str] = []

    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    for line in text.split("\n"):
                        line = line.strip()
                        if line:
                            paragraphs.append(line)
        if paragraphs:
            return paragraphs
    except Exception:
        pass

    try:
        import fitz  # PyMuPDF（可选）
        doc = fitz.open(file_path)
        for page in doc:
            text = page.get_text("text")
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    paragraphs.append(line)
    except Exception:
        pass

    return paragraphs


def _libreoffice_candidates() -> List[str]:
    """收集本机可能的 LibreOffice 可执行文件路径"""
    names = ["soffice", "libreoffice"]
    paths = [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    ]
    found: List[str] = []
    for name in names:
        exe = shutil.which(name)
        if exe:
            found.append(exe)
    for p in paths:
        if os.path.isfile(p):
            found.append(p)
    # 去重，保持顺序
    seen = set()
    result = []
    for item in found:
        key = os.path.normcase(item)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _convert_doc_via_libreoffice(doc_path: str) -> Optional[str]:
    """用 LibreOffice 将 .doc 转为 .docx"""
    abs_doc = os.path.abspath(doc_path)
    output_dir = tempfile.mkdtemp()

    for exe in _libreoffice_candidates():
        try:
            result = subprocess.run(
                [exe, "--headless", "--convert-to", "docx", "--outdir", output_dir, abs_doc],
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                continue
            converted = os.path.join(output_dir, f"{Path(doc_path).stem}.docx")
            if os.path.isfile(converted):
                return converted
        except Exception:
            continue
    return None


def _convert_doc_via_word_com(doc_path: str) -> Optional[str]:
    """用本机 Microsoft Word（COM）将 .doc 转为 .docx"""
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return None

    abs_doc = os.path.abspath(doc_path)
    output_dir = tempfile.mkdtemp()
    out_path = os.path.join(output_dir, f"{Path(doc_path).stem}.docx")

    word = None
    doc = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(abs_doc, ReadOnly=True)
        # 16 = wdFormatXMLDocument (.docx)
        doc.SaveAs2(out_path, FileFormat=16)
        doc.Close(False)
        word.Quit()
        if os.path.isfile(out_path):
            return out_path
    except Exception:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
    return None


def _convert_doc_to_docx(doc_path: str) -> Tuple[Optional[str], str]:
    """
    将 .doc 转为 .docx。
    Returns: (转换后路径或 None, 失败原因说明)
    """
    converted = _convert_doc_via_libreoffice(doc_path)
    if converted:
        return converted, ""

    converted = _convert_doc_via_word_com(doc_path)
    if converted:
        return converted, ""

    return None, (
        ".doc 为旧版 Word 格式，系统无法直接读取。"
        "请任选其一：① 用 Word 将模版另存为 .docx 后放入模版库；"
        "② 安装 LibreOffice；"
        "③ 若已安装 Microsoft Word，执行 py -m pip install pywin32 后重启服务。"
    )


def extract_from_doc(doc_path: str) -> Tuple[List[str], str]:
    """从 .doc 文件提取文本，返回 (段落列表, 错误说明)"""
    converted, err = _convert_doc_to_docx(doc_path)
    if not converted:
        return [], err
    try:
        return extract_from_docx(converted), ""
    except Exception as e:
        return [], f".doc 转换成功但读取失败：{e}"


def extraction_hint(file_path: str) -> str:
    """无法提取文本时，返回面向用户的操作建议"""
    ext = Path(file_path).suffix.lower()
    if ext == ".doc":
        return (
            "无法从 .doc 模版提取文本。"
            "建议将模版用 Word 另存为 .docx 后放入 contract_templates 目录；"
            "或安装 LibreOffice / pywin32（需本机已安装 Microsoft Word）。"
        )
    if ext == ".pdf":
        return "无法从 PDF 提取文本，可能是扫描件图片 PDF，请提供可编辑的 Word 版本。"
    return "无法从文件中提取文本，请检查文件是否损坏，或改用 .docx 格式。"


def extract_text(file_path: str) -> List[str]:
    """
    通用文本提取入口。
    Returns: 按顺序提取的段落/行列表；失败时返回空列表。
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".docx":
        try:
            return extract_from_docx(file_path)
        except Exception:
            return []

    if ext == ".doc":
        paras, _ = extract_from_doc(file_path)
        return paras

    if ext == ".pdf":
        return extract_from_pdf(file_path)

    return []

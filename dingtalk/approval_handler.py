"""
合同审批事件处理核心

当钉钉审批实例创建（type=start）时被调用：
1. 拉取审批实例详情，找到合同附件
2. 下载合同文件
3. 调用共享审核流水线（modules/review_pipeline.py）：
   模版匹配 → 差异比对/LLM初审 → 关键信息提取 → 乙方成立日期查询
   → 日期合规核查 → 生成评论文本 → 生成 Word 报告
4. 把结果评论回写审批单，并尝试把完整 Word 报告作为附件上传

核心审核逻辑统一放在 modules/review_pipeline.py 中，
与本地测试工具 test_dingtalk_flow.py 共用同一套代码，避免两处逻辑走偏。
"""
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import config as app_config
from modules.extractor import extract_text
from modules.review_pipeline import run_review

from dingtalk import api_client
from dingtalk.dingtalk_config import (
    DINGTALK_APP_KEY, DINGTALK_APP_SECRET,
    DINGTALK_BOT_USER_ID, TEMPLATES_DIR,
)

logger = logging.getLogger(__name__)

# 模版匹配相似度阈值：高于此值认为是模版合同
TEMPLATE_MATCH_THRESHOLD = 0.60


def handle_approval_created(instance_id: str) -> None:
    """
    处理审批实例创建事件的主入口。
    下载合同 → 审核 → 把结果评论回审批单（附完整 Word 报告）。
    """
    logger.info("开始处理审批实例: %s", instance_id)

    # ── 1. 获取 access_token ──
    try:
        token = api_client.get_access_token(DINGTALK_APP_KEY, DINGTALK_APP_SECRET)
    except Exception as e:
        logger.error("获取 access_token 失败: %s", e)
        return

    # ── 2. 获取审批实例详情 ──
    try:
        instance = api_client.get_process_instance(instance_id, token)
    except Exception as e:
        logger.error("获取审批实例详情失败 %s: %s", instance_id, e)
        return

    form_values = instance.get("formComponentValues") or []
    contract_name = instance.get("title", "未知合同")

    # ── 3. 提取附件 ──
    attachments = api_client.extract_attachments_from_form(form_values)
    if not attachments:
        logger.info("审批实例 %s 没有附件，跳过审核", instance_id)
        return

    # 只取第一个附件（主合同文件）
    att = attachments[0]
    file_name = att.get("file_name", "contract.docx")
    download_url = att.get("download_url", "")
    if not download_url:
        logger.warning("附件没有下载地址，跳过")
        return

    suffix = Path(file_name).suffix.lower() or ".docx"
    if suffix not in (".docx", ".doc", ".pdf"):
        logger.info("附件类型 %s 不是合同文件，跳过", suffix)
        return

    # ── 4. 下载合同文件 ──
    try:
        local_path = api_client.download_attachment(download_url, suffix)
        logger.info("合同文件已下载到: %s", local_path)
    except Exception as e:
        _post_error_comment(instance_id, token, f"合同文件下载失败：{e}")
        return

    # ── 5. 提取文本 ──
    try:
        contract_paras = extract_text(local_path)
    except Exception as e:
        _post_error_comment(instance_id, token, f"合同文本提取失败：{e}")
        return

    if not contract_paras:
        _post_error_comment(
            instance_id, token,
            "无法从附件中提取文字（可能是扫描件或格式问题），请人工审核。"
        )
        return

    # ── 6. 执行共享审核流水线 ──
    outcome = run_review(
        contract_name=contract_name,
        contract_paragraphs=contract_paras,
        llm_api_key=app_config.LLM_API_KEY,
        template_match_threshold=TEMPLATE_MATCH_THRESHOLD,
        templates_dir=TEMPLATES_DIR,
        generate_report=True,
    )

    # ── 7. 回写审批评论（尝试携带完整 Word 报告附件） ──
    if not DINGTALK_BOT_USER_ID:
        logger.warning("未配置 DINGTALK_BOT_USER_ID，无法发表评论，仅输出到日志")
        logger.info("审核结果:\n%s", outcome.comment_text)
        return

    comment_attachments = None
    if outcome.word_report_path and os.path.exists(outcome.word_report_path):
        try:
            comment_attachments = api_client.upload_report_and_get_attachment(
                DINGTALK_BOT_USER_ID, outcome.word_report_path, token
            )
            if not comment_attachments:
                logger.warning("Word 报告附件上传未成功，将仅发送文字评论")
        except Exception as e:
            logger.warning("Word 报告附件上传出错，将仅发送文字评论：%s", e)

    try:
        api_client.add_comment(
            instance_id, DINGTALK_BOT_USER_ID, outcome.comment_text, token,
            attachments=comment_attachments,
        )
        logger.info("审核评论已发表到审批实例 %s（附件：%s）", instance_id, "有" if comment_attachments else "无")
    except Exception as e:
        logger.error("发表评论失败: %s\n评论内容:\n%s", e, outcome.comment_text)


def _post_error_comment(instance_id: str, token: str, message: str) -> None:
    """发表错误提示评论"""
    if not DINGTALK_BOT_USER_ID:
        logger.warning("AI审核出错（未配置BOT_USER_ID，无法评论）：%s", message)
        return
    try:
        text = f"【AI 合同审核】\n⚠️ 自动审核未能完成，请人工审核。\n原因：{message}"
        api_client.add_comment(instance_id, DINGTALK_BOT_USER_ID, text, token)
    except Exception as e:
        logger.error("发表错误评论也失败了: %s", e)

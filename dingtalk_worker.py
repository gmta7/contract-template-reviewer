"""
钉钉合同审批 AI 审核服务

使用 dingtalk-stream SDK 通过 WebSocket 长连接接收审批事件，
无需公网服务器，在任何能访问互联网的机器上运行即可。

启动方式：
  py dingtalk_worker.py

停止方式：Ctrl+C

环境变量（启动前必须设置）：
  DINGTALK_APP_KEY       企业内部应用 AppKey
  DINGTALK_APP_SECRET    企业内部应用 AppSecret
  DINGTALK_PROCESS_CODE  合同审批模板的 processCode（PROC-XXXXXXXX 格式）
  DINGTALK_BOT_USER_ID   发评论所用的 userId
  OPENAI_API_KEY         LLM API Key（用于非模版合同初审、公司信息查询，可选）
"""
import json
import logging
import os
import sys
import threading

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dingtalk_worker")

try:
    import dingtalk_stream
except ImportError:
    logger.error(
        "缺少 dingtalk-stream 依赖，请先执行：\n"
        "  py -m pip install dingtalk-stream -i https://pypi.tuna.tsinghua.edu.cn/simple"
    )
    sys.exit(1)

from dingtalk.dingtalk_config import (
    DINGTALK_APP_KEY, DINGTALK_APP_SECRET,
    DINGTALK_PROCESS_CODES,
)
from dingtalk.approval_handler import handle_approval_created


class ApprovalEventHandler(dingtalk_stream.EventHandler):
    """监听审批实例变更事件"""

    def process(self, event: dingtalk_stream.CloudEvent) -> dingtalk_stream.EventAck:
        try:
            event_type = event.type or ""

            # 只处理审批相关事件
            if "bpms_instance_change" not in event_type:
                return dingtalk_stream.AckMessage.STATUS_OK, dingtalk_stream.AckMessage.EMPTY_RESPONSE

            data = event.data
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    pass

            if not isinstance(data, dict):
                return dingtalk_stream.AckMessage.STATUS_OK, dingtalk_stream.AckMessage.EMPTY_RESPONSE

            event_action = data.get("type") or data.get("eventType", "")
            instance_id = data.get("processInstanceId", "")
            process_code = data.get("processCode", "")

            # 只处理「开始」事件（审批发起时），忽略结束/终止/删除
            if event_action != "start":
                return dingtalk_stream.AckMessage.STATUS_OK, dingtalk_stream.AckMessage.EMPTY_RESPONSE

            # 如果配置了 processCode 过滤，跳过不相关的审批模板
            if DINGTALK_PROCESS_CODES and process_code not in DINGTALK_PROCESS_CODES:
                logger.debug("跳过非合同审批模板：processCode=%s", process_code)
                return dingtalk_stream.AckMessage.STATUS_OK, dingtalk_stream.AckMessage.EMPTY_RESPONSE

            if not instance_id:
                logger.warning("事件中没有 processInstanceId，跳过")
                return dingtalk_stream.AckMessage.STATUS_OK, dingtalk_stream.AckMessage.EMPTY_RESPONSE

            logger.info(
                "收到合同审批事件，processCode=%s，instanceId=%s",
                process_code, instance_id,
            )

            # 在子线程中异步处理，避免阻塞事件接收循环
            t = threading.Thread(
                target=_safe_handle,
                args=(instance_id,),
                daemon=True,
            )
            t.start()

        except Exception as e:
            logger.exception("处理审批事件时发生异常: %s", e)

        # 立即 ACK，告知钉钉服务器已收到事件
        return dingtalk_stream.AckMessage.STATUS_OK, dingtalk_stream.AckMessage.EMPTY_RESPONSE


def _safe_handle(instance_id: str) -> None:
    """在子线程中安全执行审核逻辑，捕获所有异常"""
    try:
        handle_approval_created(instance_id)
    except Exception as e:
        logger.exception("审核逻辑执行失败 instanceId=%s: %s", instance_id, e)


def _check_config() -> bool:
    """检查必要配置是否齐全"""
    ok = True
    if not DINGTALK_APP_KEY:
        logger.error("❌ 未设置 DINGTALK_APP_KEY")
        ok = False
    if not DINGTALK_APP_SECRET:
        logger.error("❌ 未设置 DINGTALK_APP_SECRET")
        ok = False
    if not DINGTALK_PROCESS_CODES:
        logger.warning("⚠️  未设置 DINGTALK_PROCESS_CODE，将监听该企业所有审批类型（不推荐）")
    from dingtalk.dingtalk_config import DINGTALK_BOT_USER_ID
    if not DINGTALK_BOT_USER_ID:
        logger.warning("⚠️  未设置 DINGTALK_BOT_USER_ID，审核结果只会输出到日志，不会发表为审批评论")
    return ok


def main():
    if not _check_config():
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("合同审批 AI 审核服务启动")
    logger.info("监听的审批模板: %s", DINGTALK_PROCESS_CODES or ["全部"])
    logger.info("=" * 50)

    credential = dingtalk_stream.Credential(DINGTALK_APP_KEY, DINGTALK_APP_SECRET)
    client = dingtalk_stream.DingTalkStreamClient(credential)

    # 注册审批事件监听器
    # bpms_instance_change 事件的 topic
    client.register_all_event_handler(ApprovalEventHandler())

    logger.info("正在连接钉钉 Stream 服务...")
    logger.info("按 Ctrl+C 停止服务")

    try:
        client.start_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止")


if __name__ == "__main__":
    main()

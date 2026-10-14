"""
钉钉集成配置

所有值均从环境变量读取，也可在此文件中直接填写（仅限本地测试，不要提交到代码仓库）。

配置方式：
  在运行 dingtalk_worker.py 的终端中设置环境变量，例如：
    set DINGTALK_APP_KEY=dingtalkxxxxxxxx
    set DINGTALK_APP_SECRET=xxxxxxxxxxxxxxxx
    set DINGTALK_PROCESS_CODE=PROC-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
    set DINGTALK_BOT_USER_ID=xxxxxxxxxxxx
    py dingtalk_worker.py

如何获取这些值：
  APP_KEY / APP_SECRET : 钉钉开放平台 → 我的应用 → 企业内部应用 → 应用信息 → AppKey/AppSecret
  PROCESS_CODE         : 在审批模板设置页 URL 中或通过 API 获取，格式 PROC-XXXXXXXX
  BOT_USER_ID          : 在开放平台创建机器人后获取，或用员工账号的 userId（评论将以该用户名义发出）
  CONTRACT_TEMPLATES_DIR: 合同模版文件夹路径（与 config.py 共用同一环境变量）
"""
import os

# ── 必填 ──────────────────────────────────────────────
DINGTALK_APP_KEY    = os.environ.get("DINGTALK_APP_KEY", "")
DINGTALK_APP_SECRET = os.environ.get("DINGTALK_APP_SECRET", "")

# 合同审批模板的 processCode，多个用英文逗号分隔
# 留空则监听该企业所有审批（不推荐，应尽量指定）
DINGTALK_PROCESS_CODES = [
    c.strip()
    for c in os.environ.get("DINGTALK_PROCESS_CODE", "").split(",")
    if c.strip()
]

# 发表审批评论时使用的 userId（必须是该企业的真实用户）
DINGTALK_BOT_USER_ID = os.environ.get("DINGTALK_BOT_USER_ID", "")

# ── 可选 ──────────────────────────────────────────────
# 钉钉 API 基础地址（一般不需要改）
DINGTALK_API_BASE = "https://api.dingtalk.com"
DINGTALK_OAPI_BASE = "https://oapi.dingtalk.com"

# 审批附件字段名（从审批表单中获取合同文件的字段 key，需与钉钉表单字段对应）
# 如果留空，程序会自动尝试找附件类型字段
CONTRACT_ATTACHMENT_FIELD = os.environ.get("CONTRACT_ATTACHMENT_FIELD", "")

# 模版库目录（和主 config.py 使用同一环境变量）
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config as app_config
TEMPLATES_DIR = app_config.TEMPLATES_DIR

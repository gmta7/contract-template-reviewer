import os

# 从项目根目录的 .env 文件加载环境变量。
# .env 不会提交到 git（见 .gitignore），本机配置 LLM_API_KEY 等敏感信息更安全，
# 优先级低于系统已设置的环境变量（不会覆盖你手动 set 过的值）。
_ENV_FILE = os.path.join(os.path.dirname(__file__), ".env")
try:
    from dotenv import load_dotenv
    load_dotenv(_ENV_FILE)
except ImportError:
    # 未安装 python-dotenv（requirements.txt 已包含，建议 pip install -r requirements.txt）
    # 时的兜底：手动解析 .env 里的简单 KEY=VALUE 行，保证不额外装包也能读取到配置。
    if os.path.isfile(_ENV_FILE):
        with open(_ENV_FILE, "r", encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip()
                if _k and _k not in os.environ:
                    os.environ[_k] = _v

# 模版库根目录（存放所有合同模版，可按子目录分类）
TEMPLATES_DIR = os.environ.get("CONTRACT_TEMPLATES_DIR", os.path.join(os.path.dirname(__file__), "contract_templates"))

# 本公司各主体及其邮箱后缀配置（用于模版合同的邮箱核查）
COMPANY_EMAILS_FILE = os.environ.get("COMPANY_EMAILS_FILE", os.path.join(os.path.dirname(__file__), "company_emails.json"))

# 上传临时目录
UPLOADS_DIR = os.environ.get("UPLOADS_DIR", os.path.join(os.path.dirname(__file__), "uploads"))

# 报告输出目录
REPORTS_DIR = os.environ.get("REPORTS_DIR", os.path.join(os.path.dirname(__file__), "reports"))

# LLM 配置（可选，仅在方案二置信度不足时使用）
LLM_API_KEY = os.environ.get("OPENAI_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o")

# 差异分析阈值：相似度低于此值时建议使用 LLM 辅助分析
LOW_CONFIDENCE_THRESHOLD = 0.5

# 允许上传的文件类型
ALLOWED_EXTENSIONS = {".docx", ".doc", ".pdf"}

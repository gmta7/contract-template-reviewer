"""
模版合同邮箱核查模块

本公司存在多个法律主体，各自使用独立的邮箱后缀（见 company_emails.json）。
核查规则：
1. 根据合同甲方名称，识别甲方属于配置中的哪个主体
2. 扫描合同全文中出现的邮箱 / 邮箱后缀（如 zhangsan@a.com、@a.com）
3. 若出现了"本公司其他主体"的邮箱后缀 → 报错"邮箱使用错误"
   （合作方等非本公司域名的邮箱不在此核查范围内）
"""
import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

import config as app_config

logger = logging.getLogger(__name__)

# local part 允许为空，以便识别模版中"邮箱后缀为 @xxx.com"这类只写后缀的写法
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])([A-Za-z0-9._%+\-]*)@"
    r"([A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+)"
)

_CONTEXT_CHARS = 30


@dataclass
class EmailIssue:
    level: str          # "error" | "warning"
    code: str
    message: str


@dataclass
class EmailCheckResult:
    party_a_company: Optional[str] = None
    issues: List[EmailIssue] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.level == "warning" for i in self.issues)

    def to_text(self) -> str:
        lines = [f"  ✅ {item}" for item in self.info]
        for issue in self.issues:
            icon = "🚨" if issue.level == "error" else "⚠️"
            lines.append(f"  {icon} {issue.message}")
        return "\n".join(lines) if lines else "  ✅ 未发现需核查的邮箱"


@dataclass
class _Company:
    name: str
    aliases: List[str]
    domains: List[str]


def _normalize_name(text: str) -> str:
    text = re.sub(r"\s+", "", text or "")
    return text.replace("（", "(").replace("）", ")")


def _normalize_domain(domain: str) -> str:
    return domain.strip().lstrip("@").lower()


def load_companies(path: Optional[str] = None) -> List[_Company]:
    """每次调用都重新读取，修改配置文件后无需重启服务即可生效"""
    path = path or app_config.COMPANY_EMAILS_FILE
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    companies = []
    for item in data.get("companies", []):
        name = (item.get("name") or "").strip()
        domains = [_normalize_domain(d) for d in item.get("email_domains", []) if d and d.strip()]
        if not name or not domains:
            continue
        aliases = [a.strip() for a in item.get("aliases", []) if a and a.strip()]
        companies.append(_Company(name=name, aliases=aliases, domains=domains))
    return companies


def _match_company(party_a: str, companies: List[_Company]) -> Optional[_Company]:
    """按甲方名称匹配主体：全称/简称出现在甲方名称中即视为命中，多个命中时取匹配文本最长者"""
    target = _normalize_name(party_a)
    if not target:
        return None

    best, best_len = None, 0
    for c in companies:
        for key in [c.name, *c.aliases]:
            k = _normalize_name(key)
            if k and (k in target or (len(target) >= 4 and target in k)) and len(k) > best_len:
                best, best_len = c, len(k)
    return best


def _domain_owner(domain: str, companies: List[_Company]) -> Optional[_Company]:
    for c in companies:
        for d in c.domains:
            if domain == d or domain.endswith("." + d):
                return c
    return None


def _find_emails(paragraphs: List[str]):
    """返回 [(邮箱原文, 域名, 上下文片段)]"""
    found = []
    for p in paragraphs:
        text = p.replace("＠", "@")
        for m in _EMAIL_RE.finditer(text):
            start = max(0, m.start() - _CONTEXT_CHARS)
            end = min(len(text), m.end() + _CONTEXT_CHARS)
            found.append((m.group(0), m.group(2).lower(), text[start:end].strip()))
    return found


def check_emails(
    paragraphs: List[str],
    party_a: Optional[str],
    config_path: Optional[str] = None,
) -> EmailCheckResult:
    result = EmailCheckResult()

    try:
        companies = load_companies(config_path)
    except FileNotFoundError:
        result.info.append("未配置公司主体邮箱（company_emails.json 不存在），跳过邮箱核查")
        return result
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取公司主体邮箱配置失败: %s", e)
        result.issues.append(EmailIssue("warning", "config_error", f"公司主体邮箱配置文件格式有误，无法核查邮箱：{e}"))
        return result

    if not companies:
        result.info.append("公司主体邮箱配置为空，跳过邮箱核查")
        return result

    internal_emails = []
    for raw, domain, ctx in _find_emails(paragraphs):
        owner = _domain_owner(domain, companies)
        if owner:
            internal_emails.append((raw, owner, ctx))

    company = _match_company(party_a or "", companies)
    if not company:
        if internal_emails:
            owners = sorted({o.name for _, o, _ in internal_emails})
            result.issues.append(EmailIssue(
                "warning", "party_a_unmatched",
                f"甲方「{party_a or '未识别'}」未匹配到已配置的公司主体，无法判断邮箱是否正确；"
                f"合同中出现了以下主体的邮箱：{'、'.join(owners)}，请人工核对",
            ))
        else:
            result.info.append(f"甲方「{party_a or '未识别'}」非已配置的公司主体，且合同中未出现本公司邮箱")
        return result

    result.party_a_company = company.name
    correct_count = 0
    reported = set()
    for raw, owner, ctx in internal_emails:
        if owner is company:
            correct_count += 1
            continue
        if raw.lower() in reported:
            continue
        reported.add(raw.lower())
        expected = "、".join("@" + d for d in company.domains)
        result.issues.append(EmailIssue(
            "error", "wrong_entity_email",
            f"邮箱使用错误：甲方为「{company.name}」，应使用 {expected}，"
            f"但合同中出现了「{owner.name}」的邮箱 {raw}（上下文：…{ctx}…）",
        ))

    if not result.issues:
        if correct_count:
            result.info.append(f"甲方主体「{company.name}」，合同中 {correct_count} 处本公司邮箱均使用正确")
        else:
            result.info.append(f"甲方主体「{company.name}」，合同中未出现本公司邮箱")
    return result

"""
合同日期合规性核查模块

检查项：
1. 乙方公司成立日期 vs 合同落款日期（主体资格）
2. 合同落款日期是否陈旧（模板类合同时效性强，落款过旧多为填写有误）
3. 合作期限有效性：终止日期必须晚于起始日期（业务常把两者写成相等或颠倒）
4. 判定基准日与合作期限（起始~终止）的关系：
   - 不晚于起始日期 → 正常
   - 落在起始~终止之间 → 补签，不太合规
   - 晚于终止日期 → 倒签，需重点关注

关于"判定基准日"：优先取合同落款日期。中文合同常在落款处留空不写日期，
此时上层会以"提交合同审核系统的日期"代入，并置 signing_date_assumed=True。
不直接用审核日期做基准，是因为审核日期还包含签完之后走审批流程的延迟，
拿它去比合作期限会把正常的流程耗时误判成补签。
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

# 落款日期距审核日期超过这个天数即视为异常（模板类合同时效性强，不应差到一年前）
STALE_SIGNING_DAYS = 365


@dataclass
class DateIssue:
    level: str          # "error" | "warning" | "info"
    code: str           # 问题类型标识
    message: str        # 面向审核人的描述


@dataclass
class DateCheckResult:
    issues: list = field(default_factory=list)
    info: list = field(default_factory=list)
    duration_days: Optional[int] = None
    duration_desc: str = ""

    @property
    def has_errors(self) -> bool:
        return any(i.level == "error" for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.level == "warning" for i in self.issues)

    def to_text(self) -> str:
        """生成纯文本摘要，用于钉钉审批评论"""
        lines = []
        for item in self.info:
            lines.append(f"  ✅ {item}")
        for issue in self.issues:
            icon = "🚨" if issue.level == "error" else "⚠️"
            lines.append(f"  {icon} {issue.message}")
        if lines:
            return "\n".join(lines)
        return "  ⚠️ 未能从合同中提取到落款日期与合作期限，无法自动核查，请人工核对"


def _parse_date(value) -> Optional[date]:
    """
    尝试把各种格式的日期字符串解析为 date 对象。
    支持：YYYY-MM-DD / YYYY/MM/DD / YYYY年MM月DD日 / YYYYMMDD，
    并容忍数字与分隔符之间出现任意空格（含全角空格），
    如合同里常见的排版"2026　年　5　月　15　日"。
    """
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()

    s = str(value).strip()
    patterns = [
        # 数字与"年/月/日"或"-、/、."之间允许任意空白（普通空格、全角空格、Tab）
        r"(\d{4})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*[日号]?",
        r"(\d{4})(\d{2})(\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
    return None


def check_dates(
    signing_date=None,
    effective_date=None,
    expiry_date=None,
    establishment_date=None,
    party_b_name: str = "",
    signing_date_assumed: bool = False,
) -> DateCheckResult:
    """
    执行所有日期合规性检查。

    Args:
        signing_date:       合同落款日期（缺失时由上层代入提交审核日期）
        effective_date:     合作期限起始日期
        expiry_date:        合作期限终止日期
        establishment_date: 乙方公司成立日期（可为 None，表示未查到）
        party_b_name:       乙方公司名称（用于描述文字）
        signing_date_assumed: signing_date 是否为"合同没写、按提交审核日期推定"的值。
                            推定值只影响文案表述和第 2 项（陈旧检查无意义故跳过），
                            补签/倒签判定照常进行。
    """
    result = DateCheckResult()
    today = date.today()
    signing_label = "提交审核日期" if signing_date_assumed else "合同落款日期"

    signing   = _parse_date(signing_date)
    effective = _parse_date(effective_date)
    expiry    = _parse_date(expiry_date)
    establish = _parse_date(establishment_date)

    # ── 0. 落款日期缺失说明 ──
    if signing_date_assumed and signing:
        result.info.append(
            f"合同落款未填写日期，按提交审核系统的日期 {signing} 作为判定基准"
        )

    # ── 1. 乙方成立日期 vs 合同落款日期 ──
    if establish and signing:
        if establish > signing:
            result.issues.append(DateIssue(
                level="error",
                code="party_b_not_established",
                message=(
                    f"{party_b_name or '乙方'}成立日期为 {establish}，"
                    f"晚于{signing_label} {signing}，"
                    f"签约时乙方主体尚未依法成立，合同效力存疑"
                ),
            ))
        else:
            days_before = (signing - establish).days
            years = days_before // 365
            result.info.append(
                f"乙方成立日期 {establish}，"
                f"截至{signing_label}已成立约 {years} 年，主体资格正常"
            )
    # 注：查不到成立日期时这里不再重复提示，「乙方主体核查」一节已经会说明
    # 查询失败的具体原因（含人工核实入口），两处都写会让日期核查看起来答非所问。

    # ── 2. 落款日期本身是否陈旧 ──
    # 推定值恒等于今天，比出来必然是 0 天，无需检查
    if signing and not signing_date_assumed:
        gap_days = (today - signing).days
        if gap_days > STALE_SIGNING_DAYS:
            result.issues.append(DateIssue(
                level="warning",
                code="stale_signing_date",
                message=(
                    f"合同落款日期 {signing} 距审核日期 {today} 已 {gap_days} 天"
                    f"（超过 {STALE_SIGNING_DAYS} 天），模板类合同时效性较强，"
                    f"落款日期偏旧，请核实是否填写有误"
                ),
            ))
        else:
            result.info.append(f"合同落款日期 {signing}，距审核日期 {gap_days} 天")

    # ── 3. 合作期限有效性：终止日期必须晚于起始日期 ──
    # 业务有时会把起始日期错写成等于、甚至晚于终止日期
    period_valid = False
    if effective and expiry:
        if expiry < effective:
            result.issues.append(DateIssue(
                level="error",
                code="period_reversed",
                message=(
                    f"合作期限起始日期 {effective} 晚于终止日期 {expiry}，"
                    f"起止顺序颠倒，属于填写错误，需核实"
                ),
            ))
        elif expiry == effective:
            result.issues.append(DateIssue(
                level="error",
                code="zero_duration",
                message=(
                    f"合作期限起始日期与终止日期相同（{effective}），"
                    f"合作期限为零天，属于填写错误，需核实"
                ),
            ))
        else:
            period_valid = True
            days = (expiry - effective).days
            result.duration_days = days
            months = days / 30.44
            if months < 1:
                result.duration_desc = f"{days} 天"
            elif months < 24:
                result.duration_desc = f"约 {months:.1f} 个月（{days} 天）"
            else:
                years_f = months / 12
                result.duration_desc = f"约 {years_f:.1f} 年（{days} 天）"
            result.info.append(
                f"合同合作期限：{effective} 至 {expiry}，共 {result.duration_desc}"
            )

    # ── 4. 判定基准日 vs 合作期限：正常 / 补签 / 倒签 ──
    # 基准日 = 合同落款日期，落款没写时才退回提交审核的系统日期。
    # 不直接用审核日期，是因为审核日期里还含"签完之后走审批流程"的延迟，
    # 拿它比期限会把正常的流程耗时误算成补签。
    #   - 不晚于起始日期            → 签约先于履行，正常（含当天签约当天起算）
    #   - 落在起始~终止之间         → 业务已开始履行才签约，属于补签，不太合规
    #   - 晚于终止日期              → 整个合作期限已结束才签约，属于倒签，重点关注
    # 期限本身填错（第 3 项报错）时不再判定，否则会输出自相矛盾的结论。
    if signing and effective and (period_valid or not expiry):
        if signing <= effective:
            result.info.append(
                f"{signing_label} {signing} 不晚于合作期限起始日期 {effective}，签约先于履行，正常"
            )
        elif expiry and signing > expiry:
            result.issues.append(DateIssue(
                level="error",
                code="retroactive_after_period",
                message=(
                    f"{signing_label} {signing} 已晚于合作期限终止日期 {expiry}，"
                    f"疑似倒签（整个合作期限已结束才签约），请重点核查，提请特别注意"
                ),
            ))
        elif expiry:
            result.issues.append(DateIssue(
                level="warning",
                code="backfilled_within_period",
                message=(
                    f"{signing_label} {signing} 落在合作期限（{effective} 至 {expiry}）之内，"
                    f"疑似补签（业务可能已开始履行才签约），不完全合规，请核实实际执行进度"
                ),
            ))
        else:
            result.issues.append(DateIssue(
                level="warning",
                code="on_or_after_start_no_expiry",
                message=(
                    f"{signing_label} {signing} 晚于合作期限起始日期 {effective}"
                    f"（未识别到终止日期，无法判断合作期限是否已结束），请核实是否存在补签/倒签情形"
                ),
            ))

    return result

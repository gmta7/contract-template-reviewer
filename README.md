# 合同模版审核系统

面向甲方法务 / 合同审核场景的自动化工具。核心能力：

1. **模版比对**：回签合同与公司标准模版逐段对比，区分「正常填写 / 疑似修改 / 缺失 / 模版外新增」
2. **日期合规**：落款日期、合作期限起始/终止、补签/倒签、期限填错（起止颠倒或为零）
3. **邮箱跨主体核查**：我方多个签约主体各有邮箱后缀，防止合同串用其他主体邮箱
4. **乙方主体核查**（可选 LLM）：查询乙方工商成立日期，与落款日期比对
5. **钉钉审批集成**（可选）：审批流自动下载附件、审核、回写评论/附件

---

## 适用人群

适合拥有标准合同模版、需要批量核查回签版本差异的法务、采购、销售运营或开发团队。
公开版不包含任何真实公司合同、主体名称、邮箱后缀、账号或密钥；使用前请按本文配置自己的数据。

---

## 环境要求

- Windows 10/11（当前开发与部署环境）
- Python 3.10+（建议 3.11～3.12；若用 3.14，注意部分可选包可能无预编译包）
- 推荐使用项目内虚拟环境 `.venv`

---

## 1. 获取代码

```powershell
git clone https://github.com/gmta7/contract-template-reviewer.git
cd contract-template-reviewer
```

也可以在 GitHub 页面点击 **Code → Download ZIP**，解压后进入目录。

若已有本地目录并关联了远程：

```powershell
cd <你的本地路径>
git pull origin main
```

---

## 2. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

处理旧版 `.doc` 时建议安装 LibreOffice，或本机有 Word 时再装 `pywin32`。模版优先统一为 `.docx`。

---

## 3. 配置环境变量

```powershell
copy .env.example .env
```

用编辑器打开 `.env`，按需填写：

| 变量 | 是否必填 | 说明 |
|------|----------|------|
| `OPENAI_API_KEY` | 否 | LLM Key；留空则降级为纯规则审核 |
| `LLM_BASE_URL` | 否 | 默认示例为 DeepSeek；按服务商文档改 |
| `LLM_MODEL` | 否 | 与服务商模型名一致，如 `deepseek-chat` |

钉钉相关变量见 `dingtalk/.env.example` 与 `dingtalk/SETUP.md`。

---

## 4. 准备业务配置

### 4.1 合同模版

把标准合同模版（`.docx` / `.pdf`）放入：

```
contract_templates/
```

支持子目录分类。系统启动后会扫描该目录做相似度匹配。

### 4.2 我方主体邮箱后缀

编辑根目录 `company_emails.json`：

```json
{
  "companies": [
    {
      "name": "某某科技有限公司",
      "aliases": ["某某"],
      "email_domains": ["@example.com"]
    }
  ]
}
```

- `name`：工商全称，尽量与合同「甲方」一致  
- `aliases`：简称，便于模糊匹配  
- `email_domains`：该主体专属后缀（可带或不带 `@`）  
- **规则**：只报「跨主体串用」；乙方等外部邮箱不提示  
- **范围**：模版合同与非模版合同都会核查

---

## 5. 如何启动（两套界面，端口不同）

### A. 完整审核流程测试页（日常使用推荐）

```powershell
python test_dingtalk_flow.py
```

浏览器打开：**http://localhost:8002**

用于上传合同、跑完整审核流水线（模版比对 + 日期 + 邮箱 + 报告），不依赖钉钉权限。

### B. 正式 Web UI（按选定模版比对）

```powershell
python main.py
```

或：

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

浏览器打开：**http://localhost:8000**

### C. 钉钉生产 Worker（需权限）

按 `dingtalk/SETUP.md` 配置后：

```powershell
python dingtalk_worker.py
```

---

## 6. 审核结果怎么读

| 标记 | 含义 |
|------|------|
| ✅ 正常填写 | 模版空白处填入内容，属预期 |
| 🚨 疑似修改 | 模版固定条款文字被改动 |
| ⚠️ 内容缺失 | 模版条款在回签中消失 |
| ➕ 模版外新增 | 回签多出模版没有的段落 |
| 日期·补签 | 落款日期落在合作期限内 |
| 日期·倒签 | 落款日期晚于合作期限终止日 |
| 邮箱使用错误 | 合同出现了我方其他主体的邮箱后缀 |

**日期判定基准**：优先合同落款日期；落款未填则用「提交审核当天」的系统日期，并在报告中注明。

---

## 7. 没有 LLM API Key 时

仍可用：

- 模版匹配与差异比对  
- 邮箱跨主体核查  
- 日期规则核查（落款/期限/补签倒签）  
- HTML / Word 报告  

会降级或不可用：

- 乙方成立日期自动查询  
- 非模版合同的 AI 全文初审  
- 依赖 LLM 的关键信息补充提取（正则仍可提取部分甲乙方/期限）

申请 Key 后写入本地 `.env` 即可，无需改代码。

---

## 8. 目录结构（摘要）

```
├── main.py                 # Web UI（端口 8000）
├── test_dingtalk_flow.py   # 审批流模拟页（端口 8002）
├── dingtalk_worker.py      # 钉钉事件监听
├── config.py
├── company_emails.json     # 我方主体 ↔ 邮箱后缀
├── .env.example            # 环境变量模板（提交仓库）
├── .env                    # 本地密钥（勿提交）
├── modules/
│   ├── review_pipeline.py  # 统一审核流水线
│   ├── differ.py           # 段落差异比对
│   ├── date_checker.py     # 日期合规
│   ├── email_checker.py    # 邮箱跨主体核查
│   ├── extractor.py        # docx/pdf 文本提取
│   └── ...
├── dingtalk/               # 钉钉 API 与接入说明
├── contract_templates/     # 标准模版库
└── requirements.txt
```

---

## 9. 常见问题

**Q：改完代码网页还是旧逻辑？**  
A：重启对应 Python 进程；确认访问的是 8002（模拟页）还是 8000（main）。

**Q：甲方识别不到 / 首部错位报「修改」？**  
A：多为 Word 修订模式、表格拆段或模版与回签段落条数不一致。可用诊断脚本排查（若仓库中有 `debug_extract.py`），或对比模版与回签前 20 段提取结果。

**Q：`.env` 能不能发给实习生？**  
A：不要发含真实 Key 的 `.env`。只发仓库权限 + 本 README + 自行申请的新 Key。

**Q：合同涉密怎么办？**  
A：优先不上传公有模型；配置私有部署 `LLM_BASE_URL`，或关闭 LLM、只用规则审核。

---

## 注意事项

- **`.doc`**：优先另存为 `.docx`  
- **PDF 扫描件**：无法抽字，需可编辑版  
- **密钥**：`.env` 已在 `.gitignore` 中；提交前用 `git status` 确认未纳入 `.env`  
- **示例配置**：`company_emails.json` 中均为虚构主体和 `example-*` 域名，请替换后使用  
- **合同数据**：公开仓库不附带真实合同模版；请勿提交含个人信息、商业秘密或签章的合同  
- **合同保密**：外传 LLM 前评估合规要求  

# 钉钉合同审批 AI 审核 - 配置指南

## 你需要完成的步骤

### 第一步：创建钉钉企业内部应用（需要管理员账号）

1. 用企业管理员账号登录 [https://open-dev.dingtalk.com](https://open-dev.dingtalk.com)
2. 点击「创建应用」→ 选择「企业内部应用」→ 选择「H5微应用」
3. 填写应用名称（如「合同 AI 审核助手」），保存
4. 在应用详情页找到「AppKey」和「AppSecret」，复制备用

### 第二步：申请 API 权限

在应用「开发配置」→「权限管理」中，搜索并申请以下权限：

| 权限名称 | 权限标识 | 说明 |
|---|---|---|
| 工作流实例写权限 | Workflow.Instance.Write | 发表审批评论 |
| 工作流实例读权限 | Workflow.Instance.Read | 读取审批详情和附件 |
| 工作流表单读权限 | Workflow.Form.Read | 读取表单字段 |

点击「申请」后，企业管理员需在手机端钉钉审批通过权限申请。

### 第三步：开启事件订阅（Stream 模式）

1. 在应用「开发配置」→「事件与回调」→「事件订阅」
2. 「推送方式」选择「**Stream 模式**」（无需公网地址！）
3. 点击「订阅事件」，搜索 `bpms_instance_change`（审批实例开始、结束事件），勾选后保存

### 第四步：获取合同审批模板的 processCode

方法 A（直接查 URL）：
- 管理员打开钉钉 OA 后台 → 审批 → 找到合同审批模板 → 点击编辑
- 浏览器地址栏中会包含类似 `processCode=PROC-XXXXXXXX` 的参数，复制该值

方法 B（通过员工 userId 查）：
- 让员工提交一条合同审批
- 在应用「API 调试工具」中调用获取审批实例详情接口，响应中含 `processCode` 字段

### 第五步：获取 Bot 用户 userId

审核评论需要以某个员工身份发出（建议创建专用的「AI审核机器人」虚拟员工账号，或使用法务人员账号）：

1. 在应用「开发配置」→ 选择「通讯录」权限
2. 使用 API 调试工具调用「根据手机号获取 userId」接口
3. 或者让 IT 管理员在「组织架构」中查询对应员工的 userId

### 第六步：安装依赖并启动

```powershell
cd <你克隆本仓库后的目录>

# 安装依赖（只需第一次执行）
py -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 设置环境变量（每次启动前）
$env:DINGTALK_APP_KEY     = "dingxxxxxxxxxxxxxxxx"
$env:DINGTALK_APP_SECRET  = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$env:DINGTALK_PROCESS_CODE = "PROC-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
$env:DINGTALK_BOT_USER_ID = "xxxxxxxxxx"
$env:OPENAI_API_KEY       = "sk-xxxx"   # 可选，用于非模版合同AI初审

# 启动钉钉审核服务
py dingtalk_worker.py
```

看到以下输出说明连接成功：
```
正在连接钉钉 Stream 服务...
```

### 第七步：验证 Stream 通道

1. 服务启动后（看到连接成功日志）
2. 回到钉钉开放平台「事件与回调」→「事件订阅」页面
3. 点击「验证 Stream 模式通道」按钮
4. 出现绿色对号表示连通

---

## 运行模式

- **本地持续运行**：在你的电脑上保持 `py dingtalk_worker.py` 运行，只要电脑开机联网就能接收审批事件
- **服务器部署（推荐长期使用）**：将整个项目复制到云服务器，同样用上述命令启动，无需开放防火墙端口（Stream 模式是主动外连）
- **Windows 开机自启**：可用 Windows 任务计划程序，设置开机时自动运行该脚本

---

## 工作流程说明

```
员工提交合同审批
       ↓
钉钉推送 bpms_instance_change 事件（Stream 模式，实时）
       ↓
dingtalk_worker.py 收到事件
       ↓
下载合同附件文件（.docx/.pdf）
       ↓
┌──────────────────────────────────────┐
│  是否能匹配到模版库中的模版？              │
│  是 → 文档比对，标记修改/填写字段          │
│  否 → LLM 全文初审，提取风险点            │
└──────────────────────────────────────┘
       ↓
提取合同关键信息（甲乙方、日期、金额）
       ↓
查询乙方公司成立日期（LLM + 搜索引擎）
       ↓
日期合规性核查（补签/倒签/笔误检测）
       ↓
汇总结果，以评论形式贴回审批单
       ↓
法务打开审批单，看到 AI 审核报告后人工决定
```

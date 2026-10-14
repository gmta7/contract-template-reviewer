"""
钉钉审批流核心逻辑测试工具

在浏览器模拟完整的审批处理流程，无需钉钉权限，可在本地直接验证：
  - 合同文件解析
  - 模版自动匹配
  - 差异比对（模版合同）或 LLM 初审（非模版合同）
  - 乙方公司成立日期查询
  - 日期合规性核查
  - 审批评论报告生成（完整预览会被贴到钉钉审批单的内容）

启动：
  py test_dingtalk_flow.py

访问：http://localhost:8002
"""
import logging
import os
import sys
import tempfile
import uuid
from pathlib import Path

# ── 确保项目模块可以被 import ──────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

import uvicorn
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
import config as app_config
from modules.extractor import extract_text
from modules.review_pipeline import run_review, DEFAULT_TEMPLATE_MATCH_THRESHOLD

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("test_flow")

TEMPLATE_MATCH_THRESHOLD = DEFAULT_TEMPLATE_MATCH_THRESHOLD

# 生成的 Word 报告缓存在这里，供 /api/download-report 下载
_REPORT_STORE: dict = {}

app = FastAPI(title="钉钉流程测试工具", version="1.0.0")


# ─────────────────────────────────────────────────────────
# 内联前端（单文件，无需 static 目录）
# ─────────────────────────────────────────────────────────
_INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>钉钉审批流核心逻辑测试</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#f1f5f9;font-size:14px;color:#111827}
header{background:#1a56db;color:#fff;padding:14px 24px;font-size:18px;font-weight:700}
header small{font-size:12px;opacity:.7;margin-left:12px;font-weight:400}
.wrap{max-width:900px;margin:24px auto;padding:0 16px 48px}
.card{background:#fff;border-radius:10px;border:1px solid #e5e7eb;padding:20px 24px;margin-bottom:16px}
h2{font-size:15px;font-weight:600;margin-bottom:14px;color:#1e3a8a}
label{display:block;font-size:13px;font-weight:500;margin-bottom:5px;color:#374151}
input[type=text],input[type=date],select{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;color:#111;outline:none}
input:focus,select:focus{border-color:#1a56db;box-shadow:0 0 0 3px #dbeafe}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px}
.fg{margin-bottom:12px}
.drop{border:2px dashed #d1d5db;border-radius:8px;padding:24px;text-align:center;cursor:pointer;transition:.2s;background:#f9fafb}
.drop:hover,.drop.over{border-color:#1a56db;background:#eff6ff}
.drop .ico{font-size:30px;margin-bottom:6px}
.drop .fn{color:#1a56db;font-weight:600;margin-top:6px;font-size:13px}
input[type=file]{display:none}
.btn{display:inline-flex;align-items:center;gap:6px;padding:10px 22px;border:none;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;transition:.2s}
.btn-primary{background:#1a56db;color:#fff;width:100%;justify-content:center}
.btn-primary:hover:not(:disabled){background:#1e40af}
.btn-primary:disabled{opacity:.5;cursor:not-allowed}
.note{font-size:12px;color:#6b7280;margin-top:4px}
.spinner{display:none;text-align:center;padding:40px}
.spin{width:36px;height:36px;border:3px solid #e5e7eb;border-top-color:#1a56db;border-radius:50%;animation:spin .8s linear infinite;margin:0 auto 12px}
@keyframes spin{to{transform:rotate(360deg)}}
.result{display:none}
.result-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.result-title{font-size:16px;font-weight:700}
.tag{display:inline-block;padding:3px 10px;border-radius:9999px;font-size:12px;font-weight:600}
.tag-ok{background:#d1fae5;color:#065f46}
.tag-warn{background:#fef3c7;color:#92400e}
.tag-error{background:#fee2e2;color:#991b1b}
pre{white-space:pre-wrap;word-break:break-all;font-family:"Cascadia Code","Courier New",monospace;font-size:12.5px;line-height:1.7;background:#0f172a;color:#e2e8f0;padding:20px;border-radius:8px;max-height:600px;overflow-y:auto}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.meta-card{background:#f8fafc;border:1px solid #e5e7eb;border-radius:6px;padding:10px 12px}
.meta-card .k{font-size:11px;color:#6b7280;margin-bottom:3px}
.meta-card .v{font-size:14px;font-weight:600}
.meta-card.red .v{color:#dc2626}
.meta-card.green .v{color:#16a34a}
.meta-card.blue .v{color:#2563eb}
.err-banner{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px;color:#991b1b;margin-bottom:14px}
</style>
</head>
<body>
<header>🔬 钉钉审批流核心逻辑测试<small>模拟完整的审批处理流程，无需钉钉权限</small></header>
<div class="wrap">

  <div class="card">
    <h2>📂 上传合同文件</h2>
    <div class="drop" id="drop" onclick="document.getElementById('fi').click()">
      <div class="ico">📄</div>
      <div>点击或拖拽合同文件到此处</div>
      <div class="note">支持 .docx / .doc / .pdf</div>
      <div class="fn" id="fn" style="display:none"></div>
    </div>
    <input type="file" id="fi" accept=".docx,.doc,.pdf"/>
  </div>

  <div class="card">
    <h2>📝 合同信息（可选 — 留空则由 AI 自动提取）</h2>
    <div class="row">
      <div class="fg"><label>合同名称</label><input type="text" id="cName" placeholder="如：引流服务合同"/></div>
      <div class="fg"><label>甲方全称</label><input type="text" id="partyA" placeholder="如：甲公司"/></div>
    </div>
    <div class="row">
      <div class="fg"><label>乙方全称</label><input type="text" id="partyB" placeholder="如：乙方公司"/></div>
    </div>
    <div class="row3">
      <div class="fg"><label>落款日期</label><input type="date" id="signDate"/></div>
      <div class="fg"><label>期限起始日期</label><input type="date" id="effDate"/></div>
      <div class="fg"><label>期限终止日期</label><input type="date" id="expDate"/></div>
    </div>
    <div class="note">如果填写了上面的字段，将优先使用你填写的值；留空则 AI 从合同文本中自动提取。</div>
  </div>

  <div class="card">
    <h2>⚙️ 运行设置</h2>
    <div class="row">
      <div class="fg">
        <label>LLM API Key（可选）</label>
        <input type="text" id="llmKey" placeholder="sk-xxx，留空则不使用 LLM 功能"/>
        <div class="note">用于：AI 提取合同信息、非模版合同初审、公司信息查询</div>
      </div>
      <div class="fg">
        <label>模版匹配阈值</label>
        <select id="threshold">
          <option value="0.5">0.5 — 宽松</option>
          <option value="0.6" selected>0.6 — 默认</option>
          <option value="0.7">0.7 — 严格</option>
          <option value="0.8">0.8 — 很严格</option>
        </select>
        <div class="note">相似度超过此值才认为是模版合同</div>
      </div>
    </div>
  </div>

  <button class="btn btn-primary" id="runBtn" onclick="run()" disabled>🚀 运行审核测试</button>

  <div class="spinner" id="spinner">
    <div class="spin"></div>
    <div style="color:#6b7280">正在处理，可能需要 10-30 秒...</div>
  </div>

  <div class="result" id="result">
    <div class="result-header">
      <div class="result-title">📊 审核结果预览</div>
      <span id="overallTag" class="tag"></span>
    </div>
    <div id="errBanner" class="err-banner" style="display:none"></div>
    <div class="meta-grid" id="metaGrid"></div>
    <div style="margin-bottom:8px;font-size:13px;color:#6b7280;display:flex;justify-content:space-between;align-items:center">
      <span>以下是实际会被贴到钉钉审批评论的内容：</span>
      <a id="reportLink" href="#" style="display:none;color:#1a56db;font-weight:600;text-decoration:none" target="_blank">📎 下载完整 Word 报告</a>
    </div>
    <pre id="commentPre"></pre>
  </div>

</div>
<script>
let file = null;
const fi = document.getElementById('fi');
const drop = document.getElementById('drop');

fi.onchange = () => { if(fi.files[0]) setFile(fi.files[0]); };
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); if(e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); };

function setFile(f){
  const ok = /\\.(docx|doc|pdf)$/i.test(f.name);
  if(!ok){ alert('仅支持 .docx / .doc / .pdf'); return; }
  file = f;
  const fn = document.getElementById('fn');
  fn.textContent = '已选：' + f.name;
  fn.style.display = 'block';
  document.getElementById('runBtn').disabled = false;
}

async function run(){
  if(!file) return;
  document.getElementById('spinner').style.display = 'block';
  document.getElementById('result').style.display = 'none';
  document.getElementById('runBtn').disabled = true;

  const fd = new FormData();
  fd.append('contract_file', file);
  fd.append('contract_name', document.getElementById('cName').value);
  fd.append('party_a', document.getElementById('partyA').value);
  fd.append('party_b', document.getElementById('partyB').value);
  fd.append('signing_date', document.getElementById('signDate').value);
  fd.append('effective_date', document.getElementById('effDate').value);
  fd.append('expiry_date', document.getElementById('expDate').value);
  fd.append('llm_api_key', document.getElementById('llmKey').value);
  fd.append('threshold', document.getElementById('threshold').value);

  try {
    const res = await fetch('/api/test-review', { method:'POST', body:fd });
    const data = await res.json();
    showResult(data);
  } catch(e) {
    showResult({ error: '请求失败：' + e.message });
  } finally {
    document.getElementById('spinner').style.display = 'none';
    document.getElementById('runBtn').disabled = false;
  }
}

function showResult(data){
  const el = document.getElementById('result');
  el.style.display = 'block';

  const errBanner = document.getElementById('errBanner');
  if(data.error){
    errBanner.textContent = '❌ ' + data.error;
    errBanner.style.display = 'block';
    document.getElementById('commentPre').textContent = '';
    document.getElementById('metaGrid').innerHTML = '';
    document.getElementById('overallTag').textContent = '';
    return;
  }
  errBanner.style.display = 'none';

  // 总体标签
  const tag = document.getElementById('overallTag');
  if(data.has_critical){ tag.textContent='🔴 发现严重问题'; tag.className='tag tag-error'; }
  else if(data.has_warnings){ tag.textContent='🟡 发现需关注问题'; tag.className='tag tag-warn'; }
  else { tag.textContent='🟢 未发现明显问题'; tag.className='tag tag-ok'; }

  // 元信息卡片
  const mg = document.getElementById('metaGrid');
  const meta = data.meta || {};
  const cards = [
    {k:'甲方', v: meta.party_a || '（未识别）', cls: meta.party_a ? '' : 'red'},
    {k:'乙方', v: meta.party_b || '（未识别）', cls: meta.party_b ? '' : 'red'},
    {k:'合同类型', v: data.is_template ? '模版合同' : '非模版合同', cls: data.is_template ? 'green' : 'blue'},
    {k:'匹配模版', v: data.template_name || '（未匹配）', cls:''},
    {k:'模版相似度', v: data.similarity ? (data.similarity*100).toFixed(1)+'%' : '—', cls:''},
    {k:'修改/缺失/新增', v: (data.tampered||0)+'/'+(data.removed||0)+'/'+(data.added||0),
      cls: (data.tampered||data.removed||data.added) ? 'red' : 'green'},
    {k:'日期问题', v: data.date_issues_count ? data.date_issues_count+'项' : '无',
      cls: data.date_issues_count ? 'red' : 'green'},
    {k:'乙方成立日期', v: data.establishment_date || '未查到', cls: data.establishment_date ? '' : 'red'},
  ];
  mg.innerHTML = cards.map(c =>
    `<div class="meta-card ${c.cls}"><div class="k">${c.k}</div><div class="v">${c.v}</div></div>`
  ).join('');

  document.getElementById('commentPre').textContent = data.comment_text || '（无输出）';

  const reportLink = document.getElementById('reportLink');
  if (data.report_url) {
    reportLink.href = data.report_url;
    reportLink.style.display = 'inline';
  } else {
    reportLink.style.display = 'none';
  }

  el.scrollIntoView({behavior:'smooth'});
}
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────
# 路由（核心审核逻辑统一调用 modules/review_pipeline.py，
# 与生产环境 dingtalk/approval_handler.py 共用同一套代码）
# ─────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return _INDEX_HTML


@app.get("/api/download-report/{token}")
async def download_report(token: str):
    path = _REPORT_STORE.get(token)
    if not path or not os.path.exists(path):
        raise HTTPException(404, detail="报告不存在或已过期")
    return FileResponse(path, filename=os.path.basename(path))


@app.post("/api/test-review")
async def test_review(
    contract_file: UploadFile = File(...),
    contract_name: str = Form(""),
    party_a: str = Form(""),
    party_b: str = Form(""),
    signing_date: str = Form(""),
    effective_date: str = Form(""),
    expiry_date: str = Form(""),
    llm_api_key: str = Form(""),
    threshold: float = Form(0.6),
):
    suffix = Path(contract_file.filename or "c.docx").suffix.lower()
    if suffix not in (".docx", ".doc", ".pdf"):
        raise HTTPException(400, detail="仅支持 .docx / .doc / .pdf")

    # 保存上传文件到临时目录
    tmp_dir = tempfile.mkdtemp(prefix="test_review_")
    local_path = os.path.join(tmp_dir, f"contract{suffix}")
    content = await contract_file.read()
    with open(local_path, "wb") as f:
        f.write(content)

    # 提取文本（按真实文档顺序，段落与表格交错，见 modules/extractor.py 的修复）
    try:
        contract_paras = extract_text(local_path)
    except Exception as e:
        return JSONResponse({"error": f"文本提取失败：{e}"})

    if not contract_paras:
        return JSONResponse({"error": "无法从文件中提取文字（可能是扫描件或格式问题）"})

    effective_key = llm_api_key or app_config.LLM_API_KEY
    display_name = contract_name or contract_file.filename or "待审合同"

    # 人工手填字段优先于自动提取结果
    manual_meta = {
        "party_a": party_a,
        "party_b": party_b,
        "signing_date": signing_date,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
    }

    # 调用与生产环境（dingtalk/approval_handler.py）共用的同一套审核流水线
    outcome = run_review(
        contract_name=display_name,
        contract_paragraphs=contract_paras,
        llm_api_key=effective_key,
        template_match_threshold=threshold,
        manual_meta=manual_meta,
        generate_report=True,
    )

    report_url = None
    if outcome.word_report_path and os.path.exists(outcome.word_report_path):
        token = uuid.uuid4().hex
        _REPORT_STORE[token] = outcome.word_report_path
        report_url = f"/api/download-report/{token}"

    diff_result = outcome.diff_result
    date_result = outcome.date_result

    return JSONResponse({
        "is_template": outcome.is_template,
        "template_name": outcome.template_name,
        "similarity": round(outcome.similarity, 4),
        "tampered": diff_result.tampered_count,
        "removed": diff_result.removed_count,
        "added": diff_result.added_count,
        "filled": diff_result.filled_count,
        "date_issues_count": len(date_result.issues),
        "establishment_date": outcome.company_result.get("establishment_date"),
        "has_critical": outcome.has_critical,
        "has_warnings": outcome.has_warnings,
        "comment_text": outcome.comment_text,
        "report_url": report_url,
        "meta": outcome.meta,
    })


if __name__ == "__main__":
    print("=" * 55)
    print("  钉钉审批流核心逻辑测试工具")
    print("  访问地址：http://localhost:8002")
    print("=" * 55)
    uvicorn.run(app, host="0.0.0.0", port=8002)

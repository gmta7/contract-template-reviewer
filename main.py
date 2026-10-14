"""
合同模版审核系统 - FastAPI 主应用

启动方式：
  pip install -r requirements.txt
  python main.py

或：
  uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import os
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
from modules.extractor import extract_text, extraction_hint
from modules.differ import compute_diff
from modules.reporter import generate_html_report, generate_word_report
from modules.llm_analyzer import analyze_with_llm

# ── 确保目录存在 ──
for _d in (config.TEMPLATES_DIR, config.UPLOADS_DIR, config.REPORTS_DIR):
    os.makedirs(_d, exist_ok=True)

app = FastAPI(title="合同模版审核系统", version="1.0.0")

# 挂载静态文件
_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────
def _allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in config.ALLOWED_EXTENSIONS


def _list_templates_recursive(base_dir: str) -> list:
    """递归扫描模版目录，返回模版列表"""
    result = []
    base = Path(base_dir)
    if not base.exists():
        return result
    for p in sorted(base.rglob("*")):
        if p.is_file() and p.suffix.lower() in config.ALLOWED_EXTENSIONS:
            rel = p.relative_to(base)
            result.append({
                "id": str(rel).replace("\\", "/"),
                "name": p.stem,
                "path": str(rel).replace("\\", "/"),
                "category": str(rel.parent).replace("\\", "/") if rel.parent != Path(".") else "默认",
            })
    return result


# ─────────────────────────────────────────────
# 路由
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = os.path.join(_static_dir, "index.html")
    async with aiofiles.open(index_path, encoding="utf-8") as f:
        return await f.read()


@app.get("/api/templates")
async def get_templates():
    """获取模版库列表"""
    templates = _list_templates_recursive(config.TEMPLATES_DIR)
    return {"templates": templates, "templates_dir": config.TEMPLATES_DIR}


@app.post("/api/analyze")
async def analyze(
    template_id: str = Form(..., description="模版相对路径（来自 /api/templates）"),
    contract_file: UploadFile = File(..., description="乙方回签合同文件"),
    use_llm: bool = Form(False, description="是否启用 LLM 辅助分析"),
    llm_api_key: Optional[str] = Form(None, description="LLM API Key（可选，覆盖环境变量）"),
    llm_base_url: Optional[str] = Form(None, description="LLM Base URL（可选）"),
):
    """核心分析接口：比对模版与回签合同"""
    # 校验文件类型
    if not _allowed(contract_file.filename or ""):
        raise HTTPException(400, detail="不支持的文件类型，请上传 .docx / .doc / .pdf")

    # 校验模版路径（防止路径穿越）
    template_path = os.path.normpath(
        os.path.join(config.TEMPLATES_DIR, template_id.replace("/", os.sep))
    )
    if not template_path.startswith(os.path.normpath(config.TEMPLATES_DIR)):
        raise HTTPException(400, detail="非法的模版路径")
    if not os.path.isfile(template_path):
        raise HTTPException(404, detail=f"模版文件不存在：{template_id}")

    # 保存上传文件
    job_id = str(uuid.uuid4())
    upload_dir = os.path.join(config.UPLOADS_DIR, job_id)
    os.makedirs(upload_dir, exist_ok=True)
    suffix = Path(contract_file.filename or "contract.docx").suffix.lower()
    contract_path = os.path.join(upload_dir, f"contract{suffix}")

    async with aiofiles.open(contract_path, "wb") as out:
        content = await contract_file.read()
        await out.write(content)

    # 提取文本
    try:
        template_paras = extract_text(template_path)
        contract_paras = extract_text(contract_path)
    except Exception as e:
        raise HTTPException(500, detail=f"文本提取失败：{e}")

    if not template_paras:
        raise HTTPException(422, detail=extraction_hint(template_path))
    if not contract_paras:
        raise HTTPException(422, detail=extraction_hint(contract_path))

    # 差异比对
    diff_result = compute_diff(template_paras, contract_paras)

    # LLM 辅助（可选，或置信度低时自动触发）
    llm_result = None
    llm_auto_triggered = False
    if use_llm or diff_result.needs_llm:
        if not use_llm:
            llm_auto_triggered = True
        llm_result = analyze_with_llm(
            template_text="\n".join(template_paras),
            contract_text="\n".join(contract_paras),
            api_key=llm_api_key,
            base_url=llm_base_url,
        )

    # 生成报告
    template_name = Path(template_id).stem
    contract_name = Path(contract_file.filename or "contract").stem
    report_dir = os.path.join(config.REPORTS_DIR, job_id)
    os.makedirs(report_dir, exist_ok=True)

    html_content = generate_html_report(
        diff_result=diff_result,
        template_name=template_name,
        contract_name=contract_name,
        llm_result=llm_result,
    )
    html_path = os.path.join(report_dir, "report.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    word_path = os.path.join(report_dir, "report.docx")
    generate_word_report(
        diff_result=diff_result,
        template_name=template_name,
        contract_name=contract_name,
        output_path=word_path,
        llm_result=llm_result,
    )

    return JSONResponse({
        "job_id": job_id,
        "similarity": round(diff_result.similarity, 4),
        "filled_count": diff_result.filled_count,
        "tampered_count": diff_result.tampered_count,
        "added_count": diff_result.added_count,
        "removed_count": diff_result.removed_count,
        "alert_count": diff_result.alert_count,
        "needs_llm": diff_result.needs_llm,
        "llm_auto_triggered": llm_auto_triggered,
        "llm_error": llm_result.get("error") if llm_result else None,
        "report_html_url": f"/api/report/{job_id}/html",
        "report_word_url": f"/api/report/{job_id}/word",
    })


@app.get("/api/report/{job_id}/html", response_class=HTMLResponse)
async def get_html_report(job_id: str):
    """返回 HTML 审核报告"""
    path = os.path.join(config.REPORTS_DIR, job_id, "report.html")
    if not os.path.exists(path):
        raise HTTPException(404, detail="报告不存在")
    async with aiofiles.open(path, encoding="utf-8") as f:
        return await f.read()


@app.get("/api/report/{job_id}/word")
async def get_word_report(job_id: str):
    """下载 Word 审核报告"""
    path = os.path.join(config.REPORTS_DIR, job_id, "report.docx")
    if not os.path.exists(path):
        raise HTTPException(404, detail="报告不存在")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="合同审核报告.docx",
    )


@app.get("/api/settings")
async def get_settings():
    """返回当前配置（隐藏 API Key 明文）"""
    return {
        "templates_dir": config.TEMPLATES_DIR,
        "llm_configured": bool(config.LLM_API_KEY),
        "llm_model": config.LLM_MODEL,
        "llm_base_url": config.LLM_BASE_URL,
    }


@app.post("/api/settings/llm")
async def update_llm_settings(
    api_key: str = Form(""),
    base_url: str = Form(""),
    model: str = Form(""),
):
    """临时更新 LLM 设置（仅当前进程生效，重启后重置）"""
    if api_key:
        config.LLM_API_KEY = api_key
    if base_url:
        config.LLM_BASE_URL = base_url
    if model:
        config.LLM_MODEL = model
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

"""
钉钉 API 客户端

封装：
- 获取 access_token
- 获取审批实例详情（含表单字段 + 附件下载地址）
- 下载附件文件
- 添加审批评论
"""
import os
import tempfile
import time
import urllib.parse
import urllib.request
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_token_cache = {"token": "", "expires_at": 0}


def get_access_token(app_key: str, app_secret: str) -> str:
    """获取企业内部应用 access_token，带本地缓存"""
    now = time.time()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["token"]

    url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
    body = json.dumps({"appKey": app_key, "appSecret": app_secret}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    token = data.get("accessToken") or data.get("access_token", "")
    expires_in = int(data.get("expireIn", 7200))
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expires_in
    return token


def _request(method: str, path: str, token: str, payload: dict = None) -> dict:
    """通用 API 调用"""
    url = f"https://api.dingtalk.com{path}"
    body = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-acs-dingtalk-access-token": token,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        logger.error("DingTalk API error %s %s: %s", method, path, body_text)
        raise


def get_process_instance(instance_id: str, token: str) -> dict:
    """
    获取审批实例详情。
    返回字段包括：
      - title, createTime, finishTime
      - originatorUserId, originatorDeptId
      - formComponentValues: 表单字段列表
      - tasks: 审批任务列表
    """
    return _request("GET", f"/v1.0/workflow/processInstances/{instance_id}", token)


def add_comment(
    instance_id: str,
    user_id: str,
    text: str,
    token: str,
    attachments: Optional[list] = None,
) -> dict:
    """
    在审批实例上添加评论。

    attachments: 附件列表，每项形如
      {"spaceId": "...", "fileId": "...", "fileName": "...", "fileSize": "...", "fileType": "file"}
      通常由 upload_report_and_get_attachment() 生成，直接传入即可。
    """
    payload = {
        "processInstanceId": instance_id,
        "commentUserId": user_id,
        "text": text,
    }
    if attachments:
        payload["file"] = {"attachments": attachments}
    return _request("POST", "/v1.0/workflow/processInstances/comments", token, payload)


# ─────────────────────────────────────────────
# 审批评论附件上传（钉盘空间中转）
#
# 流程（参考钉钉开放平台文档「获取审批钉盘空间信息」「获取文件上传信息」「提交文件」）：
#   1. get_approval_attachment_space  获取审批钉盘空间 spaceId，并为操作人授予上传权限
#   2. get_unionid                    上传接口部分场景要求 unionId 而非 userId
#   3. upload_file_to_approval_space  Header加签方式把本地文件上传到该空间，返回 fileId 等信息
#   4. add_comment(..., attachments=[...])  贴评论时带上附件
#
# 注意：这一整套依赖真实企业应用凭证和已开通的钉盘/存储相关权限，
# 在没有拿到企业权限之前无法端到端联调，代码按官方文档最佳实现编写，
# 若实际调用报错（如权限/参数不符），请对照最新官方文档核对字段名。
# ─────────────────────────────────────────────
def get_approval_attachment_space(user_id: str, token: str, agent_id: Optional[int] = None) -> Optional[str]:
    """
    获取审批钉盘空间 spaceId，同时会为 user_id 授予该空间的上传权限。
    官方建议：每次调用上传附件接口前，都用同一个 user_id 重新调用一次本接口。
    """
    payload = {"userId": user_id}
    if agent_id:
        payload["agentId"] = agent_id
    try:
        data = _request("POST", "/v1.0/workflow/processInstances/spaces/infos/query", token, payload)
    except Exception as e:
        logger.error("获取审批钉盘空间失败: %s", e)
        return None

    result = data.get("result") if isinstance(data.get("result"), dict) else data
    space_id = result.get("spaceId")
    return str(space_id) if space_id else None


def get_unionid(user_id: str, token: str) -> Optional[str]:
    """根据 userId 查询 unionId（钉盘存储上传接口部分场景要求传 unionId）"""
    try:
        data = _request("GET", f"/v1.0/contact/users/{user_id}", token)
    except Exception as e:
        logger.warning("获取 unionId 失败，将直接使用 userId 代替: %s", e)
        return None
    return data.get("unionId")


def upload_file_to_approval_space(
    space_id: str,
    file_path: str,
    union_id: str,
    token: str,
) -> Optional[dict]:
    """
    把本地文件上传到审批钉盘空间。

    上传三步骤：
      1) 获取文件上传信息（uploadKey + Header加签的 resourceUrls/headers）
      2) 用返回的 headers，PUT 文件内容到 resourceUrls[0]（阿里云 OSS）
      3) 提交文件，换取正式 fileId（dentry.id）

    返回可直接用于 add_comment(attachments=[...]) 的字典，失败返回 None。
    """
    file_name = os.path.basename(file_path)
    file_size = os.path.getsize(file_path)
    encoded_union_id = urllib.parse.quote(union_id)

    query_payload = {
        "protocol": "HEADER_SIGNATURE",
        "option": {
            "storageDriver": "DINGTALK",
            "preCheckParam": {"size": file_size, "name": file_name},
        },
    }
    try:
        info = _request(
            "POST",
            f"/v1.0/storage/spaces/{space_id}/files/uploadInfos/query?unionId={encoded_union_id}",
            token,
            query_payload,
        )
    except Exception as e:
        logger.error("获取文件上传信息失败: %s", e)
        return None

    upload_key = info.get("uploadKey")
    sig_info = info.get("headerSignatureInfo") or {}
    resource_urls = sig_info.get("resourceUrls") or []
    upload_headers = sig_info.get("headers") or {}
    if not upload_key or not resource_urls:
        logger.error("上传信息不完整，无法继续上传: %s", info)
        return None

    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        put_req = urllib.request.Request(
            resource_urls[0], data=file_bytes, headers=upload_headers, method="PUT"
        )
        with urllib.request.urlopen(put_req, timeout=60) as resp:
            resp.read()
    except Exception as e:
        logger.error("上传文件到 OSS 失败: %s", e)
        return None

    commit_payload = {
        "uploadKey": upload_key,
        "name": file_name,
        "parentId": "0",
        "option": {"size": file_size, "conflictStrategy": "AUTO_RENAME"},
    }
    try:
        commit_result = _request(
            "POST",
            f"/v1.0/storage/spaces/{space_id}/files/commit?unionId={encoded_union_id}",
            token,
            commit_payload,
        )
    except Exception as e:
        logger.error("提交文件失败: %s", e)
        return None

    dentry = commit_result.get("dentry") or {}
    file_id = dentry.get("id")
    if not file_id:
        logger.error("提交文件后未返回 fileId: %s", commit_result)
        return None

    return {
        "spaceId": space_id,
        "fileId": str(file_id),
        "fileName": file_name,
        "fileSize": str(file_size),
        "fileType": "file",
    }


def upload_report_and_get_attachment(user_id: str, file_path: str, token: str) -> Optional[list]:
    """
    高层封装：获取钉盘空间 → 查 unionId → 上传文件，
    返回可直接传给 add_comment(attachments=...) 的列表；任意一步失败都返回 None
    （调用方应当降级为仅发送纯文字评论，不要因为附件失败而丢失文字审核结果）。
    """
    space_id = get_approval_attachment_space(user_id, token)
    if not space_id:
        return None
    union_id = get_unionid(user_id, token) or user_id
    file_info = upload_file_to_approval_space(space_id, file_path, union_id, token)
    if not file_info:
        return None
    return [file_info]


def download_attachment(download_url: str, suffix: str = ".docx") -> str:
    """
    下载附件到本地临时目录，返回本地文件路径。
    """
    tmp_dir = tempfile.mkdtemp(prefix="dingtalk_contract_")
    local_path = os.path.join(tmp_dir, f"contract{suffix}")

    req = urllib.request.Request(
        download_url,
        headers={
            "User-Agent": "Mozilla/5.0 contract-reviewer/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        with open(local_path, "wb") as f:
            f.write(resp.read())

    return local_path


def extract_attachments_from_form(form_component_values: list) -> list:
    """
    从表单字段中提取附件信息。

    钉钉审批表单的附件字段 componentType 通常为 'ATTACHMENT'，
    value 是 JSON 字符串，内含 downloadUrl / fileName 等。

    返回: [{"file_name": str, "download_url": str, "file_size": int}, ...]
    """
    attachments = []
    for field in (form_component_values or []):
        comp_type = field.get("componentType", "").upper()
        if comp_type not in ("ATTACHMENT", "IMAGE"):
            continue
        value_str = field.get("value", "")
        if not value_str:
            continue
        try:
            items = json.loads(value_str)
            if isinstance(items, list):
                for item in items:
                    url = item.get("downloadUrl") or item.get("download_url", "")
                    name = item.get("fileName") or item.get("file_name", "attachment")
                    size = item.get("fileSize") or item.get("file_size", 0)
                    if url:
                        attachments.append({
                            "file_name": name,
                            "download_url": url,
                            "file_size": size,
                        })
        except (json.JSONDecodeError, TypeError):
            pass
    return attachments

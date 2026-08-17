# -*- coding: utf-8 -*-
"""用已登录 Playwright 会话读采购明细、下商品图。不走换货写接口。"""
from __future__ import annotations

import base64
import json
import re
from html import unescape
from urllib.parse import urlencode, urljoin, urlparse

from .errors import ErpError

IMAGE_FIELDS = ("pic300", "pic160", "pic100", "pic60", "pic30", "pic")
ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp")
_JT_DATA_RE = re.compile(
    r"""id=["']_jt_data["'][^>]*>(.*?)</(?:textarea|script|div)""",
    re.IGNORECASE | re.DOTALL,
)


def purchase_origin(page, fallback: str = "") -> str:
    href = str(getattr(page, "url", "") or fallback or "")
    parsed = urlparse(href)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return "https://www.erp321.com"


def purchase_item_url(origin: str, po_id: str, owner_co_id: str) -> str:
    po_id = str(po_id or "").strip()
    owner = str(owner_co_id or "").strip()
    if not po_id.isdigit():
        raise ErpError("采购单号格式不正确")
    if not owner.isdigit():
        raise ErpError("货主编码格式不正确")
    query = urlencode({
        "po_id": po_id,
        "p_co_id": owner,
        "p_owner_co_id": owner,
        "all_data": "true",
        "archive": "false",
        "owner_co_id": owner,
        "authorize_co_id": owner,
    })
    return f"{str(origin).rstrip('/')}/app/scm/purchase/purchaseitem.aspx?{query}"


def parse_purchase_items(html: str) -> list[dict]:
    text = str(html or "")
    match = _JT_DATA_RE.search(text)
    if not match:
        if "login.aspx" in text.lower():
            raise ErpError("采购明细接口转到登录页，登录态已失效")
        raise ErpError("采购明细接口没有返回 #_jt_data，可能登录已失效")
    try:
        payload = json.loads(unescape(match.group(1)).strip())
    except json.JSONDecodeError as exc:
        raise ErpError("采购明细接口 #_jt_data 不是 JSON") from exc
    rows = payload.get("datas") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ErpError("采购明细接口 datas 格式不正确")
    return rows


def fetch_purchase_items(page, po_id: str, *, owner_co_id: str, origin: str = "") -> list[dict]:
    request = getattr(page, "request", None)
    if request is None or not hasattr(request, "get"):
        raise ErpError("当前页面没有 request，无法读取采购明细")
    url = purchase_item_url(purchase_origin(page, origin), po_id, owner_co_id)
    response = request.get(url, timeout=30000)
    status = int(getattr(response, "status", 0) or 0)
    if status >= 400:
        raise ErpError(f"采购明细接口 HTTP {status}")
    reader = getattr(response, "text", None)
    html = reader() if callable(reader) else str(reader or "")
    return parse_purchase_items(html)


def item_image_url(item: dict | None, origin: str) -> str:
    for field in IMAGE_FIELDS:
        raw = str((item or {}).get(field) or "").strip()
        if not raw:
            continue
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return urljoin(str(origin).rstrip("/") + "/", raw)
    return ""


def guess_image_mime(data: bytes, hinted: str = "", url: str = "") -> str:
    hinted = str(hinted or "").split(";", 1)[0].strip().lower()
    if hinted in ALLOWED_MIME:
        return hinted
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    path = urlparse(url).path.lower()
    if path.endswith(".png"):
        return "image/png"
    if path.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def download_image(page, url: str) -> dict:
    request = getattr(page, "request", None)
    if request is None or not hasattr(request, "get"):
        raise ErpError("当前页面没有 request，无法下载商品图片")
    response = request.get(url, timeout=30000)
    status = int(getattr(response, "status", 0) or 0)
    if status >= 400:
        raise ErpError(f"图片下载 HTTP {status}")
    reader = getattr(response, "body", None)
    data = reader() if callable(reader) else b""
    if not data:
        raise ErpError("图片为空")
    headers = getattr(response, "headers", {}) or {}
    hinted = ""
    if isinstance(headers, dict):
        hinted = str(headers.get("content-type") or headers.get("Content-Type") or "")
    return {"bytes": data, "mimeType": guess_image_mime(data, hinted, url), "sourceUrl": url}


def sync_images(page, job: dict, images, worker_id: str, *, owner_co_id: str,
                origin: str = "") -> dict:
    """领取后的图片任务：读采购明细、下载、写入 ProductImageService。"""
    targets = job.get("targets") if isinstance(job.get("targets"), list) else []
    po_id = str(job.get("purchaseOrderNo") or job.get("po_id") or "").strip()
    try:
        rows = fetch_purchase_items(page, po_id, owner_co_id=owner_co_id, origin=origin)
    except Exception as exc:
        return images.finish(job["id"], worker_id, {"failed": targets, "error": str(exc)})
    failed = []
    base = purchase_origin(page, origin)
    for target in targets:
        sku = str((target or {}).get("sku") or "").strip()
        try:
            item = next(
                (row for row in rows if str((row or {}).get("sku_id") or "") == sku),
                None,
            )
            if item is None:
                raise ErpError("采购明细接口未找到该 SKU")
            source = item_image_url(item, base)
            if not source:
                raise ErpError("采购明细接口没有 pic300/pic160/pic100")
            downloaded = download_image(page, source)
            images.upload(job["id"], worker_id, {
                "sku": sku,
                "sourceUrl": source,
                "mimeType": downloaded["mimeType"],
                "imageBase64": base64.b64encode(downloaded["bytes"]).decode("ascii"),
            })
        except Exception as exc:
            failed.append({"sku": sku, "error": str(exc)})
    return images.finish(
        job["id"], worker_id, {"failed": failed, "attempted": len(targets)},
    )

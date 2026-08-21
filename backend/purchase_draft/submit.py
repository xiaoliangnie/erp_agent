# -*- coding: utf-8 -*-
"""草稿确认后写 ERP，成功再试生成合同。"""
from __future__ import annotations

from ..business_time import business_now
from ..contracts import generate_contract
from ..erp.errors import ErpUnknownResult
from ..paths import OUTPUTS_DIR
from .service import (
    apply_draft_edits,
    erp_payload,
    public_draft,
    save_purchase_draft,
    validate_for_erp,
)


def submit_purchase_draft(draft: dict, runtime, *, env_path: str, root=None, body=None) -> dict:
    if draft.get("poId"):
        raise ValueError(f"这份草稿已经建成采购单 {draft['poId']}，不要重复提交")
    if body:
        draft = apply_draft_edits(draft, body, root=root)
    validate_for_erp(draft)
    payload = erp_payload(draft)
    with runtime.exclusive():
        result = runtime.run("erp.create_purchase_order", payload)
    po_id = str((result or {}).get("poId") or "").strip()
    if not po_id.isdigit():
        raise ErpUnknownResult("ERP 没有回采购单号")
    draft["poId"] = po_id
    draft["submittedAt"] = business_now().strftime("%Y-%m-%d %H:%M:%S")
    draft["writesErp"] = True
    contract = None
    header = draft.get("header") or {}
    invoice = str(header.get("invoiceType") or "special_invoice")
    try:
        output = OUTPUTS_DIR / "generated" / f"采购合同-{po_id}-{invoice}.xlsx"
        generate_contract(
            po_id, invoice, output,
            tax_rate=header.get("taxRate") or None,
            payment_option=header.get("paymentMethod") or None,
            env_path=env_path,
        )
        contract = {
            "ok": True,
            "fileName": output.name,
            "page": f"/contract?po_id={po_id}",
        }
    except Exception as exc:
        contract = {
            "ok": False,
            "error": str(exc)[:300],
            "page": f"/contract?po_id={po_id}",
        }
    draft["contract"] = contract
    save_purchase_draft(draft, root=root)
    public = public_draft(draft)
    public["poId"] = po_id
    public["contract"] = contract
    public["writesErp"] = True
    return public

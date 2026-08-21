# -*- coding: utf-8 -*-
"""从鞋服/百货结果表勾选款，收成采购单草稿。数量用建议下单，单价只认最近采购。"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from ..business_time import business_now
from ..contract_history import last_payment_choice, load_payment_history
from ..contract_mappings import load_mappings
from ..contracts import payment_options as contract_payment_options
from ..database import (
    REALTIME_ITEM_TABLE,
    REALTIME_MAIN_TABLE,
    REALTIME_PRODUCT_TABLE,
    connect,
    fetch_product_master,
)
from ..paths import local_dir
from ..spu_plan.service import INVENTORY_TABLE, load_style_snapshot, normalize_board
from ..supplier_master import load_supplier_book
from .workbook import write_blank_purchase_template, write_purchase_draft_workbook

MISSING_SUPPLIER = "未对上供应商"
# 页面下拉与合同同一套；写入 ERP 时再映射回 payment_method。
ERP_PAYMENT_FALLBACK = "CurrentSettlement"
WAREHOUSES = [
    {"id": "0", "name": "--(本仓)--"},
    {"id": "10235039", "name": "杭州无际云帆电子商务有限公司"},
    {"id": "10576231", "name": "乾盛云仓"},
    {"id": "11968703", "name": "八爪云分仓"},
    {"id": "12020807", "name": "天津优利云仓-泰山"},
    {"id": "12109492", "name": "利顺百通-杭州云仓"},
    {"id": "12509502", "name": "师华加工仓"},
    {"id": "12893936", "name": "盐城旭汇云仓-景云"},
    {"id": "13048420", "name": "蜀黍家-YT鞋子云仓"},
    {"id": "15434141", "name": "辛集市润欧商贸有限公司"},
    {"id": "15467878", "name": "义乌-陈高"},
]
INVOICE_TYPES = ("no_invoice", "normal_invoice", "special_invoice")
DEFAULT_INVOICE_RATES = {
    "no_invoice": 0,
    "normal_invoice": 0,
    "special_invoice": 13,
}
TAX_RATE_OPTIONS = (0, 1, 3, 6, 9, 13)
SETTLEMENT_PAYMENTS = (
    ("账期", "MonthlyStatement"),
    ("月结", "MonthlyStatement"),
    ("到付", "CashOnDelivery"),
    ("现结", "CurrentSettlement"),
)


class PurchaseDraftError(ValueError):
    """勾选或结果表对不上。"""


def draft_dir(*, root=None) -> Path:
    return local_dir("outputs", root=root) / "purchase-drafts"


def draft_json_path(draft_id: str, *, root=None) -> Path:
    return draft_dir(root=root) / f"{draft_id}.json"


def draft_xlsx_path(draft_id: str, *, root=None) -> Path:
    return draft_dir(root=root) / f"{draft_id}.xlsx"


def public_draft(draft: dict) -> dict:
    """给页面的摘要，不含本机路径。"""
    return {
        "ok": True,
        "id": draft.get("id"),
        "board": draft.get("board"),
        "createdAt": draft.get("createdAt"),
        "filename": draft.get("filename"),
        "lines": list(draft.get("lines") or []),
        "groups": list(draft.get("groups") or []),
        "stats": dict(draft.get("stats") or {}),
        "notes": list(draft.get("notes") or []),
        "header": dict(draft.get("header") or {}),
        "options": dict(draft.get("options") or {}),
        "supplierNote": draft.get("supplierNote") or "",
        "writesErp": False,
        "poId": draft.get("poId") or "",
        "contract": draft.get("contract"),
    }


def load_purchase_draft(draft_id: str, *, root=None) -> dict:
    text = str(draft_id or "").strip()
    path = draft_json_path(text, root=root)
    if not path.is_file():
        raise PurchaseDraftError("采购单草稿不存在或已清理")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("id") != text:
        raise PurchaseDraftError("采购单草稿损坏")
    return payload


def _qty(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number)


def _load_last_purchases(env_path, keys: list[str]) -> dict:
    unique = [str(key).strip() for key in dict.fromkeys(keys) if str(key).strip()]
    if not unique or not env_path:
        return {}
    marks = ",".join(["%s"] * len(unique))
    sql = f"""
        SELECT i.sku_id, i.i_id, i.name, i.properties_value, i.price, i.qty,
               i.remark AS item_remark,
               m.seller, m.supplier_id, m.po_id, m.po_date,
               m.wms_co_name, m.purchaser_name, m.payment_method,
               m.send_address, m.remark AS po_remark
        FROM `{REALTIME_ITEM_TABLE}` i
        JOIN `{REALTIME_MAIN_TABLE}` m ON m.po_id = i.po_id
        WHERE (i.sku_id IN ({marks}) OR i.i_id IN ({marks}))
          AND COALESCE(m.status, '') NOT IN ('Cancelled', 'Delete', 'Merged')
        ORDER BY m.po_date DESC, i.po_id DESC
    """
    try:
        with connect(env_path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, tuple(unique + unique))
                rows = cursor.fetchall() or []
    except Exception:
        return {}
    hints = {}
    for row in rows:
        sku = str(row.get("sku_id") or "").strip()
        style = str(row.get("i_id") or "").strip()
        record = {
            "sku": sku,
            "styleId": style,
            "name": str(row.get("name") or "").strip(),
            "spec": str(row.get("properties_value") or "").strip(),
            "price": row.get("price") if row.get("price") not in (None, "") else None,
            "qty": _qty(row.get("qty")),
            "supplier": str(row.get("seller") or "").strip(),
            "supplierId": str(row.get("supplier_id") or "").strip(),
            "poId": str(row.get("po_id") or "").strip(),
            "wmsCoId": next(
                (item["id"] for item in WAREHOUSES if item["name"] == str(row.get("wms_co_name") or "").strip()),
                "0",
            ),
            "wmsCoName": str(row.get("wms_co_name") or "").strip(),
            "purchaserName": str(row.get("purchaser_name") or "").strip(),
            "paymentMethod": str(row.get("payment_method") or "").strip(),
            "sendAddress": str(row.get("send_address") or "").strip(),
            "remark": str(row.get("po_remark") or "").strip(),
            "itemRemark": str(row.get("item_remark") or "").strip(),
        }
        if sku and sku not in hints:
            hints[sku] = record
        if style and style not in hints:
            hints[style] = record
    return hints


def _load_supplier_book(root=None):
    try:
        return load_supplier_book(root=root)
    except Exception:
        return None


def list_payment_options() -> list[dict]:
    """合同 mappings 里的 3/7、发货前付款等，label 给下拉，text 写合同。"""
    try:
        return [
            {
                "id": str(item.get("key") or "").strip(),
                "name": str(item.get("label") or "").strip(),
                "text": str(item.get("text") or "").strip(),
            }
            for item in contract_payment_options()
            if str(item.get("key") or "").strip()
        ]
    except Exception:
        return []


def _contract_payment(value: str) -> str:
    """认合同 key，或把 ERP payment_method 翻成合同 key。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if any(item["id"] == text for item in list_payment_options()):
        return text
    try:
        return str(load_mappings()["erp_payment_defaults"].get(text) or "")
    except Exception:
        return ""


def _erp_payment_from_contract(option_key: str) -> str:
    key = _contract_payment(option_key)
    if not key:
        return ""
    try:
        reverse = {}
        for erp_code, mapped in load_mappings()["erp_payment_defaults"].items():
            reverse.setdefault(str(mapped), str(erp_code))
        return reverse.get(key, "") or ERP_PAYMENT_FALLBACK
    except Exception:
        return ERP_PAYMENT_FALLBACK


def _contract_payment_from_history(seller: str, history=None) -> str:
    last = last_payment_choice(seller, history=history)
    return _contract_payment(str(last.get("option") or "").strip())


def _contract_payment_from_settlement(settlement: str) -> str:
    text = str(settlement or "")
    for needle, erp_code in SETTLEMENT_PAYMENTS:
        if needle in text:
            return _contract_payment(erp_code)
    return ""


def _supplier_option(record: dict, history=None) -> dict:
    invoice = str(record.get("erp_price_mode") or "")
    if invoice not in INVOICE_TYPES:
        invoice = ""
    rates = record.get("invoice_rates") or {}
    tax = rates.get(invoice) if invoice else None
    if tax is None and invoice:
        tax = DEFAULT_INVOICE_RATES[invoice]
    seller = str(record.get("short_name") or "").strip()
    settlement = str(record.get("settlement") or "").strip()
    return {
        "seller": seller,
        "sellerId": str(record.get("code") or "").strip(),
        "legalName": str(record.get("legal_name") or "").strip(),
        "invoiceType": invoice or "special_invoice",
        "invoiceLabel": str(record.get("invoice_label") or "").strip(),
        "taxRate": "" if tax is None else tax,
        "invoiceRates": {
            key: rates[key] if rates.get(key) is not None else DEFAULT_INVOICE_RATES[key]
            for key in INVOICE_TYPES
        },
        "settlement": settlement,
        "paymentMethod": (
            _contract_payment_from_history(seller, history)
            or _contract_payment_from_settlement(settlement)
        ),
        "frozen": bool(record.get("frozen")),
        "internal": bool(record.get("internal")),
    }


def _list_supplier_options(book) -> list[dict]:
    if book is None:
        return []
    history = load_payment_history()
    options = []
    seen = set()
    for record in book.as_dict().values():
        if record.get("frozen"):
            continue
        option = _supplier_option(record, history)
        key = option["seller"] or option["sellerId"]
        if not key or key in seen:
            continue
        seen.add(key)
        options.append(option)
    options.sort(key=lambda item: item["seller"])
    return options


def _apply_supplier_master(header: dict, book, *, seller: str = "") -> dict:
    """用供应商管理表和合同付款历史补编号、票种、税率、付款。不覆盖已有付款。"""
    header = dict(header)
    name = str(seller or header.get("seller") or "").strip()
    if not name or book is None:
        return header
    record = book.lookup(name)
    if not record or record.get("frozen"):
        return header
    option = _supplier_option(record)
    header["seller"] = option["seller"] or header.get("seller") or ""
    if option["sellerId"]:
        header["sellerId"] = option["sellerId"]
    if option["invoiceType"]:
        header["invoiceType"] = option["invoiceType"]
    if option["taxRate"] != "":
        header["taxRate"] = option["taxRate"]
    if not _contract_payment(header.get("paymentMethod") or ""):
        header["paymentMethod"] = option["paymentMethod"] or header.get("paymentMethod") or ""
    header["supplierNote"] = (
        f"{option['legalName'] or option['seller']}"
        + (f" · {option['invoiceLabel']}" if option["invoiceLabel"] else "")
        + (f" · 编码 {option['sellerId']}" if option["sellerId"] else "")
    )
    return header


def _load_purchasers(env_path) -> list[str]:
    """镜像库里出现过的采购员署名，给建单下拉用。"""
    if not env_path:
        return []
    sql = (
        f"SELECT DISTINCT TRIM(purchaser_name) AS name FROM `{REALTIME_MAIN_TABLE}` "
        "WHERE COALESCE(TRIM(purchaser_name), '') <> '' ORDER BY name LIMIT 200"
    )
    try:
        with connect(env_path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall() or []
    except Exception:
        return []
    names = []
    seen = set()
    for row in rows:
        name = str(row.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def tax_rate_choices(*extra) -> list[float]:
    values = []
    seen = set()
    for raw in list(TAX_RATE_OPTIONS) + list(extra):
        if raw in (None, ""):
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if number in seen:
            continue
        seen.add(number)
        values.append(number)
    values.sort()
    return values


def erp_po_datetime(po_date: str, now=None) -> str:
    """页面只选年月日；写入 ERP 时带上当下时分秒，让聚水潭按落单时间建字段。"""
    day = str(po_date or "").strip()[:10]
    stamp = now or business_now()
    clock = stamp.strftime("%H:%M:%S")
    if len(day) == 10 and day[4] == "-" and day[7] == "-":
        return f"{day} {clock}"
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def _add_style_sku(out: dict[str, list[dict]], style: str, sku: str, name: str, spec: str) -> None:
    if not style or not sku:
        return
    bucket = out.setdefault(style, [])
    for item in bucket:
        if item["sku"] == sku:
            if name and not item["name"]:
                item["name"] = name
            if spec and not item["spec"]:
                item["spec"] = spec
            return
    bucket.append({"sku": sku, "name": name, "spec": spec})


def _load_style_skus(env_path, style_ids: list[str]) -> dict[str, list[dict]]:
    """该款式编码下全部商品编码：商品资料 + 库存里出现过的码。"""
    unique = [str(key).strip() for key in dict.fromkeys(style_ids) if str(key).strip()]
    if not unique or not env_path:
        return {}
    marks = ",".join(["%s"] * len(unique))
    out: dict[str, list[dict]] = {}
    try:
        with connect(env_path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT sku_id, i_id, name, properties_value FROM `{REALTIME_PRODUCT_TABLE}` "
                    f"WHERE i_id IN ({marks}) AND COALESCE(sku_id, '') <> ''",
                    tuple(unique),
                )
                for row in cursor.fetchall() or []:
                    _add_style_sku(
                        out,
                        str(row.get("i_id") or "").strip(),
                        str(row.get("sku_id") or "").strip(),
                        str(row.get("name") or "").strip(),
                        str(row.get("properties_value") or "").strip(),
                    )
                try:
                    cursor.execute(
                        f"SELECT sku_id, i_id FROM `{INVENTORY_TABLE}` "
                        f"WHERE i_id IN ({marks}) AND COALESCE(sku_id, '') <> ''",
                        tuple(unique),
                    )
                    for row in cursor.fetchall() or []:
                        _add_style_sku(
                            out,
                            str(row.get("i_id") or "").strip(),
                            str(row.get("sku_id") or "").strip(),
                            "",
                            "",
                        )
                except Exception:
                    pass
    except Exception:
        return out
    for rows in out.values():
        rows.sort(key=lambda item: item.get("sku") or "")
    return out


def _sku_rows_for_style(
    *,
    board_key: str,
    style_id: str,
    style: dict,
    hint: dict,
    product: dict,
    catalog: list[dict],
    hints: dict,
) -> list[dict]:
    """鞋服列出该款全部商品编码；百货一行就是勾选的那个编码。"""
    if board_key == "baihuo":
        return [{
            "sku": style_id,
            "name": str(style.get("name") or product.get("name") or hint.get("name") or "").strip(),
            "spec": str(product.get("specification") or hint.get("spec") or "").strip(),
        }]
    rows = [dict(item) for item in catalog]
    seen = {str(item.get("sku") or "").strip() for item in rows}
    for key, record in hints.items():
        sku = str(record.get("sku") or "").strip()
        if str(record.get("styleId") or "") != style_id or not sku or key != sku:
            continue
        if sku in seen:
            continue
        rows.append({
            "sku": sku,
            "name": str(record.get("name") or "").strip(),
            "spec": str(record.get("spec") or "").strip(),
        })
        seen.add(sku)
    if not rows:
        rows = [{
            "sku": str(hint.get("sku") or style_id).strip(),
            "name": str(style.get("name") or product.get("name") or hint.get("name") or "").strip(),
            "spec": str(product.get("specification") or hint.get("spec") or "").strip(),
        }]
    rows.sort(key=lambda item: item.get("sku") or "")
    return rows


def _line_qty(*, override, last_qty, sku_count: int, style_order) -> int:
    if override is not None and sku_count == 1:
        return override
    if last_qty is not None:
        return last_qty
    if sku_count == 1:
        return _qty(style_order) or 0
    return 0


def _group_lines(lines: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    order = []
    for line in lines:
        key = str(line.get("supplier") or "").strip() or MISSING_SUPPLIER
        if key not in buckets:
            buckets[key] = {"supplier": key, "lines": 0, "qty": 0, "missingPrice": 0}
            order.append(key)
        bucket = buckets[key]
        bucket["lines"] += 1
        bucket["qty"] += int(line.get("qty") or 0)
        if line.get("price") is None:
            bucket["missingPrice"] += 1
    return [buckets[key] for key in order]


def create_purchase_draft(
    *,
    board: str,
    style_ids: list[str],
    quantities: dict | None = None,
    env_path: str | None = None,
    operator: str | None = None,
    root=None,
) -> dict:
    """勾选结果表里的款，写成一份可预览/下载的采购单草稿。"""
    board_key = normalize_board(board)
    wanted = [str(item).strip() for item in style_ids or [] if str(item).strip()]
    if not wanted:
        raise PurchaseDraftError("先在看板勾选要下的款")
    if len(wanted) > 400:
        raise PurchaseDraftError("一次最多勾 400 款")
    snapshot = load_style_snapshot(env_path, board=board_key)
    by_id = {str(item.get("styleId") or ""): item for item in snapshot.get("styles") or []}
    missing = [sid for sid in wanted if sid not in by_id]
    if missing:
        raise PurchaseDraftError("结果表里没有：" + "、".join(missing[:8]))

    overrides = {
        str(key).strip(): _qty(value)
        for key, value in (quantities or {}).items()
        if str(key).strip()
    }
    keys = list(wanted)
    hints = _load_last_purchases(env_path, keys)
    style_skus = _load_style_skus(env_path, keys) if board_key != "baihuo" else {}
    master = {}
    if env_path:
        try:
            master = fetch_product_master(env_path, keys, keys)
        except Exception:
            master = {}

    lines = []
    notes = [
        "鞋服同款列出该款式编码下全部商品编码。数量、单价、备注参考上次采购；没填数量的行不写入采购单。",
        "供应商编号、票种、税率走本机供应商管理表，和合同同一份。采购日期只选年月日，写入 ERP 时带上确认当下的时分秒。",
        "付款与合同同一套（3/7、发货前、到仓后、月度结算）。确认后按聚水潭「手工下单」保存，默认待审核。",
    ]
    header_hint = {}
    for sid in wanted:
        style = by_id[sid]
        hint = hints.get(sid) or {}
        if hint and not header_hint:
            header_hint = hint
        product = master.get(sid) or {}
        sku_rows = _sku_rows_for_style(
            board_key=board_key,
            style_id=sid,
            style=style,
            hint=hint,
            product=product,
            catalog=style_skus.get(sid) or [],
            hints=hints,
        )
        override = overrides.get(sid)
        for sku_row in sku_rows:
            sku = str(sku_row.get("sku") or "").strip()
            sku_hint = hints.get(sku) or {}
            source = sku_hint or hint
            price = source.get("price")
            try:
                price = None if price in (None, "") else float(price)
            except (TypeError, ValueError):
                price = None
            last_qty = _qty(sku_hint.get("qty")) if sku_hint else None
            lines.append({
                "styleId": sid,
                "sku": sku,
                "name": str(sku_row.get("name") or style.get("name") or "").strip(),
                "spec": str(sku_row.get("spec") or "").strip(),
                "qty": _line_qty(
                    override=override,
                    last_qty=last_qty,
                    sku_count=len(sku_rows),
                    style_order=style.get("orderQty"),
                ),
                "price": price,
                "supplier": str(source.get("supplier") or "").strip(),
                "supplierId": str(source.get("supplierId") or "").strip(),
                "deliveryDate": "",
                "warehouse": str(source.get("wmsCoName") or "").strip(),
                "remark": str(source.get("itemRemark") or "").strip(),
                "orderQty": _qty(style.get("orderQty")),
                "lastQty": last_qty,
                "replenishQty": _qty(style.get("replenishQty")),
                "lastPoId": source.get("poId") or "",
            })
    if not lines:
        raise PurchaseDraftError("勾选的款对不上商品编码，请换一批")

    groups = _group_lines(lines)
    if any(group["supplier"] == MISSING_SUPPLIER for group in groups):
        notes.append("有款没对上最近采购供应商，表里是「未对上供应商」，建单前补全。")
    if any(group["missingPrice"] for group in groups):
        notes.append("有行没有最近采购价，单价留空，不编数字。")

    book = _load_supplier_book(root)
    supplier_options = _list_supplier_options(book)
    purchasers = _load_purchasers(env_path)
    operator_name = str(operator or "").strip()
    purchaser = str(header_hint.get("purchaserName") or "").strip() or operator_name
    if purchaser and purchaser not in purchasers:
        purchasers = [purchaser, *purchasers]
    draft_id = secrets.token_hex(12)
    created = business_now().strftime("%Y-%m-%d %H:%M:%S")
    filename = f"{created[2:10].replace('-', '')}-采购单草稿.xlsx"
    header = _apply_supplier_master({
        "seller": header_hint.get("supplier") or "",
        "sellerId": header_hint.get("supplierId") or "",
        "purchaserName": purchaser,
        "paymentMethod": _contract_payment(header_hint.get("paymentMethod") or ""),
        "wmsCoId": header_hint.get("wmsCoId") or "0",
        "wmsCoName": header_hint.get("wmsCoName") or "--(本仓)--",
        "poDate": created[:10],
        "arriveDate": "",
        "taxRate": "",
        "remark": str(header_hint.get("remark") or "").strip(),
        "invoiceType": "special_invoice",
        "confirmAndAudit": False,
    }, book)
    if header.get("taxRate") in ("", None):
        header["taxRate"] = DEFAULT_INVOICE_RATES.get(
            str(header.get("invoiceType") or "special_invoice"), 13,
        )
    supplier_note = str(header.pop("supplierNote", "") or "")
    if header.get("seller") and not header.get("sellerId"):
        notes.append("供应商管理表没对上编码，供应商编号请按最近采购或主数据补全。")
    payload = {
        "id": draft_id,
        "board": board_key,
        "createdAt": created,
        "filename": filename,
        "lines": lines,
        "groups": groups,
        "stats": {
            "lines": len(lines),
            "qty": sum(int(line.get("qty") or 0) for line in lines),
            "suppliers": len(groups),
            "missingSupplier": sum(1 for line in lines if not line.get("supplier")),
            "missingPrice": sum(1 for line in lines if line.get("price") is None),
        },
        "header": header,
        "options": {
            "warehouses": WAREHOUSES,
            "payments": list_payment_options(),
            "invoices": list(INVOICE_TYPES),
            "invoiceRates": dict(DEFAULT_INVOICE_RATES),
            "taxRates": tax_rate_choices(header.get("taxRate"), *DEFAULT_INVOICE_RATES.values()),
            "purchasers": [{"id": name, "name": name} for name in purchasers],
            "suppliers": supplier_options,
        },
        "supplierNote": supplier_note,
        "notes": notes,
        "writesErp": False,
        "poId": "",
        "contract": None,
    }
    json_path = draft_json_path(draft_id, root=root)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_purchase_draft_workbook(payload, draft_xlsx_path(draft_id, root=root))
    write_blank_purchase_template(root=root)
    return payload


def save_purchase_draft(draft: dict, *, root=None) -> dict:
    draft_id = str(draft.get("id") or "").strip()
    if not draft_id:
        raise PurchaseDraftError("草稿没有编号")
    path = draft_json_path(draft_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    write_purchase_draft_workbook(draft, draft_xlsx_path(draft_id, root=root))
    return draft


def apply_draft_edits(draft: dict, body: dict, *, root=None) -> dict:
    """员工改单头/明细后写回草稿。"""
    header = dict(draft.get("header") or {})
    incoming = body.get("header") if isinstance(body.get("header"), dict) else {}
    for key in (
        "seller", "sellerId", "purchaserName", "paymentMethod", "wmsCoId",
        "wmsCoName", "poDate", "arriveDate", "taxRate", "remark", "invoiceType",
    ):
        if key in incoming:
            header[key] = incoming[key]
    header["poDate"] = str(header.get("poDate") or "")[:10]
    header["arriveDate"] = str(header.get("arriveDate") or "")[:10]
    if header.get("invoiceType") not in INVOICE_TYPES:
        header["invoiceType"] = "special_invoice"
    header["paymentMethod"] = _contract_payment(header.get("paymentMethod") or "")
    if header.get("taxRate") in ("", None):
        header["taxRate"] = DEFAULT_INVOICE_RATES[header["invoiceType"]]
    warehouse = next((item for item in WAREHOUSES if item["id"] == str(header.get("wmsCoId") or "")), None)
    if warehouse:
        header["wmsCoName"] = warehouse["name"]
    book = _load_supplier_book(root)
    if book is not None:
        draft.setdefault("options", {})["suppliers"] = _list_supplier_options(book)
        if str(header.get("seller") or "").strip() and not str(header.get("sellerId") or "").strip():
            record = book.lookup(header["seller"])
            if record and not record.get("frozen") and record.get("code"):
                header["sellerId"] = record["code"]
                draft["supplierNote"] = (
                    f"{record.get('legal_name') or record.get('short_name')}"
                    + (f" · {record.get('invoice_label')}" if record.get("invoice_label") else "")
                    + f" · 编码 {record['code']}"
                )
    draft["header"] = header
    if isinstance(body.get("lines"), list):
        lines = []
        for row in body["lines"]:
            if not isinstance(row, dict):
                continue
            qty = _qty(row.get("qty"))
            price = row.get("price")
            try:
                price = None if price in (None, "") else float(price)
            except (TypeError, ValueError):
                price = None
            lines.append({
                **row,
                "sku": str(row.get("sku") or "").strip(),
                "styleId": str(row.get("styleId") or "").strip(),
                "name": str(row.get("name") or "").strip(),
                "spec": str(row.get("spec") or "").strip(),
                "qty": 0 if qty is None else qty,
                "price": price,
                "supplier": str(row.get("supplier") or header.get("seller") or "").strip(),
                "supplierId": str(row.get("supplierId") or header.get("sellerId") or "").strip(),
                "remark": str(row.get("remark") or "").strip(),
            })
        if not lines:
            raise PurchaseDraftError("明细不能空")
        draft["lines"] = lines
        draft["groups"] = _group_lines(lines)
        draft["stats"] = {
            "lines": len(lines),
            "qty": sum(int(line.get("qty") or 0) for line in lines),
            "suppliers": len(draft["groups"]),
            "missingSupplier": sum(1 for line in lines if not line.get("supplier")),
            "missingPrice": sum(1 for line in lines if line.get("price") is None),
        }
    return save_purchase_draft(draft, root=root)


def validate_for_erp(draft: dict) -> list[dict]:
    """检查手工下单必填，返回可写入的明细。"""
    header = draft.get("header") or {}
    if not str(header.get("sellerId") or "").strip():
        raise PurchaseDraftError("供应商编号不能空，请选最近采购对上的供应商")
    if not str(header.get("seller") or "").strip():
        raise PurchaseDraftError("供应商不能空")
    if not str(header.get("purchaserName") or "").strip():
        raise PurchaseDraftError("采购员不能空")
    pay = _contract_payment(header.get("paymentMethod") or "")
    if not pay:
        raise PurchaseDraftError("付款方式不能空")
    if not str(header.get("wmsCoId") or "").strip():
        raise PurchaseDraftError("仓储方不能空")
    items = []
    for line in draft.get("lines") or []:
        sku = str(line.get("sku") or "").strip()
        qty = _qty(line.get("qty")) or 0
        if not sku or qty <= 0:
            continue
        price = line.get("price")
        try:
            price = 0 if price in (None, "") else float(price)
        except (TypeError, ValueError):
            raise PurchaseDraftError(f"{sku} 的单价不是数字")
        items.append({
            "sku": sku,
            "qty": qty,
            "price": price,
            "name": line.get("name") or "",
            "spec": line.get("spec") or "",
            "styleId": line.get("styleId") or "",
            "remark": str(line.get("remark") or "").strip(),
        })
    if not items:
        raise PurchaseDraftError("至少一行数量大于 0")
    if len(items) > 1000:
        raise PurchaseDraftError("明细不能超过 1000 行")
    return items


def erp_payload(draft: dict) -> dict:
    header = draft.get("header") or {}
    return {
        "seller": header.get("seller") or "",
        "sellerId": header.get("sellerId") or "",
        "purchaserName": header.get("purchaserName") or "",
        "paymentMethod": _erp_payment_from_contract(header.get("paymentMethod") or ""),
        "wmsCoId": header.get("wmsCoId") or "",
        "wmsCoName": header.get("wmsCoName") or "",
        "poDate": erp_po_datetime(header.get("poDate") or ""),
        "arriveDate": str(header.get("arriveDate") or "")[:10],
        "taxRate": header.get("taxRate"),
        "remark": header.get("remark") or "",
        "items": validate_for_erp(draft),
    }

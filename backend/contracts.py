# -*- coding: utf-8 -*-
"""从实时 ERP 数据组装并生成采购合同。"""
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .business_time import business_now
from .contract_workbook import write_contract_workbook
from .database import connect, fetch_contract_order, load_all_env
from .gb_standards import (
    CONTRACT_GB_STATUSES,
    family_ids_for,
    fetch_family_standards,
    fetch_sku_categories,
    load_category_map,
    lookup_standard_by_no,
    rank_standards,
    serialize_gb_option,
)
from .product_images import resolve_product_image


ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"
PROJECT_ENV = load_all_env(ROOT / ".env") if (ROOT / ".env").exists() else {}
CONTRACT_GB_TABLE = "contract_line_gb"
CONTRACT_GB_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS `{CONTRACT_GB_TABLE}` (
    po_id VARCHAR(64) NOT NULL,
    poi_id VARCHAR(64) NOT NULL,
    sku_id VARCHAR(128) NOT NULL DEFAULT '',
    samr_id VARCHAR(64) NOT NULL DEFAULT '',
    standard_no VARCHAR(128) NOT NULL,
    name_cn VARCHAR(512) NOT NULL DEFAULT '',
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (po_id, poi_id),
    KEY idx_contract_gb_sku (sku_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

INVOICE_LABELS = {
    "no_invoice": "不开票",
    "normal_invoice": "普票",
    "special_invoice": "专票",
}

DEFAULT_INVOICE_RATES = {
    "no_invoice": 0,
    "normal_invoice": 0,
    "special_invoice": 13,
}

PAYMENT_METHODS = {
    "CurrentSettlement": "100% - 现结，入库结算",
}

BASE_TERMS = [
    "上述价格为暂计价格，实际以购买方验收合格的产品数量为准；",
    "运费以及其他未列明的涉及费用均由供方承担；",
    "供方负责产品的包装，包装费用由供方承担，如包装不善致使产品受损，责任由供方自行承担；",
    None,
    "供方提供的产品质量不合格，购买方有权拒收货物，双方对产品质量有争议，购买方有权要求供方提供对应的检测报告，如检测不合格检测费用由供方自行承担；",
    "供方应按日期交货，每逾期一天，按逾期交货部分货物货款的百分之一标准向购买方支付违约金；",
    "订货过程中发生纠纷，双方应协商解决，协商不成，双方可向购买方所在地人民法院起诉。",
    "甲乙双方在本协议列示的地址均为各自的送达地址；任何一方的送达地址发生变化的，均应当在送达地址发生变化之日起3个工作日内书面通知对方。甲乙双方在向对方列示的任一送达地址送达有关文书时，如果发生收件人拒绝签收或其它无法送达情形的，则从发件人寄出文书之日起视为已经送达对方。",
    "当一方由于地震、水灾、政府政策等不可抗力的原因而不能履行本协议时，应立即以书面形式通知对方有关详细情况，并尽力减少双方的损失。因其怠于采取相应措施致使损失扩大的，不得就扩大的损失主张免责。",
    "双方保证，对于协议履行过程中接触到的相对方或其关联方（包括但不限于相对方所投资、控制的相关经营主体）商业秘密，除得到相对方的明确书面授权和履行职务需要外，不得以任何方式向任何未经授权的第三方公开。上述保密义务在本协议终止后仍然有效。",
    "在本订单的履行过程中，任何一方不得利用职务上的便利，索取他人财物或者非法收受他人财物，为他人谋取利益，如任何一方违反，相对方有权终止本订单的履行并要求违约方支付不少于30万的违约金。如涉嫌触犯相关法律法规的，相对方有追究刑事责任的权利。",
    "本订货单一式两份，双方各执一份，具有同等法律效力，本订货单经双方盖章后生效",
]


def load_json(name):
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))


def contract_setting(name, default=""):
    return os.environ.get(name, PROJECT_ENV.get(name, default))


def day(value):
    value = str(value or "")
    return value[:10] if len(value) >= 10 else ""


def parse_quantity(value, *, field="数量"):
    """把 ERP 数量收成 Decimal，保留小数；空当 0，负数拒绝。禁止 int() 截断。"""
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, Decimal):
        qty = value
    elif isinstance(value, bool):
        raise ValueError(f"{field}不是合法数字：{value}")
    elif isinstance(value, int):
        qty = Decimal(value)
    else:
        text = str(value).strip()
        if not text:
            return Decimal("0")
        try:
            qty = Decimal(text)
        except (InvalidOperation, ValueError):
            raise ValueError(f"{field}不是合法数字：{value}") from None
    if not qty.is_finite():
        raise ValueError(f"{field}不是合法数字：{value}")
    if qty < 0:
        raise ValueError(f"{field}不能为负数")
    return qty


def invoice_term(invoice_type, tax_rate):
    rate = f"{tax_rate:g}%"
    if invoice_type == "no_invoice":
        return (
            f"本订货单约定为不开票价格，当前录入税率为{rate}。如后续改为开票结算，双方应另行书面确认含税价格、"
            "票种与税率；"
        )
    invoice_name = "增值税普通发票" if invoice_type == "normal_invoice" else "增值税专用发票"
    return (
        f"本订货单签署时约定开具{invoice_name}，税率为{rate}。若适用的增值税率发生变化的，该增值税金额"
        "应按届时税率相应调整，本合同约定的不含税价保持不变，供方未按要求开票的，购买方可延迟付款直至供方开票；"
    )


_PREVIEW_SLOTS = threading.BoundedSemaphore(2)
BUYER_REQUIRED = ("company_name", "delivery_address", "packaging_terms", "inspection_standards")


def normalize_price_overrides(raw):
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("priceOverrides 必须是对象")
    overrides = {}
    for key, value in raw.items():
        sku = str(key or "").strip()
        if not sku:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError(f"单价覆盖 {sku} 必须是数字")
        price = float(value)
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"单价覆盖 {sku} 必须是有限正数")
        overrides[sku] = price
    return overrides


def normalize_gb_overrides(raw):
    """页面/Agent 传入的 poiId（或 SKU）→ 标准号。空字符串表示明确清空。"""
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("gbOverrides 必须是对象，键为明细 poiId")
    overrides = {}
    for key, value in raw.items():
        poi = str(key or "").strip()
        if not poi:
            continue
        overrides[poi] = str(value or "").strip()
    return overrides


def pick_saved_standard(poi_id, sku, line_saves, sku_saves):
    """本单本行优先，否则沿用同一 SKU 最近一次选择。"""
    if poi_id and poi_id in line_saves:
        return line_saves[poi_id]
    if sku and sku in sku_saves:
        return sku_saves[sku]
    return None


def resolve_line_gb(*, poi_id, sku, gb_overrides, line_saves, sku_saves, lookup):
    """把覆盖值或已保存值解析成目录行。未选返回空字段；非空必须能在目录里查到。"""
    overrides = gb_overrides or {}
    if poi_id and poi_id in overrides:
        standard_no = overrides[poi_id]
    elif sku and sku in overrides:
        standard_no = overrides[sku]
    else:
        saved = pick_saved_standard(poi_id, sku, line_saves or {}, sku_saves or {})
        standard_no = str((saved or {}).get("standard_no") or "").strip()
    if not standard_no:
        return {"standard_no": "", "samr_id": "", "name_cn": ""}
    record = lookup(standard_no)
    if not record:
        raise ValueError(f"执行标准「{standard_no}」不在国标目录中")
    status = str(record.get("status") or "")
    if status not in CONTRACT_GB_STATUSES:
        raise ValueError(f"执行标准「{standard_no}」状态为{status or '未知'}，不能写入合同")
    return {
        "standard_no": str(record.get("standard_no") or standard_no),
        "samr_id": str(record.get("samr_id") or ""),
        "name_cn": str(record.get("name_cn") or ""),
    }


def _load_category_map_or_empty():
    try:
        return load_category_map()
    except Exception:
        return {"categories": {}, "families": {}, "ignore": []}


def ensure_contract_gb_table(env_path):
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(CONTRACT_GB_SCHEMA)


def load_saved_line_gb(env_path, po_id, sku_ids):
    """本单已选 + 各 SKU 最近一次选择。表建不出来时当作从未选过。"""
    try:
        ensure_contract_gb_table(env_path)
    except Exception:
        return {}, {}
    line_saves = {}
    sku_saves = {}
    try:
        with connect(env_path, autocommit=True) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT poi_id, sku_id, samr_id, standard_no, name_cn "
                    f"FROM `{CONTRACT_GB_TABLE}` WHERE po_id = %s",
                    (str(po_id),),
                )
                for row in cursor.fetchall() or []:
                    poi_id = str(row.get("poi_id") or "")
                    if poi_id:
                        line_saves[poi_id] = row
                unique = [sku for sku in dict.fromkeys(sku_ids) if sku]
                if unique:
                    marks = ",".join(["%s"] * len(unique))
                    cursor.execute(
                        f"SELECT sku_id, samr_id, standard_no, name_cn "
                        f"FROM `{CONTRACT_GB_TABLE}` "
                        f"WHERE sku_id IN ({marks}) ORDER BY updated_at DESC",
                        tuple(unique),
                    )
                    for row in cursor.fetchall() or []:
                        sku = str(row.get("sku_id") or "")
                        if sku and sku not in sku_saves:
                            sku_saves[sku] = row
    except Exception:
        return {}, {}
    return line_saves, sku_saves


def persist_contract_gb(env_path, po_id, items):
    """预览和下载都会把当前选择写回；清空则删行，避免下次又带出来。"""
    has_selection = any(str(item.get("gbStandard") or "").strip() for item in items)
    try:
        ensure_contract_gb_table(env_path)
    except Exception as exc:
        if has_selection:
            raise RuntimeError(f"无法写入执行标准：{exc}") from exc
        return
    now = business_now().strftime("%Y-%m-%d %H:%M:%S")
    with connect(env_path, autocommit=False) as conn:
        try:
            with conn.cursor() as cursor:
                for item in items:
                    poi_id = str(item.get("poiId") or "")
                    if not poi_id:
                        continue
                    standard_no = str(item.get("gbStandard") or "").strip()
                    if not standard_no:
                        cursor.execute(
                            f"DELETE FROM `{CONTRACT_GB_TABLE}` WHERE po_id = %s AND poi_id = %s",
                            (str(po_id), poi_id),
                        )
                        continue
                    cursor.execute(
                        f"""
                        INSERT INTO `{CONTRACT_GB_TABLE}`
                            (po_id, poi_id, sku_id, samr_id, standard_no, name_cn, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            sku_id = VALUES(sku_id),
                            samr_id = VALUES(samr_id),
                            standard_no = VALUES(standard_no),
                            name_cn = VALUES(name_cn),
                            updated_at = VALUES(updated_at)
                        """,
                        (
                            str(po_id), poi_id, str(item.get("sku") or ""),
                            str(item.get("gbSamrId") or ""), standard_no,
                            str(item.get("gbStandardName") or ""), now,
                        ),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def saved_gb_option(saved: dict, catalog: dict | None = None) -> dict:
    """已保存但不在现行/即将实施候选里的执行标准：带上目录里的真实状态，便于角标。"""
    catalog = catalog or {}
    status = str(catalog.get("status") or "").strip() or "已保存"
    return {
        "samrId": str(catalog.get("samr_id") or saved.get("samr_id") or ""),
        "standardNo": str(catalog.get("standard_no") or saved.get("standard_no") or ""),
        "nameCn": str(catalog.get("name_cn") or saved.get("name_cn") or ""),
        "status": status,
        "nature": str(catalog.get("nature") or ""),
        "stdType": str(catalog.get("std_type") or ""),
    }


def _family_rows(env_path, family_ids, cache):
    key = tuple(family_ids)
    if key not in cache:
        cache[key] = fetch_family_standards(env_path, list(family_ids))
    return cache[key]


def get_contract_options(po_id, env_path=None):
    """返回员工选择票种、单价和执行标准时需要的采购信息。"""
    env_path = env_path or str(ROOT / "hanli.env")
    order, erp_items = fetch_contract_order(po_id, env_path)
    suppliers = load_json("suppliers.json")
    products = load_json("products.json")
    supplier_short = str(order.get("seller") or "").strip()
    supplier = suppliers.get(supplier_short) or {}
    sku_ids = [str(item.get("sku_id") or "").strip() for item in erp_items]
    sku_categories = fetch_sku_categories(env_path, sku_ids)
    mapping = _load_category_map_or_empty()
    line_saves, sku_saves = load_saved_line_gb(env_path, str(order["po_id"]), sku_ids)
    family_cache = {}
    items = []
    for item in erp_items:
        sku = str(item.get("sku_id") or "").strip()
        style = str(item.get("i_id") or "").strip()
        poi_id = str(item.get("poi_id") or "")
        product = products.get(sku) or products.get(style) or {}
        image = resolve_product_image(product, sku=sku, style=style)
        erp_category = sku_categories.get(sku) or str(product.get("category") or "")
        family_ids = family_ids_for(erp_category, mapping)
        gb_options = [
            serialize_gb_option(row)
            for row in rank_standards(
                _family_rows(env_path, family_ids, family_cache),
                product_name=str(item.get("name") or product.get("name") or ""),
                category=erp_category,
            )
        ]
        saved = pick_saved_standard(poi_id, sku, line_saves, sku_saves) or {}
        gb_standard = str(saved.get("standard_no") or "")
        allowed = {option["standardNo"] for option in gb_options}
        if gb_standard and gb_standard not in allowed:
            gb_options = [
                saved_gb_option(saved, lookup_standard_by_no(env_path, gb_standard)),
            ] + gb_options
        items.append({
            "poiId": poi_id,
            "sku": sku,
            "styleCode": style,
            "name": str(item.get("name") or ""),
            "quantity": float(parse_quantity(item.get("qty"))),
            "inQuantity": int(float(item.get("in_qty") or 0)),
            "erpPrice": float(item.get("price") or 0),
            "prices": product.get("prices") or {},
            "hasImage": image["status"] == "ready",
            "imageStatus": image["status"],
            "imageSource": image["source"],
            "imageError": image["error"],
            "specification": str(item.get("properties_value") or ""),
            "deliveryDate": day(item.get("delivery_date")),
            "category": erp_category,
            "familyIds": family_ids,
            "gbOptions": gb_options,
            "gbStandard": gb_standard,
        })
    configured_rates = supplier.get("invoice_rates") or {}
    invoice_rates = {
        mode: configured_rates.get(mode) if configured_rates.get(mode) is not None else default
        for mode, default in DEFAULT_INVOICE_RATES.items()
    }
    delivery_dates = [item["deliveryDate"] for item in items if item["deliveryDate"]]
    return {
        "purchaseOrderNo": str(order["po_id"]),
        "orderDate": day(order.get("po_date")),
        "deliveryDate": max(delivery_dates) if delivery_dates else "",
        "status": str(order.get("status") or ""),
        "purchaser": str(order.get("purchaser_name") or ""),
        "receiveAddress": str(order.get("send_address") or ""),
        "warehouse": str(order.get("wms_co_name") or ""),
        "paymentMethod": PAYMENT_METHODS.get(order.get("payment_method"), str(order.get("payment_method") or "")),
        "supplierShortName": supplier_short,
        "supplierMapped": bool(supplier),
        "supplierLegalName": supplier.get("legal_name", ""),
        "invoiceRates": invoice_rates,
        "erpPriceMode": supplier.get("erp_price_mode"),
        "totalQuantity": sum(item["quantity"] for item in items),
        "items": items,
    }


def build_contract_model(po_id, invoice_type, *, tax_rate=None, price_overrides=None,
                         gb_overrides=None, env_path=None, fetched=None,
                         saved_gb=None, gb_lookup=None):
    """把 ERP、供应商字典和产品补充资料合并成合同模型。"""
    if invoice_type not in INVOICE_LABELS:
        raise ValueError("票种只能是 no_invoice、normal_invoice 或 special_invoice")
    env_path = env_path or str(ROOT / "hanli.env")
    if fetched is None:
        order, erp_items = fetch_contract_order(po_id, env_path)
    else:
        order, erp_items = fetched
    buyers = load_json("buyers.json")
    suppliers = load_json("suppliers.json")
    products = load_json("products.json")

    supplier_short = str(order.get("seller") or "").strip()
    supplier = suppliers.get(supplier_short)
    if not supplier:
        raise ValueError(f"供应商简称“{supplier_short}”尚未维护完整信息")
    missing = [key for key in ("legal_name", "address", "contact_name", "contact_phone") if not supplier.get(key)]
    if missing:
        raise ValueError(f"供应商“{supplier_short}”缺少字段：{', '.join(missing)}")

    configured_rate = (supplier.get("invoice_rates") or {}).get(invoice_type)
    default_rate = configured_rate if configured_rate is not None else DEFAULT_INVOICE_RATES[invoice_type]
    rate_value = tax_rate if tax_rate is not None else default_rate
    selected_rate = float(rate_value)
    if not math.isfinite(selected_rate) or selected_rate < 0 or selected_rate > 100:
        raise ValueError("税率必须在 0% 到 100% 之间")

    warehouse_key = str(order.get("send_address") or "").strip()
    buyer = (buyers.get("warehouses") or {}).get(warehouse_key, buyers["default"])
    missing_buyer = [key for key in BUYER_REQUIRED if not str(buyer.get(key) or "").strip()]
    if missing_buyer:
        raise ValueError("买方资料缺少字段：" + "、".join(missing_buyer))
    payment_key = order.get("payment_method")
    if payment_key not in PAYMENT_METHODS:
        raise ValueError("付款方式未维护，不能生成合同")
    overrides = normalize_price_overrides(price_overrides)
    gb_overrides = normalize_gb_overrides(gb_overrides)
    sku_ids = [str(item.get("sku_id") or "").strip() for item in erp_items]
    if saved_gb is None:
        line_saves, sku_saves = load_saved_line_gb(env_path, str(order["po_id"]), sku_ids)
    else:
        line_saves, sku_saves = saved_gb

    def lookup(standard_no):
        if gb_lookup is not None:
            return gb_lookup(standard_no)
        return lookup_standard_by_no(env_path, standard_no)

    items = []
    delivery_dates = []
    for erp_item in erp_items:
        sku = str(erp_item.get("sku_id") or "").strip()
        style = str(erp_item.get("i_id") or "").strip()
        poi_id = str(erp_item.get("poi_id") or "")
        product = products.get(sku) or products.get(style) or {}
        price = overrides.get(sku)
        if price is None:
            price = (product.get("prices") or {}).get(invoice_type)
        if price is None and supplier.get("erp_price_mode") == invoice_type:
            if not product:
                raise ValueError(f"商品 {sku} 未维护商品档案，不能用 ERP 价")
            price = erp_item.get("price")
        if price is None:
            raise ValueError(f"商品 {sku} 尚未维护“{INVOICE_LABELS[invoice_type]}”单价")
        unit = str(product.get("unit") or "").strip()
        if not unit:
            raise ValueError(f"商品 {sku} 未维护单位")
        eta = day(erp_item.get("delivery_date"))
        if eta:
            delivery_dates.append(eta)
        image = resolve_product_image(product, sku=sku, style=style)
        gb = resolve_line_gb(
            poi_id=poi_id, sku=sku, gb_overrides=gb_overrides,
            line_saves=line_saves, sku_saves=sku_saves, lookup=lookup,
        )
        items.append({
            "poiId": poi_id,
            "sku": sku,
            "styleCode": style,
            "nationalCode": str(product.get("national_code") or ""),
            "gbStandard": gb["standard_no"],
            "gbSamrId": gb["samr_id"],
            "gbStandardName": gb["name_cn"],
            "name": str(erp_item.get("name") or product.get("name") or ""),
            "category": str(product.get("category") or ""),
            "virtualCategory": str(product.get("virtual_category") or ""),
            "materialProcess": str(product.get("material_process") or erp_item.get("properties_value") or ""),
            "packaging": str(product.get("packaging") or ""),
            "quantity": parse_quantity(erp_item.get("qty")),
            "unit": unit,
            "unitPrice": float(price),
            "remark": str(erp_item.get("remark") or ""),
            "imagePath": image["path"],
            "imageSource": image["source"],
            "imageStatus": image["status"],
            "imageError": image["error"],
        })

    terms = BASE_TERMS.copy()
    terms[3] = invoice_term(invoice_type, selected_rate)
    delivery_date = max(delivery_dates) if delivery_dates else ""
    payment = PAYMENT_METHODS[payment_key]
    return {
        "purchaseOrderNo": str(order["po_id"]),
        "orderDate": day(order.get("po_date")),
        "deliveryDate": delivery_date,
        "projectLead": "",
        "buyer": buyer,
        "supplier": {
            "shortName": supplier_short,
            "legalName": supplier["legal_name"],
            "address": supplier["address"],
            "contact": f"{supplier['contact_name']} {supplier['contact_phone']}".strip(),
        },
        "invoice": {
            "type": invoice_type,
            "label": INVOICE_LABELS[invoice_type],
            "taxRate": selected_rate,
        },
        "items": items,
        "packagingTerms": buyer["packaging_terms"],
        "paymentTerms": f"{payment}\n采购单号{order['po_id']}",
        "inspectionStandards": buyer["inspection_standards"],
        "deliveryAddress": buyer["delivery_address"],
        "terms": [f"{index + 1}、{term}" for index, term in enumerate(terms)],
        "applicant": str(order.get("purchaser_name") or ""),
    }


def generate_contract(po_id, invoice_type, output_path, *, tax_rate=None, price_overrides=None, gb_overrides=None, preview_path=None, env_path=None):
    """生成最终 Excel；电子表格由 openpyxl 在进程内写入。"""
    env_path = env_path or str(ROOT / "hanli.env")
    model = build_contract_model(
        po_id, invoice_type, tax_rate=tax_rate,
        price_overrides=price_overrides, gb_overrides=gb_overrides, env_path=env_path,
    )
    persist_contract_gb(env_path, po_id, model["items"])
    output_path = Path(output_path).resolve()
    outputs_root = (ROOT / "outputs").resolve()
    if outputs_root not in output_path.parents and output_path.parent != outputs_root:
        raise ValueError("合同输出路径必须位于 outputs/ 下")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_contract_workbook(model, output_path)
    if preview_path:
        render_contract_preview(output_path, preview_path)
    return output_path


def render_contract_preview(xlsx_path, preview_path):
    """用真实办公套件渲染 XLSX，确保嵌入的商品图片出现在预览中。"""
    soffice = contract_setting("CONTRACT_SOFFICE") or shutil.which("soffice")
    pdftoppm = contract_setting("CONTRACT_PDFTOPPM") or shutil.which("pdftoppm")
    if not soffice or not pdftoppm:
        raise RuntimeError("缺少 LibreOffice 或 pdftoppm，无法生成含商品图片的合同预览")
    xlsx_path = Path(xlsx_path).resolve()
    preview_path = Path(preview_path).resolve()
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    process_env = os.environ.copy()
    fontconfig = contract_setting("CONTRACT_FONTCONFIG_FILE")
    if not fontconfig and Path("/opt/homebrew/etc/fonts/fonts.conf").exists():
        fontconfig = "/opt/homebrew/etc/fonts/fonts.conf"
    if fontconfig:
        process_env["FONTCONFIG_FILE"] = fontconfig
    acquired = _PREVIEW_SLOTS.acquire(timeout=30)
    if not acquired:
        raise RuntimeError("合同预览繁忙，请稍后再试")
    try:
        with tempfile.TemporaryDirectory(prefix="contract-preview-") as temp_dir:
            profile = Path(temp_dir) / "lo-profile"
            subprocess.run([
                soffice, "--headless",
                f"-env:UserInstallation=file://{profile}",
                "--convert-to",
                'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}',
                "--outdir", temp_dir, str(xlsx_path),
            ], cwd=ROOT, env=process_env, check=True, stdout=subprocess.DEVNULL,
               timeout=120)
            pdf_path = Path(temp_dir) / f"{xlsx_path.stem}.pdf"
            if not pdf_path.exists():
                raise RuntimeError("办公套件未生成合同预览 PDF")
            prefix = preview_path.with_suffix("")
            subprocess.run([
                pdftoppm, "-png", "-singlefile", "-r", "130",
                str(pdf_path), str(prefix),
            ], cwd=ROOT, check=True, stdout=subprocess.DEVNULL, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("合同预览超时") from exc
    finally:
        _PREVIEW_SLOTS.release()
    if not preview_path.exists():
        raise RuntimeError("合同预览图片生成失败")
    return preview_path

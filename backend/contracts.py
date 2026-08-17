# -*- coding: utf-8 -*-
"""从实时 ERP 数据组装并生成采购合同。"""
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .business_time import business_now
from .contract_history import last_payment_choice, record_payment_choice
from .paths import CONFIG_DIR, OUTPUTS_DIR, ROOT, TEMPLATES_DIR
from .contract_mappings import load_mappings
from .contract_workbook import write_contract_workbook
from .database import (
    clean_master_text,
    connect,
    fetch_contract_order,
    fetch_product_master,
    fetch_supplier_price_history,
    load_all_env,
)
from .gb_standards import (
    CONTRACT_GB_STATUSES,
    family_ids_for,
    fetch_family_standards,
    load_category_map,
    lookup_standard_by_no,
    mark_recommended_options,
    rank_standards,
    serialize_gb_option,
)
from .product_images import resolve_product_image
from .supplier_master import (
    load_supplier_book,
    missing_supplier_fields,
    supplier_issue,
)


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

INTERNAL_PAYMENT_TERMS = "内部往来，不列收付款信息"
DEFAULT_RECEIVING_INFO = (
    "鄂州仓：湖北省鄂州市华容区葛店镇电商大道8号蓝库电子商务有限公司1库1号4号门，"
    "收货人：蜀黍家收货组，13385711803"
)
DEFAULT_INSPECTION_STANDARDS = "1.到仓产品及包装无破损\n2.入仓数量与下单数量一致"
RECEIVING_INFO_LIMIT = 500
INSPECTION_EXTRA_LIMIT = 500
REMARK_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)")
INSPECTION_NUMBERED_RE = re.compile(r"^\d+\s*[\.、．]")

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


def match_buyer(order, buyers=None):
    """按 ERP 发货仓匹配需方资料，未命中用 default。"""
    buyers = buyers if buyers is not None else load_json("buyers.json")
    warehouse_key = str((order or {}).get("send_address") or "").strip()
    return (buyers.get("warehouses") or {}).get(warehouse_key, buyers["default"])


PAYMENT_TEXT_LIMIT = 500


def payment_options(mappings=None):
    """页面下拉用的付款方式条款；label 只用于选择，写进合同的是 text。"""
    mappings = mappings if mappings is not None else load_mappings()
    return [dict(option) for option in mappings["payment_options"]]


def default_payment_option(order, mappings=None):
    """按 ERP 单头 payment_method 预选一条；认不出就不预选，让员工自己挑。"""
    mappings = mappings if mappings is not None else load_mappings()
    erp_code = str((order or {}).get("payment_method") or "").strip()
    return mappings["erp_payment_defaults"].get(erp_code, "")


def normalize_payment_text(raw):
    """手动输入的付款方式：去掉回车与首尾空白，限长，空值视为未填。"""
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if len(text) > PAYMENT_TEXT_LIMIT:
        raise ValueError(f"付款方式不能超过 {PAYMENT_TEXT_LIMIT} 个字")
    return text


def resolve_payment_terms(order, supplier, *, payment_option=None, payment_text=None,
                          mappings=None):
    """员工手输 > 员工选项 > 内部往来 > ERP 预选。都没有就要求先选一条。"""
    mappings = mappings if mappings is not None else load_mappings()
    manual = normalize_payment_text(payment_text)
    if manual:
        return manual
    key = str(payment_option or "").strip()
    if key:
        text = mappings["payment_texts"].get(key)
        if not text:
            raise ValueError(f"付款方式「{key}」不在 config/contract_mappings.json 的 payment_options 里")
        return text
    if (supplier or {}).get("internal"):
        return INTERNAL_PAYMENT_TERMS
    fallback = mappings["payment_texts"].get(default_payment_option(order, mappings))
    if fallback:
        return fallback
    raise ValueError("请先选择付款方式，或手动输入付款条款")


def parse_remark_unit_price(remark):
    """备注里第一个数字当作合同单价，如「包体32+2个魔术贴标3.45」→ 32。"""
    text = str(remark or "").strip()
    if not text:
        return None
    matched = REMARK_PRICE_RE.search(text)
    if not matched:
        return None
    try:
        value = Decimal(matched.group(1))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return float(value)


def normalize_receiving_info(raw, default=""):
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return str(default or DEFAULT_RECEIVING_INFO).strip()
    if len(text) > RECEIVING_INFO_LIMIT:
        raise ValueError(f"收货信息不能超过 {RECEIVING_INFO_LIMIT} 个字")
    return text


def compose_inspection_standards(default_text, extra=None):
    """默认两条保留；手输的非空行接着编号。已带序号的不再加前缀。"""
    base = str(default_text or DEFAULT_INSPECTION_STANDARDS).replace("\r\n", "\n").replace("\r", "\n").strip()
    extras = [
        line.strip()
        for line in str(extra or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip()
    ]
    if extras and len("\n".join(extras)) > INSPECTION_EXTRA_LIMIT:
        raise ValueError(f"检验标准手输不能超过 {INSPECTION_EXTRA_LIMIT} 个字")
    if not extras:
        return base
    start = sum(1 for line in base.split("\n") if line.strip())
    numbered = []
    for offset, line in enumerate(extras, start=1):
        if INSPECTION_NUMBERED_RE.match(line):
            numbered.append(line)
        else:
            numbered.append(f"{start + offset}.{line}")
    return f"{base}\n" + "\n".join(numbered) if base else "\n".join(numbered)


def format_payment_block(payment, po_id, supplier=None):
    """条款 + 采购单号 + 映射表收款账户（内部户不列）。"""
    lines = [str(payment or "").strip()]
    if po_id:
        lines.append(f"采购单号{po_id}")
    supplier = supplier or {}
    if not supplier.get("internal"):
        extras = []
        name = str(supplier.get("bank_account_name") or "").strip()
        bank = str(supplier.get("bank_name") or "").strip()
        account = str(supplier.get("bank_account") or "").strip()
        if name:
            extras.append(f"付款账户名：{name}")
        if bank:
            extras.append(f"开户行：{bank}")
        if account:
            extras.append(f"账户：{account}")
        if extras:
            lines.append("    ".join(extras))
    return "\n".join(line for line in lines if line)


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


def get_contract_options(po_id, env_path=None, supplier_book=None, product_master=None):
    """返回员工选择票种、单价和执行标准时需要的采购信息。"""
    env_path = env_path or str(ROOT / "hanli.env")
    order, erp_items = fetch_contract_order(po_id, env_path)
    book = supplier_book or load_supplier_book()
    products = load_json("products.json")
    buyers = load_json("buyers.json")
    buyer = match_buyer(order, buyers)
    supplier_short = str(order.get("seller") or "").strip()
    supplier = book.lookup(supplier_short) or {}
    sku_ids = [str(item.get("sku_id") or "").strip() for item in erp_items]
    style_ids = [str(item.get("i_id") or "").strip() for item in erp_items]
    master = product_master if product_master is not None else fetch_product_master(
        env_path, sku_ids, style_ids,
    )
    mapping = _load_category_map_or_empty()
    # 历史参考查不到不该挡住整页：库慢或没历史时照常出选项。
    try:
        price_history = fetch_supplier_price_history(
            env_path, supplier_short, sku_ids, exclude_po_id=str(order["po_id"]),
        )
    except Exception:
        price_history = {}
    line_saves, sku_saves = load_saved_line_gb(env_path, str(order["po_id"]), sku_ids)
    family_cache = {}
    items = []
    for item in erp_items:
        sku = str(item.get("sku_id") or "").strip()
        style = str(item.get("i_id") or "").strip()
        poi_id = str(item.get("poi_id") or "")
        product = products.get(sku) or products.get(style) or {}
        sku_master = master.get(sku) or master.get(style) or {}
        image = resolve_product_image(product, sku=sku, style=style)
        erp_category = (
            clean_master_text(product.get("category"))
            or sku_master.get("category")
            or ""
        )
        unit = clean_master_text(product.get("unit")) or sku_master.get("unit") or ""
        national_code = (
            clean_master_text(product.get("national_code"))
            or sku_master.get("national_code")
            or ""
        )
        family_ids = family_ids_for(erp_category, mapping)
        gb_options = mark_recommended_options([
            serialize_gb_option(row)
            for row in rank_standards(
                _family_rows(env_path, family_ids, family_cache),
                product_name=str(item.get("name") or product.get("name") or ""),
                category=erp_category,
            )
        ])
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
            "specification": str(item.get("properties_value") or sku_master.get("specification") or ""),
            "deliveryDate": day(item.get("delivery_date")),
            "category": erp_category,
            "unit": unit,
            "nationalCode": national_code,
            "familyIds": family_ids,
            "gbOptions": gb_options,
            "gbStandard": gb_standard,
            "priceHistory": price_history.get(sku) or [],
            "remark": str(item.get("remark") or ""),
            "remarkPrice": parse_remark_unit_price(item.get("remark")),
        })
    configured_rates = supplier.get("invoice_rates") or {}
    invoice_rates = {
        mode: configured_rates.get(mode) if configured_rates.get(mode) is not None else default
        for mode, default in DEFAULT_INVOICE_RATES.items()
    }
    delivery_dates = [item["deliveryDate"] for item in items if item["deliveryDate"]]
    mappings = load_mappings()
    last_payment = last_payment_choice(supplier_short)
    erp_default = default_payment_option(order, mappings)
    # 上次用过的优先于 ERP 预选：同一家供应商的条款通常沿用。
    if last_payment.get("option") in mappings["payment_texts"]:
        preselected = last_payment["option"]
        payment_source = "history"
        payment_note = f"上次用的（{last_payment.get('poId') or '—'} · {(last_payment.get('at') or '')[:10]}）"
    elif erp_default:
        preselected = erp_default
        payment_source = "erp"
        payment_note = f"按 ERP 付款方式 {order.get('payment_method')} 预选"
    else:
        preselected = ""
        payment_source = ""
        payment_note = ""
    payment_terms = (
        mappings["payment_texts"].get(preselected)
        or (INTERNAL_PAYMENT_TERMS if supplier.get("internal") else "")
    )
    return {
        "purchaseOrderNo": str(order["po_id"]),
        "orderDate": day(order.get("po_date")),
        "deliveryDate": max(delivery_dates) if delivery_dates else "",
        "status": str(order.get("status") or ""),
        "purchaser": str(order.get("purchaser_name") or ""),
        "receiveAddress": str(order.get("send_address") or ""),
        "warehouse": str(order.get("wms_co_name") or ""),
        "paymentMethod": payment_terms,
        "paymentOptions": payment_options(mappings),
        "paymentOption": preselected,
        "paymentSource": payment_source,
        "paymentNote": payment_note,
        "lastPaymentText": last_payment.get("text", ""),
        "supplierShortName": supplier_short,
        "supplierMapped": bool(supplier) and not supplier.get("frozen") and not missing_supplier_fields(supplier),
        "supplierInternal": bool(supplier.get("internal")),
        "supplierFrozen": bool(supplier.get("frozen")),
        "supplierMissingFields": missing_supplier_fields(supplier or None),
        "supplierIssue": supplier_issue(supplier_short, supplier or None),
        "supplierLegalName": supplier.get("legal_name", ""),
        "supplierInvoiceLabel": supplier.get("invoice_label", ""),
        "supplierBankAccountName": supplier.get("bank_account_name", ""),
        "supplierBankName": supplier.get("bank_name", ""),
        "supplierBankAccount": supplier.get("bank_account", ""),
        "receivingInfo": buyer.get("receiving_info") or DEFAULT_RECEIVING_INFO,
        "inspectionStandards": buyer.get("inspection_standards") or DEFAULT_INSPECTION_STANDARDS,
        "invoiceRates": invoice_rates,
        "erpPriceMode": supplier.get("erp_price_mode"),
        "useErpPrice": bool(supplier.get("internal")),
        "totalQuantity": sum(item["quantity"] for item in items),
        "items": items,
    }


def resolve_line_unit_price(erp_item, product, sku_master, *, invoice_type, supplier,
                            override=None):
    """单价：员工覆盖 → 备注首个数字 → products.json → ERP（票种匹配或内部户）。"""
    sku = str(erp_item.get("sku_id") or "").strip()
    if override is not None:
        return float(override)
    remark_price = parse_remark_unit_price(erp_item.get("remark"))
    if remark_price is not None:
        return remark_price
    configured = (product.get("prices") or {}).get(invoice_type)
    if configured is not None:
        return float(configured)
    internal = bool(supplier.get("internal"))
    if supplier.get("erp_price_mode") == invoice_type or internal:
        if not product and not sku_master and not internal:
            raise ValueError(
                f"商品 {sku} 未维护商品档案（config/products.json 与镜像商品资料都没有），不能用 ERP 价"
            )
        if erp_item.get("price") is not None and erp_item.get("price") != "":
            return float(erp_item.get("price"))
    raise ValueError(f"商品 {sku} 尚未维护“{INVOICE_LABELS[invoice_type]}”单价")


def build_contract_model(po_id, invoice_type, *, tax_rate=None, price_overrides=None,
                         gb_overrides=None, env_path=None, fetched=None,
                         saved_gb=None, gb_lookup=None, supplier_book=None, suppliers=None,
                         payment_option=None, payment_text=None, product_master=None,
                         receiving_info=None, inspection_extra=None):
    """把 ERP、供应商主数据和产品补充资料合并成合同模型。"""
    if invoice_type not in INVOICE_LABELS:
        raise ValueError("票种只能是 no_invoice、normal_invoice 或 special_invoice")
    env_path = env_path or str(ROOT / "hanli.env")
    if fetched is None:
        order, erp_items = fetch_contract_order(po_id, env_path)
    else:
        order, erp_items = fetched
    buyers = load_json("buyers.json")
    products = load_json("products.json")

    supplier_short = str(order.get("seller") or "").strip()
    if supplier_book is not None:
        supplier = supplier_book.lookup(supplier_short)
    elif suppliers is not None:
        supplier = suppliers.get(supplier_short)
    else:
        supplier = load_supplier_book().lookup(supplier_short)
    issue = supplier_issue(supplier_short, supplier)
    if issue:
        raise ValueError(issue)

    configured_rate = (supplier.get("invoice_rates") or {}).get(invoice_type)
    default_rate = configured_rate if configured_rate is not None else DEFAULT_INVOICE_RATES[invoice_type]
    rate_value = tax_rate if tax_rate is not None else default_rate
    selected_rate = float(rate_value)
    if not math.isfinite(selected_rate) or selected_rate < 0 or selected_rate > 100:
        raise ValueError("税率必须在 0% 到 100% 之间")

    buyer = match_buyer(order, buyers)
    missing_buyer = [key for key in BUYER_REQUIRED if not str(buyer.get(key) or "").strip()]
    if missing_buyer:
        raise ValueError("买方资料缺少字段：" + "、".join(missing_buyer))
    receiving = normalize_receiving_info(
        receiving_info, buyer.get("receiving_info") or DEFAULT_RECEIVING_INFO,
    )
    inspection = compose_inspection_standards(
        buyer.get("inspection_standards") or DEFAULT_INSPECTION_STANDARDS,
        inspection_extra,
    )
    internal = bool(supplier.get("internal"))
    payment = resolve_payment_terms(
        order, supplier, payment_option=payment_option, payment_text=payment_text,
    )
    payment_key = "" if normalize_payment_text(payment_text) else (
        str(payment_option or "").strip() or default_payment_option(order)
    )
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

    if product_master is not None:
        master = product_master
    else:
        master = fetch_product_master(
            env_path,
            [str(item.get("sku_id") or "").strip() for item in erp_items],
            [str(item.get("i_id") or "").strip() for item in erp_items],
        )
    items = []
    delivery_dates = []
    for erp_item in erp_items:
        sku = str(erp_item.get("sku_id") or "").strip()
        style = str(erp_item.get("i_id") or "").strip()
        poi_id = str(erp_item.get("poi_id") or "")
        product = products.get(sku) or products.get(style) or {}
        sku_master = master.get(sku) or master.get(style) or {}
        price = resolve_line_unit_price(
            erp_item, product, sku_master, invoice_type=invoice_type,
            supplier=supplier, override=overrides.get(sku),
        )
        # 单位 / 条码 / 虚拟分类允许用商品主数据兜底；单价不行，缺价仍然中止。
        unit = clean_master_text(product.get("unit")) or sku_master.get("unit") or ""
        if not unit:
            if sku_master:
                raise ValueError(f"商品 {sku} 在 ERP 商品资料里单位为空，请在聚水潭补单位")
            raise ValueError(
                f"商品 {sku} 未维护单位：config/products.json 没有该 SKU，"
                "镜像库 realtime_products 也没有这条商品资料"
            )
        national_code = (
            clean_master_text(product.get("national_code"))
            or sku_master.get("national_code")
            or ""
        )
        virtual_category = (
            clean_master_text(product.get("virtual_category"))
            or sku_master.get("virtual_category")
            or ""
        )
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
            "nationalCode": national_code,
            "gbStandard": gb["standard_no"],
            "gbSamrId": gb["samr_id"],
            "gbStandardName": gb["name_cn"],
            "name": str(
                erp_item.get("name") or product.get("name") or sku_master.get("name") or ""
            ),
            "category": (
                clean_master_text(product.get("category"))
                or sku_master.get("category")
                or ""
            ),
            "virtualCategory": virtual_category,
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
    return {
        "purchaseOrderNo": str(order["po_id"]),
        "orderDate": day(order.get("po_date")),
        "deliveryDate": delivery_date,
        "projectLead": "",
        "buyer": buyer,
        "supplier": {
            "shortName": supplier_short,
            "legalName": supplier["legal_name"],
            "address": supplier.get("address") or "",
            "contact": (
                "内部往来" if internal
                else f"{supplier['contact_name']} {supplier['contact_phone']}".strip()
            ),
            "internal": internal,
        },
        "invoice": {
            "type": invoice_type,
            "label": INVOICE_LABELS[invoice_type],
            "taxRate": selected_rate,
        },
        "items": items,
        "packagingTerms": buyer["packaging_terms"],
        "paymentOption": payment_key,
        "paymentText": payment,
        "paymentTerms": format_payment_block(payment, order["po_id"], supplier),
        "inspectionStandards": inspection,
        "receivingInfo": receiving,
        "deliveryAddress": receiving,
        "terms": [f"{index + 1}、{term}" for index, term in enumerate(terms)],
        "applicant": str(order.get("purchaser_name") or ""),
    }


def blank_contract_model():
    """每份合同都相同的固定栏：需方、收货信息、包装、检验、送货地址、条款。采购单内容留空。"""
    buyers = load_json("buyers.json")
    buyer = buyers["default"]
    receiving = str(buyer.get("receiving_info") or DEFAULT_RECEIVING_INFO).strip()
    terms = BASE_TERMS.copy()
    terms[3] = "本订货单约定的票种与税率以实际签署为准；"
    return {
        "isTemplate": True,
        "purchaseOrderNo": "",
        "orderDate": "",
        "deliveryDate": "",
        "projectLead": "",
        "buyer": buyer,
        "supplier": {
            "shortName": "",
            "legalName": "",
            "address": "",
            "contact": "",
            "internal": False,
        },
        "invoice": {},
        "items": [{
            "poiId": "",
            "sku": "",
            "styleCode": "",
            "nationalCode": "",
            "gbStandard": "",
            "name": "",
            "category": "",
            "virtualCategory": "",
            "materialProcess": "",
            "packaging": "",
            "quantity": None,
            "unit": "",
            "unitPrice": None,
            "remark": "",
            "imagePath": None,
        }],
        "packagingTerms": buyer["packaging_terms"],
        "paymentOption": "",
        "paymentText": "",
        "paymentTerms": "",
        "inspectionStandards": buyer.get("inspection_standards") or DEFAULT_INSPECTION_STANDARDS,
        "receivingInfo": receiving,
        "deliveryAddress": receiving,
        "terms": [f"{index + 1}、{term}" for index, term in enumerate(terms)],
        "applicant": "",
    }


def write_blank_contract_template(output_path=None):
    """用固定栏生成空白采购合同母版，替换 ``templates/采购合同模板.xlsx``。"""
    path = Path(output_path or TEMPLATES_DIR / "采购合同模板.xlsx")
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_contract_workbook(blank_contract_model(), path)


def generate_contract(po_id, invoice_type, output_path, *, tax_rate=None, price_overrides=None,
                      gb_overrides=None, preview_path=None, env_path=None,
                      payment_option=None, payment_text=None,
                      receiving_info=None, inspection_extra=None):
    """生成最终 Excel；电子表格由 openpyxl 在进程内写入。"""
    env_path = env_path or str(ROOT / "hanli.env")
    model = build_contract_model(
        po_id, invoice_type, tax_rate=tax_rate,
        price_overrides=price_overrides, gb_overrides=gb_overrides, env_path=env_path,
        payment_option=payment_option, payment_text=payment_text,
        receiving_info=receiving_info, inspection_extra=inspection_extra,
    )
    persist_contract_gb(env_path, po_id, model["items"])
    # 只是下次的预选参考，写不进去不该让已经算好的合同失败。
    try:
        record_payment_choice(
            model["supplier"]["shortName"], option=model["paymentOption"],
            text=model["paymentText"], po_id=str(po_id),
        )
    except Exception:
        pass
    output_path = Path(output_path).resolve()
    outputs_root = OUTPUTS_DIR.resolve()
    under_official = outputs_root in output_path.parents or output_path.parent == outputs_root
    if not under_official and "outputs" not in output_path.parts:
        raise ValueError("合同输出路径必须位于 files/outputs/ 下")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_contract_workbook(model, output_path)
    if preview_path:
        render_contract_preview(output_path, preview_path)
    return output_path


def _windows_soffice_paths():
    roots = (
        Path(r"C:\Program Files\LibreOffice\program"),
        Path(r"C:\Program Files (x86)\LibreOffice\program"),
    )
    names = ("soffice.com", "soffice.exe")
    return [root / name for root in roots for name in names]


def _extra_pdftoppm_paths():
    home = Path.home()
    return [
        home / ".cache" / "codex-runtimes" / "codex-primary-runtime"
        / "dependencies" / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe",
    ]


def resolve_preview_binary(setting_name, *candidates):
    """配置路径必须真实存在才用；macOS 的 .env 拷到 Windows 时直接跳过。"""
    configured = str(contract_setting(setting_name) or "").strip()
    names = [configured, *candidates] if configured else list(candidates)
    if setting_name == "CONTRACT_SOFFICE":
        names.extend(str(path) for path in _windows_soffice_paths())
    elif setting_name == "CONTRACT_PDFTOPPM":
        names.extend(str(path) for path in _extra_pdftoppm_paths())
    for name in names:
        if not name:
            continue
        path = Path(name)
        if path.is_file():
            return str(path)
        found = shutil.which(name)
        if found:
            return found
    return ""


def render_contract_preview(xlsx_path, preview_path):
    """用真实办公套件渲染 XLSX，确保嵌入的商品图片出现在预览中。"""
    soffice = resolve_preview_binary("CONTRACT_SOFFICE", "soffice.com", "soffice", "soffice.exe")
    pdftoppm = resolve_preview_binary("CONTRACT_PDFTOPPM", "pdftoppm", "pdftoppm.exe")
    if not soffice or not pdftoppm:
        missing = []
        if not soffice:
            missing.append("LibreOffice（soffice）")
        if not pdftoppm:
            missing.append("poppler（pdftoppm）")
        raise RuntimeError(
            "本机缺少 " + " 和 ".join(missing) + "，无法生成含商品图片的合同预览。"
            "请安装后把可执行文件路径写进 .env 的 CONTRACT_SOFFICE / CONTRACT_PDFTOPPM"
        )
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
            profile.mkdir(parents=True, exist_ok=True)
            subprocess.run([
                soffice, "--headless", "--nologo", "--nolockcheck", "--norestore",
                f"-env:UserInstallation={profile.resolve().as_uri()}",
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
    except FileNotFoundError as exc:
        raise RuntimeError("找不到预览程序，请检查 CONTRACT_SOFFICE / CONTRACT_PDFTOPPM 是否指向本机真实文件") from exc
    finally:
        _PREVIEW_SLOTS.release()
    if not preview_path.exists():
        raise RuntimeError("合同预览图片生成失败")
    return preview_path

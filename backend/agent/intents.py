# -*- coding: utf-8 -*-
"""对话意图路由：能写死的句子不进模型。

对照 DeepSeek Harness 的「用户命令无需模型轮次即可分派」，以及 Grok Build
「先处理 slash / skill，再进采样循环」。本仓库不引入 Cordis / ACP / 子 Agent；
这里只做采购域的确定性路由：抽槽 → 拒答 / 追问 / 调已注册工具。
未识别返回 None，仍交给 LLM 选工具。不在这里做权限判断。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ..quality.parse import parse_quality_command


INSOLE_QUERY = "insole_query"
INSOLE_PROCESS = "insole_process"

KIND_INVOKE = "invoke"
KIND_ASK = "ask"
KIND_REFUSE = "refuse"

_EXCLUDE_INSOLE = ("品控", "开胶", "质量问题", "催办", "采购单")
_INSOLE_HINTS = ("换鞋垫", "鞋垫订单", "鞋垫")
_PROCESS = (
    "进行处理", "处理这些", "处理一下", "开始处理", "进行更换", "换掉", "换了",
    "换鞋垫的动作", "做下换鞋垫", "需要进行换鞋垫", "也需要换鞋垫",
    "需要做下换鞋垫",
)
_QUERY = ("查询", "查一下", "看看", "有哪些", "列出", "需要更换", "待处理")

_ADDRESS = ("收货地址", "改地址", "地址写错", "改收货")
_OVERSELL = ("超卖",)
_REMARK_EXCEPTION = ("备注异常", "备注也有问题")
_CREATE_PO = ("创建采购单", "正式建单", "下采购单", "建一张采购单", "开采购单", "创建采购订单")
_RFQ = ("询价", "问个价", "问个价格", "找三家", "发起询价")
_QUOTE = ("历史报价", "上次报价", "以前这个东西多少钱", "以前多少钱")
_DONT_WRITE = ("先别改", "先别处理", "先别换", "现在有多少", "有多少张", "先查", "先别")
_ANAPHORA = re.compile(r"^(上次那个|上一张|刚才那个|那个呢)(呢|啊|呀)?[？?。]*$")
_BARE_ID = re.compile(r"^\d{5,8}[？?。!！]*$")

_PO_LABEL = re.compile(r"(?:采购单(?:号)?|单号|给)\s*[：:#]?\s*(\d{5,8})")
_PO_BARE = re.compile(r"(?:看一下|查一下|这张)\s*(\d{5,7})\b")
_OID_LABEL = re.compile(r"(?:内部订单|销售订单|订单号|内部单号|订单)\s*[：:#]?\s*(\d{6,10})")
_OID_BARE = re.compile(r"\b(1\d{7,9})\b")
_PLATFORM = re.compile(r"平台单号(?:是)?\s*[：:#]?\s*(\d{8,20})")
_SKU = re.compile(r"\b([A-Z]{2}\d[\w-]{4,22})\b")
_SHORT_TARGET = re.compile(r"换成\s*(\d{4,8})\b")
_NAMED_BUYER = re.compile(r"([\u4e00-\u9fffA-Za-z]{1,12})名下")
_I_AM = re.compile(r"我是\s*([\u4e00-\u9fffA-Za-z]{1,12})")
_FIND_PO = re.compile(r"(?:有没有|查一下|看看)\s*(\S{1,16}?)的?采购单")
_GB_QUERY = re.compile(r"([\u4e00-\u9fffA-Za-z0-9]{2,20})(?:合同)?该填哪条|([\u4e00-\u9fff]{2,12})用什么国标")
_PRODUCT_REST = re.compile(r"(?:查商品|搜商品|搜一下(?:SKU|sku))\s*(.+)$")

_INVOICE = (
    (("专票", "增值税专用", "专用发票"), "special_invoice"),
    (("普票", "普通发票"), "normal_invoice"),
    (("不开票", "无票", "不开发票"), "no_invoice"),
)
_CONTRACT_WANTS = ("出合同", "出一份", "生成采购合同", "生成合同", "采购合同")
_ARRIVAL = ("到货", "到了吗", "到了没", "到了没有", "到哪了", "到哪儿了")
_PO_REF = frozenset({"那张", "这张", "该", "那个", "这个"})
_SWAP = ("换成", "换货", "替换成")
_QUALITY_LIST = ("查一下品控", "品控台账", "品控有哪些", "质量问题清单")
_QUALITY_PUSH = ("品控日报", "发品控日报", "推送品控日报", "发品控")
_PRODUCT_HINTS = ("查商品", "搜商品", "商品主数据", "搜一下sku", "搜一下 SKU", "是什么商品", "是什么款")
_FORECAST_HINTS = ("销量预测", "预测需求", "预测一下")
_SUGGEST_HINTS = ("订货建议", "该补多少", "建议下单", "补货建议")


@dataclass(frozen=True)
class Intent:
    name: str
    arguments: dict = field(default_factory=dict)
    kind: str = KIND_INVOKE
    tool: str = ""
    reply: str = ""
    calls: tuple = ()


def intent_calls(intent: Intent) -> list[tuple[str, dict]]:
    """单工具或同轮两步查询。不猜缺失单号。"""
    if intent.calls:
        return [(str(name), dict(args or {})) for name, args in intent.calls]
    if intent.tool:
        return [(intent.tool, dict(intent.arguments or {}))]
    return []


def _shop_from_text(text: str) -> str:
    """只点名快手或视频号时收窄；说「抖音鞋垫」仍走整池。"""
    found = []
    if "快手" in text:
        found.append("快手")
    if "视频号" in text:
        found.append("视频号")
    if "抖音" in text or "douyin" in text.lower():
        found.append("抖音")
    if not found or found == ["抖音"]:
        return ""
    if len(found) == 1:
        return found[0]
    return ",".join(found)


def extract_slots(text: str) -> dict:
    """从原话抽出编号和范围；不猜缺失的单号。"""
    text = str(text or "")
    slots: dict = {"skus": [], "o_ids": [], "po_ids": []}
    for match in _PO_LABEL.finditer(text):
        _push(slots["po_ids"], match.group(1))
    for match in _PO_BARE.finditer(text):
        _push(slots["po_ids"], match.group(1))
    for match in _OID_LABEL.finditer(text):
        _push(slots["o_ids"], match.group(1))
    if any(token in text for token in ("订单", "o_id", "里面", "里都", "SKU", "sku")):
        for match in _OID_BARE.finditer(text):
            _push(slots["o_ids"], match.group(1))
    platform = _PLATFORM.search(text)
    if platform:
        slots["platform_no"] = platform.group(1)
    for match in _SKU.finditer(text):
        _push(slots["skus"], match.group(1))
    short = _SHORT_TARGET.search(text)
    if short and slots["skus"]:
        source = slots["skus"][0]
        suffix = short.group(1)
        target = source.rsplit("-", 1)[0] + "-" + suffix if "-" in source else source[: -len(suffix)] + suffix
        _push(slots["skus"], target)
    named = _NAMED_BUYER.search(text)
    if named:
        slots["buyer"] = named.group(1)
    iam = _I_AM.search(text)
    if iam:
        slots["buyer"] = iam.group(1)
    elif any(token in text for token in ("我名下", "我的逾期", "我今年", "我名下逾期")):
        slots["buyer"] = "我名下"
    for hints, value in _INVOICE:
        if any(hint in text for hint in hints):
            slots["invoice_type"] = value
            break
    if any(token in text for token in _ARRIVAL):
        for match in re.finditer(r"\b(\d{5,8})\b", text):
            _push(slots["po_ids"], match.group(1))
    if "逾期" in text:
        slots["buckets"] = ["overdue"]
    elif re.search(r"(剩|还剩|T-)?\s*10\s*天|十天档|\bd10\b", text):
        slots["buckets"] = ["d10"]
    elif re.search(r"(剩|还剩|T-)?\s*3\s*天|三天档|\bd3\b", text):
        slots["buckets"] = ["d3"]
    found = _FIND_PO.search(text)
    if found:
        query = found.group(1).strip()
        if query in _PO_REF:
            slots["po_ref"] = query
        elif query:
            slots["query"] = query
    return slots


def classify_intent(text: str, *, working_set: dict | None = None) -> Intent | None:
    """识别已落地的固定意图。未识别返回 None。不检查权限。

    ``working_set`` 只在当前话题已抽出唯一编号时补槽，不猜新单号。
    """
    text = str(text or "").strip()
    if not text:
        return None
    slots = extract_slots(text)
    refused = _refuse(text, slots)
    if refused:
        return refused
    quality = _quality(text, slots)
    if quality:
        return quality
    insole = _insole_intent(text, slots)
    if insole:
        return insole
    if _ANAPHORA.match(text):
        resolved = _anaphora_from_working_set(working_set)
        if resolved:
            return resolved
        return Intent(
            "ask_anaphora", kind=KIND_ASK,
            reply="跨话题后不能用「上次那个」当单号。请说明采购单号、内部订单号或 SKU。",
        )
    _merge_working_set(slots, working_set)
    adjacent = _adjacent(text, slots)
    if adjacent:
        return adjacent
    for builder in (
        _dropship, _send_reminder, _contract, _exchange, _dashboard, _delivery,
        _products, _forecast, _gb, _gaps, _purchase_order, _sales_order, _bare_id,
    ):
        found = builder(text, slots)
        if found:
            return found
    return None


def _merge_working_set(slots: dict, working_set: dict | None) -> None:
    """只补当前话题里唯一的编号。"""
    if not working_set:
        return
    if not slots.get("o_ids"):
        sales = [str(item).strip() for item in (working_set.get("salesOrders") or []) if str(item).strip()]
        if len(sales) == 1:
            slots["o_ids"] = [sales[0]]
    if not slots.get("po_ids"):
        orders = [str(item).strip() for item in (working_set.get("purchaseOrders") or []) if str(item).strip()]
        if len(orders) == 1:
            slots["po_ids"] = [orders[0]]
    if not slots.get("skus"):
        skus = [str(item).strip() for item in (working_set.get("skus") or []) if str(item).strip()]
        if len(skus) == 1:
            slots["skus"] = [skus[0]]


def clarify_working_set(intent: Intent, working_set: dict | None) -> Intent:
    """话题里有多个编号时，追问把候选列出来，仍不猜。"""
    if intent.kind != KIND_ASK or not working_set:
        return intent
    mapping = {
        "ask_sales_order": ("salesOrders", "销售订单"),
        "ask_purchase_order": ("purchaseOrders", "采购单"),
        "ask_product": ("skus", "SKU"),
        "ask_forecast": ("skus", "SKU"),
    }
    spec = mapping.get(intent.name)
    if not spec:
        return intent
    bucket, label = spec
    values = [str(item).strip() for item in (working_set.get(bucket) or []) if str(item).strip()]
    if len(values) < 2:
        return intent
    listed = "、".join(values[:8])
    extra = f"当前话题有{label} {listed}，请指明要看哪一个。"
    reply = str(intent.reply or "").strip()
    if extra in reply:
        return intent
    return replace(intent, reply=f"{reply}{extra}" if reply else extra)


def _anaphora_from_working_set(working_set: dict | None) -> Intent | None:
    if not working_set:
        return None
    buckets = []
    for key, name in (
        ("purchaseOrders", "po"),
        ("salesOrders", "oid"),
        ("skus", "sku"),
    ):
        values = [str(item).strip() for item in (working_set.get(key) or []) if str(item).strip()]
        if values:
            buckets.append((name, values))
    if len(buckets) != 1 or len(buckets[0][1]) != 1:
        return None
    kind, values = buckets[0]
    value = values[0]
    if kind == "po":
        return Intent("get_purchase_order", {"po_id": value}, tool="get_purchase_order")
    if kind == "oid":
        return Intent("search_sales_orders", {"query": value}, tool="search_sales_orders")
    return Intent("search_products", {"query": value}, tool="search_products")


def _insole_oids(text: str, slots: dict | None = None) -> list[str]:
    """鞋垫原话里的内部单号：粘贴多行 1155… 也要抽，不要求先写「订单」。"""
    found = []
    for item in (slots or {}).get("o_ids") or []:
        _push(found, item)
    for match in _OID_BARE.finditer(text):
        _push(found, match.group(1))
    return found


def _insole_intent(text: str, slots: dict | None = None) -> Intent | None:
    if any(token in text for token in _EXCLUDE_INSOLE):
        return None
    if "换成" in text and "XZ25401308-099" in text:
        return None
    arguments = {}
    shop = _shop_from_text(text)
    if shop:
        arguments["shop"] = shop
    o_ids = _insole_oids(text, slots)
    if o_ids:
        arguments["o_ids"] = o_ids
    if any(token in text for token in _PROCESS):
        if any(token in text for token in _INSOLE_HINTS) or any(
            text == token or text.startswith(token) for token in _PROCESS
        ):
            return Intent(INSOLE_PROCESS, arguments, tool="process_insole_orders")
        return None
    if o_ids and any(token in text for token in ("换鞋垫", "鞋垫订单")):
        return Intent(INSOLE_PROCESS, arguments, tool="process_insole_orders")
    if not any(token in text for token in _INSOLE_HINTS):
        return None
    if any(token in text for token in _QUERY) or "订单" in text:
        return Intent(INSOLE_QUERY, arguments, tool="locate_insole_orders")
    return None


def _refuse(text: str, slots: dict) -> Intent | None:
    if any(token in text for token in _ADDRESS):
        return Intent(
            "refuse_address", kind=KIND_REFUSE,
            reply="第一期异常订单只做 SKU 换货，不改地址。",
        )
    if any(token in text for token in _OVERSELL) or (
        any(token in text for token in _REMARK_EXCEPTION) and "处理" in text
    ):
        return Intent(
            "refuse_scope", kind=KIND_REFUSE,
            reply="超卖、备注异常第一期没有规则，不能自行定义，也不能拿换货硬套。",
        )
    if any(token in text for token in _CREATE_PO) or (
        "生成采购单" in text and "合同" not in text
    ) or ("创建" in text and "采购单" in text and "合同" not in text):
        return Intent(
            "refuse_create_po", kind=KIND_REFUSE,
            reply="正式创建采购单还没开闸，不能写 ERP。现有采购单可以查到货或出合同。",
        )
    if any(token in text for token in _RFQ) or re.search(r"\brfq\b", text, re.I):
        return Intent(
            "refuse_rfq", kind=KIND_REFUSE,
            reply="询价和找三家还没开闸，不能编价格或发起 RFQ。",
        )
    if any(token in text for token in _QUOTE):
        return Intent(
            "refuse_quote", kind=KIND_REFUSE,
            reply="历史报价还没开闸，不能编价格。缺价请查主数据缺口。",
        )
    skus = slots.get("skus") or []
    if skus and "缺" not in text and any(token in text for token in ("多少钱", "什么价", "单价多少", "报价")):
        return Intent(
            "refuse_quote", kind=KIND_REFUSE,
            reply="历史报价还没开闸，不能编价格。缺价请查主数据缺口。",
        )
    return None


def _quality(text: str, slots: dict) -> Intent | None:
    if any(token in text for token in _QUALITY_PUSH):
        return Intent("push_quality_report", tool="push_quality_report")
    if "品控" in text and any(token in text for token in ("一键关闭", "全部关闭", "都关掉")):
        return Intent(
            "ask_quality_command", kind=KIND_ASK,
            reply="关闭品控必须带 6 位编号，不能批量关。用法：品控关闭 abcdef。",
        )
    if any(token in text for token in _QUALITY_LIST):
        return Intent("list_quality_issues", tool="list_quality_issues")
    if text in {"品控", "品控登记"}:
        return Intent(
            "ask_quality", kind=KIND_ASK,
            reply="登记品控请写现象，例如「品控 佰特 604264 开胶 3 双」。",
        )
    command = parse_quality_command(text)
    if command is None:
        return None
    action = str(command.get("action") or "")
    if action == "query":
        arguments = {}
        query = str(command.get("query") or "").strip()
        if query:
            arguments["query"] = query
        return Intent("list_quality_issues", arguments, tool="list_quality_issues")
    if action == "record":
        raw = str(command.get("raw") or "").strip()
        if not raw:
            return Intent(
                "ask_quality", kind=KIND_ASK,
                reply="登记品控请写现象，例如「品控 佰特 604264 开胶 3 双」。",
            )
        arguments = {"description": raw}
        po_match = re.search(r"\b(\d{5,8})\b", raw)
        if po_match:
            arguments["po_id"] = po_match.group(1)
        sku_match = _SKU.search(raw)
        if sku_match:
            arguments["sku"] = sku_match.group(1)
        elif slots.get("skus"):
            arguments["sku"] = slots["skus"][0]
        return Intent("record_quality_issue", arguments, tool="record_quality_issue")
    if action == "resolve":
        issue_id = str(command.get("issueId") or "").strip()
        if not issue_id:
            return Intent(
                "ask_quality_command", kind=KIND_ASK,
                reply="关闭品控需要 6 位编号。用法：品控关闭 abcdef。",
            )
        arguments = {"issue_id": issue_id}
        resolution = str(command.get("resolution") or "").strip()
        if resolution:
            arguments["resolution"] = resolution
        return Intent("resolve_quality_issue", arguments, tool="resolve_quality_issue")
    if action == "cancel":
        issue_id = str(command.get("issueId") or "").strip()
        if not issue_id:
            return Intent(
                "ask_quality_command", kind=KIND_ASK,
                reply="撤销品控需要 6 位编号。用法：撤销品控 abcdef。",
            )
        return Intent(
            "cancel_quality_issue", {"issue_id": issue_id}, tool="cancel_quality_issue",
        )
    return None


def _adjacent(text: str, slots: dict) -> Intent | None:
    po_ids = slots.get("po_ids") or []
    skus = slots.get("skus") or []
    arrival = any(token in text for token in _ARRIVAL)
    contract = any(token in text for token in _CONTRACT_WANTS)
    if po_ids and arrival and contract:
        if any(token in text for token in ("别出合同", "不要出合同", "不是出合同", "别出")):
            return Intent("get_purchase_order", {"po_id": po_ids[0]}, tool="get_purchase_order")
        return Intent(
            "ask_adjacent", kind=KIND_ASK,
            arguments={"po_id": po_ids[0]},
            reply="先查到货还是先出合同？请分开说。",
        )
    if any(token in text for token in ("逾期", "催办", "要催")) and any(
        token in text for token in ("鞋垫", "换成", "换货")
    ):
        if "XZ25401308-099" in text or len(skus) >= 2:
            return None
        if any(token in text for token in ("发给钉钉", "发到钉钉", "发催办")):
            return None
        return Intent(
            "ask_adjacent", kind=KIND_ASK,
            reply="是催逾期采购单，还是处理鞋垫/换货？请分开说。",
        )
    if any(token in text for token in ("代发订单", "代发表", "今天的代发")) and contract:
        return Intent(
            "ask_adjacent", kind=KIND_ASK,
            reply="是导出代发表，还是出采购合同？请分开说。",
        )
    if "缺价" in text and contract:
        return Intent(
            "ask_adjacent", kind=KIND_ASK,
            reply="先看主数据缺口，还是先出合同？请分开说。",
        )
    if skus and any(token in text for token in _SUGGEST_HINTS + _FORECAST_HINTS) and contract:
        return Intent(
            "ask_adjacent", kind=KIND_ASK,
            reply="先看订货建议/预测，还是先出合同？请分开说。",
        )
    return None


def _dropship(text: str, slots: dict) -> Intent | None:
    if any(token in text for token in ("代发订单", "代发表", "导出代发", "今天的代发")):
        return Intent("dropship", tool="generate_dropship_workbook")
    return None


def _send_reminder(text: str, slots: dict) -> Intent | None:
    if not any(token in text for token in ("发给钉钉", "发到钉钉", "发催办", "发给群")):
        return None
    if any(token in text for token in _SWAP + ("鞋垫", "出合同", "代发")):
        return None
    arguments = {}
    if slots.get("buckets"):
        arguments["buckets"] = slots["buckets"]
    if slots.get("buyer"):
        arguments["buyer"] = slots["buyer"]
    return Intent("send_reminder", arguments, tool="send_delivery_reminder")


def _contract(text: str, slots: dict) -> Intent | None:
    wants = any(token in text for token in _CONTRACT_WANTS)
    if not wants:
        return None
    if "执行标准" in text or "国标" in text:
        return None
    po_ids = slots.get("po_ids") or []
    invoice = slots.get("invoice_type")
    if po_ids and invoice:
        return Intent(
            "generate_contract",
            {"po_id": po_ids[0], "invoice_type": invoice},
            tool="generate_purchase_contract",
        )
    if po_ids and wants:
        return Intent(
            "ask_contract", kind=KIND_ASK,
            arguments={"po_id": po_ids[0]},
            reply="生成合同需要说明专票 / 普票 / 不开票。",
        )
    if "合同" in text and (invoice or wants) and not po_ids:
        return Intent(
            "ask_contract", kind=KIND_ASK,
            reply="生成合同需要采购单号。请发 ERP 采购单号，并说明专票 / 普票 / 不开票。",
        )
    return None


def _exchange(text: str, slots: dict) -> Intent | None:
    skus = list(slots.get("skus") or [])
    o_ids = list(slots.get("o_ids") or [])
    swapping = any(token in text for token in _SWAP)
    abnormal = any(token in text for token in ("异常订单", "异常单"))
    if not swapping and not abnormal:
        return None
    if any(token in text for token in _INSOLE_HINTS) and "XZ25401308-099" not in text and len(skus) < 2:
        return None
    if any(token in text for token in _DONT_WRITE) and skus:
        arguments = {"source_sku": skus[0], "status": "待发货"}
        return Intent("search_exchange_candidates", arguments, tool="search_sales_orders")
    if swapping and len(skus) >= 2 and o_ids:
        return Intent(
            "submit_exchange",
            {"source_sku": skus[0], "target_sku": skus[1], "o_ids": o_ids},
            tool="submit_exchange_dry_run",
        )
    if swapping and len(skus) >= 2 and not o_ids:
        return Intent(
            "search_exchange_candidates",
            {"source_sku": skus[0], "status": "待发货"},
            tool="search_sales_orders",
        )
    if abnormal and "处理" in text and (len(skus) < 2 or not o_ids):
        return Intent(
            "ask_exchange", kind=KIND_ASK,
            reply="换货需要明确：异常类型、源 SKU、目标 SKU，以及内部订单号。单号不明时先查候选，不能直接改 ERP。",
        )
    return None


def _delivery(text: str, slots: dict) -> Intent | None:
    if not any(token in text for token in ("催", "逾期", "交期", "剩 10", "剩10", "这一档")):
        return None
    if any(token in text for token in ("发给钉钉", "发到钉钉", "发催办")):
        return None
    if any(token in text for token in _SWAP + ("鞋垫", "出合同", "代发", "国标", "品控")):
        return None
    if slots.get("o_ids") and "采购单" not in text:
        return None
    if "今年" in text and "多少" in text:
        return None
    if not any(token in text for token in ("采购单", "催", "档", "我名下", "交期", "有哪些", "清单", "要催")):
        return None
    arguments = {}
    if slots.get("buckets"):
        arguments["buckets"] = slots["buckets"]
    if slots.get("buyer"):
        arguments["buyer"] = slots["buyer"]
    return Intent("delivery_reminders", arguments, tool="delivery_reminders")


def _dashboard(text: str, slots: dict) -> Intent | None:
    wants = any(token in text for token in ("采购金额", "看板", "看看板", "入库率", "待入库大概"))
    if not wants and "今年" in text and "逾期" in text and "多少" in text:
        wants = True
    if not wants:
        return None
    arguments = {}
    if slots.get("buyer"):
        arguments["buyer"] = slots["buyer"]
    return Intent("dashboard_summary", arguments, tool="dashboard_summary")


def _products(text: str, slots: dict) -> Intent | None:
    skus = list(slots.get("skus") or [])
    if any(token in text for token in ("国标", "执行标准", "订单", "采购单", "换成", "换货", "品控")):
        return None
    wants = any(token in text for token in _PRODUCT_HINTS)
    if skus and "是什么" in text:
        wants = True
    if not wants:
        return None
    if skus:
        return Intent("search_products", {"query": skus[0]}, tool="search_products")
    rest = _PRODUCT_REST.search(text)
    query = (rest.group(1) if rest else "").strip(" 一下？?。")
    if query:
        return Intent("search_products", {"query": query}, tool="search_products")
    return Intent(
        "ask_product", kind=KIND_ASK,
        reply="查商品需要 SKU 或商品名称。",
    )


def _forecast(text: str, slots: dict) -> Intent | None:
    skus = list(slots.get("skus") or [])
    wants_suggest = any(token in text for token in _SUGGEST_HINTS)
    wants_forecast = any(token in text for token in _FORECAST_HINTS) or (
        "预测" in text and bool(skus) and "订货" not in text
    )
    if not wants_suggest and not wants_forecast:
        return None
    if wants_suggest:
        if skus:
            return Intent("order_suggestion", {"keys": skus}, tool="order_suggestion")
        return Intent(
            "ask_forecast", kind=KIND_ASK,
            reply="订货建议需要 SKU，库存缺失时会直接说明缺什么，不会用 0 兜底。",
        )
    if skus:
        return Intent("forecast_demand", {"keys": skus}, tool="forecast_demand")
    return Intent(
        "ask_forecast", kind=KIND_ASK,
        reply="销量预测需要 SKU 或款式编码。",
    )


def _gb(text: str, slots: dict) -> Intent | None:
    if "国标码" in text and "执行标准" not in text and "GB/T" not in text and "GB" not in text:
        if any(token in text for token in ("是什么", "是不是", "条形码", "条码")):
            return Intent(
                "ask_gb", kind=KIND_ASK,
                reply="国标码是商品条码，不是 GB/T 执行标准。查执行标准请说分类、商品名或 SKU。",
            )
    if not any(token in text for token in ("执行标准", "国标", "GB/T")):
        return None
    if any(token in text for token in ("同步", "有多少条", "国标库")):
        return Intent("gb_catalog_status", tool="gb_catalog_status")
    arguments = {}
    if slots.get("skus"):
        arguments["sku"] = slots["skus"][0]
    match = _GB_QUERY.search(text)
    if match:
        query = (match.group(1) or match.group(2) or "").replace("合同", "").strip()
        if query and query not in {"出", "给", "这张", "该"}:
            arguments["query"] = query
    elif "毛绒" in text:
        arguments["query"] = "毛绒玩具"
    if arguments:
        return Intent("lookup_gb_standards", arguments, tool="lookup_gb_standards")
    return Intent(
        "ask_gb", kind=KIND_ASK,
        reply="查执行标准请发分类、商品名或 SKU。国标码是商品条码，不是 GB/T。",
    )


def _gaps(text: str, slots: dict) -> Intent | None:
    if any(token in text for token in ("还没维护", "没图", "主数据缺口", "缺价")):
        return Intent("master_data_gaps", tool="master_data_gaps")
    return None


def _purchase_order(text: str, slots: dict) -> Intent | None:
    po_ids = slots.get("po_ids") or []
    if po_ids and any(token in text for token in ("采购单", "待入库", "明细") + _ARRIVAL):
        return Intent("get_purchase_order", {"po_id": po_ids[0]}, tool="get_purchase_order")
    if slots.get("po_ref") and "采购单" in text and not po_ids:
        return Intent(
            "ask_purchase_order", kind=KIND_ASK,
            reply="请发 ERP 采购单号。",
        )
    if any(token in text for token in ("查采购单", "看看采购单", "这张采购单")) and not po_ids:
        return Intent(
            "ask_purchase_order", kind=KIND_ASK,
            reply="请发 ERP 采购单号。",
        )
    if "采购单" not in text:
        return None
    query = slots.get("query") or slots.get("buyer")
    if query:
        return Intent("search_purchase_orders", {"query": query}, tool="search_purchase_orders")
    return None


def _sales_order(text: str, slots: dict) -> Intent | None:
    o_ids = list(slots.get("o_ids") or [])
    skus = list(slots.get("skus") or [])
    if not o_ids:
        for match in _OID_BARE.finditer(text):
            _push(o_ids, match.group(1))
    wants_items = any(token in text for token in (
        "哪些 SKU", "哪些SKU", "商品明细", "明细", "里面", "鞋垫码", "挂着",
    ))
    wants_status = any(token in text for token in (
        "内部订单", "现在什么状态", "订单状态", "还能不能发", "能不能发", "逾期了吗",
    ))
    if o_ids and wants_items and wants_status:
        return Intent(
            "inspect_sales_order",
            {"query": o_ids[0], "o_ids": o_ids},
            tool="get_sales_order_items",
            calls=(
                ("search_sales_orders", {"query": o_ids[0]}),
                ("get_sales_order_items", {"o_ids": o_ids}),
            ),
        )
    if o_ids and wants_items:
        return Intent("get_sales_order_items", {"o_ids": o_ids}, tool="get_sales_order_items")
    if slots.get("platform_no"):
        return Intent(
            "search_sales_orders",
            {"query": slots["platform_no"]},
            tool="search_sales_orders",
        )
    if skus and any(token in text for token in ("待发货", "哪些", "订单里还有", "异常单")):
        return Intent(
            "search_sales_orders",
            {"source_sku": skus[0], "status": "待发货"},
            tool="search_sales_orders",
        )
    if o_ids and wants_status:
        return Intent("search_sales_orders", {"query": o_ids[0]}, tool="search_sales_orders")
    if wants_items and not o_ids and any(token in text for token in ("订单", "里面", "明细")):
        return Intent(
            "ask_sales_order", kind=KIND_ASK,
            reply="看订单明细需要内部订单号。",
        )
    return None


def _bare_id(text: str, slots: dict) -> Intent | None:
    if not _BARE_ID.match(text.strip()):
        return None
    return Intent(
        "ask_which_id", kind=KIND_ASK,
        reply="这是采购单号还是内部订单号？要查到货、出合同还是看出库？",
    )


def _push(bucket: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in bucket:
        bucket.append(text)

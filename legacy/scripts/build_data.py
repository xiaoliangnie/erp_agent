# -*- coding: utf-8 -*-
"""把「采购单完整数据.csv」压成看板用的 dashboard-data.js。

输出结构（字典编码，体积约为原 CSV 的 1/10）：
  PO_DATA.dict   各维度取值表，行数据里存下标
  PO_DATA.orders 采购单级属性（401 条）
  PO_DATA.lines  采购明细行（6208 条），o 字段指向 orders 下标
"""
import json
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "snapshots" / "采购单完整数据.csv"
OUT = ROOT / "frontend" / "data" / "dashboard-data.js"

df = pd.read_csv(SRC, dtype=str, encoding="utf-8-sig")

for c in ["数量", "item_in_qty", "基本金额", "基本售价", "预计到货数量"]:
    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

df["_日期"] = pd.to_datetime(df["采购日期"], errors="coerce").dt.strftime("%Y-%m-%d")
df["_到货"] = (
    pd.to_datetime(df["最早预计到货日期"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
)

# ---------------------------------------------------------------- 尺码解析
# 服装走号型（身高/胸围+体型，如 175/92B），鞋类走鞋码（37码 / 250(2.5) 鞋垫毫米）
SIZE_NONE, SIZE_HT, SIZE_SHOE, SIZE_ALPHA = 0, 1, 2, 3
ALPHA = re.compile(r"^(X{0,3}[SML]|[2-6]XL|均码)$")


def parse_size(spec):
    """返回 (类型, 归一化尺码)。毫米长度按 码=mm/5-10 折算成鞋码。"""
    if not spec:
        return SIZE_NONE, ""
    m = re.match(r"^(\d{3})/\d{2,3}", spec)
    if m and 100 <= int(m.group(1)) <= 200:
        return SIZE_HT, m.group(1)
    m = re.match(r"^(\d{2,3}(?:\.\d)?)\s*码", spec)
    if m:
        return SIZE_SHOE, _shoe(float(m.group(1)))
    m = re.match(r"^(\d{3}(?:\.\d)?)\s*[（(]", spec)  # 250(2.5) / 262.5（2.5）
    if m:
        return SIZE_SHOE, _shoe(float(m.group(1)) / 5 - 10)
    m = re.match(r"^(\d{2}(?:\.\d)?)$", spec)
    if m and 30 <= float(m.group(1)) <= 50:
        return SIZE_SHOE, _shoe(float(m.group(1)))
    if ALPHA.match(spec):
        return SIZE_ALPHA, spec
    return SIZE_NONE, ""


def _shoe(v):
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


parts = df["颜色及规格"].fillna("").str.split(";", n=1)
df["_颜色"] = parts.str[0].fillna("")
df["_规格"] = parts.str[1].fillna("")
sizes = [parse_size(s) for s in df["_规格"]]
df["_尺码类型"] = [s[0] for s in sizes]
df["_尺码"] = [s[1] for s in sizes]

# ---------------------------------------------------------------- 字典编码
DICTS = {}


def encode(series, key, default="未知"):
    vals = series.fillna(default).replace("", default)
    cats = sorted(vals.unique())
    DICTS[key] = cats
    idx = {v: i for i, v in enumerate(cats)}
    return vals.map(idx)


df["_采购员"] = encode(df["采购员"], "buyers")
df["_品类"] = encode(df["item_sku_other_3"], "cats", "未分类")
df["_季节"] = encode(df["item_sku_other_2"], "seasons")
df["_品牌"] = encode(df["item_brand"], "brands")
df["_渠道"] = encode(df["item_sku_other_10"], "channels")
df["_仓储方"] = encode(df["仓储方"], "warehouses", "未指定")
df["_收货"] = encode(df["收货地址"], "addrs", "未指定")
df["_付款"] = encode(df["付款方式"], "pays", "未指定")
df["_款式"] = encode(df["款式编码"], "styles")
df["_商品"] = encode(df["item_sku_other_1"], "spus")
df["_颜色i"] = encode(df["_颜色"], "colors", "—")
df["_供应商"] = encode(df["item_supplier_id"], "suppliers")

# ---------------------------------------------------------------- 采购单
order_no = df["采购单号"]
first = df.groupby(order_no, sort=False).first()
order_ids = list(first.index)
order_idx = {v: i for i, v in enumerate(order_ids)}

orders = [
    [
        oid,                                    # 0 单号
        r["_日期"] or "",                        # 1 采购日期
        1 if r["状态"] == "已确认" else 0,        # 2 状态
        int(r["_采购员"]),                       # 3 采购员
        int(r["_供应商"]),                       # 4 供应商
        int(r["_仓储方"]),                       # 5 仓储方
        int(r["_收货"]),                         # 6 收货地址
        int(r["_付款"]),                         # 7 付款方式
        r["外部单号"] if isinstance(r["外部单号"], str) else "",  # 8 外部单号
        (r["审核日期"] or "")[:10] if isinstance(r["审核日期"], str) else "",  # 9 审核日期
    ]
    for oid, (_, r) in zip(order_ids, first.iterrows())
]

# ---------------------------------------------------------------- 明细行
# 列顺序 = 明细行数组的字段顺序，改这里就等于改 LN_* 常量
LINE_COLS = [
    "采购单号", "_商品", "_款式", "_颜色i", "_规格", "_品类", "_季节", "_品牌",
    "_渠道", "数量", "item_in_qty", "基本金额", "基本售价", "_尺码类型", "_尺码",
    "_到货", "商品编码",
]
INT_COLS = {"_商品", "_款式", "_颜色i", "_品类", "_季节", "_品牌", "_渠道",
            "数量", "item_in_qty", "_尺码类型"}

lines = []
for row in zip(*(df[c] for c in LINE_COLS)):
    rec = []
    for col, v in zip(LINE_COLS, row):
        if col == "采购单号":
            rec.append(order_idx[v])
        elif col in INT_COLS:
            rec.append(int(v))
        elif col in ("基本金额", "基本售价"):
            rec.append(round(float(v), 2))
        else:
            rec.append(v if isinstance(v, str) else "")
    lines.append(rec)

dates = sorted(o[1] for o in orders if o[1])
payload = {
    "meta": {
        "source": SRC.name,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rows": len(lines),
        "orders": len(orders),
        "minDate": dates[0],
        "maxDate": dates[-1],
    },
    "dict": DICTS,
    "orders": orders,
    "lines": lines,
}

with open(OUT, "w", encoding="utf-8") as f:
    f.write("/* 由 build_data.py 生成，请勿手改 */\nwindow.PO_DATA = ")
    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    f.write(";\n")

print(f"{OUT}: {len(lines)} 行 / {len(orders)} 单 / {dates[0]} ~ {dates[-1]}")

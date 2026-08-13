# -*- coding: utf-8 -*-
"""把「采购单完整数据.csv」压成交期提醒台账用的 delivery-data.js。

和 build_data.py 各管各的：那个喂 `采购看板.html`，走 `最早预计到货日期`；
这个喂 `交期提醒台账.html`，走 `item_delivery_date`（交期），只依赖标准库。

输出结构（字典编码）：
  DELIV.dict   各维度取值表，行数据里存下标
  DELIV.orders 采购单级属性（401 条）
  DELIV.lines  采购明细行（6208 条），o 字段指向 orders 下标
单级的数量、交期、提醒档位都在前端按当前筛选现算，这里只出原始事实。
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "snapshots" / "采购单完整数据.csv"
OUT = ROOT / "frontend" / "data" / "delivery-data.js"

csv.field_size_limit(1 << 24)


def day(s):
    """'2026-08-20 23:59:59' -> '2026-08-20'；空值/脏值 -> ''。"""
    s = (s or "").strip()
    return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else ""


def num(s):
    try:
        return float((s or "").strip() or 0)
    except ValueError:
        return 0.0


with open(SRC, encoding="utf-8-sig", newline="") as f:
    rows = list(csv.DictReader(f))

# ---------------------------------------------------------------- 字典编码
DICTS = {}
_idx = {}


def enc(key, val, default="未知"):
    v = (val or "").strip() or default
    table = DICTS.setdefault(key, [])
    lut = _idx.setdefault(key, {})
    if v not in lut:
        lut[v] = len(table)
        table.append(v)
    return lut[v]


# ---------------------------------------------------------------- 采购单
# 单内供应商 / 采购员已校验唯一（见 README），取首行即可
order_idx, orders = {}, []
for r in rows:
    no = r["采购单号"]
    if no in order_idx:
        continue
    order_idx[no] = len(orders)
    orders.append([
        no,                                          # 0  单号
        day(r["采购日期"]),                           # 1  采购日期
        1 if r["状态"] == "已确认" else 0,             # 2  状态
        enc("buyers", r["采购员"]),                   # 3  采购员
        enc("suppliers", r["item_supplier_id"]),      # 4  供应商 ID（数据里没有名称）
        enc("warehouses", r["仓储方"], "未指定"),      # 5  仓储方
        (r.get("外部单号") or "").strip(),             # 6  外部单号
        day(r.get("审核日期")),                        # 7  审核日期
    ])

# ---------------------------------------------------------------- 明细行
lines = []
for r in rows:
    color, _, spec = (r.get("颜色及规格") or "").partition(";")
    lines.append([
        order_idx[r["采购单号"]],                      # 0  所属单
        enc("spus", r["item_sku_other_1"], "未命名"),   # 1  商品
        (r.get("商品编码") or "").strip(),              # 2  商品编码
        enc("colors", color, "—"),                     # 3  颜色
        spec.strip(),                                  # 4  规格
        enc("cats", r["item_sku_other_3"], "未分类"),   # 5  品类
        int(num(r["数量"])),                            # 6  数量
        int(num(r["item_in_qty"])),                     # 7  已入库
        day(r.get("item_delivery_date")),               # 8  交期
        day(r.get("最早预计到货日期")),                  # 9  预计到货日（对照用）
        round(num(r["基本金额"]), 2),                    # 10 金额
    ])

# ---------------------------------------------------------------- 元信息
buy_dates = sorted(o[1] for o in orders if o[1])
etas = sorted({l[8] for l in lines if l[8]})
covered = sum(1 for l in lines if l[8])

payload = {
    "meta": {
        "source": SRC.name,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "rows": len(lines),
        "orders": len(orders),
        "minDate": buy_dates[0],
        "maxDate": buy_dates[-1],
        "etaMin": etas[0],
        "etaMax": etas[-1],
        "etaCoverage": round(covered / len(lines), 4),
        # 快照没有「真实今天」，以最后一笔采购日期为准，前端可改
        "today": buy_dates[-1],
    },
    "dict": DICTS,
    "orders": orders,
    "lines": lines,
}

with open(OUT, "w", encoding="utf-8") as f:
    f.write("/* 由 build_delivery_data.py 生成，请勿手改 */\nwindow.DELIV = ")
    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    f.write(";\n")

size = round(len(open(OUT, encoding="utf-8").read().encode()) / 1024)
print(f"{OUT}: {len(lines)} 行 / {len(orders)} 单 / {size} KB", file=sys.stderr)
print(f"  采购日期 {buy_dates[0]} ~ {buy_dates[-1]}", file=sys.stderr)
print(f"  交期     {etas[0]} ~ {etas[-1]}（{covered}/{len(lines)} 行有交期，"
      f"{covered / len(lines) * 100:.1f}%）", file=sys.stderr)

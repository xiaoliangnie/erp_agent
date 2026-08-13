# -*- coding: utf-8 -*-
"""预测特征抽取。

销量预测的数据前提（架构方案 §6）：实时库需要**销售出库（或订单）表**和**现势库存表**。
这两张表还没进库，所以这里把"从哪张表、哪几列取数"做成配置：表到位后只改 `.env`，
不改代码。在途待入库可以直接从现有采购明细算出来，已经实现。

训练可以完全离线：`load_from_csv` 支持用导出的销售明细先把模型链路跑通。
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from ..database import REALTIME_ITEM_TABLE, REALTIME_MAIN_TABLE, connect
from ..procurement_data import day, integer, number, text


IDENTIFIER = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class DataUnavailable(RuntimeError):
    """缺少必要的数据源；错误信息要说清缺哪张表、该配哪个变量。"""


@dataclass
class SalesTableConfig:
    """销售出库表的位置与列名，全部来自 `.env`。"""

    table: str = ""
    key_column: str = "sku_id"
    date_column: str = "io_date"
    qty_column: str = "qty"
    status_column: str = ""
    excluded_statuses: tuple = ("Cancelled", "Delete", "Merged")

    @classmethod
    def from_settings(cls, setting):
        return cls(
            table=str(setting("FORECAST_SALES_TABLE", "") or "").strip(),
            key_column=str(setting("FORECAST_SALES_KEY_COLUMN", "sku_id") or "sku_id").strip(),
            date_column=str(setting("FORECAST_SALES_DATE_COLUMN", "io_date") or "io_date").strip(),
            qty_column=str(setting("FORECAST_SALES_QTY_COLUMN", "qty") or "qty").strip(),
            status_column=str(setting("FORECAST_SALES_STATUS_COLUMN", "") or "").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.table)

    def validate(self) -> None:
        if not self.configured:
            raise DataUnavailable(
                "销售出库表尚未接入：请在 .env 配置 FORECAST_SALES_TABLE（以及需要时的列名），"
                "或改用 --csv 离线训练"
            )
        for name in (self.key_column, self.date_column, self.qty_column):
            if not IDENTIFIER.fullmatch(name):
                raise DataUnavailable(f"列名 {name} 不是合法标识符")
        if self.status_column and not IDENTIFIER.fullmatch(self.status_column):
            raise DataUnavailable(f"列名 {self.status_column} 不是合法标识符")


@dataclass
class InventoryTableConfig:
    """现势库存表的位置与列名。"""

    table: str = ""
    key_column: str = "sku_id"
    qty_column: str = "qty"

    @classmethod
    def from_settings(cls, setting):
        return cls(
            table=str(setting("FORECAST_INVENTORY_TABLE", "") or "").strip(),
            key_column=str(setting("FORECAST_INVENTORY_KEY_COLUMN", "sku_id") or "sku_id").strip(),
            qty_column=str(setting("FORECAST_INVENTORY_QTY_COLUMN", "qty") or "qty").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self.table)

    def validate(self) -> None:
        if not self.configured:
            raise DataUnavailable(
                "现势库存表尚未接入：请在 .env 配置 FORECAST_INVENTORY_TABLE，"
                "或在调用订货建议时显式传入 inventory"
            )
        for name in (self.key_column, self.qty_column):
            if not IDENTIFIER.fullmatch(name):
                raise DataUnavailable(f"列名 {name} 不是合法标识符")


@dataclass
class DemandDataset:
    """逐日需求序列。粒度由抽取方决定，`Forecaster` 不预设粒度。"""

    records: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        clean = []
        for record in self.records:
            key = str(record.get("key") or "").strip()
            stamp = day(record.get("date"))
            if not key or not stamp:
                continue
            clean.append({"key": key, "date": stamp, "qty": float(record.get("qty") or 0)})
        clean.sort(key=lambda item: (item["key"], item["date"]))
        self.records = clean

    @property
    def keys(self) -> list[str]:
        return sorted({record["key"] for record in self.records})

    @property
    def start(self) -> str:
        return self.records[0]["date"] if self.records else ""

    @property
    def end(self) -> str:
        return max(record["date"] for record in self.records) if self.records else ""

    def series(self, key: str) -> list[tuple[str, float]]:
        """某个 key 的逐日序列，缺失日按 0 补齐。"""
        points = {}
        for record in self.records:
            if record["key"] == key:
                points[record["date"]] = points.get(record["date"], 0.0) + record["qty"]
        if not points:
            return []
        first = date.fromisoformat(min(points))
        last = date.fromisoformat(max(points))
        filled = []
        cursor = first
        while cursor <= last:
            stamp = cursor.isoformat()
            filled.append((stamp, points.get(stamp, 0.0)))
            cursor += timedelta(days=1)
        return filled

    def summary(self) -> dict:
        return {
            "keys": len(self.keys),
            "records": len(self.records),
            "start": self.start,
            "end": self.end,
            "totalQty": round(sum(record["qty"] for record in self.records), 2),
            **self.meta,
        }


def load_from_csv(path, *, key_field="", date_field="", qty_field="") -> DemandDataset:
    """从导出的销售明细 CSV 建数据集；列名可覆盖，缺省猜常见中文表头。"""
    path = Path(path)
    if not path.exists():
        raise DataUnavailable(f"销售明细文件不存在：{path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        key_field = key_field or _pick(headers, ("商品编码", "sku", "sku_id", "SKU"))
        date_field = date_field or _pick(headers, ("日期", "出库日期", "下单日期", "date", "io_date"))
        qty_field = qty_field or _pick(headers, ("数量", "销量", "qty", "quantity"))
        missing = [name for name in (key_field, date_field, qty_field) if not name]
        if missing:
            raise DataUnavailable(
                f"无法在 CSV 表头里识别 SKU / 日期 / 数量列，请用参数显式指定；当前表头：{headers}"
            )
        records = [{
            "key": text(row.get(key_field)),
            "date": day(row.get(date_field)),
            "qty": number(row.get(qty_field)),
        } for row in reader]
    dataset = DemandDataset(records, {"source": f"CSV · {path.name}"})
    if not dataset.records:
        raise DataUnavailable(f"{path} 里没有可用的销售明细行")
    return dataset


def _pick(headers, candidates):
    for candidate in candidates:
        if candidate in headers:
            return candidate
    return ""


def load_from_database(env_path, config: SalesTableConfig, *, start=None, end=None) -> DemandDataset:
    """从实时库的销售出库表抽逐日需求。表名和列名来自配置，不接受调用方拼 SQL。"""
    config.validate()
    end_date = day(end) or date.today().isoformat()
    start_date = day(start) or (date.fromisoformat(end_date) - timedelta(days=730)).isoformat()
    where = [f"LEFT(`{config.date_column}`, 10) >= %s", f"LEFT(`{config.date_column}`, 10) <= %s"]
    params = [start_date, end_date]
    if config.status_column:
        placeholders = ", ".join(["%s"] * len(config.excluded_statuses))
        where.append(f"COALESCE(`{config.status_column}`, '') NOT IN ({placeholders})")
        params.extend(config.excluded_statuses)
    sql = f"""
        SELECT `{config.key_column}` AS `key`,
               LEFT(`{config.date_column}`, 10) AS `date`,
               SUM(COALESCE(`{config.qty_column}`, 0)) AS qty
        FROM `{config.table}`
        WHERE {' AND '.join(where)}
          AND COALESCE(`{config.key_column}`, '') <> ''
        GROUP BY `key`, `date`
        ORDER BY `key`, `date`
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
    dataset = DemandDataset(
        [{"key": text(row.get("key")), "date": day(row.get("date")), "qty": number(row.get("qty"))}
         for row in rows],
        {"source": f"MySQL · {config.table}", "window": [start_date, end_date]},
    )
    if not dataset.records:
        raise DataUnavailable(f"{config.table} 在 {start_date} ~ {end_date} 没有销售数据")
    return dataset


def load_inventory(env_path, config: InventoryTableConfig, keys=None) -> dict:
    """读现势库存；表未接入时抛 DataUnavailable，由调用方决定是否要求显式传入。"""
    config.validate()
    sql = f"""
        SELECT `{config.key_column}` AS `key`, SUM(COALESCE(`{config.qty_column}`, 0)) AS qty
        FROM `{config.table}`
        WHERE COALESCE(`{config.key_column}`, '') <> ''
        GROUP BY `key`
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    wanted = set(keys) if keys else None
    return {text(row.get("key")): number(row.get("qty")) for row in rows
            if wanted is None or text(row.get("key")) in wanted}


def load_in_transit(env_path, keys=None) -> dict:
    """在途待入库 = 未取消采购单里 数量 − 已入库，按 SKU 汇总。

    这份数据现有采购表就能给，不依赖尚未接入的销售/库存表。
    """
    sql = f"""
        SELECT i.sku_id AS sku, i.qty AS qty, i.in_qty AS in_qty
        FROM `{REALTIME_ITEM_TABLE}` AS i
        INNER JOIN `{REALTIME_MAIN_TABLE}` AS m ON m.po_id = i.po_id
        WHERE COALESCE(m.status, '') NOT IN ('Cancelled', 'Delete', 'Merged')
          AND COALESCE(i.sku_id, '') <> ''
    """
    with connect(env_path, autocommit=True) as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    wanted = set(keys) if keys else None
    totals: dict[str, int] = {}
    for row in rows:
        sku = text(row.get("sku"))
        if wanted is not None and sku not in wanted:
            continue
        pending = integer(row.get("qty")) - integer(row.get("in_qty"))
        if pending > 0:
            totals[sku] = totals.get(sku, 0) + pending
    return totals

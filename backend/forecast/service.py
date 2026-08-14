# -*- coding: utf-8 -*-
"""预测查询与订货建议。

订货建议全部是确定性计算，LLM 只负责解释（架构方案 §6）：

    建议下单量 = 交期内预测需求（按服务水平取分位）
               + 安全库存（由预测区间宽度折算）
               − 可用库存 − 在途待入库
    建议下单日 = 需求缺口出现日 − 供应商交期 − 缓冲天数

缺库存数据、缺模型工件时直接报错说明缺什么，不用 0 兜底——避免员工把不完整的
建议当成可下单的结论。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from ..business_time import business_today
from pathlib import Path
from statistics import NormalDist

from .dataset import (
    DataUnavailable,
    InventoryTableConfig,
    SalesTableConfig,
    load_in_transit,
    load_inventory,
)
from .models import ForecastError, assert_quantile_order, sigma_from_interval
from .store import ForecastStore


class ForecastUnavailable(ForecastError):
    """预测能力尚不可用（模型没训练 / 数据源没接入）。"""


class ForecastService:
    def __init__(self, *, store: ForecastStore, env_path: str,
                 sales_config: SalesTableConfig | None = None,
                 inventory_config: InventoryTableConfig | None = None,
                 default_lead_time_days: int = 15, default_buffer_days: int = 3,
                 default_service_level: float = 0.9, default_horizon_days: int = 30,
                 max_keys: int = 50):
        self.store = store
        self.env_path = env_path
        self.sales_config = sales_config or SalesTableConfig()
        self.inventory_config = inventory_config or InventoryTableConfig()
        self.default_lead_time_days = int(default_lead_time_days)
        self.default_buffer_days = int(default_buffer_days)
        self.default_service_level = float(default_service_level)
        self.default_horizon_days = int(default_horizon_days)
        self.max_keys = int(max_keys)
        self._model = None
        self._metadata: dict = {}

    @classmethod
    def from_settings(cls, setting, *, root, env_path):
        directory = Path(setting("FORECAST_MODEL_DIR", "data/models"))
        if not directory.is_absolute():
            directory = Path(root) / directory
        return cls(
            store=ForecastStore(directory),
            env_path=env_path,
            sales_config=SalesTableConfig.from_settings(setting),
            inventory_config=InventoryTableConfig.from_settings(setting),
            default_lead_time_days=int(setting("FORECAST_LEAD_TIME_DAYS", "15") or 15),
            default_buffer_days=int(setting("FORECAST_BUFFER_DAYS", "3") or 3),
            default_service_level=float(setting("FORECAST_SERVICE_LEVEL", "0.9") or 0.9),
            default_horizon_days=int(setting("FORECAST_HORIZON_DAYS", "30") or 30),
        )

    # ------------------------------------------------------------- 模型工件

    def forecaster(self):
        """惰性加载最新工件；加载失败给出该做什么的提示。"""
        if self._model is None:
            try:
                self._model, self._metadata = self.store.load()
            except ForecastError as exc:
                raise ForecastUnavailable(str(exc)) from exc
        return self._model

    def reload(self) -> dict:
        self._model = None
        self._metadata = {}
        self.forecaster()
        return self.status()

    def status(self) -> dict:
        artifact = self.store.status()
        return {
            "ready": bool(artifact.get("latestVersion")),
            "artifact": artifact,
            "loaded": self._metadata.get("version", "") if self._model else "",
            "dataSources": {
                "salesTable": self.sales_config.table or "未接入",
                "inventoryTable": self.inventory_config.table or "未接入",
                "inTransit": "现有采购明细（数量 − 已入库）",
            },
            "defaults": {
                "leadTimeDays": self.default_lead_time_days,
                "bufferDays": self.default_buffer_days,
                "serviceLevel": self.default_service_level,
                "horizonDays": self.default_horizon_days,
            },
        }

    # --------------------------------------------------------------- 预测查询

    def _clean_keys(self, keys) -> list[str]:
        cleaned = []
        for raw in keys or []:
            key = str(raw or "").strip()
            if key and key not in cleaned:
                cleaned.append(key)
        if not cleaned:
            raise ForecastError("至少要给一个 SKU 或款式编码")
        if len(cleaned) > self.max_keys:
            raise ForecastError(f"一次最多查询 {self.max_keys} 个 SKU")
        return cleaned

    def predict(self, keys, *, horizon_days=None, start_date=None) -> dict:
        model = self.forecaster()
        keys = self._clean_keys(keys)
        horizon = max(1, min(int(horizon_days or model.default_horizon_days or self.default_horizon_days), 365))
        points = model.predict(keys, horizon, start_date=start_date)
        assert_quantile_order(points)
        covered = sorted({point["key"] for point in points})
        missing = [key for key in keys if key not in covered]
        series = {}
        for point in points:
            series.setdefault(point["key"], []).append(point)
        return {
            "model": {"name": model.name, "version": model.version,
                      "granularity": model.granularity},
            "horizonDays": horizon,
            "startDate": (start_date or business_today().isoformat()),
            "keys": covered,
            "missingKeys": missing,
            "totals": {key: round(sum(item["p50"] for item in items), 2)
                       for key, items in series.items()},
            "series": series,
            "note": "p50 是点预测，p10/p90 是区间；安全库存由区间宽度折算，不要自行改动这些数字",
        }

    # ------------------------------------------------------------- 订货建议

    def order_suggestion(self, keys, *, lead_time_days=None, service_level=None,
                         buffer_days=None, inventory=None, in_transit=None,
                         today=None) -> dict:
        model = self.forecaster()
        keys = self._clean_keys(keys)
        lead_time = max(1, min(int(lead_time_days or self.default_lead_time_days), 180))
        buffer = max(0, min(int(buffer_days if buffer_days is not None else self.default_buffer_days), 60))
        level = float(service_level if service_level is not None else self.default_service_level)
        if not 0.5 <= level <= 0.99:
            raise ForecastError("服务水平必须在 0.5 到 0.99 之间")
        first = date.fromisoformat(str(today)) if today else business_today()

        stock = self._resolve_inventory(keys, inventory)
        pipeline = self._resolve_in_transit(keys, in_transit)
        # 交期内需求只看前 lead_time 天，但缺口出现日要往后多看一段，否则库存刚好
        # 覆盖交期时就永远算不出建议下单日。
        search_days = max(self.default_horizon_days, lead_time + buffer)
        points = model.predict(keys, search_days, start_date=first.isoformat())
        by_key: dict[str, list[dict]] = {}
        for point in points:
            by_key.setdefault(point["key"], []).append(point)

        z_value = NormalDist().inv_cdf(level)
        suggestions, skipped = [], []
        for key in keys:
            series = by_key.get(key)
            if not series:
                skipped.append({"key": key, "reason": "模型没有这个 SKU 的历史，无法预测"})
                continue
            window = series[:lead_time]
            demand = sum(point["p50"] for point in window)
            variance = sum(sigma_from_interval(point["p10"], point["p90"]) ** 2 for point in window)
            safety = z_value * math.sqrt(variance)
            available = float(stock.get(key, 0))
            on_the_way = float(pipeline.get(key, 0))
            gap = demand + safety - available - on_the_way
            cumulative, gap_date = 0.0, ""
            for point in series:
                cumulative += point["p50"]
                if cumulative > available + on_the_way:
                    gap_date = point["date"]
                    break
            order_date = ""
            if gap_date:
                planned = date.fromisoformat(gap_date) - timedelta(days=lead_time + buffer)
                order_date = max(planned, first).isoformat()
            suggestions.append({
                "key": key,
                "suggestedQty": max(0, math.ceil(gap)),
                "suggestedOrderDate": order_date,
                "orderNow": bool(gap_date and order_date == first.isoformat()),
                "leadTimeDemand": round(demand, 2),
                "safetyStock": round(safety, 2),
                "availableStock": round(available, 2),
                "inTransit": round(on_the_way, 2),
                "shortageDate": gap_date,
                "leadTimeDays": lead_time,
                "searchDays": len(series),
                "interval": {
                    "p10": round(sum(point["p10"] for point in window), 2),
                    "p90": round(sum(point["p90"] for point in window), 2),
                },
            })

        inputs = {
            "keys": keys, "leadTimeDays": lead_time, "bufferDays": buffer,
            "serviceLevel": level, "today": first.isoformat(),
            "availableStock": {key: stock.get(key, 0) for key in keys},
            "inTransit": {key: pipeline.get(key, 0) for key in keys},
        }
        return {
            "model": {"name": model.name, "version": model.version,
                      "granularity": model.granularity},
            "inputs": inputs,
            "parameters": {"leadTimeDays": lead_time, "bufferDays": buffer,
                           "serviceLevel": level, "zValue": round(z_value, 4)},
            "suggestions": suggestions,
            "skipped": skipped,
            "totals": {
                "suggestedQty": sum(item["suggestedQty"] for item in suggestions),
                "keys": len(suggestions),
            },
            "formula": (
                "建议下单量 = 交期内预测需求(∑p50) + 安全库存(z×√∑σ²，σ 由 p90−p10 折算)"
                " − 可用库存 − 在途待入库；建议下单日 = 缺口出现日 − 交期 − 缓冲天数"
            ),
            "note": "这些数字由确定性公式算出，只做解释，不要重算或调整",
        }

    def _resolve_inventory(self, keys, inventory) -> dict:
        if isinstance(inventory, dict) and inventory:
            provided = {str(key): float(value or 0) for key, value in inventory.items()}
            missing = [key for key in keys if key not in provided]
            if missing:
                raise ForecastUnavailable(
                    "缺少这些 SKU 的可用库存：" + "、".join(missing)
                    + "。请补齐 inventory 参数，或接入现势库存表（FORECAST_INVENTORY_TABLE）"
                )
            return provided
        try:
            loaded = load_inventory(self.env_path, self.inventory_config, keys)
        except DataUnavailable as exc:
            raise ForecastUnavailable(str(exc)) from exc
        missing = [key for key in keys if key not in loaded]
        if missing:
            raise ForecastUnavailable(
                "现势库存表里没有这些 SKU：" + "、".join(missing) + "。请核对 SKU 或补齐库存数据"
            )
        return loaded

    def _resolve_in_transit(self, keys, in_transit) -> dict:
        if isinstance(in_transit, dict):
            return {str(key): float(value or 0) for key, value in in_transit.items()}
        return load_in_transit(self.env_path, keys)

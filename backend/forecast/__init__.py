# -*- coding: utf-8 -*-
"""销量预测与订货建议子系统。

对外只暴露 `Forecaster` 接口和 `ForecastService`：模型换实现、换训练方式都不影响
调用方。接入自己训练的模型见 `docs/预测模型接入.md`。
"""
from .dataset import (
    DataUnavailable,
    DemandDataset,
    InventoryTableConfig,
    SalesTableConfig,
    load_from_csv,
    load_from_database,
    load_in_transit,
    load_inventory,
)
from .models import (
    BaselineForecaster,
    ForecastError,
    Forecaster,
    forecaster_ref,
    load_forecaster_class,
    sigma_from_interval,
)
from .service import ForecastService, ForecastUnavailable
from .store import ForecastStore


__all__ = [
    "BaselineForecaster", "DataUnavailable", "DemandDataset", "ForecastError",
    "ForecastService", "ForecastStore", "ForecastUnavailable", "Forecaster",
    "InventoryTableConfig", "SalesTableConfig", "forecaster_ref", "load_forecaster_class",
    "load_from_csv", "load_from_database", "load_in_transit", "load_inventory",
    "sigma_from_interval",
]

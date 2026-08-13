# -*- coding: utf-8 -*-
"""预测模型统一接口。

调用方（`service.py` / 工具层 / `/api/forecast/*`）只依赖 `Forecaster`，换实现不改
调用方。第一版是 `BaselineForecaster`（移动平均 + 星期季节因子），只用来把链路打通。

接入自己训练好的模型只需要三步：
 1. 继承 `Forecaster`，实现 `fit` / `predict`，以及 `state` / `restore`（或直接重写
    `save` / `load`，比如存 LightGBM booster、joblib、torch 权重）；
 2. `scripts/train_forecast_model.py --forecaster 你的模块:你的类` 训练并落工件；
 3. 服务端按工件里的 `forecaster` 引用自动加载，不需要改任何调用方代码。

约定：`predict` 返回逐日点预测与分位区间 `[{key, date, p50, p10, p90}, ...]`，
p10 ≤ p50 ≤ p90 且都不为负。安全库存由区间宽度折算，所以区间必须是真区间。
"""
from __future__ import annotations

import importlib
import json
import math
from abc import ABC, abstractmethod
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist

from .dataset import DemandDataset


MODEL_FILE = "model.json"
# 由 p10/p90 反推标准差用的系数：正态分布下 p90 − p10 = 2.5631σ
INTERVAL_TO_SIGMA = NormalDist().inv_cdf(0.9) - NormalDist().inv_cdf(0.1)


class ForecastError(RuntimeError):
    """预测过程中的可解释错误。"""


class Forecaster(ABC):
    """预测模型统一接口；`name` 和 `version` 会写进每次建议的审计记录。"""

    name: str = "abstract"
    version: str = "0"
    granularity: str = "sku-day"
    default_horizon_days: int = 30

    @abstractmethod
    def fit(self, dataset: DemandDataset) -> None:
        """用历史数据训练。实现方自行决定特征与超参。"""

    @abstractmethod
    def predict(self, keys, horizon_days: int, *, start_date=None) -> list[dict]:
        """返回 `[{key, date, p50, p10, p90}, ...]`，逐 key 逐日，长度 = keys × horizon_days。"""

    def state(self) -> dict:
        """可 JSON 序列化的模型参数。重写 `save`/`load` 时可以不实现。"""
        raise NotImplementedError("请实现 state() 或重写 save()")

    def restore(self, state: dict) -> None:
        raise NotImplementedError("请实现 restore() 或重写 load()")

    def save(self, directory) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / MODEL_FILE).write_text(
            json.dumps({"name": self.name, "version": self.version,
                        "granularity": self.granularity,
                        "defaultHorizonDays": self.default_horizon_days,
                        "state": self.state()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, directory) -> "Forecaster":
        payload = json.loads((Path(directory) / MODEL_FILE).read_text(encoding="utf-8"))
        model = cls()
        model.name = payload.get("name", cls.name)
        model.version = str(payload.get("version", cls.version))
        model.granularity = payload.get("granularity", cls.granularity)
        model.default_horizon_days = int(payload.get("defaultHorizonDays", cls.default_horizon_days))
        model.restore(payload.get("state") or {})
        return model

    def describe(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "granularity": self.granularity,
            "defaultHorizonDays": self.default_horizon_days,
            "forecaster": forecaster_ref(type(self)),
            "keys": len(self.known_keys()),
        }

    def known_keys(self) -> list[str]:
        """模型见过的 key；用于提前告诉员工哪些 SKU 没有可用历史。"""
        return []


def forecaster_ref(target) -> str:
    """把 Forecaster 类序列化成 `模块:类名`，工件靠它找回实现。"""
    cls = target if isinstance(target, type) else type(target)
    return f"{cls.__module__}:{cls.__qualname__}"


def load_forecaster_class(ref: str):
    """按 `模块:类名` 导入 Forecaster 子类。"""
    ref = str(ref or "").strip()
    if ":" not in ref:
        raise ForecastError(f"模型引用 {ref!r} 格式应为 模块:类名")
    module_name, class_name = ref.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ForecastError(f"无法导入模型模块 {module_name}：{exc}") from exc
    target = module
    for part in class_name.split("."):
        target = getattr(target, part, None)
        if target is None:
            raise ForecastError(f"模块 {module_name} 里没有 {class_name}")
    if not (isinstance(target, type) and issubclass(target, Forecaster)):
        raise ForecastError(f"{ref} 不是 Forecaster 的子类")
    return target


class BaselineForecaster(Forecaster):
    """移动平均 + 星期季节因子。

    先把「抽特征 → 训练 → 落工件 → 服务端加载 → 订货建议」整条链路跑通，
    数字精度不是它的目标；换成 LightGBM 或统计模型时接口不变。
    """

    name = "baseline-seasonal"

    def __init__(self, *, window_days: int = 28, min_history_days: int = 7,
                 seasonality: str = "weekday"):
        self.window_days = max(7, int(window_days))
        self.min_history_days = max(1, int(min_history_days))
        self.seasonality = seasonality
        self.levels: dict[str, float] = {}
        self.sigmas: dict[str, float] = {}
        self.factors: list[float] = [1.0] * 7
        self.trained_through: str = ""

    def fit(self, dataset: DemandDataset) -> None:
        if not dataset.records:
            raise ForecastError("训练数据为空")
        weekday_totals = [0.0] * 7
        weekday_counts = [0] * 7
        levels, sigmas = {}, {}
        for key in dataset.keys:
            series = dataset.series(key)
            if len(series) < self.min_history_days:
                continue
            window = series[-self.window_days:]
            values = [qty for _, qty in window]
            level = sum(values) / len(values)
            levels[key] = level
            variance = sum((qty - level) ** 2 for qty in values) / max(1, len(values) - 1)
            sigmas[key] = math.sqrt(variance)
            for stamp, qty in window:
                index = date.fromisoformat(stamp).weekday()
                weekday_totals[index] += qty
                weekday_counts[index] += 1
        if not levels:
            raise ForecastError(
                f"没有任何 SKU 满足最少 {self.min_history_days} 天历史，无法训练"
            )
        overall = sum(weekday_totals) / max(1, sum(weekday_counts))
        if self.seasonality == "weekday" and overall > 0:
            self.factors = [
                round((weekday_totals[index] / weekday_counts[index]) / overall, 4)
                if weekday_counts[index] else 1.0
                for index in range(7)
            ]
        else:
            self.factors = [1.0] * 7
        self.levels = levels
        self.sigmas = sigmas
        self.trained_through = dataset.end
        self.version = f"{dataset.end or date.today().isoformat()}-w{self.window_days}"

    def predict(self, keys, horizon_days: int, *, start_date=None) -> list[dict]:
        horizon_days = max(1, int(horizon_days or self.default_horizon_days))
        if not self.levels:
            raise ForecastError("模型尚未训练，无法预测")
        first = date.fromisoformat(str(start_date)) if start_date else date.today()
        low = NormalDist().inv_cdf(0.1)
        high = NormalDist().inv_cdf(0.9)
        points = []
        for key in keys:
            level = self.levels.get(key)
            if level is None:
                continue
            sigma = self.sigmas.get(key, 0.0)
            for offset in range(horizon_days):
                current = first + timedelta(days=offset)
                factor = self.factors[current.weekday()]
                p50 = max(0.0, level * factor)
                spread = sigma * factor
                points.append({
                    "key": key,
                    "date": current.isoformat(),
                    "p50": round(p50, 3),
                    "p10": round(max(0.0, p50 + low * spread), 3),
                    "p90": round(max(0.0, p50 + high * spread), 3),
                })
        return points

    def known_keys(self) -> list[str]:
        return sorted(self.levels)

    def state(self) -> dict:
        return {
            "windowDays": self.window_days,
            "minHistoryDays": self.min_history_days,
            "seasonality": self.seasonality,
            "factors": self.factors,
            "trainedThrough": self.trained_through,
            "levels": {key: round(value, 4) for key, value in self.levels.items()},
            "sigmas": {key: round(value, 4) for key, value in self.sigmas.items()},
        }

    def restore(self, state: dict) -> None:
        self.window_days = int(state.get("windowDays", self.window_days))
        self.min_history_days = int(state.get("minHistoryDays", self.min_history_days))
        self.seasonality = state.get("seasonality", self.seasonality)
        factors = state.get("factors") or []
        self.factors = [float(value) for value in factors] if len(factors) == 7 else [1.0] * 7
        self.trained_through = str(state.get("trainedThrough") or "")
        self.levels = {str(key): float(value) for key, value in (state.get("levels") or {}).items()}
        self.sigmas = {str(key): float(value) for key, value in (state.get("sigmas") or {}).items()}


def sigma_from_interval(p10: float, p90: float) -> float:
    """由分位区间宽度反推日标准差，安全库存就是从这里折算的。"""
    return max(0.0, (float(p90) - float(p10)) / INTERVAL_TO_SIGMA)

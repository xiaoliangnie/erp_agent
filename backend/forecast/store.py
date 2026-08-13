# -*- coding: utf-8 -*-
"""模型工件的读写与版本管理。

训练离线跑，工件落在 `FORECAST_MODEL_DIR`（默认 `data/models/`，已 gitignore）；
服务端只读。每个版本一个目录，`metadata.json` 记训练窗口、评估指标、特征清单和
`forecaster` 引用——服务端靠这个引用把任何 `Forecaster` 子类加载回来。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import ForecastError, Forecaster, forecaster_ref, load_forecaster_class


METADATA_FILE = "metadata.json"
LATEST_FILE = "latest"


class ForecastStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def versions(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(
            item.name for item in self.directory.iterdir()
            if item.is_dir() and (item / METADATA_FILE).exists()
        )

    def latest_version(self) -> str:
        pointer = self.directory / LATEST_FILE
        if pointer.exists():
            name = pointer.read_text(encoding="utf-8").strip()
            if name and (self.directory / name / METADATA_FILE).exists():
                return name
        versions = self.versions()
        return versions[-1] if versions else ""

    def metadata(self, version: str) -> dict:
        path = self.directory / version / METADATA_FILE
        if not path.exists():
            raise ForecastError(f"模型版本 {version} 不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, model: Forecaster, *, version: str = "", metadata: dict | None = None,
             mark_latest: bool = True) -> dict:
        version = str(version or model.version or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
        version = version.replace("/", "-").replace("..", "-")
        target = self.directory / version
        target.mkdir(parents=True, exist_ok=True)
        model.save(target)
        payload = {
            "version": version,
            "name": model.name,
            "forecaster": forecaster_ref(type(model)),
            "granularity": model.granularity,
            "defaultHorizonDays": model.default_horizon_days,
            "keys": len(model.known_keys()),
            "trainedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **(metadata or {}),
        }
        (target / METADATA_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        if mark_latest:
            (self.directory / LATEST_FILE).write_text(version, encoding="utf-8")
        return payload

    def load(self, version: str = "") -> tuple[Forecaster, dict]:
        version = version or self.latest_version()
        if not version:
            raise ForecastError(
                f"{self.directory} 下没有任何模型工件："
                "请先运行 scripts/train_forecast_model.py 训练，或把训练好的工件放进该目录"
            )
        metadata = self.metadata(version)
        ref = metadata.get("forecaster")
        if not ref:
            raise ForecastError(f"模型版本 {version} 的 metadata.json 缺少 forecaster 引用")
        model = load_forecaster_class(ref).load(self.directory / version)
        model.version = metadata.get("version", model.version)
        return model, metadata

    def status(self) -> dict:
        version = self.latest_version()
        info = {
            "directory": str(self.directory),
            "versions": self.versions(),
            "latestVersion": version,
        }
        if version:
            try:
                metadata = self.metadata(version)
                info.update(
                    model=metadata.get("name", ""),
                    forecaster=metadata.get("forecaster", ""),
                    granularity=metadata.get("granularity", ""),
                    trainedAt=metadata.get("trainedAt", ""),
                    keys=metadata.get("keys", 0),
                    metrics=metadata.get("metrics", {}),
                )
            except (ForecastError, json.JSONDecodeError) as exc:
                info["error"] = str(exc)
        return info

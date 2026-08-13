#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线训练预测模型并落工件。

服务端只读工件，训练完全离线（cron / 定时任务）。

    # 用导出的销售明细 CSV 训练第一版 Baseline
    .venv/bin/python scripts/train_forecast_model.py --csv data/snapshots/销售明细.csv

    # 销售表进实时库后（.env 配好 FORECAST_SALES_TABLE）
    .venv/bin/python scripts/train_forecast_model.py --days 365

    # 换成自己训练好的模型实现
    .venv/bin/python scripts/train_forecast_model.py --csv ... \
        --forecaster mymodels.lgbm:LgbmForecaster

`--forecaster` 接受任何 `Forecaster` 子类（`模块:类名`）。工件目录里的
metadata.json 会记下这个引用，服务端据此把实现加载回来，调用方不用改。
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.database import load_all_env  # noqa: E402
from backend.forecast import (  # noqa: E402
    BaselineForecaster,
    DataUnavailable,
    ForecastError,
    ForecastStore,
    SalesTableConfig,
    load_forecaster_class,
    load_from_csv,
    load_from_database,
)


PROJECT_ENV = load_all_env(ROOT / ".env") if (ROOT / ".env").exists() else {}


def setting(name, default=""):
    """与服务端同一规则：进程环境变量优先于 .env。"""
    return os.environ.get(name, PROJECT_ENV.get(name, default))


def evaluate(model, dataset, holdout_days):
    """留出最后若干天做回测，指标写进工件元数据。"""
    if holdout_days <= 0:
        return {}
    cutoff = dataset.end
    if not cutoff:
        return {}
    start = (date.fromisoformat(cutoff) - timedelta(days=holdout_days - 1)).isoformat()
    actual = {}
    for record in dataset.records:
        if record["date"] >= start:
            actual[(record["key"], record["date"])] = actual.get((record["key"], record["date"]), 0) + record["qty"]
    if not actual:
        return {}
    points = model.predict(dataset.keys, holdout_days, start_date=start)
    errors, absolutes, total = [], [], 0.0
    for point in points:
        truth = actual.get((point["key"], point["date"]), 0.0)
        errors.append(point["p50"] - truth)
        absolutes.append(abs(point["p50"] - truth))
        total += truth
    if not absolutes:
        return {}
    mae = sum(absolutes) / len(absolutes)
    return {
        "holdoutDays": holdout_days,
        "holdoutStart": start,
        "points": len(absolutes),
        "mae": round(mae, 4),
        "bias": round(sum(errors) / len(errors), 4),
        "wape": round(sum(absolutes) / total, 4) if total else None,
        "note": "留出期回测：模型在自己的训练窗口里评估，仅供版本间比较",
    }


def main():
    parser = argparse.ArgumentParser(description="训练销量预测模型并写入工件目录")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--csv", help="销售明细 CSV，列可用 --key-field/--date-field/--qty-field 指定")
    source.add_argument("--from-db", action="store_true", help="从 FORECAST_SALES_TABLE 抽数（默认）")
    parser.add_argument("--key-field", default="", help="CSV 里的 SKU 列名")
    parser.add_argument("--date-field", default="", help="CSV 里的日期列名")
    parser.add_argument("--qty-field", default="", help="CSV 里的数量列名")
    parser.add_argument("--days", type=int, default=365, help="从数据库抽多少天历史，默认 365")
    parser.add_argument("--forecaster", default="", help="Forecaster 实现，格式 模块:类名")
    parser.add_argument("--window-days", type=int, default=28, help="Baseline 的移动平均窗口")
    parser.add_argument("--min-history-days", type=int, default=7, help="SKU 最少历史天数")
    parser.add_argument("--holdout-days", type=int, default=14, help="留出回测天数，0 表示不评估")
    parser.add_argument("--version", default="", help="工件版本名，缺省用模型自报版本")
    parser.add_argument("--model-dir", default="", help="工件目录，缺省用 FORECAST_MODEL_DIR")
    parser.add_argument("--no-latest", action="store_true", help="不把这次训练标记为 latest")
    args = parser.parse_args()

    try:
        if args.csv:
            dataset = load_from_csv(args.csv, key_field=args.key_field,
                                    date_field=args.date_field, qty_field=args.qty_field)
        else:
            config = SalesTableConfig.from_settings(setting)
            end = date.today()
            dataset = load_from_database(
                str(ROOT / setting("REALTIME_DATABASE_ENV_FILE", "hanli.env")), config,
                start=(end - timedelta(days=max(1, args.days))).isoformat(), end=end.isoformat(),
            )
        print("数据集：" + json.dumps(dataset.summary(), ensure_ascii=False))

        if args.forecaster:
            model = load_forecaster_class(args.forecaster)()
        else:
            model = BaselineForecaster(window_days=args.window_days,
                                       min_history_days=args.min_history_days)
        model.fit(dataset)
        metrics = evaluate(model, dataset, args.holdout_days)

        directory = Path(args.model_dir or setting("FORECAST_MODEL_DIR", "data/models"))
        if not directory.is_absolute():
            directory = ROOT / directory
        metadata = ForecastStore(directory).save(
            model, version=args.version, mark_latest=not args.no_latest,
            metadata={
                "dataset": dataset.summary(),
                "trainWindow": [dataset.start, dataset.end],
                "features": ["历史逐日销量", "星期季节因子"] if not args.forecaster else [],
                "metrics": metrics,
                "trainedBy": "scripts/train_forecast_model.py",
            },
        )
    except (DataUnavailable, ForecastError) as exc:
        raise SystemExit(f"训练中止：{exc}")

    print("工件：" + json.dumps(metadata, ensure_ascii=False, indent=2))
    print(f"\n已写入 {directory / metadata['version']}")
    print("服务端会在下次调用预测时加载，也可以调 POST /api/forecast/reload 立即重载。")


if __name__ == "__main__":
    main()

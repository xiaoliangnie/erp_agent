# -*- coding: utf-8 -*-
"""预测接口、工件版本管理与订货建议的确定性计算。全部离线，不连数据库。"""
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from backend.forecast import (
    BaselineForecaster,
    DemandDataset,
    ForecastError,
    ForecastService,
    ForecastStore,
    ForecastUnavailable,
    Forecaster,
    forecaster_ref,
    load_forecaster_class,
    sigma_from_interval,
)


def daily_dataset(keys=("SKU-1",), days=60, qty=10.0, end=None):
    end = date.fromisoformat(end) if end else date(2026, 8, 10)
    records = []
    for key in keys:
        for offset in range(days):
            records.append({
                "key": key,
                "date": (end - timedelta(days=days - 1 - offset)).isoformat(),
                "qty": qty,
            })
    return DemandDataset(records, {"source": "test"})


class FlatForecaster(Forecaster):
    """最小可用的自定义实现，验证「训练好的模型可以直接接进来」这条路。"""

    name = "flat-test"

    def __init__(self, level=7.0):
        self.level = float(level)
        self.version = "test-1"

    def fit(self, dataset):
        values = [record["qty"] for record in dataset.records]
        self.level = sum(values) / len(values)

    def predict(self, keys, horizon_days, *, start_date=None):
        first = date.fromisoformat(str(start_date)) if start_date else date.today()
        return [{
            "key": key,
            "date": (first + timedelta(days=offset)).isoformat(),
            "p50": self.level, "p10": self.level * 0.5, "p90": self.level * 1.5,
        } for key in keys for offset in range(horizon_days)]

    def state(self):
        return {"level": self.level}

    def restore(self, state):
        self.level = float(state.get("level", 0))

    def known_keys(self):
        return []


class DatasetTests(unittest.TestCase):
    def test_series_fills_missing_days_with_zero(self):
        dataset = DemandDataset([
            {"key": "A", "date": "2026-08-01", "qty": 3},
            {"key": "A", "date": "2026-08-04", "qty": 5},
        ])
        self.assertEqual(
            [("2026-08-01", 3.0), ("2026-08-02", 0.0), ("2026-08-03", 0.0), ("2026-08-04", 5.0)],
            dataset.series("A"),
        )

    def test_summary_and_key_cleanup(self):
        dataset = DemandDataset([
            {"key": " A ", "date": "2026-08-01", "qty": 2},
            {"key": "", "date": "2026-08-01", "qty": 9},
            {"key": "B", "date": "", "qty": 9},
        ])
        self.assertEqual(["A"], dataset.keys)
        self.assertEqual(1, dataset.summary()["records"])


class BaselineForecasterTests(unittest.TestCase):
    def test_fit_predict_returns_ordered_quantiles(self):
        model = BaselineForecaster(window_days=28, min_history_days=7)
        model.fit(daily_dataset(days=60, qty=10))
        points = model.predict(["SKU-1"], 5, start_date="2026-08-11")
        self.assertEqual(5, len(points))
        for point in points:
            self.assertLessEqual(point["p10"], point["p50"])
            self.assertLessEqual(point["p50"], point["p90"])
            self.assertGreaterEqual(point["p10"], 0)
        self.assertAlmostEqual(10.0, points[0]["p50"], places=2)

    def test_unknown_key_is_skipped_not_invented(self):
        model = BaselineForecaster()
        model.fit(daily_dataset(keys=("SKU-1",)))
        self.assertEqual([], model.predict(["SKU-UNKNOWN"], 3))

    def test_refuses_to_train_without_enough_history(self):
        model = BaselineForecaster(min_history_days=30)
        with self.assertRaisesRegex(ForecastError, "无法训练"):
            model.fit(daily_dataset(days=5))

    def test_save_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BaselineForecaster()
            model.fit(daily_dataset(days=40, qty=6))
            model.save(Path(tmp))
            restored = BaselineForecaster.load(Path(tmp))
            self.assertEqual(model.levels, restored.levels)
            self.assertEqual(
                model.predict(["SKU-1"], 3, start_date="2026-08-11"),
                restored.predict(["SKU-1"], 3, start_date="2026-08-11"),
            )


class ForecasterPluginTests(unittest.TestCase):
    def test_ref_round_trip(self):
        ref = forecaster_ref(FlatForecaster)
        self.assertEqual(FlatForecaster, load_forecaster_class(ref))

    def test_rejects_non_forecaster(self):
        with self.assertRaisesRegex(ForecastError, "不是 Forecaster"):
            load_forecaster_class("json:JSONDecoder")
        with self.assertRaisesRegex(ForecastError, "格式应为"):
            load_forecaster_class("nocolon")

    def test_store_loads_custom_implementation_by_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ForecastStore(Path(tmp) / "models")
            model = FlatForecaster()
            model.fit(daily_dataset(qty=8))
            metadata = store.save(model, version="v1", metadata={"metrics": {"mae": 0.5}})
            self.assertEqual(forecaster_ref(FlatForecaster), metadata["forecaster"])
            loaded, meta = store.load()
            self.assertIsInstance(loaded, FlatForecaster)
            self.assertAlmostEqual(8.0, loaded.level, places=6)
            self.assertEqual({"mae": 0.5}, meta["metrics"])

    def test_latest_pointer_tracks_newest_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ForecastStore(Path(tmp) / "models")
            model = FlatForecaster()
            model.fit(daily_dataset(qty=8))
            store.save(model, version="v1")
            store.save(model, version="v2")
            self.assertEqual("v2", store.latest_version())
            self.assertEqual(["v1", "v2"], store.versions())
            store.save(model, version="v3", mark_latest=False)
            self.assertEqual("v2", store.latest_version())

    def test_missing_artifact_says_what_to_do(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ForecastError, "train_forecast_model"):
                ForecastStore(Path(tmp) / "models").load()


class OrderSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ForecastStore(Path(self.tmp.name) / "models")
        model = FlatForecaster(level=10.0)
        self.store.save(model, version="v1")
        self.service = ForecastService(
            store=self.store, env_path="unused.env",
            default_lead_time_days=10, default_buffer_days=2, default_service_level=0.9,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def suggest(self, **kwargs):
        options = {"inventory": {"SKU-1": 30}, "in_transit": {"SKU-1": 0},
                   "today": "2026-08-11"}
        options.update(kwargs)
        return self.service.order_suggestion(["SKU-1"], **options)

    def test_formula_matches_documented_definition(self):
        result = self.suggest()
        suggestion = result["suggestions"][0]
        self.assertEqual(100.0, suggestion["leadTimeDemand"])
        sigma_day = sigma_from_interval(5.0, 15.0)
        expected_safety = result["parameters"]["zValue"] * math.sqrt(10 * sigma_day ** 2)
        self.assertAlmostEqual(expected_safety, suggestion["safetyStock"], places=2)
        expected_qty = math.ceil(100.0 + expected_safety - 30 - 0)
        self.assertEqual(expected_qty, suggestion["suggestedQty"])

    def test_inventory_and_in_transit_reduce_the_suggestion(self):
        lean = self.suggest()["suggestions"][0]["suggestedQty"]
        rich = self.suggest(inventory={"SKU-1": 300}, in_transit={"SKU-1": 200})
        self.assertEqual(0, rich["suggestions"][0]["suggestedQty"])
        self.assertEqual("", rich["suggestions"][0]["shortageDate"])
        self.assertGreater(lean, 0)

    def test_shortage_date_drives_order_date(self):
        result = self.suggest(inventory={"SKU-1": 45}, lead_time_days=2, buffer_days=1)
        suggestion = result["suggestions"][0]
        self.assertEqual("2026-08-15", suggestion["shortageDate"])
        self.assertEqual("2026-08-12", suggestion["suggestedOrderDate"])

    def test_order_now_when_shortage_is_within_lead_time(self):
        suggestion = self.suggest(inventory={"SKU-1": 0})["suggestions"][0]
        self.assertEqual("2026-08-11", suggestion["shortageDate"])
        self.assertTrue(suggestion["orderNow"])

    def test_higher_service_level_orders_more(self):
        low = self.suggest(service_level=0.6)["suggestions"][0]["suggestedQty"]
        high = self.suggest(service_level=0.99)["suggestions"][0]["suggestedQty"]
        self.assertLess(low, high)

    def test_rejects_out_of_range_service_level(self):
        with self.assertRaisesRegex(ForecastError, "服务水平"):
            self.suggest(service_level=1.5)

    def test_missing_inventory_is_reported_not_zero_filled(self):
        with self.assertRaisesRegex(ForecastUnavailable, "缺少这些 SKU 的可用库存"):
            self.service.order_suggestion(["SKU-1", "SKU-2"], inventory={"SKU-1": 5},
                                          in_transit={}, today="2026-08-11")

    def test_audit_snapshot_records_inputs(self):
        result = self.suggest()
        self.assertEqual("flat-test", result["model"]["name"])
        self.assertEqual({"SKU-1": 30}, result["inputs"]["availableStock"])
        self.assertEqual(10, result["inputs"]["leadTimeDays"])

    def test_predict_reports_missing_keys(self):
        result = self.service.predict(["SKU-1"], horizon_days=3, start_date="2026-08-11")
        self.assertEqual(3, len(result["series"]["SKU-1"]))
        self.assertEqual([], result["missingKeys"])
        self.assertEqual(30.0, result["totals"]["SKU-1"])

    def test_status_without_artifact_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = ForecastService(store=ForecastStore(Path(tmp) / "none"), env_path="unused.env")
            self.assertFalse(service.status()["ready"])
            with self.assertRaises(ForecastUnavailable):
                service.predict(["SKU-1"])


if __name__ == "__main__":
    unittest.main()

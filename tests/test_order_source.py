import unittest
from unittest.mock import MagicMock, patch

from backend.order_source import (
    OrderSourceError,
    _mirror_state,
    fetch_exchange_order_items,
    fetch_exchange_orders,
    source_status,
)


def settings(values):
    return lambda name, default="": values.get(name, default)


class OrderSourceTests(unittest.TestCase):
    @staticmethod
    def mirror_row(row):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.return_value = row
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        return connection

    def test_unconfigured_source_is_explicit_and_empty(self):
        setting = settings({})
        status = source_status(setting)
        self.assertFalse(status["configured"])
        self.assertEqual("unconfigured", status["source"])
        result = fetch_exchange_orders(setting, "unused.env", query="10001")
        self.assertEqual([], result["orders"])
        self.assertIn("尚未接入", result["message"])
        items = fetch_exchange_order_items(setting, "unused.env", o_ids=["10001"])
        self.assertFalse(items["configured"])
        self.assertEqual([], items["items"])

    def test_rejects_unsafe_table_identifier(self):
        with self.assertRaisesRegex(OrderSourceError, "字段配置"):
            source_status(settings({"EXCHANGE_ORDER_TABLE": "orders; DROP TABLE orders"}))

    def test_syncing_mirror_remains_available_after_a_successful_sync(self):
        row = {"status": "syncing", "last_success_at": "2026-08-12 10:00:00", "error_message": ""}
        with patch("backend.order_source.connect", return_value=self.mirror_row(row)):
            self.assertEqual({}, _mirror_state("unused.env", "realtime_orders"))

    def test_mirror_is_unavailable_while_first_sync_is_running(self):
        row = {"status": "syncing", "last_success_at": None, "error_message": ""}
        with patch("backend.order_source.connect", return_value=self.mirror_row(row)):
            status = _mirror_state("unused.env", "realtime_orders")
        self.assertFalse(status["configured"])
        self.assertIn("首次同步", status["message"])


if __name__ == "__main__":
    unittest.main()

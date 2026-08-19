import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pymysql.err import OperationalError

from backend.database import is_transient_mysql_error
from backend.realtime_mirror import (
    ORDER_ROUTE,
    PRODUCT_ROUTE,
    PURCHASEIN_ROUTE,
    SUPPLIER_ROUTE,
    ProxyAPIError,
    RealtimeMirror,
    blocked_image_url,
    cache_product_image,
    extract_page,
    normalize_order,
    normalize_product,
    normalize_purchase,
    normalize_purchase_in,
    normalize_supplier,
)


class RealtimeMirrorParsingTests(unittest.TestCase):
    def test_classifies_only_lost_mysql_connections_as_transient(self):
        self.assertTrue(is_transient_mysql_error(OperationalError(2006, "gone away")))
        self.assertTrue(is_transient_mysql_error(OperationalError(2013, "lost connection")))
        self.assertFalse(is_transient_mysql_error(OperationalError(1045, "access denied")))

    def test_uses_authorized_order_search_route(self):
        self.assertEqual("/api/proxy/v1/jushuitan/orders/search", ORDER_ROUTE)
        self.assertEqual("/api/proxy/v1/jushuitan/items/query", PRODUCT_ROUTE)
        self.assertEqual("/api/proxy/v1/jushuitan/suppliers/query", SUPPLIER_ROUTE)
        self.assertEqual("/api/proxy/v1/jushuitan/purchase/inbound/query", PURCHASEIN_ROUTE)

    def test_extracts_nested_proxy_page_and_request_id(self):
        value = {
            "request_id": "req-01",
            "data": {
                "code": 0,
                "data": {
                    "page_index": 1,
                    "page_count": 2,
                    "datas": [{"po_id": "600001"}],
                },
            },
        }
        records, more, request_id = extract_page(value, 1, 100)
        self.assertEqual([{"po_id": "600001"}], records)
        self.assertTrue(more)
        self.assertEqual("req-01", request_id)

    def test_surfaces_proxy_error_without_exposing_credentials(self):
        with self.assertRaises(ProxyAPIError) as caught:
            extract_page({
                "request_id": "req-denied",
                "error": {"code": "not_found", "message": "Route not authorized"},
            }, 1, 100)
        self.assertEqual("req-denied", caught.exception.request_id)
        self.assertIn("not authorized", str(caught.exception))

    def test_accepts_successful_empty_purchase_page(self):
        records, more, request_id = extract_page({
            "request_id": "req-empty",
            "data": {
                "code": 0,
                "issuccess": True,
                "data_count": 0,
                "page_count": 0,
                "has_next": False,
                "datas": None,
            },
        }, 1, 50)
        self.assertEqual([], records)
        self.assertFalse(more)
        self.assertEqual("req-empty", request_id)

    def test_normalizes_purchase_items_and_largest_image(self):
        order, items, has_items = normalize_purchase({
            "po_id": 604264,
            "po_date": "2026-06-05 10:00:00",
            "seller": "供应商甲",
            "items": [{
                "poi_id": 1,
                "sku_id": "SKU-01",
                "i_id": "STYLE-01",
                "name": "测试商品",
                "qty": 2,
                "price": 10.5,
                "pic100": "https://img.example/small.jpg",
                "pic300": "https://img.example/large.jpg",
            }],
        }, "2026-08-12 17:00:00")
        self.assertEqual("604264", order[0])
        self.assertTrue(has_items)
        self.assertEqual("SKU-01", items[0][3])
        self.assertEqual("https://img.example/large.jpg", items[0][19])

    def test_normalizes_purchase_inbound_without_cost_columns(self):
        header, items, has_items = normalize_purchase_in({
            "io_id": "IO-01",
            "po_id": "604264",
            "io_date": "2026-08-01 10:00:00",
            "status": "Confirmed",
            "supplier_name": "供应商甲",
            "items": [{
                "ioi_id": "9",
                "sku_id": "SKU-01",
                "i_id": "STYLE-01",
                "name": "测试商品",
                "qty": 2,
                "cost_price": 99,
            }],
        }, "2026-08-19 10:00:00")
        self.assertEqual("IO-01", header[0])
        self.assertEqual("604264", header[1])
        self.assertEqual("2026-08-01 10:00:00", header[9])
        self.assertTrue(has_items)
        self.assertEqual("SKU-01", items[0][4])
        self.assertEqual("2", items[0][8])
        self.assertNotIn(99, items[0])

    def test_purchasein_sync_uses_inbound_route(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def post(self, route, body):
                self.calls.append((route, body))
                return {
                    "request_id": "req-in",
                    "data": {"code": 0, "datas": [{
                        "io_id": "IO-02", "po_id": "1", "io_date": "2026-08-01 00:00:00",
                        "items": [{"sku_id": "A", "qty": 1}],
                    }]},
                }

        client = FakeClient()
        mirror = RealtimeMirror("unused.env", client, request_interval=0, purchasein_initial_days=1)
        with patch("backend.realtime_mirror.sync_state", return_value={}), \
                patch("backend.realtime_mirror._state_update"), \
                patch("backend.realtime_mirror.RealtimeMirror._ensure_schema"), \
                patch(
                    "backend.realtime_mirror.upsert_purchase_in_records",
                    return_value={"orders": 1, "items": 1},
                ) as upserted:
            result = mirror.sync_source("purchasein")
        self.assertTrue(result["ok"])
        self.assertEqual(PURCHASEIN_ROUTE, client.calls[0][0])
        self.assertEqual(100, client.calls[0][1]["page_size"])
        upserted.assert_called()

    def test_order_without_items_does_not_request_item_replacement(self):
        order, items, has_items = normalize_order({
            "o_id": "10001", "so_id": "SHOP-001", "status": "Sent",
        }, "2026-08-12 17:00:00")
        self.assertEqual("10001", order[0])
        self.assertEqual([], items)
        self.assertFalse(has_items)

    def test_normalizes_order_item_image(self):
        order, items, has_items = normalize_order({
            "o_id": "10002",
            "so_id": "SHOP-002",
            "items": [{
                "oi_id": "1",
                "sku_id": "SKU-02",
                "name": "图片商品",
                "qty": 3,
                "pic": "https://img.example/product.jpg",
            }],
        }, "2026-08-12 17:00:00")
        self.assertEqual("10002", order[0])
        self.assertTrue(has_items)
        self.assertEqual("https://img.example/product.jpg", items[0][7])

    def test_normalizes_product_master_and_image(self):
        product = normalize_product({
            "sku_id": "SKU-03", "i_id": "STYLE-03", "name": "商品三",
            "properties_value": "黑色;42", "category": "鞋类", "brand": "品牌甲",
            "supplier_id": 1001, "unit": "双", "cost_price": 12.5,
            "pic_big": "https://img.example/large.jpg", "pic": "https://img.example/small.jpg",
            "enabled": 1, "modified": "2026-08-13 10:00:00",
        }, "2026-08-13 10:01:00")
        self.assertEqual("SKU-03", product[0])
        self.assertEqual("STYLE-03", product[1])
        self.assertEqual("https://img.example/large.jpg", product[23])
        self.assertEqual("https://img.example/small.jpg", product[24])
        self.assertEqual(1, product[17])

    def test_normalizes_supplier_contract_fields(self):
        supplier = normalize_supplier({
            "supplier_id": 1001, "name": "供应商甲", "enabled": True,
            "contacts": "联系人", "mobile": "13800000000", "address": "测试地址",
            "depositbank": "测试银行", "bankacount": "账户名", "acountnumber": "1234",
            "taxpayer_identification_num": "TAX-01", "modified": "2026-08-13 10:00:00",
        }, "2026-08-13 10:01:00")
        self.assertEqual("1001", supplier[0])
        self.assertEqual("供应商甲", supplier[1])
        self.assertEqual("联系人", supplier[5])
        self.assertEqual("测试银行", supplier[9])
        self.assertEqual("TAX-01", supplier[16])

    def test_rejects_private_image_hosts(self):
        self.assertTrue(blocked_image_url("http://127.0.0.1/pic.png"))
        self.assertTrue(blocked_image_url("https://localhost/pic.png"))
        self.assertTrue(blocked_image_url("http://10.1.2.3/pic.png"))
        self.assertTrue(blocked_image_url("http://192.168.1.9/pic.png"))
        self.assertTrue(blocked_image_url("http://172.16.0.8/pic.png"))
        self.assertTrue(blocked_image_url("http://169.254.1.1/pic.png"))
        self.assertTrue(blocked_image_url("http://[::1]/pic.png"))
        self.assertFalse(blocked_image_url("https://img.example/large.jpg"))
        _, items, _ = normalize_purchase({
            "po_id": 1,
            "items": [{"poi_id": 1, "sku_id": "SKU-01", "pic300": "http://127.0.0.1/secret.png"}],
        }, "2026-08-13 10:00:00")
        self.assertEqual("", items[0][19])

    def test_cache_product_image_does_not_fetch_private_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "内网或本机"):
                cache_product_image(Path(tmp), "SKU-01", "http://127.0.0.1/pic.png")

    def test_refresh_orders_looks_up_by_internal_ids(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def post(self, route, body):
                self.calls.append((route, body))
                return {
                    "request_id": "req-refresh",
                    "data": {"code": 0, "datas": [{"o_id": "11549976", "items": []}]},
                }

        client = FakeClient()
        mirror = RealtimeMirror("unused.env", client, request_interval=0)
        with patch(
            "backend.realtime_mirror.upsert_order_records",
            return_value={"orders": 1, "items": 0},
        ) as upserted:
            result = mirror.refresh_orders(["11549976", "11549976", "11550001"])
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["orders"])
        self.assertEqual(ORDER_ROUTE, client.calls[0][0])
        self.assertEqual("11549976,11550001", client.calls[0][1]["o_ids"])
        upserted.assert_called_once()


class FakeSkuCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, args=None):
        self.conn.calls.append((str(sql), tuple(args or ())))
        if "SELECT" in str(sql).upper():
            wanted = {str(item) for item in (args or ())}
            self._rows = [row for row in self.conn.rows if str(row["o_id"]) in wanted]
        else:
            self._rows = []

    def executemany(self, sql, rows):
        self.conn.calls.append((str(sql), list(rows)))
        self.conn.inserted.extend(list(rows))

    def fetchall(self):
        return list(self._rows)


class FakeSkuConn:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []
        self.inserted = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return FakeSkuCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class ReplaceOrderItemSkuTests(unittest.TestCase):
    def test_batch_uses_one_connection_and_in_clause(self):
        from backend.realtime_mirror import replace_order_item_skus

        conn = FakeSkuConn([
            {
                "source_key": "k1", "o_id": "1", "sku_id": "OLD",
                "i_id": "S", "name": "垫", "properties_value": "",
                "qty": "1", "image_url": "", "source_payload": "{}",
            },
            {
                "source_key": "k2", "o_id": "2", "sku_id": "OLD",
                "i_id": "S", "name": "垫", "properties_value": "",
                "qty": "1", "image_url": "", "source_payload": "{}",
            },
            {
                "source_key": "k3", "o_id": "3", "sku_id": "OTHER",
                "i_id": "S", "name": "鞋", "properties_value": "",
                "qty": "1", "image_url": "", "source_payload": "{}",
            },
        ])
        connects = []

        def connect(_env):
            connects.append(1)
            return conn

        with patch("backend.realtime_mirror.connect", side_effect=connect):
            applied = replace_order_item_skus("unused.env", [
                {"o_id": "1", "source_sku": "OLD", "target_sku": "NEW-1"},
                {"o_id": "2", "source_sku": "OLD", "target_sku": "NEW-2"},
                {"o_id": "3", "source_sku": "OLD", "target_sku": "NEW-3"},
            ])
        self.assertEqual(1, len(connects))
        self.assertEqual(1, conn.commits)
        self.assertEqual(["1", "2"], applied)
        select_sql = conn.calls[0][0]
        self.assertIn("IN (", select_sql)
        delete_sql = next(sql for sql, _args in conn.calls if sql.startswith("DELETE"))
        self.assertIn("IN (", delete_sql)
        self.assertEqual(2, len(conn.inserted))
        self.assertEqual("NEW-1", conn.inserted[0][2])
        self.assertEqual("NEW-2", conn.inserted[1][2])

    def test_single_replace_reuses_batch(self):
        from backend.realtime_mirror import replace_order_item_sku

        conn = FakeSkuConn([{
            "source_key": "k1", "o_id": "9", "sku_id": "OLD",
            "i_id": "S", "name": "垫", "properties_value": "",
            "qty": "1", "image_url": "", "source_payload": "{}",
        }])
        with patch("backend.realtime_mirror.connect", return_value=conn):
            self.assertTrue(replace_order_item_sku("unused.env", "9", "OLD", "NEW"))
        self.assertEqual(1, conn.commits)


if __name__ == "__main__":
    unittest.main()

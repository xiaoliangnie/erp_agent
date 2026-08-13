import base64
import tempfile
import unittest
from pathlib import Path

from backend.product_images import ProductImageError, ProductImageService, resolve_product_image


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProductImageServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cache = root / "images"
        self.service = ProductImageService(root / "jobs.sqlite3", self.cache)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_upload_finish_and_resolve(self):
        job = self.service.create("557593", [{"sku_id": "SKU-01", "i_id": "STYLE-01"}])
        self.assertEqual("pending", job["status"])
        claimed = self.service.next("erp-one")
        self.assertEqual(job["id"], claimed["id"])
        self.service.upload(job["id"], "erp-one", {
            "sku": "SKU-01", "mimeType": "image/png",
            "imageBase64": base64.b64encode(PNG_1X1).decode("ascii"),
            "sourceUrl": "https://img.example/SKU-01.png",
        })
        done = self.service.finish(job["id"], "erp-one", {"failed": []})
        self.assertEqual("done", done["status"])
        image = resolve_product_image({}, sku="SKU-01", style="STYLE-01", cache_dir=self.cache)
        self.assertEqual("ready", image["status"])
        self.assertEqual("聚水潭接口缓存", image["source"])

    def test_rejects_non_target_or_invalid_image(self):
        job = self.service.create("557593", [{"sku_id": "SKU-01", "i_id": ""}])
        self.service.next("erp-one")
        with self.assertRaisesRegex(ProductImageError, "非目标"):
            self.service.upload(job["id"], "erp-one", {
                "sku": "SKU-99", "mimeType": "image/png",
                "imageBase64": base64.b64encode(PNG_1X1).decode("ascii"),
            })
        with self.assertRaisesRegex(ProductImageError, "签名"):
            self.service.upload(job["id"], "erp-one", {
                "sku": "SKU-01", "mimeType": "image/png",
                "imageBase64": base64.b64encode(b"not-an-image").decode("ascii"),
            })


if __name__ == "__main__":
    unittest.main()

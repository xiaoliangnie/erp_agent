import tempfile
import unittest
from pathlib import Path

from backend.paths import CONFIG_DIR, DATA_DIR, OUTPUTS_DIR, ROOT, TEMPLATES_DIR, local_dir, resolve_repo_path


class RepoPathTests(unittest.TestCase):
    def test_official_dirs_live_under_files(self):
        self.assertEqual(ROOT / "files" / "config", CONFIG_DIR)
        self.assertTrue(CONFIG_DIR.is_dir())
        self.assertTrue(TEMPLATES_DIR.is_dir())
        self.assertEqual(ROOT / "files" / "data", DATA_DIR)
        self.assertEqual(ROOT / "files" / "outputs", OUTPUTS_DIR)

    def test_legacy_relative_paths_map_to_files(self):
        self.assertEqual(DATA_DIR / "app.log", resolve_repo_path("data/app.log"))
        self.assertEqual(CONFIG_DIR / "buyers.json", resolve_repo_path("config/buyers.json"))
        self.assertEqual(OUTPUTS_DIR / "generated", resolve_repo_path("files/outputs/generated"))

    def test_temp_root_keeps_its_own_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            self.assertEqual(root / "config", local_dir("config", root=root))
            self.assertEqual(root / "config" / "buyers.json", resolve_repo_path("config/buyers.json", root=root))


if __name__ == "__main__":
    unittest.main()

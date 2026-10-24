import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from src.tts import node_pool

class TestTierRouting(unittest.TestCase):
    def test_tier_routing_defaults(self):
        # デフォルトの4段階Tierルーティングが取得できること
        default_order = node_pool.get_tier_routing_order("default")
        self.assertEqual(len(default_order), 4)
        self.assertEqual(set(default_order), {"gpu_ram", "cpu_ram", "gpu_ssd", "cpu_ssd"})

    def test_tier_routing_set_and_get(self):
        new_order = ["gpu_ssd", "gpu_ram", "cpu_ssd", "cpu_ram"]
        self.assertTrue(node_pool.set_tier_routing_order("voicevox", new_order))

        # 取得確認（大文字小文字の非依存性含む）
        self.assertEqual(node_pool.get_tier_routing_order("voicevox"), new_order)
        self.assertEqual(node_pool.get_tier_routing_order("VOICEVOX"), new_order)

        # 設定されていないエンジンはdefaultへフォールバック
        self.assertEqual(node_pool.get_tier_routing_order("unknown_engine"), node_pool.get_tier_routing_order("default"))

    def test_tier_routing_invalid_orders(self):
        # 要素数不足
        self.assertFalse(node_pool.set_tier_routing_order("coeiroink", ["gpu_ram", "cpu_ram"]))

        # 不正なTierキー
        self.assertFalse(node_pool.set_tier_routing_order("coeiroink", ["gpu_ram", "cpu_ram", "invalid_tier", "cpu_ssd"]))

        # 重複あり（4要素だがsetにしたとき要素数不足）
        self.assertFalse(node_pool.set_tier_routing_order("coeiroink", ["gpu_ram", "gpu_ram", "cpu_ram", "cpu_ssd"]))

    def test_tier_routing_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            test_json = Path(tmpdir) / "test_tier_routing.json"
            with patch.object(node_pool, "TIER_ROUTING_CONFIG_PATH", test_json):
                custom_orders = {
                    "default": ["cpu_ram", "gpu_ram", "cpu_ssd", "gpu_ssd"],
                    "sharevox": ["gpu_ssd", "cpu_ssd", "gpu_ram", "cpu_ram"]
                }

                # 保存
                self.assertTrue(node_pool.save_tier_routing_to_disk(custom_orders))
                self.assertTrue(test_json.exists())

                # 一旦リセット
                node_pool.set_tier_routing_order("default", ["gpu_ram", "cpu_ram", "gpu_ssd", "cpu_ssd"])

                # ロードして復元されることを確認
                node_pool.load_tier_routing_from_disk()
                self.assertEqual(node_pool.get_tier_routing_order("default"), custom_orders["default"])
                self.assertEqual(node_pool.get_tier_routing_order("sharevox"), custom_orders["sharevox"])

if __name__ == "__main__":
    unittest.main()

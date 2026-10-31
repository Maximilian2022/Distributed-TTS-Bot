import unittest
import asyncio
from unittest.mock import patch
from src.config import TTSNodeConfig
from src.tts.node_pool import TTSNode, TTSNodePool

class TestTurnAntiAffinity(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 2台のGPUノードを準備 (RTX 3090 と GTX 1080 Ti)
        cfg_3090 = TTSNodeConfig(
            id="worker_MASTERSOMEN2022_voicevox",
            url="http://192.168.11.200:50021",
            priority=1,
            max_concurrent=3,
            enabled=True
        )
        self.node_3090 = TTSNode(
            config=cfg_3090,
            specs={
                "gpu": True,
                "gpu_name": "NVIDIA GeForce RTX 3090",
                "is_ramdisk": True,
                "ram_models": ["vv_81"]
            },
            hostname="MASTERSOMEN2022"
        )

        cfg_1080ti = TTSNodeConfig(
            id="worker_MASTEROMEN2017_voicevox",
            url="http://192.168.11.207:50021",
            priority=1,
            max_concurrent=2,
            enabled=True
        )
        self.node_1080ti = TTSNode(
            config=cfg_1080ti,
            specs={
                "gpu": True,
                "gpu_name": "NVIDIA GeForce GTX 1080 Ti",
                "is_ramdisk": True,
                "ram_models": ["vv_81"]
            },
            hostname="MASTEROMEN2017"
        )

        self.pool = TTSNodePool("voicevox", [cfg_3090, cfg_1080ti])
        self.pool.nodes = [self.node_3090, self.node_1080ti]

    async def asyncTearDown(self):
        await self.pool.close()

    def test_order_nodes_without_avoid(self):
        """avoid_nodes がない場合、GPU性能が高い RTX 3090 が最優先になること"""
        ordered = self.pool._order_available_nodes(
            self.pool.nodes,
            text_len=10,
            is_longest_chunk=True,
            avoid_nodes=None
        )
        self.assertEqual(len(ordered), 2)
        self.assertIn("3090", ordered[0].machine_name)
        self.assertIn("1080", ordered[1].machine_name)

    def test_order_nodes_with_avoid_anti_affinity(self):
        """avoid_nodes に RTX 3090 が指定された場合、GTX 1080 Ti が最優先になること (Anti-Affinity)"""
        avoid_nodes = {"MASTERSOMEN2022"}
        ordered = self.pool._order_available_nodes(
            self.pool.nodes,
            text_len=10,
            is_longest_chunk=True,
            avoid_nodes=avoid_nodes
        )
        self.assertEqual(len(ordered), 2)
        self.assertIn("1080", ordered[0].machine_name)
        self.assertIn("3090", ordered[1].machine_name)

    async def test_acquire_node_anti_affinity_and_set_mutation(self):
        """acquire_node でノード獲得時に avoid_nodes にノードが追加され、後続チャンクが別ノードへ分散すること"""
        turn_avoid_nodes = set()

        # チャンク1: 先頭パーツ (prefer_earliest_finish=True)
        # 3090の完了予測が早いため3090を獲得
        with patch.object(self.pool, "predict_eta_ms", side_effect=lambda n, l: 300.0 if "3090" in n.machine_name else 800.0):
            lease1 = await self.pool.acquire_node(
                timeout=1.0,
                text_len=4,
                prefer_earliest_finish=True,
                avoid_nodes=turn_avoid_nodes
            )
            self.assertIsNotNone(lease1)
            self.assertIn("3090", lease1.node.machine_name)
            # 獲得ノードが自動で turn_avoid_nodes に追加されていること
            self.assertTrue(any("3090" in a or "MASTERSOMEN2022" in a for a in turn_avoid_nodes))

            # チャンク2: 後続パーツ (prefer_earliest_finish=False)
            # 3090 に空き枠 (残り2) があっても、avoid_nodes により 1080 Ti へ自動分散すること
            lease2 = await self.pool.acquire_node(
                timeout=1.0,
                text_len=6,
                is_longest_chunk=True,
                prefer_earliest_finish=False,
                avoid_nodes=turn_avoid_nodes
            )
            self.assertIsNotNone(lease2)
            self.assertIn("1080", lease2.node.machine_name)
            # 1080 Ti も turn_avoid_nodes に追加されること
            self.assertTrue(any("1080" in a or "MASTEROMEN2017" in a for a in turn_avoid_nodes))

            # クリーンアップ (Lease 解放)
            async with lease1:
                pass
            async with lease2:
                pass

    async def test_fallback_when_single_node_available(self):
        """利用可能な別ノードが存在しない場合 (単一ノード)、avoid_nodes に含まれていてもスタックせず同一ノードを獲得できること"""
        single_pool = TTSNodePool("voicevox", [self.node_3090.config])
        single_pool.nodes = [self.node_3090]
        try:
            turn_avoid_nodes = {"MASTERSOMEN2022"}
            lease = await single_pool.acquire_node(
                timeout=1.0,
                text_len=10,
                prefer_earliest_finish=False,
                avoid_nodes=turn_avoid_nodes
            )
            self.assertIsNotNone(lease)
            self.assertIn("3090", lease.node.machine_name)
            async with lease:
                pass
        finally:
            await single_pool.close()

if __name__ == "__main__":
    unittest.main()

import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from src.cache_prewarm import IntelligentPrewarmService
from src.cogs.vc_handler import QueueItem, VCHandlerCog
from src.config import TTSNodeConfig
from src.database import Database
from src.tts.azure_tts import AzureTTSClient
from src.tts.edge_tts import EdgeTTSClient
from src.tts.google_tts import GoogleTTSClient
from src.tts.node_pool import TTSNode, TTSNodePool
from src.utils.latency_profiler import NodeLatencyProfile
from src.utils.latency_tracker import LatencyTracker, get_synthesis_category, parse_smart_worker_label
from src.utils.text_cleaner import TextCleaner


class TestAuditRemediationSync(unittest.TestCase):
    # ==============================================================================
    # 項目①: 動的チャンク長と連動した「最長チャンク → 最強Node」ルーティングの検証
    # ==============================================================================
    def test_item1_node_pool_longest_chunk_routing(self):
        cfg_gpu_high = TTSNodeConfig(id="worker_gpu_high", url="http://192.168.1.10:50021", priority=10, enabled=True)
        cfg_gpu_mid = TTSNodeConfig(id="worker_gpu_mid", url="http://192.168.1.20:50021", priority=20, enabled=True)
        pool = TTSNodePool("voicevox", [cfg_gpu_high, cfg_gpu_mid])

        gpu_high = pool.nodes[0]
        gpu_mid = pool.nodes[1]

        gpu_high.is_healthy = True
        gpu_high.engine_running = True
        gpu_high.specs = {"gpu": True, "gpu_name": "RTX 4090", "vram_mb": 24576, "engine_running": True}

        gpu_mid.is_healthy = True
        gpu_mid.engine_running = True
        gpu_mid.specs = {"gpu": True, "gpu_name": "RTX 3060", "vram_mb": 8192, "engine_running": True}

        # 1. 短文 (6文字) かつ is_longest_chunk=False の場合:
        # 最強GPUノード (RTX 4090) を後続の最長チャンクのために温存し、2番手の GPU (RTX 3060) が先頭に選ばれること
        ordered_short = pool._order_available_nodes([gpu_high, gpu_mid], text_len=6, is_longest_chunk=False)
        self.assertIs(ordered_short[0], gpu_mid)

        # 2. 12文字のチャンクでも、本文中の最長チャンク (is_longest_chunk=True) であれば:
        # 固定15文字制限に阻まれず、最強の GPU ノード (RTX 4090) が先頭に選ばれること
        ordered_longest = pool._order_available_nodes([gpu_high, gpu_mid], text_len=12, is_longest_chunk=True)
        self.assertIs(ordered_longest[0], gpu_high)

        # 3. peek_best_node が現在利用可能な最優先ノードを返すこと
        best = pool.peek_best_node()
        self.assertIs(best, gpu_high)

    # ==============================================================================
    # 項目②: Edge-TTS の文字数比例動的タイムアウト & クラウド3社サーキットブレーカー
    # ==============================================================================
    def test_item2_edge_tts_dynamic_timeout_and_circuit_breakers(self):
        edge = EdgeTTSClient(timeout_seconds=8.0)

        # 短文 (4文字) -> 4.1s
        t_short = edge.calc_dynamic_timeout(4)
        self.assertTrue(4.0 <= t_short <= 4.5)

        # 中文 (20文字) -> 3.5 + 3.0 = 6.5s
        t_mid = edge.calc_dynamic_timeout(20)
        self.assertAlmostEqual(t_mid, 6.5, places=2)

        # 長文 (60文字) -> 上限 9.0s
        t_long = edge.calc_dynamic_timeout(60)
        self.assertEqual(t_long, 9.0)

        # 明示的に短いタイムアウト (テスト用 0.5s) が指定された場合はそれを尊重
        edge_fast = EdgeTTSClient(timeout_seconds=0.5)
        self.assertEqual(edge_fast.calc_dynamic_timeout(20), 0.5)

        # サーキットブレーカー検証 (Edge, Azure, Google)
        self.assertTrue(edge.is_available())
        edge._cooldown_until = time.time() + 30.0
        self.assertFalse(edge.is_available())
        edge.reset_circuit_breaker()
        self.assertTrue(edge.is_available())

        azure = AzureTTSClient(key="dummy_key", region="japaneast")
        self.assertTrue(azure.is_available())
        azure._cooldown_until = time.time() + 30.0
        self.assertFalse(azure.is_available())
        azure.reset_circuit_breaker()
        self.assertTrue(azure.is_available())

        google = GoogleTTSClient(api_key="dummy_key")
        self.assertTrue(google.is_available())
        google._cooldown_until = time.time() + 30.0
        self.assertFalse(google.is_available())
        google.reset_circuit_breaker()
        self.assertTrue(google.is_available())

    # ==============================================================================
    # 項目③: QueueItem.speculative_timeout フィールドの整合性
    # ==============================================================================
    def test_item3_queue_item_speculative_timeout(self):
        item = QueueItem(
            text="こんにちは",
            user_id=456,
            user_name="テスト",
            speaker_id="nanami",
            pitch="0",
            speed=1.0,
            speculative_timeout=0.35,
        )
        self.assertEqual(item.speculative_timeout, 0.35)

    # ==============================================================================
    # 項目⑤: OSS汎用化（特定個人環境のマシン名・型番ハードコード非依存）
    # ==============================================================================
    def test_item5_oss_generic_hardware_classification(self):
        # 任意のホスト名でも has_gpu や汎用GPU名で正しく分類されること
        self.assertEqual(get_synthesis_category("voicevox", "custom-linux-box (GPU)", has_gpu=True), "LOCAL_GPU_FAST")
        self.assertEqual(get_synthesis_category("coeiroink", "my-home-server (CPU)", has_gpu=False), "COEIROINK_WARM")
        self.assertEqual(get_synthesis_category("sharevox", "worker-alpha (NVIDIA GeForce RTX 3060)"), "LOCAL_GPU_FAST")
        self.assertEqual(get_synthesis_category("voicevox", "worker-beta (Ryzen 7)", has_gpu=False), "LOCAL_CPU")
        self.assertEqual(get_synthesis_category("edge", "Edge Cloud"), "CLOUD")

        # ラベル生成で特定の個人CPU型番 (7700K等) がハードコードされないこと
        machine, engine = parse_smart_worker_label("local_master", "voicevox")
        self.assertNotIn("7700K", machine)
        self.assertEqual(machine, "Primary Node")
        self.assertEqual(engine, "VOICEVOX")

        # NodeLatencyProfile が VRAM 容量と実測速度から汎用的にスコア計算すること
        prof_high = NodeLatencyProfile(
            node_id="node1", engine_name="VOICEVOX", has_gpu=True, gpu_name="Custom GPU A", vram_mb=16384
        )
        prof_low = NodeLatencyProfile(
            node_id="node2", engine_name="VOICEVOX", has_gpu=True, gpu_name="Custom GPU B", vram_mb=4096
        )
        self.assertGreater(prof_high.gpu_power_score, prof_low.gpu_power_score)

    # ==============================================================================
    # 項目⑥: オフライン外部Workerへの無駄なオンデマンド起動・ブロードキャスト抑止
    # ==============================================================================
    def test_item6_offline_external_worker_reachability(self):
        conf = TTSNodeConfig(id="worker_ext_1", url="http://192.168.1.99:50021", enabled=True)
        node = TTSNode(conf, specs={"initial_standby": True, "engine_running": False})
        self.assertTrue(node.is_external)
        # 一度もハートビートを受信していない初期スタンバイ状態の外部ノードは到達不可と判定されること
        self.assertFalse(node.is_agent_reachable)

        # ハートビートを受信すると到達可能になること
        node.specs["initial_standby"] = False
        node.last_heartbeat = time.time()
        self.assertTrue(node.is_agent_reachable)

        # ハートビートが90秒以上途絶すると再び到達不可になること
        node.last_heartbeat = time.time() - 120.0
        self.assertFalse(node.is_agent_reachable)

    # ==============================================================================
    # 項目⑧: 事前コンパイル正規表現による TextCleaner 動作検証
    # ==============================================================================
    def test_item8_precompiled_regex_in_text_cleaner(self):
        converted, unknown = TextCleaner.convert_english_to_kana("Hello Discord World XYZABC")
        self.assertIsInstance(converted, str)

        parts = TextCleaner._split_long_sentence_morphological(
            "今日はとても天気が良いので午後から公園へ散歩に行きたいと思います",
            "。",
            min_chars_threshold=15,
        )
        self.assertGreaterEqual(len(parts), 2)


class TestAuditRemediationAsync(unittest.IsolatedAsyncioTestCase):
    # ==============================================================================
    # 項目④: フォールバック時の失敗・成功統計記録 (Aの失敗、Bの成功＆フォールバック記録)
    # ==============================================================================
    async def test_item4_fallback_failure_and_success_statistics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_stats.db")
            db = Database(db_path)
            await db.init_db()
            try:
                # プロバイダー A (coeiroink) が失敗し、プロバイダー B (edge) にフォールバックして成功したケース
                await db.record_tts_failure("coeiroink")
                await db.record_tts_usage("edge", 15, is_fallback=True)
                # 通常の成功ケース
                await db.record_tts_usage("voicevox", 20, is_fallback=False)

                monthly = await db.get_monthly_tts_usage()
                self.assertEqual(monthly["coeiroink"]["failed_requests"], 1)
                self.assertEqual(monthly["coeiroink"]["requests"], 0)

                self.assertEqual(monthly["edge"]["requests"], 1)
                self.assertEqual(monthly["edge"]["fallback_requests"], 1)
                self.assertEqual(monthly["edge"]["characters"], 15)

                self.assertEqual(monthly["voicevox"]["requests"], 1)
                self.assertEqual(monthly["voicevox"]["fallback_requests"], 0)
                self.assertEqual(monthly["voicevox"]["failed_requests"], 0)
            finally:
                await db.close()

            # LatencyTracker でも失敗プロバイダーとフォールバック勝者が記録されること
            tracker = LatencyTracker()
            now = time.time()
            tracker.record(
                user_name="TestUser",
                text="テスト発話",
                speaker_name="テスト話者",
                provider="edge",
                worker_label="Edge Cloud",
                t_discord_created=now - 0.3,
                t_received=now - 0.2,
                t_queued=now - 0.2,
                t_dequeued=now - 0.19,
                t_synth_start=now - 0.18,
                t_synth_end=now - 0.02,
                t_play_start=now,
                is_cached=False,
                is_fallback=True,
                chunks=[
                    {
                        "chunk_index": 1,
                        "provider": "edge",
                        "synth_ms": 160.0,
                        "duration_ms": 1200.0,
                        "is_cached": False,
                        "is_fallback": True,
                        "failed_providers": ["coeiroink"],
                    }
                ],
                incident_context={"expected_provider": "coeiroink"},
            )
            summary = tracker.get_summary()
            workers = summary.get("workers", {})

            # 失敗した COEIROINK 側の fail_count が 1 記録されていること
            ci_stats = next((v for k, v in workers.items() if "COEIROINK" in k), None)
            self.assertIsNotNone(ci_stats)
            self.assertEqual(ci_stats["fail_count"], 1)
            self.assertEqual(ci_stats["count"], 0)

            # フォールバック成功した Edge-TTS 側の count と fallback_win_count が 1 記録されていること
            edge_stats = next((v for k, v in workers.items() if "Edge" in k), None)
            self.assertIsNotNone(edge_stats)
            self.assertEqual(edge_stats["count"], 1)
            self.assertEqual(edge_stats["fallback_win_count"], 1)

    # ==============================================================================
    # 項目⑦: 投機的プリウォーム (run_speculative_prewarm) のキャッシュ先行確認
    # ==============================================================================
    async def test_item7_speculative_prewarm_skips_engine_start_when_all_cached(self):
        mock_tts = MagicMock()
        mock_rm = MagicMock()
        mock_rm.start_engine = AsyncMock()
        mock_rm.stop_all_active_engines = AsyncMock()
        mock_tts.resource_manager = mock_rm

        mock_pool = MagicMock()
        mock_pool.wake_up_nodes = AsyncMock(return_value=True)
        mock_tts.voicevox_pool = mock_pool

        mock_cache = MagicMock()
        # 全フレーズがキャッシュヒットする状態をシミュレート
        mock_cache.get = AsyncMock(return_value="/tmp/cached.wav")

        mock_db = MagicMock()
        mock_db.get_frequent_phrases_for_prewarm = AsyncMock(
            return_value=[{"text": "草", "hits": 10}, {"text": "了解", "hits": 8}, {"text": "ありがとう", "hits": 5}]
        )
        mock_db.get_configured_speakers = AsyncMock(return_value=["vv_1"])

        mock_config = MagicMock()
        mock_config.default_speaker = "vv_1"
        mock_config.voices = {}

        service = IntelligentPrewarmService(mock_tts, mock_cache, mock_db, mock_config)
        stats = await service.run_speculative_prewarm(days=3, min_hits=3, max_candidates=10)

        self.assertEqual(stats["status"], "completed")
        self.assertEqual(stats["generated"], 0)
        self.assertGreater(stats["skipped"], 0)
        # 全件キャッシュヒット時はローカルエンジンの起動・停止が一度も呼ばれないこと
        mock_pool.wake_up_nodes.assert_not_called()
        mock_rm.start_engine.assert_not_called()
        mock_rm.stop_all_active_engines.assert_not_called()

    # ==============================================================================
    # 項目⑨: _enqueue_vc_event_announcement の正常動作検証
    # ==============================================================================
    async def test_item9_enqueue_vc_event_announcement(self):
        mock_bot = MagicMock()
        mock_config = MagicMock()
        mock_config.default_speaker = "nanami"
        mock_config.default_pitch = "+0Hz"
        mock_config.default_speed = 1.0
        mock_config.max_text_length = 100
        mock_config.gemini_api_key = ""
        mock_config.gemini_model = "gemini-2.5-flash"
        mock_config.voices = {"nanami": MagicMock()}

        mock_db = MagicMock()
        mock_db.get_guild_settings = AsyncMock(
            return_value={"announce_vc_events": 1, "default_speaker": "nanami", "speculative_timeout_seconds": 1.5}
        )
        mock_db.get_dictionary_words = AsyncMock(return_value=[("テスト", "てすと")])
        mock_cache = MagicMock()
        mock_tts = MagicMock()

        handler = VCHandlerCog(mock_bot, mock_config, mock_db, mock_cache, mock_tts)
        state = handler.get_guild_state(999)
        # 再生ループが自動起動しないようダミータスクをセット
        dummy_future = asyncio.get_running_loop().create_future()
        state.play_task = dummy_future  # type: ignore

        mock_guild = MagicMock()
        mock_guild.id = 999
        mock_guild.name = "TestGuild"
        mock_vc = MagicMock()
        mock_vc.is_connected.return_value = True
        mock_guild.voice_client = mock_vc

        mock_member = MagicMock()
        mock_member.id = 111
        mock_member.display_name = "テスト"

        await handler._enqueue_vc_event_announcement(mock_guild, mock_member, "join")
        self.assertFalse(state.queue.empty())
        queued_item = await state.queue.get()
        self.assertIn("てすとさんが入室しました", queued_item.text)
        dummy_future.cancel()

    # ==============================================================================
    # 項目⑩: 試聴サンプル生成 (ensure_preview_cached) の speculative_timeout=999.0 とフォールバック汚染防止
    # ==============================================================================
    async def test_item10_preview_cache_prevents_fallback_pollution(self):
        from src.tts.manager import TTSManager

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_config = MagicMock()
            mock_config.cache_dir = tmpdir
            mock_config.voices = {}
            mock_config.voicevox_url = "http://localhost:50021"
            mock_config.coeiroink_url = "http://localhost:50032"
            mock_config.sharevox_url = "http://localhost:50025"
            mock_cache = MagicMock()
            mock_cache.get = AsyncMock(return_value=None)

            mgr = TTSManager(mock_config, mock_cache)
            mgr.preview_dir = os.path.join(tmpdir, "previews")
            os.makedirs(mgr.preview_dir, exist_ok=True)

            # フォールバックが発生したダミー合成をシミュレート
            dummy_src = os.path.join(tmpdir, "fallback_edge.mp3")
            with open(dummy_src, "wb") as f:
                f.write(b"RIFF" + b"\x00" * 500)

            recorded_kwargs = {}

            async def fake_get_audio_path(*args, **kwargs):
                recorded_kwargs.update(kwargs)
                mgr.last_synthesis_meta = {"provider": "edge", "is_fallback": True}
                return dummy_src

            mgr.get_audio_path = fake_get_audio_path

            # ローカル話者 (vv_1) のプレビュー生成で Edge-TTS にフォールバックした場合は保存せず None を返すこと
            res = await mgr.ensure_preview_cached("vv_1")
            self.assertEqual(recorded_kwargs.get("speculative_timeout"), 999.0)
            self.assertIsNone(res)
            self.assertFalse(os.path.exists(os.path.join(mgr.preview_dir, "preview_vv_1.wav")))


if __name__ == "__main__":
    unittest.main()

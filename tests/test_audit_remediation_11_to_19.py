import os
import sys
import unittest
import asyncio
import tempfile
import shutil
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# テスト対象モジュールのインポート
from src.utils.text_cleaner import TextCleaner
from src.utils.latency_profiler import EngineLatencyProfiler
from src.utils.streaming_audio import StreamingAudioSource
from src.utils.security import is_private_or_local_ip
from src.cache_manager import CacheManager, calculate_effective_cache_limits
from src.resource_manager import LocalEngineResourceManager
from src.config import TTSNodeConfig, AppConfig
from src.tts.node_pool import TTSNode, TTSNodePool
from src.tts.voicevox_tts import VoicevoxTTSClient
from src.tts.coeiroink_tts import CoeiroinkTTSClient
from src.tts.sharevox_tts import SharevoxTTSClient
from src.tts.coeiroink_ram_manager import CoeiroinkRamManager
from src.web.server import WebServer


DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


class TestAuditRemediation11To19(unittest.IsolatedAsyncioTestCase):
    """第2フェーズ（項目⑪〜⑲および HA Slave Standby 対策）の網羅的単体テスト"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    # =========================================================================
    # 項目⑫: テキスト分割・動的チャンク長適応テスト
    # =========================================================================
    def test_item_12_text_cleaner_dynamic_chunk_scaling(self):
        """低速環境（短い上限）と高速環境（長い上限）に応じた動的チャンク分割の検証"""
        text = "本日は大変良い天気ですね。明日の予定について確認したいのですが、午後から雨が降るそうですよ。"
        
        # 厳しい文字数上限 (max_first_chunk_chars=8, max_chunk_chars=12)
        chunks_short = TextCleaner.split_into_chunks(
            text,
            max_first_chunk_chars=8,
            max_chunk_chars=12
        )
        # 緩やかな文字数上限 (max_first_chunk_chars=30, max_chunk_chars=30)
        chunks_long = TextCleaner.split_into_chunks(
            text,
            max_first_chunk_chars=30,
            max_chunk_chars=30
        )

        # 短い上限の場合はより細かくチャンク分割されること
        self.assertGreater(len(chunks_short), len(chunks_long))
        # 先頭チャンクの文字数が上限内に収まっていること
        first_short_len = len(chunks_short[0].text.strip())
        self.assertLessEqual(first_short_len, 14)  # 読点や区切りによる安全マージンを含む

        # テキスト内容の欠落がないこと
        reconstructed_short = "".join(c.text for c in chunks_short).replace(" ", "")
        reconstructed_long = "".join(c.text for c in chunks_long).replace(" ", "")
        self.assertEqual(reconstructed_short, reconstructed_long)

    # =========================================================================
    # 項目⑪: Speculative TTS バッファ残量連動タイムアウト＆残量計算テスト
    # =========================================================================
    def test_item_11_speculative_timeout_buffer_scaling(self):
        """再生バッファ残量に応じた動的タイムアウトスケーリングの検証"""
        profiler = EngineLatencyProfiler()

        # 1. 初動チャンク (chunk_index=0, buffer=0.0s) -> 初動最優先で上限2.5s以内にクランプ
        timeout_initial = profiler.calculate_dynamic_speculative_timeout(
            base_timeout=1.5,
            engine="voicevox",
            text_len=15,
            chunk_index=0,
            buffer_headroom_sec=0.0
        )
        self.assertLessEqual(timeout_initial, 2.5)
        self.assertGreaterEqual(timeout_initial, 0.8)

        # 2. 後続チャンク (chunk_index=1, buffer=3.5s) -> バッファ猶予を活用してローカル完了を待機
        timeout_buffered = profiler.calculate_dynamic_speculative_timeout(
            base_timeout=1.5,
            engine="voicevox",
            text_len=15,
            chunk_index=1,
            buffer_headroom_sec=3.5
        )
        self.assertGreater(timeout_buffered, timeout_initial)
        self.assertLessEqual(timeout_buffered, 6.0)

    def test_item_11_streaming_audio_buffer_calculation(self):
        """StreamingAudioSource の PCM キュー残り秒数プロパティの検証"""
        source = StreamingAudioSource()
        self.assertEqual(source.buffer_remaining_seconds, 0.0)

        # 48000Hz, 16bit, 2ch = 192,000 bytes/sec
        # 1秒分のPCMデータを直接投入
        dummy_pcm_1sec = b"\x00" * 192000
        source.feed_raw_pcm(dummy_pcm_1sec)

        self.assertAlmostEqual(source.buffer_remaining_seconds, 1.0, places=2)
        source.cleanup()
        self.assertEqual(source.buffer_remaining_seconds, 0.0)

    # =========================================================================
    # 項目⑬: ローカルエンジン通信タイムアウト分離＆文字数連動テスト
    # =========================================================================
    def test_item_13_local_engine_synthesis_timeout_calculation(self):
        """各TTSエンジンの波形合成タイムアウトの動的算出（文字数・GPU/CPU連動）の検証"""
        for cls in [VoicevoxTTSClient, SharevoxTTSClient, CoeiroinkTTSClient]:
            # 短文 (5文字, GPU) -> 下限 6.0s〜7.0s 付近
            t_short = cls.calculate_synthesis_timeout(text_len=5, has_gpu=True, base_timeout=6.0)
            self.assertGreaterEqual(t_short, 6.0)
            self.assertLessEqual(t_short, 8.0)

            # 長文 (80文字, CPU環境) -> CPU補正 (1.5倍) と文字数連動で延長
            t_long_cpu = cls.calculate_synthesis_timeout(text_len=80, has_gpu=False, base_timeout=6.0)
            self.assertGreater(t_long_cpu, t_short)
            self.assertLessEqual(t_long_cpu, 25.0)

    # =========================================================================
    # 項目⑭: RAMDiskモデル昇格判定・実サイズ事前シミュレーションテスト
    # =========================================================================
    def test_item_14_ram_manager_promotion_simulation(self):
        """RAM昇格前の事前実サイズシミュレーションと警戒域メモリ保護の検証"""
        ram_dir = Path(self.temp_dir) / "ram"
        ssd_dir = Path(self.temp_dir) / "ssd"
        ram_dir.mkdir()
        ssd_dir.mkdir()

        # テスト用モデルディレクトリを作成 (約 10MB のダミーファイル)
        test_model = ssd_dir / "test_model_dir"
        test_model.mkdir()
        (test_model / "model.bin").write_bytes(b"\x00" * (10 * 1024 * 1024))
        (test_model / "metas.json").write_text('{"name": "test"}')

        manager = CoeiroinkRamManager(
            ssd_dir=str(ssd_dir),
            ram_dir=str(ram_dir)
        )

        # 1. 正常系: メモリ使用率 50% (used=8GB, total=16GB) -> 昇格可能
        ok, reason = manager.can_safely_promote_model(str(test_model), mem_stats=(50.0, 8.0, 16.0))
        self.assertTrue(ok, f"Promotion should succeed, got: {reason}")

        # 2. 警戒域オーバー: 現在のメモリ使用率が 86% (WARNING_THRESHOLD 85% 超過) -> 昇格拒否
        ok, reason = manager.can_safely_promote_model(str(test_model), mem_stats=(86.0, 13.76, 16.0))
        self.assertFalse(ok)
        self.assertIn("警戒域", reason)

        # 3. 昇格後の空きメモリ不足・危険域: 昇格後に使用率や空きメモリが安全域を割り込む場合 -> 昇格拒否
        # used=14.9GB, total=16GB -> 残り空き 1.1GB (< 1.2GB)
        ok, reason = manager.can_safely_promote_model(str(test_model), mem_stats=(75.0, 14.9, 16.0))
        self.assertFalse(ok)
        self.assertIn("危険域", reason)

    # =========================================================================
    # 項目⑮: キャッシュ容量上限の動的クランプ＆HA Slave Standby 対策テスト
    # =========================================================================
    def test_item_15_cache_limits_dynamic_clamp(self):
        """ディスク空き容量に応じたキャッシュ上限の動的クランプの検証"""
        test_dir = Path(self.temp_dir) / "cache"
        test_dir.mkdir()

        # 1. ディスク空き容量が潤沢な場合 (total 100GB, free 50GB)
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = DiskUsage(
                total=100 * (1024 ** 3),
                used=50 * (1024 ** 3),
                free=50 * (1024 ** 3)
            )
            eff_max, eff_target = calculate_effective_cache_limits(
                directory=test_dir,
                configured_max_bytes=10 * (1024 ** 3),      # 10GB設定
                configured_target_bytes=8 * (1024 ** 3),    # 8GB設定
                current_cache_size_bytes=1 * (1024 ** 3)
            )
            self.assertEqual(eff_max, 10 * (1024 ** 3))
            self.assertEqual(eff_target, 8 * (1024 ** 3))

        # 2. ディスク空き容量が逼迫している場合 (total 20GB, free 1GB -> 安全マージン 3GB を割り込む)
        with patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = DiskUsage(
                total=20 * (1024 ** 3),
                used=19 * (1024 ** 3),
                free=1 * (1024 ** 3)
            )
            eff_max, eff_target = calculate_effective_cache_limits(
                directory=test_dir,
                configured_max_bytes=10 * (1024 ** 3),
                configured_target_bytes=8 * (1024 ** 3),
                current_cache_size_bytes=500 * (1024 ** 2)
            )
            # 設定値(10GB)より小さく動的クランプされていること
            self.assertLess(eff_max, 10 * (1024 ** 3))
            self.assertLess(eff_target, eff_max)

    # =========================================================================
    # 項目⑯: 動的並列数＆TTS異常閾値テスト
    # =========================================================================
    def test_item_16_dynamic_concurrency_and_anomaly_limits(self):
        """リソースに応じた動的並列数算出とTTSサイズ上限の検証"""
        rm = LocalEngineResourceManager()

        # 1. 低リソース環境 (RAM 4GB) -> 1並列に制限
        with patch.object(rm, "get_system_memory_info", return_value={"total_gb": 4.0, "total_bytes": 4 * 1024**3}):
            with patch("os.cpu_count", return_value=4):
                self.assertEqual(rm.get_dynamic_max_concurrent(), 1)

        # 2. 高リソース環境 (RAM 16GB, 8コア) -> 4並列
        with patch.object(rm, "get_system_memory_info", return_value={"total_gb": 16.0, "total_bytes": 16 * 1024**3}):
            with patch("os.cpu_count", return_value=8):
                self.assertEqual(rm.get_dynamic_max_concurrent(), 4)

        # 3. 異常判定閾値: RAM 8GB の場合 -> 8GB * 0.45 = 3.6GB
        with patch.object(rm, "get_system_memory_info", return_value={"total_bytes": 8 * (1024 ** 3)}):
            threshold = rm.get_dynamic_tts_anomaly_threshold_bytes()
            expected = int(8 * (1024 ** 3) * 0.45)
            self.assertEqual(threshold, expected)

    # =========================================================================
    # 項目⑱: IP境界判定の汎用化＆Active Handshake 到達性検証テスト
    # =========================================================================
    def test_item_18_is_private_or_local_ip_universal(self):
        """RFC1918, CGNAT, Loopback, IPv6 ULA の正確なプライベート/ローカル判定"""
        # ループバック & ローカル
        self.assertTrue(is_private_or_local_ip("127.0.0.1"))
        self.assertTrue(is_private_or_local_ip("localhost"))
        self.assertTrue(is_private_or_local_ip("::1"))

        # RFC 1918
        self.assertTrue(is_private_or_local_ip("192.168.1.100"))
        self.assertTrue(is_private_or_local_ip("10.0.0.5"))
        self.assertTrue(is_private_or_local_ip("172.20.0.10"))

        # RFC 6598 (Carrier-Grade NAT / Tailscale 100.64.0.0/10)
        self.assertTrue(is_private_or_local_ip("100.64.0.1"))
        self.assertTrue(is_private_or_local_ip("100.100.50.25"))

        # グローバル パブリック IP
        self.assertFalse(is_private_or_local_ip("8.8.8.8"))
        self.assertFalse(is_private_or_local_ip("1.1.1.1"))
        self.assertFalse(is_private_or_local_ip("93.184.216.34"))

    async def test_item_18_active_handshake_reachability_verification(self):
        """Active Handshake による双方向到達性・node_id検証とWebSocket自動誘導"""
        bot = MagicMock()
        config = AppConfig()
        server = WebServer(
            bot=bot,
            config=config,
            database=MagicMock(),
            cache_manager=MagicMock(),
            tts_manager=MagicMock()
        )

        node_id = "worker-node-abc"
        target_ip = "192.168.1.50"
        agent_port = 8089

        # aiohttp.ClientSession の非同期コンテキストマネージャーモック作成ヘルパー
        def _create_mock_session(status: int, data: dict, raise_timeout: bool = False):
            mock_session = MagicMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=None)

            if raise_timeout:
                mock_session.get.side_effect = asyncio.TimeoutError()
            else:
                mock_resp = AsyncMock()
                mock_resp.status = status
                mock_resp.json = AsyncMock(return_value=data)
                mock_ctx = MagicMock()
                mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
                mock_ctx.__aexit__ = AsyncMock(return_value=None)
                mock_session.get.return_value = mock_ctx

            return mock_session

        # 1. 正常疎通: candidate_ip:8089/health が 200 かつ node_id 一致
        sess_ok = _create_mock_session(200, {"status": "ok", "node_id": node_id})
        with patch("aiohttp.ClientSession", return_value=sess_ok):
            is_ok = await server._verify_worker_direct_reachability(
                candidate_ip=target_ip,
                agent_port=agent_port,
                expected_node_id=node_id,
                force_refresh=True
            )
            self.assertTrue(is_ok)

        # 2. node_id 不一致 (NATやIP重複による別端末への接続)
        sess_diff = _create_mock_session(200, {"status": "ok", "node_id": "different-node-xyz"})
        with patch("aiohttp.ClientSession", return_value=sess_diff):
            is_ok = await server._verify_worker_direct_reachability(
                candidate_ip=target_ip,
                agent_port=agent_port,
                expected_node_id=node_id,
                force_refresh=True
            )
            self.assertFalse(is_ok)

        # 3. タイムアウト / 不通 (インバウンドポート閉塞または拠点間疎通不可)
        sess_timeout = _create_mock_session(200, {}, raise_timeout=True)
        with patch("aiohttp.ClientSession", return_value=sess_timeout):
            is_ok = await server._verify_worker_direct_reachability(
                candidate_ip=target_ip,
                agent_port=agent_port,
                expected_node_id=node_id,
                force_refresh=True
            )
            self.assertFalse(is_ok)

    # =========================================================================
    # 項目⑲: COEIROINK モデル保有検証＆双方向モデル差分自動同期テスト
    # =========================================================================
    def test_item_19_model_supports_and_diff_computation(self):
        """Worker のモデル保有検証と Master/Worker 間の差分計算の正確性"""
        cfg = TTSNodeConfig(id="worker-1", url="http://192.168.1.50:50031", priority=1)
        node = TTSNode(
            config=cfg,
            specs={
                "installed_models": ["model_a", "model_b"]
            }
        )

        # supports_model の検証: 保有モデルは True、未保有モデルは False (誤ルーティング遮断)
        self.assertTrue(node.supports_model("coeiroink", "model_a"))
        self.assertTrue(node.supports_model("coeiroink", "model_b"))
        self.assertFalse(node.supports_model("coeiroink", "model_c"))

        # WebServer._compute_coeiroink_model_diff の検証
        server = WebServer(
            bot=MagicMock(),
            config=AppConfig(),
            database=MagicMock(),
            cache_manager=MagicMock(),
            tts_manager=MagicMock()
        )

        with patch("src.tts.node_pool.scan_local_coeiroink_models", return_value={"model_a": {}, "model_c": {}}):
            models_to_pull, models_to_upload = server._compute_coeiroink_model_diff(
                worker_models_raw=["model_a", "model_b"]
            )
            # Master は model_c を持つが Worker は持たない -> Worker が Pull すべきモデル: ["model_c"]
            self.assertEqual(models_to_pull, ["model_c"])
            # Worker は model_b を持つが Master は持たない -> Worker が Upload (または Master が Pull) すべきモデル: ["model_b"]
            self.assertEqual(models_to_upload, ["model_b"])


if __name__ == "__main__":
    unittest.main()

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

    def test_cloud_tts_dynamic_chunk_scaling_and_feedback(self):
        """クラウドTTS (Edge/Azure/Google) の動的文字数適応・安全下限保護・実績フィードバックの検証"""
        profiler = EngineLatencyProfiler()

        # 1. コールドスタート初期値: クラウド安全下限 (14文字) と安全上限 (38文字) の保証
        edge_chars_init = profiler.get_dynamic_max_chars("edge", target_budget_ms=1400.0)
        self.assertGreaterEqual(edge_chars_init, 14)
        self.assertLessEqual(edge_chars_init, 38)

        azure_chars_init = profiler.get_dynamic_max_chars("azure", target_budget_ms=1400.0)
        self.assertGreaterEqual(azure_chars_init, 14)
        self.assertLessEqual(azure_chars_init, 38)

        # 2. ネットワーク遅延・混雑フィードバック (低速回線シミュレーション)
        # 15文字で 1200ms かかった実績を記録
        for _ in range(5):
            profiler.record_synth_direct("edge", 15, 1200.0)

        edge_chars_slow = profiler.get_dynamic_max_chars("edge", target_budget_ms=1400.0)
        # 遅延増大に伴い文字数が絞り込まれること
        self.assertLess(edge_chars_slow, edge_chars_init)
        # ただし API 乱発 (429/1006) を防ぐ安全下限 14文字 は下回らないこと
        self.assertGreaterEqual(edge_chars_slow, 14)

        # 3. 高速回線フィードバック (低遅延通信シミュレーション)
        # 25文字でわずか 200ms で完了する超高速実績を記録
        for _ in range(5):
            profiler.record_synth_direct("azure", 25, 200.0)

        azure_chars_fast = profiler.get_dynamic_max_chars("azure", target_budget_ms=1400.0)
        # 高速回線では自然なイントネーションのため 30文字以上にスケールアップすること
        self.assertGreaterEqual(azure_chars_fast, 30)
        self.assertLessEqual(azure_chars_fast, 38)

        # 4. TextCleaner.split_into_chunks でのクラウド話者自律連動
        text = "クラウド音声合成の動的文字数テストです。回線状況に応じて適切な長さで分割されます。"
        chunks_cloud = TextCleaner.split_into_chunks(text, speaker_id="edge_nanami")
        self.assertGreater(len(chunks_cloud), 0)
        # 先頭パーツが安全下限 14文字以上を尊重して自然に切り出されていること
        self.assertTrue(any(len(c.text.strip()) >= 10 for c in chunks_cloud))

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

    # =========================================================================
    # GitHub Code Scanning (CodeQL) セキュリティ修復テスト (CWE-918, CWE-22)
    # =========================================================================
    def test_code_scanning_security_remediation(self):
        """CodeQL Alert #83-#90 (SSRF / Path Injection) の防御検証"""
        from src.utils.security import sanitize_worker_ip, validate_safe_worker_ip, sanitize_filename

        # 1. SSRF 防護: sanitize_worker_ip の検証
        self.assertEqual(sanitize_worker_ip("192.168.1.50"), "192.168.1.50")
        self.assertEqual(sanitize_worker_ip(" 10.0.0.1 "), "10.0.0.1")
        self.assertEqual(sanitize_worker_ip("100.64.1.2"), "100.64.1.2")
        
        # 危険なIP・メタデータ・未指定・ループバックのブロック
        self.assertIsNone(sanitize_worker_ip("169.254.169.254"))  # クラウドメタデータ (AWS/GCP/Azure)
        self.assertIsNone(sanitize_worker_ip("127.0.0.1"))        # ループバック
        self.assertIsNone(sanitize_worker_ip("0.0.0.0"))          # 未指定
        self.assertIsNone(sanitize_worker_ip("224.0.0.1"))        # マルチキャスト
        self.assertIsNone(sanitize_worker_ip("192.168.1.50; id")) # コマンドインジェクション混入
        self.assertIsNone(sanitize_worker_ip("evil.com"))         # ドメイン名混入
        self.assertIsNone(sanitize_worker_ip(None))
        self.assertIsNone(sanitize_worker_ip(""))

        # validate_safe_worker_ip も一貫して動作すること
        self.assertTrue(validate_safe_worker_ip("192.168.1.50"))
        self.assertFalse(validate_safe_worker_ip("169.254.169.254"))
        self.assertFalse(validate_safe_worker_ip("127.0.0.1"))

        # 2. Path Injection 防護: 一時ファイル名にモデル名が混入しないことの検証
        server = WebServer(
            bot=MagicMock(),
            config=AppConfig(),
            database=MagicMock(),
            cache_manager=MagicMock(),
            tts_manager=MagicMock()
        )

        # _pull_missing_models_from_worker のテスト: 危険なモデル名
        dangerous_model_name = "../../etc/passwd"
        clean_name = sanitize_filename(dangerous_model_name)
        self.assertEqual(clean_name, "passwd")  # トラバーサル除去確認

    def test_mycoeiroink_upload_2gb_limit_and_worker_agent_version(self):
        """MYCOEIROINK 2GBアップロード拡張および Worker Agent バージョン単一管理元(SSOT)の検証"""
        import re
        from src.tts.mycoeiroink import MAX_UPLOAD_SIZE, MAX_EXTRACT_SIZE, MAX_SINGLE_FILE_SIZE
        from src.version import AGENT_VERSION as SERVER_AGENT_VERSION, sync_agent_py_version

        # 1. 2GB アップロード上限の検証
        self.assertEqual(MAX_UPLOAD_SIZE, 2048 * 1024 * 1024)
        self.assertEqual(MAX_EXTRACT_SIZE, 5120 * 1024 * 1024)
        self.assertEqual(MAX_SINGLE_FILE_SIZE, 2048 * 1024 * 1024)

        # 2. Worker Agent と Server 間バージョン整合性 & SSOT の検証
        agent_py_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "agent.py"
        self.assertTrue(agent_py_path.exists())
        agent_content = agent_py_path.read_text(encoding="utf-8")
        agent_ver_match = re.search(r'^\s*AGENT_VERSION\s*=\s*["\']([^"\']+)["\']', agent_content, re.MULTILINE)
        self.assertIsNotNone(agent_ver_match)
        agent_py_version = agent_ver_match.group(1)

        self.assertEqual(SERVER_AGENT_VERSION, "3.13.4")
        self.assertEqual(agent_py_version, "3.13.4")
        self.assertEqual(SERVER_AGENT_VERSION, agent_py_version)

        # 3. HTML テンプレートの単一管理元プレースホルダー検証 (ハードコード廃止)
        index_html_path = Path(__file__).resolve().parent.parent / "src" / "web" / "templates" / "index.html"
        self.assertTrue(index_html_path.exists())
        index_html_text = index_html_path.read_text(encoding="utf-8")
        self.assertIn("最新 Agent v{{AGENT_VERSION}} (自律同期対応)", index_html_text)
        self.assertIn('"agent_version": "{{AGENT_VERSION}}"', index_html_text)
        self.assertNotIn("最新 Agent v3.13.2", index_html_text)

        # 4. JS 側のハードコード廃止検証
        app_js_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "app.js"
        self.assertTrue(app_js_path.exists())
        app_js_text = app_js_path.read_text(encoding="utf-8")
        self.assertNotIn('|| "3.13.2"', app_js_text)

        # 5. sync_agent_py_version 関数の動作検証
        # 既に同期済みなので False が返るはず
        self.assertFalse(sync_agent_py_version())

    def test_dynamic_speculative_timeout_policy(self):
        """投機的フォールバックの動的最適化と上限キャップ機能の検証"""
        from src.utils.latency_profiler import latency_profiler

        # 1. 高速エンジン (VOICEVOX): cap_timeout が大きくても、下限付近(0.8s〜1.2s)で即時ローカル優先になること
        vv_timeout_cap25 = latency_profiler.calculate_dynamic_speculative_timeout(
            base_timeout=2.5, engine="voicevox", text_len=10, chunk_index=0
        )
        vv_timeout_cap40 = latency_profiler.calculate_dynamic_speculative_timeout(
            base_timeout=4.0, engine="voicevox", text_len=10, chunk_index=0
        )
        # 高速モデルは 4.0s まで無駄に待たず、下限0.8s〜1.2s前後で即座に返る
        self.assertLessEqual(vv_timeout_cap25, 1.5)
        self.assertLessEqual(vv_timeout_cap40, 1.5)
        self.assertGreaterEqual(vv_timeout_cap25, 0.8)

        # 2. 重いエンジン (COEIROINK): 上限キャップ (1.5s / 2.5s / 4.0s) まで粘り強くローカルを待つこと
        # 1.5s テンポ優先モード: 1.5s でクランプ
        ci_timeout_cap15 = latency_profiler.calculate_dynamic_speculative_timeout(
            base_timeout=1.5, engine="coeiroink", text_len=20, chunk_index=0
        )
        self.assertEqual(ci_timeout_cap15, 1.5)

        # 2.5s バランスモード: 2.5s でクランプ
        ci_timeout_cap25 = latency_profiler.calculate_dynamic_speculative_timeout(
            base_timeout=2.5, engine="coeiroink", text_len=20, chunk_index=0
        )
        self.assertEqual(ci_timeout_cap25, 2.5)

        # 4.0s 声色優先モード: 2.5s を超えて 3秒以上粘る
        ci_timeout_cap40 = latency_profiler.calculate_dynamic_speculative_timeout(
            base_timeout=4.0, engine="coeiroink", text_len=20, chunk_index=0
        )
        self.assertGreater(ci_timeout_cap40, 2.5)
        self.assertLessEqual(ci_timeout_cap40, 4.0)

        # 3. 後続チャンク (chunk_index >= 1) でのバッファヘッドルーム連動
        # ヘッドルームが 3.0秒ある場合、少なくとも 2.5秒以上ローカルを粘る
        follow_timeout = latency_profiler.calculate_dynamic_speculative_timeout(
            base_timeout=2.5, engine="coeiroink", text_len=15, chunk_index=1, buffer_headroom_sec=3.0
        )
        self.assertGreaterEqual(follow_timeout, 2.5)
        self.assertLessEqual(follow_timeout, 6.0)

    def test_latency_tracker_and_profiler_remediation(self):
        """レイテンシモニター統計機能＆プロファイラの是正検証 (独立サンプリング, 10文字正規化P90, 物理的整合, 体感評価, ウォームスタート)"""
        from src.utils.latency_tracker import (
            LatencyTracker, get_synthesis_category, get_provider_profile_key,
            CAT_CLOUD, CAT_GPU_FAST, CAT_CPU,
            CAT_COEIROINK_WARM, CAT_COEIROINK_COLD
        )
        from src.utils.latency_profiler import EngineLatencyProfiler, NodeLatencyProfile

        # 1. プロバイダー別独立カテゴリ分類 & バッファキーの検証
        self.assertEqual(get_synthesis_category("azure"), CAT_CLOUD)
        self.assertEqual(get_synthesis_category("edge"), CAT_CLOUD)
        self.assertEqual(get_synthesis_category("google"), CAT_CLOUD)
        self.assertEqual(get_synthesis_category("openjtalk"), CAT_CPU)
        self.assertEqual(get_synthesis_category("voicevox", has_gpu=True), CAT_GPU_FAST)
        self.assertEqual(get_synthesis_category("voicevox", has_gpu=False), CAT_CPU)
        self.assertEqual(get_synthesis_category("coeiroink", is_model_loaded=True), CAT_COEIROINK_WARM)
        self.assertEqual(get_synthesis_category("coeiroink", is_model_loaded=False), CAT_COEIROINK_COLD)

        self.assertEqual(get_provider_profile_key("azure", CAT_CLOUD), "azure")
        self.assertEqual(get_provider_profile_key("edge", CAT_CLOUD), "edge")
        self.assertEqual(get_provider_profile_key("google", CAT_CLOUD), "google")

        # 2. 独立サンプリング & 10文字正規化 P90 の検証
        tracker = LatencyTracker(max_history=50)

        # Azure: 10文字 200ms を多数投入
        for _ in range(10):
            tracker._add_chunk_sample({"provider": "azure", "char_count": 10, "synth_ms": 200.0, "is_cached": False})

        # Edge: 10文字 3500ms を多数投入
        for _ in range(10):
            tracker._add_chunk_sample({"provider": "edge", "char_count": 10, "synth_ms": 3500.0, "is_cached": False})

        # Azure と Edge のバッファが分離され、Azure に Edge が潰されないこと
        est_azure = tracker.get_estimated_synthesis_time("azure", char_count=10)
        est_edge = tracker.get_estimated_synthesis_time("edge", char_count=10)

        # Azure は高速 (0.15s〜0.35s)
        self.assertLess(est_azure, 0.40)
        # Edge は 1.5s キャップで切り捨てられず、3.0秒以上の実態を反映すること
        self.assertGreater(est_edge, 2.5)
        self.assertLessEqual(est_edge, 4.5)

        # 3. 内訳バーの物理的整合性 & 非キャッシュ実測平均の分離検証
        # 発話1: 名前キャッシュ(0.1ms) + 本文推論(1500ms)
        chunks1 = [
            {"chunk_index": 0, "role": "name", "synth_ms": 0.1, "is_cached": True, "duration_ms": 800.0},
            {"chunk_index": 1, "role": "body", "synth_ms": 1500.0, "is_cached": False, "duration_ms": 2000.0}
        ]
        r1 = tracker.record(
            user_name="User1",
            text="テスト発話本文です",
            speaker_name="Speaker1",
            provider="edge",
            worker_label="Edge / CLOUD",
            t_discord_created=100.0,
            t_received=100.1,
            t_queued=100.1,
            t_dequeued=100.1,
            t_synth_start=100.1,
            t_synth_end=100.15,
            t_play_start=100.3,
            is_cached=False,
            chunks=chunks1,
            underrun_ms=0.0,
            turn_ttfs_ms=200.0,
            core_ttfs_ms=50.0,
            initial_synth_ms=0.1,
        )
        self.assertEqual(r1.initial_synth_ms, 0.1)

        summary = tracker.get_summary()
        # 内訳バー用の synth_ms は initial_synth_ms (0.1ms) を反映していること
        self.assertEqual(summary["breakdown"]["synth_ms"], 0.1)
        self.assertEqual(summary["breakdown"]["total_synth_ms"], 1500.1)

        # 4. ステータス評価の体感整合性 (アンダーラン発生時の即座警告判定)
        r_underrun = tracker.record(
            user_name="User2",
            text="音飛びテスト",
            speaker_name="Speaker1",
            provider="edge",
            worker_label="Edge / CLOUD",
            t_discord_created=100.0,
            t_received=100.1,
            t_queued=100.1,
            t_dequeued=100.1,
            t_synth_start=100.1,
            t_synth_end=100.15,
            t_play_start=100.3,
            is_cached=False,
            chunks=chunks1,
            underrun_ms=500.0,  # 500msの音飛び
            turn_ttfs_ms=200.0,
            core_ttfs_ms=10.0,  # core_ttfs が小さくても
        )
        # 音飛びがある場合は "slow" (要警告) になること
        self.assertEqual(r_underrun.status_level, "slow")

        # 名前キャッシュで core_ttfs=0.2ms でも turn_ttfs=750ms なら "good"/"ultra" に偽装されないこと
        r_slow_turn = tracker.record(
            user_name="User3",
            text="待機ラグテスト",
            speaker_name="Speaker1",
            provider="edge",
            worker_label="Edge / CLOUD",
            t_discord_created=100.0,
            t_received=100.1,
            t_queued=100.1,
            t_dequeued=100.1,
            t_synth_start=100.1,
            t_synth_end=100.15,
            t_play_start=100.85,
            is_cached=False,
            chunks=chunks1,
            underrun_ms=0.0,
            turn_ttfs_ms=750.0,  # 750ms待たされた
            core_ttfs_ms=0.2,   # 名前キャッシュによりcore_ttfsは極小
        )
        self.assertIn(r_slow_turn.status_level, ("fair", "slow"))

        # 5. 適応型 EWMA & ウォームスタートの検証
        prof = NodeLatencyProfile("test_node", "VOICEVOX", has_gpu=True, gpu_name="RTX 4090")
        # 1回目の実績 (10文字 100ms)
        prof.record(10, 100.0)
        # 初回は alpha=1.0 が適用され、即座に実測値に急収束すること
        self.assertLessEqual(prof.ms_per_char, 15.0)

        # restore_from_history の検証
        eng_prof = EngineLatencyProfiler()
        history_records = [
            {
                "chunks": [
                    {"provider": "edge", "char_count": 10, "synth_ms": 1200.0, "is_cached": False},
                    {"provider": "azure", "char_count": 10, "synth_ms": 210.0, "is_cached": False},
                ]
            }
        ]
        cnt = eng_prof.restore_from_history(history_records)
        self.assertEqual(cnt, 2)
        stats = eng_prof.get_all_stats()
        self.assertIn("cloud_direct:EDGE", stats["nodes"])
        self.assertIn("cloud_direct:AZURE", stats["nodes"])

    # =========================================================================
    # 項目⑲: Worker Agent モデル配信エンドポイントのパストラバーサル防御テスト (CodeQL CWE-22)
    # =========================================================================
    async def test_item_19_agent_model_download_path_traversal_protection(self):
        """Worker Agent の handle_model_download および validate_safe_path におけるパストラバーサル遮断の検証"""
        import importlib.util
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        agent_spec = importlib.util.spec_from_file_location("worker_agent", "src/web/static/agent.py")
        agent_module = importlib.util.module_from_spec(agent_spec)
        agent_spec.loader.exec_module(agent_module)

        # 1. validate_safe_path の直接検証
        base_dir = Path(self.temp_dir) / "models"
        base_dir.mkdir(parents=True, exist_ok=True)
        (base_dir / "safe_model").mkdir()

        # 正常なサブパス
        self.assertIsNotNone(agent_module.validate_safe_path(base_dir, "safe_model"))
        # トラバーサル攻撃パターン
        self.assertIsNone(agent_module.validate_safe_path(base_dir, "../../../etc/passwd"))
        self.assertIsNone(agent_module.validate_safe_path(base_dir, "safe_model/../../.."))
        self.assertIsNone(agent_module.validate_safe_path(base_dir, "/etc/passwd"))

        # 2. handle_model_download の動作検証
        app = web.Application()
        app["agent"] = None

        with patch.object(agent_module, "resolve_work_dir", return_value=str(self.temp_dir)):
            models_root = Path(self.temp_dir) / "coeiroink_models"
            models_root.mkdir(parents=True, exist_ok=True)
            valid_m = models_root / "test_model_1"
            valid_m.mkdir(parents=True, exist_ok=True)
            (valid_m / "metas.json").write_text('{"characterName": "Test1"}')

            # ケースA: 無効な識別子 (..を含む、または空) -> 400 Bad Request
            req_invalid = make_mocked_request("GET", "/download/....", match_info={"model_name": "...."}, app=app)
            res_invalid = await agent_module.handle_model_download(req_invalid)
            self.assertEqual(res_invalid.status, 400)

            # ケースB: パストラバーサル試行 (../../etc/passwd) -> basenameが無害化されるが実在しないため 404 Not Found
            req_traversal = make_mocked_request("GET", "/download/..%2F..%2Fetc%2Fpasswd", match_info={"model_name": "../../etc/passwd"}, app=app)
            res_traversal = await agent_module.handle_model_download(req_traversal)
            self.assertEqual(res_traversal.status, 404)

            # ケースC: 存在しないモデル名 -> 404 Not Found
            req_not_found = make_mocked_request("GET", "/download/non_existent_model", match_info={"model_name": "non_existent_model"}, app=app)
            res_not_found = await agent_module.handle_model_download(req_not_found)
            self.assertEqual(res_not_found.status, 404)

            # ケースD: 実在するモデル名 -> 200 OK (Content-Type: application/x-tar)
            req_valid = make_mocked_request("GET", "/download/test_model_1", match_info={"model_name": "test_model_1"}, app=app)
            res_valid = await agent_module.handle_model_download(req_valid)
            self.assertEqual(res_valid.status, 200)
            self.assertEqual(res_valid.headers.get("Content-Type"), "application/x-tar")


if __name__ == "__main__":
    unittest.main()

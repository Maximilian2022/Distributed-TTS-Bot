import asyncio
import gzip
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.cache_manager import CacheManager
from src.cogs.vc_handler import VCHandlerCog
from src.config import AppConfig, TTSNodeConfig, load_config
from src.database import Database
from src.ha_watchdog import HAWatchdog
from src.stt.dispatcher import SpeechDispatcher
from src.stt.manager import STTManager
from src.tts.node_pool import TTSNode, TTSNodePool
from src.tts.ws_node_adapter import WSWorkerNode
from src.utils.text_cleaner import TextCleaner
from src.utils.worker_log_collector import WorkerLogCollector
from src.web.server import WebServer


class TestReauditSecurityAndUserDict(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self._tmpdir.name)
        self.data_dir = self.base_dir / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.cfg = AppConfig(
            db_path=str(self.data_dir / "test.db"),
            cache_dir=str(self.base_dir / "cache"),
            voicevox_cache_dir=str(self.base_dir / "cache_vv"),
            web_session_secret="reaudit-test-session-secret-0123456789",
            worker_shared_secret="reaudit-worker-shared-secret",
            ha_secret="reaudit-ha-shared-secret",
            admin_password="InitialAdminPass!123",
            web_dashboard_url="https://tts.example.com",
            discord_owner_user_id=111111111,
            admin_user_ids=[111111111],
        )
        self.db = Database(self.cfg.db_path)
        await self.db.init_db()
        self.cache = CacheManager(
            cache_dir=self.cfg.cache_dir,
            voicevox_cache_dir=self.cfg.voicevox_cache_dir,
        )
        self.cache.set_database(self.db)

        # Guild 999:
        # 111111111 = System Super Admin
        # 222222222 = Guild Admin (NOT System Super Admin)
        # 333333333 = Regular Member
        guild_999 = MagicMock()
        guild_999.id = 999
        guild_999.name = "ReauditGuild"

        def _make_member(uid: int, is_guild_admin: bool):
            m = MagicMock()
            m.id = uid
            m.display_name = f"Member{uid}"
            m.guild_permissions = SimpleNamespace(
                administrator=is_guild_admin,
                manage_guild=is_guild_admin,
            )
            m.roles = []
            return m

        members = {
            111111111: _make_member(111111111, True),
            222222222: _make_member(222222222, True),
            333333333: _make_member(333333333, False),
        }
        guild_999.get_member = lambda uid: members.get(int(uid))
        guild_999.members = list(members.values())

        self.bot = MagicMock()
        self.bot.is_ready.return_value = True
        self.bot.latency = 0.015
        self.bot.guilds = [guild_999]
        self.bot.get_guild = lambda gid: guild_999 if int(gid) == 999 else None

        self.tts = MagicMock()
        self.tts.voicevox_pool = TTSNodePool("voicevox", [])
        self.tts.sharevox_pool = TTSNodePool("sharevox", [])
        self.tts.coeiroink_pool = TTSNodePool("coeiroink", [])
        self.tts.ws_worker_nodes = {}
        self.tts.resource_manager = None

        self.web_srv = WebServer(
            bot=self.bot,
            config=self.cfg,
            database=self.db,
            cache_manager=self.cache,
            tts_manager=self.tts,
        )
        self.client = TestClient(TestServer(self.web_srv.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.db.close()
        self._tmpdir.cleanup()

    def _cookie_for(self, uid: int, is_admin: bool = False) -> dict:
        tok = self.web_srv.auth.create_session_token(
            {"id": str(uid), "username": f"user_{uid}", "global_name": f"User {uid}"},
            is_admin=is_admin,
        )
        return {"ttsbot_session": tok}

    async def test_s2_1_fake_cwtok_blocked_on_wan_and_valid_cwtok_accepted(self):
        """S2-1: WAN経由の偽 cwtok_ バイパス遮断と、DB登録済み正規 cwtok_ の Heartbeat / Models 認可"""
        wan_headers_fake = {
            "Host": "tts.example.com",
            "CF-Connecting-IP": "203.0.113.55",
            "Authorization": "Bearer cwtok_forged_attacker_token_999",
        }
        r_fake_models = await self.client.get("/api/worker/models/list", headers=wan_headers_fake)
        self.assertIn(r_fake_models.status, (401, 403))

        r_fake_hb = await self.client.post(
            "/api/worker/heartbeat",
            json={"node_id": "attacker_node"},
            headers=wan_headers_fake,
        )
        self.assertIn(r_fake_hb.status, (401, 403))

        # DBで正規の worker セットアップトークンを発行
        _tid, valid_cwtok = await self.db.create_node_setup_token(111111111, "OwnerAdmin", "worker")
        wan_headers_valid = {
            "Host": "tts.example.com",
            "CF-Connecting-IP": "203.0.113.55",
            "Authorization": f"Bearer {valid_cwtok}",
        }
        r_valid_models = await self.client.get("/api/worker/models/list", headers=wan_headers_valid)
        self.assertEqual(r_valid_models.status, 200)

        # Heartbeat テスト用にノードを1件追加しておく
        self.tts.voicevox_pool.add_node(
            TTSNode(TTSNodeConfig(id="valid_node_voicevox", url="http://10.0.0.5:50021", priority=1, max_concurrent=2, enabled=True))
        )
        r_valid_hb = await self.client.post(
            "/api/worker/heartbeat",
            json={"node_id": "valid_node", "engines": {"voicevox": {"running": True, "healthy": True}}},
            headers=wan_headers_valid,
        )
        self.assertEqual(r_valid_hb.status, 200)
        hb_json = await r_valid_hb.json()
        self.assertTrue(hb_json.get("registered"))

    async def test_s2_4_guild_admin_vs_system_admin_two_tier_permissions(self):
        """S2-4: ギルド管理者は統計・モデル状況を閲覧でき、全体キャッシュ本文・システム変更はシステム最高管理者のみ許可"""
        guild_admin_cookies = self._cookie_for(222222222, is_admin=False)
        sys_admin_cookies = self._cookie_for(111111111, is_admin=True)

        # ギルド管理者に許可される読み取り系 (テキスト本文を含まないサマリ・モデル状況)
        r_summary = await self.client.get("/api/cache/stats/summary?guild_id=999", cookies=guild_admin_cookies)
        self.assertEqual(r_summary.status, 200)

        r_models = await self.client.get("/api/system/models/status?guild_id=999", cookies=guild_admin_cookies)
        self.assertEqual(r_models.status, 200)

        # ギルド管理者には拒否されるシステム全体・他サーバー本文系
        for restricted_get in (
            "/api/cache/files?guild_id=999",
            "/api/cache/audio/cloud/nonexistent.wav?guild_id=999",
            "/api/system/latency/incidents?guild_id=999",
        ):
            resp = await self.client.get(restricted_get, cookies=guild_admin_cookies)
            self.assertEqual(resp.status, 403, f"Expected 403 for guild admin on {restricted_get}")

        for restricted_post in (
            "/api/cache/scan?guild_id=999",
            "/api/system/latency/reset?guild_id=999",
        ):
            resp = await self.client.post(restricted_post, json={}, cookies=guild_admin_cookies)
            self.assertEqual(resp.status, 403, f"Expected 403 for guild admin on {restricted_post}")

        # システム最高管理者は閲覧・実行可能
        r_sys_files = await self.client.get("/api/cache/files", cookies=sys_admin_cookies)
        self.assertEqual(r_sys_files.status, 200)

        r_sys_inc = await self.client.get("/api/system/latency/incidents", cookies=sys_admin_cookies)
        self.assertEqual(r_sys_inc.status, 200)

        r_sys_reset = await self.client.post("/api/system/latency/reset", json={}, cookies=sys_admin_cookies)
        self.assertEqual(r_sys_reset.status, 200)

    async def test_m2_7_s2_7_password_rotation_invalidates_old_admin_sessions_and_headers(self):
        """M2-7 & S2-7: 管理者パスワード変更時の旧 uid=0 セッション即時失効・CSP付与・LAN/HTTPS Cookie 属性制御"""
        # 1. LAN HTTP でのパスワードログイン -> Secure 属性が付かないこと & CSP ヘッダがあること
        r_login = await self.client.post(
            "/api/auth/login_password",
            json={"password": "InitialAdminPass!123"},
        )
        self.assertEqual(r_login.status, 200)
        self.assertIn("Content-Security-Policy", r_login.headers)
        set_cookie_lan = r_login.headers.get("Set-Cookie", "")
        self.assertIn("ttsbot_session=", set_cookie_lan)
        self.assertNotIn("Secure", set_cookie_lan)

        old_token = r_login.cookies["ttsbot_session"].value

        # 旧トークンで管理者APIへアクセス可能
        r_before = await self.client.get("/api/system/credentials", cookies={"ttsbot_session": old_token})
        self.assertEqual(r_before.status, 200)

        # HTTPS 経由でパスワードを変更 -> 新 Cookie には Secure が付き、旧トークンは即座に失効すること
        self.client.session.cookie_jar.clear()
        r_save = await self.client.post(
            "/api/system/credentials",
            json={"admin_password": "RotatedSuperPass!456"},
            cookies={"ttsbot_session": old_token},
            headers={"X-Forwarded-Proto": "https"},
        )
        self.assertEqual(r_save.status, 200)
        set_cookie_https = r_save.headers.get("Set-Cookie", "")
        self.assertIn("Secure", set_cookie_https)
        new_token = r_save.cookies["ttsbot_session"].value
        self.assertNotEqual(old_token, new_token)

        # 旧セッション Cookie は即座に無効化 (401/403)
        self.client.session.cookie_jar.clear()
        r_old_after = await self.client.get("/api/system/credentials", cookies={"ttsbot_session": old_token})
        self.assertIn(r_old_after.status, (401, 403))

        # 新セッション Cookie は引き続き有効 (200)
        self.client.session.cookie_jar.clear()
        r_new_after = await self.client.get("/api/system/credentials", cookies={"ttsbot_session": new_token})
        self.assertEqual(r_new_after.status, 200)

    async def test_s2_6_app_login_rejects_non_member(self):
        """S2-6: Bot未参加ユーザーIDに対する Discord App Login リクエストの遮断"""
        r_outsider = await self.client.post(
            "/api/auth/discord_app_login/request",
            json={"username_or_id": "999999999999"},
        )
        self.assertIn(r_outsider.status, (403, 404))
        self.assertEqual(self.web_srv.auth.pending_app_logins, {})

    async def test_a3_user_personal_dictionary_api_and_priority(self):
        """A3: ユーザー個人辞書 API (/api/me/dict) と「個人辞書 > サーバー辞書 > 共通辞書」の最優先適用検証"""
        # 未ログインは 401
        r_unauth = await self.client.get("/api/me/dict")
        self.assertEqual(r_unauth.status, 401)

        member_cookies = self._cookie_for(333333333, is_admin=False)

        # 個人辞書へ登録
        r_add = await self.client.post(
            "/api/me/dict",
            json={"word": "草", "reading": "くさもち"},
            cookies=member_cookies,
        )
        self.assertEqual(r_add.status, 200)

        r_add2 = await self.client.post(
            "/api/me/dict",
            json={"word": "MyTag", "reading": "マイタグ"},
            cookies=member_cookies,
        )
        self.assertEqual(r_add2.status, 200)

        # 一覧取得 (他ユーザーには見えないことも検証)
        r_list = await self.client.get("/api/me/dict", cookies=member_cookies)
        self.assertEqual(r_list.status, 200)
        list_data = await r_list.json()
        words_map = {item["word"]: item["reading"] for item in list_data["dictionary"]}
        self.assertEqual(words_map.get("草"), "くさもち")
        self.assertEqual(words_map.get("MyTag"), "マイタグ")

        other_cookies = self._cookie_for(222222222, is_admin=False)
        r_other = await self.client.get("/api/me/dict", cookies=other_cookies)
        other_data = await r_other.json()
        self.assertEqual(other_data["dictionary"], [])

        # サーバー辞書に同じ「草 -> わらい」「ServerOnly -> サーバーオンリー」を登録
        await self.db.add_dictionary_word(999, "草", "わらい")
        await self.db.add_dictionary_word(999, "ServerOnly", "サーバーオンリー")

        user_words = await self.db.get_user_dictionary_words(333333333)
        guild_words = await self.db.get_dictionary_words(999)
        merged = VCHandlerCog._merge_user_and_guild_dicts(user_words, guild_words)

        cleaner = TextCleaner(max_length=100)
        cleaned = cleaner.clean("草とMyTagとServerOnly", dictionary_entries=merged)
        self.assertIn("くさもち", cleaned)
        self.assertNotIn("わらい", cleaned)
        self.assertIn("マイタグ", cleaned)
        self.assertIn("サーバーオンリー", cleaned)
        self.assertTrue(any(m.lower() == "serveronly" for m in cleaner.last_matched_dict_words))

        # 削除
        r_del = await self.client.delete("/api/me/dict?word=%E8%8D%89", cookies=member_cookies)
        self.assertEqual(r_del.status, 200)
        after_words = await self.db.get_user_dictionary_words(333333333)
        self.assertNotIn("草", [w for w, _ in after_words])

    async def test_m2_1_ha_state_files_snapshot_and_watchdog_sync(self):
        """M2-1: Master の /api/ha/state_files_snapshot 配信・ETag 304・HAWatchdog によるアトミック同期"""
        (self.data_dir / "tier_routing.json").write_text(
            json.dumps({"tier_order": ["gpu_ram", "gpu_ssd", "cpu_ram", "cpu_ssd"]}),
            encoding="utf-8",
        )
        (self.data_dir / "custom_common_reading.json").write_text(
            json.dumps({"entries": {"初見": "しょけん"}}),
            encoding="utf-8",
        )

        ha_headers = {"Authorization": f"Bearer {self.cfg.ha_secret}"}
        r_snap = await self.client.get("/api/ha/state_files_snapshot", headers=ha_headers)
        self.assertEqual(r_snap.status, 200)
        etag = r_snap.headers.get("ETag", "")
        self.assertTrue(etag)
        raw_payload = json.loads(gzip.decompress(await r_snap.read()).decode("utf-8"))
        self.assertIn("tier_routing.json", raw_payload["files"])
        self.assertIn("custom_common_reading.json", raw_payload["files"])

        # ETag 一致時は 304 Not Modified
        r_304 = await self.client.get(
            "/api/ha/state_files_snapshot",
            headers={**ha_headers, "If-None-Match": etag},
        )
        self.assertEqual(r_304.status, 304)

        # Slave Watchdog 側への同期テスト
        slave_dir = self.base_dir / "slave_data"
        slave_dir.mkdir(parents=True, exist_ok=True)
        master_url = f"http://127.0.0.1:{self.client.server.port}"
        with patch.dict(
            "os.environ",
            {
                "TTSBOT_MASTER_URL": master_url,
                "TTSBOT_HA_SECRET": self.cfg.ha_secret,
                "TTSBOT_LOCAL_DB_PATH": str(slave_dir / "bot.db"),
            },
        ):
            wd = HAWatchdog()
            ok = await asyncio.to_thread(wd.sync_state_files_snapshot)
            self.assertTrue(ok)
            self.assertTrue((slave_dir / "tier_routing.json").is_file())
            synced_common = json.loads((slave_dir / "custom_common_reading.json").read_text(encoding="utf-8"))
            self.assertEqual(synced_common["entries"]["初見"], "しょけん")

    async def test_p2_1_write_behind_cache_hit_buffer_flush(self):
        """P2-1: キャッシュヒット統計のライトビハインド集約と Read-your-writes フラッシュ整合性"""
        await self.db.record_cache_saved(
            cache_key="ck_test_1",
            provider="voicevox",
            speaker_id="vv_3",
            speaker_name="Zundamon",
            text="こんにちは",
            size_bytes=4800,
        )
        await self.db.record_cache_hit("ck_test_1")
        await self.db.record_cache_hit("ck_test_1")
        await self.db.record_cache_hit(5, is_local=True)
        await self.db.record_cache_hit(8, is_local=False)

        # get_monthly_tts_usage と get_cache_metadata が未フラッシュ分を自動フラッシュして正確な値を返すこと
        usage = await self.db.get_monthly_tts_usage()
        self.assertEqual(usage["savings"]["local"]["saved_characters"], 5)
        self.assertEqual(usage["savings"]["cloud"]["saved_characters"], 8)
        self.assertEqual(usage["savings"]["hits"], 2)

        meta = await self.db.get_cache_metadata("ck_test_1")
        self.assertEqual(meta["hit_count"], 3)


class TestAppJsEncodingAndPipelineFixes(unittest.IsolatedAsyncioTestCase):
    def test_s2_3_app_js_safe_encode_attr_uri_escapes_single_quotes(self):
        """S2-3: app.js の safeEncodeAttrUri がシングルクォートを %27 に確実にエスケープし XSS 脱出を防ぐこと"""
        import shutil
        js_path = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "app.js"
        js_src = js_path.read_text(encoding="utf-8")
        self.assertIn("function safeEncodeAttrUri(", js_src)
        self.assertIn("function safeExternalUrl(", js_src)
        self.assertIn("async function loadUserDict(", js_src)
        # 個人辞書描画関数内で innerHTML が使われていないこと
        fn_segment = js_src.split("async function loadUserDict(", 1)[1][:2200]
        self.assertNotIn("innerHTML", fn_segment)

        if shutil.which("node"):
            script = (
                "function safeEncodeAttrUri(val) {"
                "  return encodeURIComponent(String(val ?? '')).replace(/[!'()*]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());"
                "}"
                "console.log(safeEncodeAttrUri(\"word');alert(1);//\"));"
            )
            res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
            out = res.stdout.strip()
            self.assertNotIn("'", out)
            self.assertNotIn("(", out)
            self.assertNotIn(")", out)
            self.assertIn("%27", out)

    def test_s2_2_b2_6_config_reload_preserves_dynamic_voices_and_hidden_flag(self):
        """S2-2 & B2-6: AppConfig.reload() での動的話者・シークレット保持と hidden フラグ読み込み"""
        cfg = load_config()
        self.assertIn("edge_nanami", cfg.voices)
        self.assertTrue(cfg.voices["edge_nanami"].hidden)
        self.assertEqual(cfg.az_margin_seconds, 0.68)
        self.assertEqual(cfg.chunk_singleflight, "on")

        from src.config import VoiceConfig
        cfg.voices["ci_dynamic_999"] = VoiceConfig(
            id="ci_dynamic_999",
            name="Dynamic Lilin",
            gender="female",
            description="Dynamic local voice",
            provider="coeiroink",
            voice_name="cb11bdbd-78fc-4f16-b528-a400bae1782d_90",
        )
        cfg.azure_speech_key = "db-injected-azure-key"
        cfg.web_session_secret = "runtime-generated-secret"

        cfg.reload()
        self.assertIn("ci_dynamic_999", cfg.voices)
        self.assertEqual(cfg.azure_speech_key, "db-injected-azure-key")
        self.assertEqual(cfg.web_session_secret, "runtime-generated-secret")

    async def test_b2_3_b2_7_node_pool_gpu_suppression_and_ws_model_mapping(self):
        """B2-3 & B2-7: 空きGPU稼働中の下位CPU起動抑止、success:false 拒否、WSWorkerNode UUIDマッピング照合"""
        gpu_node = TTSNode(
            TTSNodeConfig(id="gpu_1", url="http://10.0.0.10:50031", priority=1, max_concurrent=2, enabled=True),
            specs={"gpu": True, "engine_running": True, "engine_healthy": True},
        )
        cpu_node = TTSNode(
            TTSNodeConfig(id="cpu_debian", url="http://10.0.0.20:50031", priority=5, max_concurrent=1, enabled=True),
            specs={"gpu": False, "engine_running": False, "engine_healthy": False},
        )
        cpu_node.is_healthy = False
        cpu_node.last_heartbeat = 9999999999.0
        cpu_node.try_remote_start = AsyncMock(return_value=False)

        pool = TTSNodePool("coeiroink", [])
        pool.add_node(gpu_node)
        pool.add_node(cpu_node)

        lease = await pool.acquire_node(timeout=0.5, text_len=10)
        self.assertIsNotNone(lease)
        self.assertEqual(lease.node.id, "gpu_1")
        await lease.__aexit__(None, None, None)
        # 空きGPUがある状態では停止中CPUノードへの try_remote_start は呼ばれないこと
        cpu_node.try_remote_start.assert_not_called()

        # WSWorkerNode がディレクトリ名申告 ('lilin-2.1.0') でも UUID_style リクエストにマッチすること
        mock_ws = MagicMock()
        mock_ws.closed = False
        ws_node = WSWorkerNode(
            node_id="vol_test",
            node_name="GamingPC",
            ws=mock_ws,
            supported_engines=["coeiroink"],
            supported_models={"coeiroink": ["lilin-2.1.0"]},
        )
        self.assertTrue(ws_node.supports_model("coeiroink", "cb11bdbd-78fc-4f16-b528-a400bae1782d_90"))
        self.assertFalse(ws_node.supports_model("coeiroink", "00000000-0000-0000-0000-000000000000_0"))

    async def test_b2_4_b2_5_stt_multilang_and_multibot_isolation(self):
        """B2-4 & B2-5: SpeechDispatcher 2言語同時翻訳保持 + AllowedMentions.none() + マルチVC独立STT管理"""
        sent_calls = []
        mock_ch = MagicMock()

        async def _fake_send(content, **kwargs):
            sent_calls.append((content, kwargs))

        mock_ch.send = _fake_send
        mock_bot = MagicMock()
        mock_bot.get_channel.return_value = mock_ch

        disp = SpeechDispatcher(bot=mock_bot, text_channel_id=555, buffer_seconds=0.3)
        await disp.on_transcript(1001, "Alice", {"text": "こんにちは @everyone", "is_final": True, "seq": 1})
        await disp.on_translation(1001, "Alice", {"text": "Hello", "lang": "en", "seq": 1})
        await disp.on_translation(1001, "Alice", {"text": "안녕하세요", "lang": "ko", "seq": 1})

        await STTManager._flush_dispatcher_immediate(disp)
        self.assertEqual(len(sent_calls), 1)
        msg_text, send_kwargs = sent_calls[0]
        self.assertIn("[EN] Hello", msg_text)
        self.assertIn("[KO] 안녕하세요", msg_text)
        self.assertIn("allowed_mentions", send_kwargs)
        self.assertFalse(send_kwargs["allowed_mentions"].everyone)

        # マルチBot独立セッション管理の検証
        with tempfile.TemporaryDirectory() as td:
            db = Database(str(Path(td) / "stt.db"))
            await db.init_db()
            try:
                cfg = AppConfig(hayamimi_ws_url="ws://127.0.0.1:8833/ws/stream")
                mgr = STTManager(mock_bot, db, cfg)

                rec1 = MagicMock(_is_running=True, stop=AsyncMock())
                rec2 = MagicMock(_is_running=True, stop=AsyncMock())
                disp1 = SpeechDispatcher(bot=mock_bot, text_channel_id=None)
                disp2 = SpeechDispatcher(bot=mock_bot, text_channel_id=None)
                mgr._receivers_by_pair[(777, 1)] = rec1
                mgr._dispatchers_by_pair[(777, 1)] = disp1
                mgr._receivers_by_pair[(777, 2)] = rec2
                mgr._dispatchers_by_pair[(777, 2)] = disp2
                mgr.receivers[777] = rec2

                self.assertTrue(mgr.is_active(777, bot_index=1))
                self.assertTrue(mgr.is_active(777, bot_index=2))

                # 1号機のみ停止しても2号機は稼働継続すること
                await mgr.stop_stt(777, clear_persisted=False, bot_index=1)
                self.assertFalse(mgr.is_active(777, bot_index=1))
                self.assertTrue(mgr.is_active(777, bot_index=2))
                rec1.stop.assert_awaited_once()
                rec2.stop.assert_not_awaited()

                await mgr.stop_all(clear_persisted=False)
                self.assertFalse(mgr.is_active(777))
            finally:
                await db.close()

    async def test_p2_2_p2_3_precompute_local_kanji_and_worker_log_collector(self):
        """P2-2 & P2-3: split_into_chunks の local_text 事前計算と WorkerLogCollector 非同期書き出し"""
        chunks = await TextCleaner.split_into_chunks_async(
            "明日は東京駅に行きます。よろしくお願いします！",
            author_prefix="太郎、",
            engine="coeiroink",
            precompute_local_kanji=True,
        )
        self.assertGreaterEqual(len(chunks), 2)
        for ch in chunks:
            if not ch.is_beep:
                self.assertIsNotNone(ch.local_text)

        with tempfile.TemporaryDirectory() as td:
            with patch("src.utils.worker_log_collector.LOG_DIR_BASE", td):
                wlc = WorkerLogCollector(check_interval=60.0)
                ok = await asyncio.to_thread(
                    wlc._persist_worker_logs_sync,
                    "worker_test",
                    "10.0.0.99",
                    {
                        "agent_version": "3.15.0",
                        "agent_logs": ["line1\x07", "line2"],
                        "engines": {"coeiroink": {"running": True, "logs": ["eng1"]}},
                    },
                )
                self.assertTrue(ok)
                content = wlc.get_log_content("worker_test", target="agent", tail=10)
                self.assertTrue(content["success"])
                self.assertEqual(content["lines"], ["line1", "line2"])


if __name__ == "__main__":
    unittest.main()

import array
import asyncio
import io
import json
import struct
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.cache_manager import CacheManager
from src.config import AppConfig, VoiceConfig
from src.database import Database
from src.stt.hayamimi_client import HayamimiSession
from src.stt.manager import STTManager
from src.tts.manager import TTSManager
from src.tts.protocols.worker_protocol import HeartbeatMessage
from src.tts.ws_node_adapter import WSWorkerNode
from src.utils.security import safe_json_for_html_script
from src.utils.streaming_audio import FRAME_SIZE, StreamingAudioSource, trim_trailing_silence
from src.utils.text_cleaner import TextCleaner
from src.web.server import WebServer


def _make_wav_bytes(sample_rate: int = 24000, nchannels: int = 1, sampwidth: int = 2, voice_ms: int = 120, silence_ms: int = 300) -> bytes:
    """テスト用の有音＋末尾無音WAVバイナリを生成"""
    v_frames = int(sample_rate * (voice_ms / 1000.0))
    s_frames = int(sample_rate * (silence_ms / 1000.0))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        if sampwidth == 2:
            arr = array.array("h")
            for i in range(v_frames):
                val = 6000 if (i % 20) < 10 else -6000
                for _ in range(nchannels):
                    arr.append(val)
            for _ in range(s_frames * nchannels):
                arr.append(0)
            wf.writeframes(arr.tobytes())
        elif sampwidth == 1:
            raw = bytearray()
            for i in range(v_frames):
                val = 200 if (i % 20) < 10 else 56
                for _ in range(nchannels):
                    raw.append(val)
            raw.extend([128] * (s_frames * nchannels))
            wf.writeframes(bytes(raw))
    return buf.getvalue()


class TestSecurityVerificationPlan(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        base = Path(self._tmpdir.name)
        self.cfg = AppConfig(
            db_path=str(base / "test.db"),
            cache_dir=str(base / "cache"),
            voicevox_cache_dir=str(base / "cache_vv"),
            web_session_secret="unit-test-secret-key-0123456789abcdef",
            worker_shared_secret="shared-worker-secret-xyz",
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

        # モックBotとギルド構成:
        # user 111111111 = システム最高管理者
        # user 222222222 = ギルド999のサーバー管理者だがシステム最高管理者ではない
        # user 333333333 = ギルド999の一般メンバー
        guild_999 = MagicMock()
        guild_999.id = 999
        guild_999.name = "TestGuild"

        def _make_member(uid: int, is_guild_admin: bool):
            m = MagicMock()
            m.id = uid
            m.display_name = f"User{uid}"
            m.guild_permissions = SimpleNamespace(administrator=is_guild_admin, manage_guild=is_guild_admin)
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
        self.bot.latency = 0.02
        self.bot.guilds = [guild_999]
        self.bot.get_guild = lambda gid: guild_999 if int(gid) == 999 else None

        self.tts = MagicMock()
        self.tts.voicevox_pool = SimpleNamespace(nodes=[], get_nodes=lambda: [])
        self.tts.sharevox_pool = SimpleNamespace(nodes=[], get_nodes=lambda: [])
        self.tts.coeiroink_pool = SimpleNamespace(nodes=[], get_nodes=lambda: [])
        self.tts.ws_worker_nodes = {}
        self.tts.resource_manager = None

        self.stt_mgr = STTManager(self.bot, self.db, self.cfg)
        self.server = WebServer(
            bot=self.bot,
            config=self.cfg,
            database=self.db,
            cache_manager=self.cache,
            tts_manager=self.tts,
        )
        self.test_server = TestServer(self.server.app)
        self.client = TestClient(self.test_server)
        await self.client.start_server()

    async def asyncTearDown(self):
        STTManager._instance = None
        await self.client.close()
        await self.db.close()
        self._tmpdir.cleanup()

    def _cookie_for(self, uid: int, username: str = "u", is_admin: bool = False) -> dict:
        tok = self.server.auth.create_session_token(
            {"id": uid, "username": username, "global_name": username},
            is_admin=is_admin,
        )
        return {"ttsbot_session": tok}

    async def test_1_stt_stream_wan_auth_and_vt_user_binding(self):
        # 1. WAN 経由で未認証の /api/stt/stream は 401 拒否されること
        resp = await self.client.get(
            "/api/stt/stream?user_id=0",
            headers={"CF-Connecting-IP": "203.0.113.50"},
        )
        self.assertIn(resp.status, (401, 403))

        # 2. ボランティア個人トークン (vt_) で接続した場合、?user_id=0 を指定しても本人の uid=333333333 に強制固定されること
        _, raw_vt = await self.db.create_volunteer_token(333333333, "Member333")
        self.assertTrue(raw_vt.startswith("vt_"))

        ws = await self.client.ws_connect(
            "/api/stt/stream?user_id=0",
            headers={
                "CF-Connecting-IP": "203.0.113.50",
                "Authorization": f"Bearer {raw_vt}",
            },
        )
        hello = await ws.receive_json()
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["target_user_id"], 333333333)
        await ws.close()

    async def test_2_system_admin_vs_guild_admin_separation(self):
        # ギルド管理者 (222222222) に仮に古い is_admin=True 付きセッションがあってもシステム管理APIは 403 拒否されること
        guild_admin_cookies = self._cookie_for(222222222, "GuildAdmin", is_admin=True)
        resp_forbidden = await self.client.get("/api/system/credentials", cookies=guild_admin_cookies)
        self.assertEqual(resp_forbidden.status, 403)

        # システムオーナー (111111111) は 200 OK
        sys_owner_cookies = self._cookie_for(111111111, "SysOwner", is_admin=True)
        resp_ok = await self.client.get("/api/system/credentials", cookies=sys_owner_cookies)
        self.assertEqual(resp_ok.status, 200)

        # パスワードログイン最高管理者 (uid=0) も 200 OK
        pw_admin_cookies = self._cookie_for(0, "Admin", is_admin=True)
        resp_pw = await self.client.get("/api/system/credentials", cookies=pw_admin_cookies)
        self.assertEqual(resp_pw.status, 200)

        # Hayamimi 疎通テストの SSRF 防御: ループバックURLや非wsスキームを拒否すること
        resp_ssrf = await self.client.post(
            "/api/system/credentials/test",
            json={"provider": "hayamimi", "hayamimi_ws_url": "ws://127.0.0.1:8080/health"},
            cookies=sys_owner_cookies,
        )
        self.assertEqual(resp_ssrf.status, 400)

    async def test_3_node_settings_and_ng_permissions(self):
        # 未ログインの /api/node_settings は 401 拒否
        r_unauth = await self.client.get("/api/node_settings")
        self.assertEqual(r_unauth.status, 401)

        # 一般メンバー (333333333) による NG ワード追加・削除・一括削除・プリセットは全て 403 拒否
        member_cookies = self._cookie_for(333333333, "NormalMember", is_admin=False)
        r_add = await self.client.post("/api/ng?guild_id=999", json={"word": "禁止語"}, cookies=member_cookies)
        self.assertEqual(r_add.status, 403)
        r_batch = await self.client.post("/api/ng/delete_batch?guild_id=999", json={"words": ["禁止語"]}, cookies=member_cookies)
        self.assertEqual(r_batch.status, 403)
        r_preset = await self.client.post("/api/ng/import_preset?guild_id=999", json={}, cookies=member_cookies)
        self.assertEqual(r_preset.status, 403)

        # ギルド管理者 (222222222) は自ギルドの NG ワード追加が可能 (200 OK)
        guild_admin_cookies = self._cookie_for(222222222, "GuildAdmin", is_admin=False)
        r_add_ok = await self.client.post("/api/ng?guild_id=999", json={"word": "テスト禁止語"}, cookies=guild_admin_cookies)
        self.assertEqual(r_add_ok.status, 200)

    async def test_4_wan_csrf_blocks_evil_ten_dot_domain_and_private_ip_spoof(self):
        guild_admin_cookies = self._cookie_for(222222222, "GuildAdmin", is_admin=False)
        # 以前 startswith("10.") でバイパスできた https://10.evil.com が WAN POST で 403 遮断されること
        r_evil = await self.client.post(
            "/api/ng?guild_id=999",
            json={"word": "csrf"},
            headers={
                "CF-Connecting-IP": "198.51.100.20",
                "Origin": "https://10.evil.com",
            },
            cookies=guild_admin_cookies,
        )
        self.assertEqual(r_evil.status, 403)

        # 正規 Origin (https://tts.example.com) は通過すること
        r_legit = await self.client.post(
            "/api/ng?guild_id=999",
            json={"word": "legit_ng"},
            headers={
                "CF-Connecting-IP": "198.51.100.20",
                "Origin": "https://tts.example.com",
            },
            cookies=guild_admin_cookies,
        )
        self.assertEqual(r_legit.status, 200)

    async def test_5_safe_json_for_html_script_prevents_xss(self):
        # worker_shared_secret に </script> やクォートが含まれていても server-data JSON が破損・XSS脱出しないこと
        import re as _re
        evil_secret = 'tok"</script><script>alert(1)</script>&x'
        self.cfg.worker_shared_secret = evil_secret
        sys_owner_cookies = self._cookie_for(111111111, "SysOwner", is_admin=True)
        resp = await self.client.get("/", cookies=sys_owner_cookies)
        self.assertEqual(resp.status, 200)
        body = await resp.text()
        m = _re.search(r'<script id="server-data" type="application/json">(.*?)</script>', body, _re.DOTALL)
        self.assertIsNotNone(m)
        raw_script_json = m.group(1)
        self.assertNotIn("</script>", raw_script_json.lower())
        parsed = json.loads(raw_script_json)
        self.assertEqual(parsed["worker_token"], evil_secret)
        self.assertTrue(parsed["user_data"]["is_admin"])

        # 非システム管理者 (ギルド管理者) には worker_token が一切露出しないこと
        guild_admin_cookies = self._cookie_for(222222222, "GuildAdmin", is_admin=True)
        resp_g = await self.client.get("/", cookies=guild_admin_cookies)
        body_g = await resp_g.text()
        m_g = _re.search(r'<script id="server-data" type="application/json">(.*?)</script>', body_g, _re.DOTALL)
        parsed_g = json.loads(m_g.group(1))
        self.assertEqual(parsed_g["worker_token"], "")
        self.assertFalse(parsed_g["user_data"]["is_admin"])


class TestAudioAndSpeculativePipeline(unittest.IsolatedAsyncioTestCase):
    def test_streaming_audio_formats_and_thread_safety(self):
        src = StreamingAudioSource()
        # 24kHz Mono (高速パス), 16kHz Mono, 44100Hz Stereo, 8-bit WAV すべて正常投入できること
        d1 = src.feed_wav_bytes(_make_wav_bytes(24000, 1, 2, voice_ms=100, silence_ms=300), pause_after_ms=40)
        d2 = src.feed_wav_bytes(_make_wav_bytes(16000, 1, 2, voice_ms=100, silence_ms=250))
        d3 = src.feed_wav_bytes(_make_wav_bytes(44100, 2, 2, voice_ms=80, silence_ms=0))
        d4 = src.feed_wav_bytes(_make_wav_bytes(24000, 1, 1, voice_ms=80, silence_ms=0))
        self.assertGreater(d1, 0.1)
        # 300ms の無音がトリムされているため 440ms より十分短いこと
        self.assertLess(d1, 0.25)
        self.assertGreater(d2, 0.08)
        self.assertGreater(d3, 0.05)
        self.assertGreater(d4, 0.05)

        # 奇数長バイト列でも trim_trailing_silence が例外を出さないこと
        odd_pcm = (b"\x10\x00\x10\x00" * 1200) + b"\x00"
        trimmed = trim_trailing_silence(odd_pcm)
        self.assertEqual(len(trimmed) % 4, 0)

        # 別スレッド read() と並行 feed_wav_bytes() 競合ストレステスト
        errors = []
        wav_sample = _make_wav_bytes(24000, 1, 2, voice_ms=60, silence_ms=40)

        def _reader():
            try:
                while True:
                    frame = src.read()
                    if frame == b"":
                        break
                    if len(frame) != FRAME_SIZE:
                        errors.append(f"Invalid frame size: {len(frame)}")
                        break
                    time.sleep(0.0005)
            except Exception as ex:
                errors.append(str(ex))

        t = threading.Thread(target=_reader)
        t.start()
        for _ in range(30):
            src.feed_wav_bytes(wav_sample, pause_after_ms=10)
            time.sleep(0.001)
        src.finish()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])

    async def test_early_hedge_and_late_commit_preserves_local_voice_when_in_grace(self):
        valid_local_wav = _make_wav_bytes(24000, 1, 2, voice_ms=60, silence_ms=0)
        valid_cloud_wav = _make_wav_bytes(24000, 1, 2, voice_ms=50, silence_ms=0)

        def _build_mgr(local_delay: float, cloud_delay: float):
            m = TTSManager.__new__(TTSManager)
            m.config = SimpleNamespace(chunk_singleflight="off", speculative_timeout_seconds=2.5, voicevox_default_fallback_style_id=3)
            m.db = None
            m._inflight_local = {}
            m._inflight_join_info = {}
            m.singleflight_shadow_hits = 0
            m._recent_chunk_meta = {}
            m.last_chunk_meta = {}
            voice = VoiceConfig(id="ci_test", name="テスト話者", gender="female", description="", provider="coeiroink", voice_name="uuid_0")
            m._get_voice_config = lambda spk: voice
            m._get_worker_label_for_provider = lambda prov: prov
            m._local_pool_for = lambda prov: None

            async def _local(*a, **kw):
                await asyncio.sleep(local_delay)
                return valid_local_wav

            async def _cloud(**kw):
                await asyncio.sleep(cloud_delay)
                return valid_cloud_wav

            m._try_local_engine = _local
            m.coeiroink_client = m.coeiroink_pool = None
            unavail = SimpleNamespace(is_available=lambda: False)
            m.azure_client = SimpleNamespace(is_available=lambda: True, synthesize=_cloud)
            m.google_client = m.edge_client = m.openjtalk_client = unavail
            return m

        # ケース1: 50msで先行投機発射し、クラウドが+30ms(80ms時点)で先に到着するが、
        # 再生枯渇(450ms時点)より前の 180ms 時点でローカルが完了 -> ローカル声質が採用されること！
        t0 = time.time()
        m1 = _build_mgr(local_delay=0.18, cloud_delay=0.03)
        audio1, prov1, _ = await m1._synthesize_single_chunk(
            "テスト", "ci_test", "0%", 1.0,
            speculative_deadline_fn=lambda: t0 + 0.05,
            depletion_deadline_fn=lambda: t0 + 0.45,
        )
        self.assertEqual(prov1, "coeiroink")
        self.assertEqual(audio1, valid_local_wav)

        # ケース2: ローカルが 1.0s かかり再生枯渇(220ms時点)に間に合わない -> 到着済みのクラウドが枯渇前に採用されること！
        t1 = time.time()
        m2 = _build_mgr(local_delay=1.0, cloud_delay=0.03)
        audio2, prov2, _ = await m2._synthesize_single_chunk(
            "テスト", "ci_test", "0%", 1.0,
            speculative_deadline_fn=lambda: t1 + 0.05,
            depletion_deadline_fn=lambda: t1 + 0.22,
        )
        elapsed2 = time.time() - t1
        self.assertEqual(prov2, "azure")
        self.assertEqual(audio2, valid_cloud_wav)
        self.assertLess(elapsed2, 0.30)


class TestTextCleanerAndDatabaseFixes(unittest.IsolatedAsyncioTestCase):
    def test_cache_key_distinct_mora_greetings_and_dictionary_protection(self):
        # 異モーラ語は別キーになり、同音のかな漢字ゆれは同一キーになること
        self.assertNotEqual(
            TextCleaner.normalize_cache_key_text("おつ"),
            TextCleaner.normalize_cache_key_text("お疲れ"),
        )
        self.assertNotEqual(
            TextCleaner.normalize_cache_key_text("よろしくです"),
            TextCleaner.normalize_cache_key_text("よろしく"),
        )
        self.assertEqual(
            TextCleaner.normalize_cache_key_text("おつかれ！"),
            TextCleaner.normalize_cache_key_text("お疲れ"),
        )

        # ギルド辞書の最長一致・アンダースコア境界・共通辞書からの上書き保護
        res = TextCleaner.apply_dictionary(
            "foo_in_bar と in と w と PK",
            [("in", "インカスタム"), ("PK", "ピーケー専用")],
        )
        self.assertIn("foo_in_bar", res)
        self.assertIn("インカスタム", res)
        self.assertIn("ピーケー専用", res)

        # 文字数制限が <<__BEEP:N__>> タグの途中にかかってもタグが破壊されないこと
        cleaner = TextCleaner(max_length=10)
        cleaned = cleaner.clean("あいうえお禁止ワードかきくけこさしすせそ", ng_words=["禁止ワード"], max_length=12)
        self.assertNotIn("<<__BEE", cleaned.replace("<<__BEEP:5__>>", ""))
        self.assertIn("<<__BEEP:5__>>", cleaned)

        # 先頭が記号のみの文でも空音声チャンクにならず次チャンクへ吸着し、全文記号のみなら空リストになること
        chunks_sym_head = TextCleaner.split_into_chunks("！？。こんにちは")
        self.assertEqual(len(chunks_sym_head), 1)
        self.assertIn("こんにちは", chunks_sym_head[0].text)
        chunks_only_sym = TextCleaner.split_into_chunks("・・・！？")
        self.assertEqual(chunks_only_sym, [])

    async def test_guild_dictionary_delete_preserves_global_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(str(Path(tmp) / "dict.db"))
            await db.init_db()
            try:
                await db.add_dictionary_word(0, "共通語", "きょうつうご")
                await db.add_dictionary_word(12345, "共通語", "ぎるどご")
                await db.delete_dictionary_word(12345, "共通語")
                rem_global = await db.get_dictionary_words(0)
                self.assertEqual(rem_global, [("共通語", "きょうつうご")])
            finally:
                await db.close()


class TestWSWorkerAndHayamimiFixes(unittest.IsolatedAsyncioTestCase):
    async def test_ws_worker_cooldown_chunk_reassembly_and_cancel_on_abort(self):
        fake_ws = MagicMock()
        fake_ws.closed = False
        sent_payloads = []

        async def _send_json(obj):
            sent_payloads.append(obj)

        fake_ws.send_json = _send_json
        fake_ws.close = AsyncMock()

        node = WSWorkerNode(
            node_id="vol_1",
            node_name="TestPC",
            ws=fake_ws,
            supported_engines=["voicevox", "coeiroink"],
            capacity=2,
        )

        # 1. 失敗クールダウン中にハートビートを受信しても隔離が早期解除されないこと
        node.mark_failed(cooldown_seconds=30.0, reason="timeout")
        self.assertFalse(node.is_available)
        hb = HeartbeatMessage(type="heartbeat", node_id="vol_1", cpu_percent=10.0, memory_percent=20.0, active_tasks=0, uptime_sec=100)
        node.update_from_heartbeat(hb)
        self.assertFalse(node.is_available)

        # クールダウン満了後はハートビートで復帰すること
        node.unhealthy_until = time.time() - 1.0
        node.update_from_heartbeat(hb)
        self.assertTrue(node.is_available)

        # 2. 分割バイナリチャンク (is_final=False -> True) が正しく結合されること
        synth_task = asyncio.create_task(node.synthesize("voicevox", "あ", "3", timeout_seconds=2.0))
        await asyncio.sleep(0.02)
        req_id = sent_payloads[-1]["req_id"]
        node.on_binary_chunk(req_id, 0, False, b"RIFF_PART1_")
        self.assertFalse(synth_task.done())
        node.on_binary_chunk(req_id, 1, True, b"PART2")
        res = await synth_task
        self.assertEqual(res, b"RIFF_PART1_PART2")
        # record_metrics=False (既定) のため total_requests が二重加算されないこと
        self.assertEqual(node.total_requests, 0)

        # 3. CancelledError 時に Worker へ synthesize_cancel が送信されること
        cancel_task = asyncio.create_task(node.synthesize("voicevox", "長文", "3", timeout_seconds=5.0))
        await asyncio.sleep(0.02)
        cancel_req_id = sent_payloads[-1]["req_id"]
        cancel_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancel_task
        self.assertEqual(sent_payloads[-1], {"type": "synthesize_cancel", "req_id": cancel_req_id})

    async def test_hayamimi_close_from_recv_task_does_not_leak_session(self):
        sess = HayamimiSession(user_id=1, user_name="u", gateway_url="ws://192.168.1.10:8833/ws/stream", token="sec")
        headers = sess.build_headers()
        self.assertEqual(headers.get("Authorization"), "Bearer sec")

        session_closed = False

        class _FakeClientSession:
            closed = False

            async def close(self):
                nonlocal session_closed
                await asyncio.sleep(0.01)
                self.closed = True
                session_closed = True

        class _FakeWS:
            closed = False

            async def close(self):
                await asyncio.sleep(0.01)
                self.closed = True

        sess._session = _FakeClientSession()
        sess._ws = _FakeWS()
        sess.is_connected = True

        async def _self_closing_recv():
            await sess.close()

        sess._recv_task = asyncio.create_task(_self_closing_recv())
        await sess._recv_task
        self.assertTrue(session_closed)
        self.assertIsNone(sess._session)


if __name__ == "__main__":
    unittest.main()

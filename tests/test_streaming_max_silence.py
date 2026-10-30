import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.config import TTSNodeConfig, VoiceConfig, normalize_rollout_mode
from src.database import clamp_max_silence
from src.tts.manager import TTSManager
from src.tts.node_pool import TTSNode, TTSNodePool
from src.utils.stream_planner import plan_head_chars
from src.utils.text_cleaner import TextChunk, TextCleaner
from src.cogs.vc_handler import PlaybackTimeline, QueueItem

WAV = b"RIFF" + b"\x00" * 100
WAV_CLOUD = b"RIFF" + b"\x01" * 100


def _node(est_base: float, est_per_char: float, pending_ms: float = 0.0):
    return {"pending_ms": pending_ms, "est_ms": (lambda L: est_base + est_per_char * L)}


# 実測回帰 (scratch/exp_measure.jsonl): 3090 / OMEN2017 / Debian CPU / Ubuntu CPU
SOMEN = _node(128, 3.0)
OMEN = _node(110, 5.0)
DEBIAN = _node(78, 55.4)
UBUNTU = _node(54, 63.8)
INCIDENT_LENS = [30, 30, 30, 30, 16]


class TestStreamPlanner(unittest.TestCase):
    def test_gpu_available_keeps_full_head(self):
        self.assertIsNone(plan_head_chars(INCIDENT_LENS, True, 0.6, [SOMEN, OMEN, DEBIAN, UBUNTU], 1.5, 0.45))
        self.assertIsNone(plan_head_chars(INCIDENT_LENS, False, 0.0, [SOMEN, OMEN, DEBIAN, UBUNTU], 1.5, 0.45))

    def test_cpu_only_shortens_head_within_limit(self):
        with_name = plan_head_chars(INCIDENT_LENS, True, 0.6, [DEBIAN, UBUNTU], 1.5, 0.45)
        no_name = plan_head_chars(INCIDENT_LENS, False, 0.0, [DEBIAN, UBUNTU], 1.5, 0.45)
        self.assertIsNotNone(with_name)
        self.assertIsNotNone(no_name)
        self.assertTrue(10 <= no_name < 30)
        # 名前の再生中に合成できる分、名前ありの方が長く取れる
        self.assertGreater(with_name, no_name)

    def test_no_nodes_or_short_head(self):
        self.assertIsNone(plan_head_chars(INCIDENT_LENS, True, 0.6, [], 1.5, 0.45))
        self.assertIsNone(plan_head_chars([8, 30], True, 0.6, [DEBIAN], 1.5, 0.45))


class TestHeadChunkLimit(unittest.TestCase):
    def test_only_head_is_shortened(self):
        chunks = [
            TextChunk(text="かつさん、", pause_after_ms=80, is_name_prefix=True),
            TextChunk(text="あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほ", pause_after_ms=60),
            TextChunk(text="まみむめもやゆよらりるれろわをん", pause_after_ms=300),
        ]
        out = TextCleaner.apply_head_chunk_limit(chunks, 12)
        self.assertEqual(len(out), 4)
        self.assertTrue(out[0].is_name_prefix)
        self.assertLessEqual(len(out[1].text), 12)
        self.assertEqual(out[1].pause_after_ms, 0)
        self.assertEqual(out[1].text + out[2].text, chunks[1].text)
        self.assertEqual(out[2].pause_after_ms, 60)
        self.assertEqual(out[3].text, chunks[2].text)

    def test_short_head_unchanged(self):
        chunks = [TextChunk(text="こんにちは", pause_after_ms=300)]
        self.assertIs(TextCleaner.apply_head_chunk_limit(chunks, 12), chunks)


class TestPlaybackTimeline(unittest.TestCase):
    def test_deadlines_before_and_after_play(self):
        src = SimpleNamespace(buffer_remaining_seconds=0.0)
        tl = PlaybackTimeline(t_turn=100.0, max_silence=1.5, az_margin=0.45, est_play=[0.6, 3.0, 3.0], stream_source=src)
        # 名前なしの先頭: 順番が来てから 1.5s - マージン
        self.assertAlmostEqual(tl.deadline_for(0), 100.0 + 1.5 - 0.45)
        # 名前 (0.8s) 投入後、再生前の第1本文: 名前の再生終了 - マージン
        tl.mark_fed(0, 0.8)
        self.assertAlmostEqual(tl.deadline_for(1), 100.0 + 1.5 + 0.8 - 0.45)
        # 未投入の第1本文を挟む第2本文は、その見積り分だけ後ろ
        self.assertAlmostEqual(tl.deadline_for(2), 100.0 + 1.5 + 0.8 + 3.0 - 0.45)
        # 再生開始後はバッファ残量基準
        tl.t_play = time.time()
        src.buffer_remaining_seconds = 2.0
        self.assertAlmostEqual(tl.deadline_for(1), time.time() + 2.0 - 0.45, delta=0.05)


class TestEarliestFinishRouting(unittest.TestCase):
    def _pool(self):
        gpu = TTSNodeConfig(id="worker_gpu_coeiroink", url="http://10.0.0.1:50031", priority=1, max_concurrent=1)
        cpu = TTSNodeConfig(id="worker_cpu_coeiroink", url="http://10.0.0.2:50031", priority=2, max_concurrent=2)
        pool = TTSNodePool("coeiroink", [gpu, cpu])
        g, c = pool.nodes
        est = {g.id: (lambda L: 100 + 5 * L), c.id: (lambda L: 60 + 60 * L)}
        pool.estimate_node_ms = lambda n, L: est[n.id](L)
        return pool, g, c

    def test_waits_for_busy_gpu_when_faster(self):
        async def run():
            pool, g, c = self._pool()
            with patch.object(TTSNode, "is_available", new=property(lambda self: True)), \
                 patch.object(TTSNode, "supports_model", lambda self, e, m: True):
                busy = await pool.acquire_node(timeout=2.0, text_len=30)  # GPU の唯一の枠を占有
                self.assertIs(busy.node, g)
                self.assertGreater(g.pending_est_ms, 0)
                async with busy:
                    t0 = time.time()
                    waiter = asyncio.create_task(pool.acquire_node(timeout=2.0, text_len=30, prefer_earliest_finish=True))
                    await asyncio.sleep(0.12)
                    self.assertFalse(waiter.done())  # 空いている CPU に流れず GPU を待つ
                lease = await waiter
                self.assertIs(lease.node, g)
                self.assertGreater(time.time() - t0, 0.1)
                async with lease:
                    pass
                self.assertAlmostEqual(g.pending_est_ms, 0.0)
                # 従来の空き枠優先なら CPU
                busy2 = await pool.acquire_node(timeout=2.0, text_len=30)
                async with busy2:
                    other = await pool.acquire_node(timeout=2.0, text_len=30)
                    self.assertIs(other.node, c)
                    async with other:
                        pass
        asyncio.run(run())

    def test_cancelled_lease_keeps_pending_until_estimated_end(self):
        async def run():
            pool, g, _ = self._pool()
            with patch.object(TTSNode, "is_available", new=property(lambda self: True)), \
                 patch.object(TTSNode, "supports_model", lambda self, e, m: True):
                lease = await pool.acquire_node(timeout=2.0, text_len=30)
                await lease.__aenter__()
                await lease.__aexit__(asyncio.CancelledError, asyncio.CancelledError(), None)
                self.assertGreater(g.pending_est_ms, 0)  # エンジンは計算を続ける前提で残す
                await asyncio.sleep(0.3)
                self.assertAlmostEqual(g.pending_est_ms, 0.0)
        asyncio.run(run())


def _make_manager(singleflight: str, local_delay: float, cloud_delay: float = 0.03):
    m = TTSManager.__new__(TTSManager)
    m.config = SimpleNamespace(chunk_singleflight=singleflight, speculative_timeout_seconds=2.5, voicevox_default_fallback_style_id=3)
    m.db = None
    m._inflight_local = {}
    m._inflight_join_info = {}
    m.singleflight_shadow_hits = 0
    m._recent_chunk_meta = {}
    m.last_chunk_meta = {}
    m.local_calls = 0
    voice = VoiceConfig(id="ci", name="金苗", gender="female", description="", provider="coeiroink", voice_name="uuid_0")
    m._get_voice_config = lambda speaker_id: voice
    m._get_worker_label_for_provider = lambda prov: prov

    async def fake_local(engine_name, client, pool, text, style_val, pitch, speed, is_longest_chunk=False, prefer_earliest_finish=False):
        m.local_calls += 1
        await asyncio.sleep(local_delay)
        return WAV

    async def fake_cloud(**kwargs):
        await asyncio.sleep(cloud_delay)
        return WAV_CLOUD

    m._try_local_engine = fake_local
    m.coeiroink_client = m.coeiroink_pool = None
    unavailable = SimpleNamespace(is_available=lambda: False)
    m.azure_client = SimpleNamespace(is_available=lambda: True, synthesize=fake_cloud)
    m.google_client = m.edge_client = m.openjtalk_client = unavailable
    return m


class TestSpeculativeDeadlineAndSingleflight(unittest.TestCase):
    def test_deadline_fn_waits_for_local_beyond_legacy_timeout(self):
        async def run():
            m = _make_manager("off", local_delay=0.4)
            t0 = time.time()
            audio, prov, _ = await m._synthesize_single_chunk(
                "テスト", "ci", "0", 1.0, speculative_timeout=0.1,
                speculative_deadline_fn=lambda: t0 + 1.0,
            )
            self.assertEqual(prov, "coeiroink")
            self.assertEqual(audio, WAV)
        asyncio.run(run())

    def test_deadline_fn_fires_cloud_when_local_too_slow(self):
        async def run():
            m = _make_manager("off", local_delay=1.0)
            t0 = time.time()
            audio, prov, _ = await m._synthesize_single_chunk(
                "テスト", "ci", "0", 1.0, speculative_deadline_fn=lambda: t0 + 0.15,
            )
            self.assertEqual(prov, "azure")
            self.assertLess(time.time() - t0, 0.5)
        asyncio.run(run())

    def test_deadline_extension_is_reevaluated(self):
        async def run():
            m = _make_manager("off", local_delay=0.5)
            t0 = time.time()
            deadline = {"t": t0 + 0.2}

            async def extend():
                await asyncio.sleep(0.1)
                deadline["t"] = t0 + 2.0  # 先行パーツの投入で再生予定が延びた
            asyncio.create_task(extend())
            _, prov, _ = await m._synthesize_single_chunk("テスト", "ci", "0", 1.0, speculative_deadline_fn=lambda: deadline["t"])
            self.assertEqual(prov, "coeiroink")
        asyncio.run(run())

    def test_singleflight_joins_inflight_local_synthesis(self):
        async def run():
            m = _make_manager("on", local_delay=0.3)
            prefetch = asyncio.create_task(m._synthesize_single_chunk("同じ文", "ci", "0", 1.0, local_only=True))
            await asyncio.sleep(0.05)
            playback = await m._synthesize_single_chunk("同じ文", "ci", "0", 1.0, speculative_deadline_fn=lambda: time.time() + 5)
            await prefetch
            self.assertEqual(m.local_calls, 1)
            self.assertEqual(playback[1], "coeiroink")
            self.assertEqual(m._inflight_local, {})
        asyncio.run(run())

    def test_cloud_win_does_not_cancel_shared_local_task(self):
        async def run():
            m = _make_manager("on", local_delay=0.4)
            prefetch = asyncio.create_task(m._synthesize_single_chunk("同じ文", "ci", "0", 1.0, local_only=True))
            await asyncio.sleep(0.05)
            t0 = time.time()
            _, prov, _ = await m._synthesize_single_chunk("同じ文", "ci", "0", 1.0, speculative_deadline_fn=lambda: t0 + 0.05)
            self.assertEqual(prov, "azure")
            audio, prov2, _ = await prefetch  # 先行合成側はローカル完了まで継続しキャッシュを温める
            self.assertEqual(prov2, "coeiroink")
            self.assertEqual(m.local_calls, 1)
        asyncio.run(run())

    def test_singleflight_off_and_shadow_do_not_join(self):
        async def run():
            for mode in ("off", "shadow"):
                m = _make_manager(mode, local_delay=0.1)
                await asyncio.gather(
                    m._synthesize_single_chunk("同じ文", "ci", "0", 1.0, speculative_timeout=1.0),
                    m._synthesize_single_chunk("同じ文", "ci", "0", 1.0, speculative_timeout=1.0),
                )
                self.assertEqual(m.local_calls, 2, mode)
                if mode == "shadow":
                    self.assertEqual(m.singleflight_shadow_hits, 1)
        asyncio.run(run())

    def test_local_only_failure_raises(self):
        async def run():
            m = _make_manager("on", local_delay=0.01)

            async def failing_local(*a, **k):
                return None
            m._try_local_engine = failing_local
            with self.assertRaises(Exception):
                await m._synthesize_single_chunk("文", "ci", "0", 1.0, local_only=True)
        asyncio.run(run())


class TestSettingsAndFlags(unittest.TestCase):
    def test_clamp_max_silence(self):
        self.assertEqual(clamp_max_silence(None), 1.5)
        self.assertEqual(clamp_max_silence("abc"), 1.5)
        self.assertEqual(clamp_max_silence(4.0), 2.5)
        self.assertEqual(clamp_max_silence(0.5), 1.0)
        self.assertEqual(clamp_max_silence(1.8), 1.8)

    def test_rollout_mode(self):
        self.assertEqual(normalize_rollout_mode("SHADOW"), "shadow")
        self.assertEqual(normalize_rollout_mode("true"), "on")
        self.assertEqual(normalize_rollout_mode("weird"), "off")

    def test_queue_item_chunks_not_persisted(self):
        item = QueueItem(text="a", user_id=1, user_name="u", speaker_id="ci", pitch="0", speed=1.0,
                         chunks=[TextChunk(text="a")])
        d = item.to_dict()
        self.assertNotIn("chunks", d)
        self.assertIsNone(QueueItem.from_dict(dict(d, chunks=["x"])).chunks)


if __name__ == "__main__":
    unittest.main()

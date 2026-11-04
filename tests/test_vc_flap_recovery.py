import asyncio
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.cogs import vc_handler
from src.cogs.vc_handler import GuildVCState, VCHandlerCog, _VoiceCloseDiagFilter


def _make_cog() -> VCHandlerCog:
    """__init__ の重い依存を避け、切断ループ検知に必要な最小状態だけ持つ Cog を作る"""
    cog = VCHandlerCog.__new__(VCHandlerCog)
    cog.bot_index = 1
    cog.guild_states = {}
    cog.bot_pool = None
    cog._pending_bot_leave_tasks = {}
    return cog


class TestFlapDetection(unittest.TestCase):
    def setUp(self):
        self.cog = _make_cog()
        self.state = GuildVCState(1)

    def test_three_disconnects_within_window_is_flapping(self):
        t = 1000.0
        self.state.last_connected_at = t - 5
        counts = [self.cog._record_voice_flap(self.state, t + i * 20) for i in range(3)]
        self.assertEqual(counts, [1, 2, 3])

    def test_events_within_debounce_collapse_into_one(self):
        self.state.last_connected_at = 995.0
        self.cog._record_voice_flap(self.state, 1000.0)
        self.assertEqual(self.cog._record_voice_flap(self.state, 1001.0), 1)

    def test_old_disconnects_leave_the_window(self):
        self.state.last_connected_at = 995.0
        self.cog._record_voice_flap(self.state, 1000.0)
        self.cog._record_voice_flap(self.state, 1020.0)
        # 直近の再接続から60秒未満 (=未安定) のまま、61秒以上前の最初の切断だけが集計から外れる
        self.state.last_connected_at = 1050.0
        self.assertEqual(self.cog._record_voice_flap(self.state, 1070.0), 2)

    def test_stable_connection_resets_escalation(self):
        self.state.flap_level = 2
        self.state.flap_times.extend([900.0, 920.0])
        self.state.flap_gave_up = True
        self.state.last_connected_at = 1000.0
        # 60秒以上安定していた後の切断は新規障害 (段階は持ち越さない)
        count = self.cog._record_voice_flap(self.state, 1100.0)
        self.assertEqual(count, 1)
        self.assertEqual(self.state.flap_level, 0)
        self.assertFalse(self.state.flap_gave_up)

    def test_early_disconnect_after_backoff_keeps_level(self):
        self.state.flap_level = 1
        self.state.last_connected_at = 1000.0
        self.cog._record_voice_flap(self.state, 1020.0)
        self.assertEqual(self.state.flap_level, 1)


class TestSuppression(unittest.TestCase):
    def test_suppressed_until_expiry_then_resumes(self):
        cog = _make_cog()
        state = GuildVCState(5)
        cog.guild_states[5] = state
        with patch.object(vc_handler.time, "time", return_value=1000.0):
            state.flap_suppressed_until = 1100.0
            self.assertTrue(cog._is_vc_reconnect_suppressed(5))
        with patch.object(vc_handler.time, "time", return_value=1101.0):
            self.assertFalse(cog._is_vc_reconnect_suppressed(5))

    def test_giveup_expiry_resets_state(self):
        cog = _make_cog()
        state = GuildVCState(5)
        cog.guild_states[5] = state
        state.flap_gave_up = True
        state.flap_level = 3
        state.flap_times.extend([1.0, 2.0])
        state.flap_suppressed_until = 1100.0
        with patch.object(vc_handler.time, "time", return_value=1200.0):
            self.assertFalse(cog._is_vc_reconnect_suppressed(5))
        self.assertEqual(state.flap_level, 0)
        self.assertFalse(state.flap_gave_up)
        self.assertEqual(len(state.flap_times), 0)

    def test_unknown_guild_is_not_suppressed(self):
        self.assertFalse(_make_cog()._is_vc_reconnect_suppressed(999))


class TestGiveUp(unittest.IsolatedAsyncioTestCase):
    async def test_give_up_disconnects_suppresses_and_alerts(self):
        cog = _make_cog()
        cog.disconnect_vc = AsyncMock()
        state = GuildVCState(7)
        guild = SimpleNamespace(id=7, name="G")
        ch = SimpleNamespace(id=70, name="VC")
        with patch.object(vc_handler.alert_service, "notify") as notify:
            await cog._give_up_voice_flapping(guild, ch, state)
        cog.disconnect_vc.assert_awaited_once_with(guild)
        self.assertTrue(state.flap_gave_up)
        self.assertGreater(state.flap_suppressed_until, 0)
        notify.assert_called_once()

    async def test_flapping_at_max_level_gives_up(self):
        cog = _make_cog()
        cog.guild_states[7] = state = GuildVCState(7)
        cog.get_guild_state = lambda gid: state
        cog._give_up_voice_flapping = AsyncMock()
        state.flap_level = len(cog.FLAP_BACKOFF_SECONDS)
        guild = SimpleNamespace(id=7, name="G", voice_client=None)
        await cog._handle_voice_flapping(guild, SimpleNamespace(id=70, name="VC"))
        cog._give_up_voice_flapping.assert_awaited_once()

    async def test_backoff_waits_then_reconnects(self):
        cog = _make_cog()
        cog.guild_states[7] = state = GuildVCState(7)
        cog.get_guild_state = lambda gid: state
        vc = MagicMock()
        vc.disconnect = AsyncMock()
        vc.is_connected.return_value = True
        guild = SimpleNamespace(id=7, name="G", voice_client=vc)
        import discord
        vch = MagicMock(spec=discord.VoiceChannel)
        vch.id, vch.name = 70, "VC"
        text = MagicMock(spec=discord.TextChannel)
        text.id = 71
        guild.get_channel = lambda cid: vch if cid == 70 else text
        state.bound_text_channel_id = 71
        cog._count_humans = lambda ch: 3
        cog.connect_vc = AsyncMock()
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        with patch.object(vc_handler.asyncio, "sleep", fake_sleep):
            await cog._handle_voice_flapping(guild, vch)

        self.assertEqual(sleeps[0], cog.FLAP_BACKOFF_SECONDS[0])
        vc.disconnect.assert_awaited_once_with(force=True)
        cog.connect_vc.assert_awaited_once()
        self.assertEqual(state.flap_suppressed_until, 0.0)
        # 安定確認後に段階がリセットされる
        self.assertEqual(state.flap_level, 0)


class TestCloseDiagFilter(unittest.TestCase):
    def test_records_close_code_from_active_exception(self):
        class FakeClosed(Exception):
            code = 4014
            reason = "disconnected"

        class FakeConn:
            def __init__(self):
                self.voice_client = SimpleNamespace(guild=SimpleNamespace(id=42), channel=SimpleNamespace(id=9))

            def emit(self, flt):
                rec = logging.LogRecord("discord.voice_state", logging.INFO, "", 0,
                                        "Disconnected from voice by force... potentially reconnecting.", None, None)
                try:
                    raise FakeClosed()
                except FakeClosed:
                    flt.filter(rec)

        vc_handler._LAST_VOICE_CLOSE.clear()
        FakeConn().emit(_VoiceCloseDiagFilter())
        self.assertEqual(vc_handler._LAST_VOICE_CLOSE[42]["code"], 4014)

    def test_ignores_unrelated_messages_and_never_raises(self):
        rec = logging.LogRecord("discord.voice_state", logging.INFO, "", 0, "Connecting to voice...", None, None)
        self.assertTrue(_VoiceCloseDiagFilter().filter(rec))


if __name__ == "__main__":
    unittest.main()

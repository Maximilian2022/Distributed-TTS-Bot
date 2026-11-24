import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.cogs import vc_handler
from src.cogs.vc_handler import GuildVCState, VCHandlerCog
from src.resource_manager import LocalEngineResourceManager


def _make_cog() -> VCHandlerCog:
    cog = VCHandlerCog.__new__(VCHandlerCog)
    cog.bot_index = 1
    cog.guild_states = {}
    cog.bot_pool = None
    cog._pending_bot_leave_tasks = {}
    cog.bot = MagicMock()
    cog.bot.user = SimpleNamespace(id=9999, bot=True)
    cog.db = MagicMock()
    cog.db.remove_active_vc = AsyncMock()
    cog.sync_nickname = AsyncMock()
    return cog


class TestStaleVCChannelAndGhostGuard(unittest.IsolatedAsyncioTestCase):
    def test_resolve_live_channel_replaces_stale_vc_channel(self):
        """Gateway再接続後の古い vc.channel (人間残存) を最新 Guild の空チャンネルへ自動修復すること"""
        cog = _make_cog()
        stale_human = SimpleNamespace(id=101, bot=False)
        bot_member = SimpleNamespace(id=9999, bot=True)

        ch_old = SimpleNamespace(id=500, name="喫煙所", members=[bot_member, stale_human])
        ch_new = SimpleNamespace(id=500, name="喫煙所", members=[bot_member])

        vc = SimpleNamespace(channel=ch_old, is_connected=lambda: True)
        guild_new = SimpleNamespace(
            id=1,
            name="TestGuild",
            voice_client=vc,
            get_channel=lambda cid: ch_new if cid == 500 else None,
        )
        cog.bot.get_guild = lambda gid: guild_new if gid == 1 else None

        resolved = cog._resolve_live_channel(guild_new, vc.channel, vc=vc)
        self.assertIs(resolved, ch_new)
        self.assertIs(vc.channel, ch_new)
        self.assertEqual(cog._count_humans(resolved), 0)

    async def test_on_voice_state_update_disconnects_when_vc_channel_was_stale(self):
        """vc.channel が古いインスタンスで人間が残って見えても、最新キャッシュが0人なら即時自動退出すること"""
        cog = _make_cog()
        cog.disconnect_vc = AsyncMock()

        stale_human = SimpleNamespace(id=101, bot=False)
        bot_member = SimpleNamespace(id=9999, bot=True)
        ch_old = SimpleNamespace(id=500, name="喫煙所", members=[bot_member, stale_human])
        ch_new = SimpleNamespace(id=500, name="喫煙所", members=[bot_member])

        vc = SimpleNamespace(channel=ch_old, is_connected=lambda: True)
        guild_new = SimpleNamespace(
            id=1,
            name="TestGuild",
            voice_client=vc,
            get_channel=lambda cid: ch_new if cid == 500 else None,
        )
        cog.bot.get_guild = lambda gid: guild_new if gid == 1 else None

        leaving_member = SimpleNamespace(id=101, bot=False, guild=guild_new)
        before = SimpleNamespace(channel=ch_new)
        after = SimpleNamespace(channel=None)

        with patch.object(vc_handler, "cache_member_info_and_avatar", new=AsyncMock()):
            await cog.on_voice_state_update(leaving_member, before, after)

        cog.disconnect_vc.assert_awaited_once_with(guild_new)
        self.assertIs(vc.channel, ch_new)

    async def test_watchdog_disconnects_stale_vc_after_grace_period(self):
        """Watchdog が古い vc.channel に惑わされず最新キャッシュ0人を検知して猶予秒数後に退出すること"""
        cog = _make_cog()
        cog.disconnect_vc = AsyncMock()
        state = GuildVCState(1)
        cog.guild_states[1] = state

        stale_human = SimpleNamespace(id=101, bot=False)
        bot_member = SimpleNamespace(id=9999, bot=True)
        ch_old = SimpleNamespace(id=500, name="喫煙所", members=[bot_member, stale_human])
        ch_new = SimpleNamespace(id=500, name="喫煙所", members=[bot_member])

        vc = SimpleNamespace(channel=ch_old, is_connected=lambda: True)
        guild_new = SimpleNamespace(
            id=1,
            name="TestGuild",
            voice_client=vc,
            me=SimpleNamespace(voice=SimpleNamespace(channel=ch_new)),
            get_channel=lambda cid: ch_new if cid == 500 else None,
        )
        cog.bot.guilds = [guild_new]
        cog.bot.get_guild = lambda gid: guild_new if gid == 1 else None
        cog.bot.wait_until_ready = AsyncMock()

        ticks = [1000.0, 1000.0 + cog.AUTO_LEAVE_GRACE_SECONDS + 1.0]
        closed = [False, False, True]

        async def fake_sleep(_sec):
            return None

        with patch.object(vc_handler.asyncio, "sleep", fake_sleep), \
             patch.object(vc_handler.time, "time", side_effect=ticks), \
             patch.object(cog.bot, "is_closed", side_effect=closed):
            await cog._auto_leave_watchdog_loop()

        cog.disconnect_vc.assert_awaited_once_with(guild_new)

    async def test_watchdog_and_disconnect_vc_clears_ghost_voice_state(self):
        """VoiceClient が None でも guild.me.voice.channel が残る無人ゴースト接続を検知・切断できること"""
        cog = _make_cog()
        state = GuildVCState(2)
        cog.guild_states[2] = state

        bot_member = SimpleNamespace(id=9999, bot=True)
        ghost_ch = SimpleNamespace(id=600, name="ゴーストVC", members=[bot_member])
        change_vs = AsyncMock()
        guild = SimpleNamespace(
            id=2,
            name="GhostGuild",
            voice_client=None,
            me=SimpleNamespace(voice=SimpleNamespace(channel=ghost_ch)),
            get_channel=lambda cid: ghost_ch if cid == 600 else None,
            change_voice_state=change_vs,
        )
        cog.bot.guilds = [guild]
        cog.bot.get_guild = lambda gid: guild if gid == 2 else None
        cog.bot.wait_until_ready = AsyncMock()

        ticks = [2000.0, 2000.0 + cog.AUTO_LEAVE_GRACE_SECONDS + 1.0]
        closed = [False, False, True]

        async def fake_sleep(_sec):
            return None

        with patch.object(vc_handler.asyncio, "sleep", fake_sleep), \
             patch.object(vc_handler.time, "time", side_effect=ticks), \
             patch.object(cog.bot, "is_closed", side_effect=closed):
            await cog._auto_leave_watchdog_loop()

        change_vs.assert_awaited_once_with(channel=None)
        cog.db.remove_active_vc.assert_awaited_once_with(2, bot_index=1)

    def test_resource_manager_ignores_stale_vc_channel_members(self):
        """LocalEngineResourceManager.is_any_human_in_vc も古い vc.channel に残った凍結メンバーを無視すること"""
        rm = LocalEngineResourceManager.__new__(LocalEngineResourceManager)
        stale_human = SimpleNamespace(id=101, bot=False)
        bot_member = SimpleNamespace(id=9999, bot=True)
        ch_old = SimpleNamespace(id=500, members=[bot_member, stale_human])
        ch_new = SimpleNamespace(id=500, members=[bot_member])

        vc = SimpleNamespace(channel=ch_old, is_connected=lambda: True)
        guild = SimpleNamespace(
            id=1,
            voice_client=vc,
            get_channel=lambda cid: ch_new if cid == 500 else None,
        )
        bot = SimpleNamespace(guilds=[guild])
        rm._get_all_bots = lambda: [bot]

        self.assertFalse(rm.is_any_human_in_vc())


if __name__ == "__main__":
    unittest.main()

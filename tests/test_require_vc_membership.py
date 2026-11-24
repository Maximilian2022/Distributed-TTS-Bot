import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from aiohttp.test_utils import TestClient, TestServer

from src.cogs.vc_handler import VCHandlerCog
from src.config import AppConfig
from src.database import Database
from src.web.server import WebServer


class TestRequireVcMembershipDBAndAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "test.db"

    async def asyncTearDown(self):
        self._tmpdir.cleanup()

    async def test_guild_settings_migration_and_default_off(self):
        """新規・既存マイグレーションいずれでもデフォルト OFF (0) で永続化できること"""
        init_db = Database(str(self.db_path))
        await init_db.init_db()
        await init_db.close()

        conn = sqlite3.connect(self.db_path)
        conn.execute("INSERT INTO guild_settings (guild_id, command_prefix) VALUES (1001, '!')")
        conn.execute("ALTER TABLE guild_settings DROP COLUMN require_vc_membership")
        conn.commit()
        conn.close()

        db = Database(str(self.db_path))
        await db.init_db()
        try:
            migrated = await db.get_guild_settings(1001)
            self.assertEqual(migrated.get("require_vc_membership"), 0)

            fresh = await db.get_guild_settings(1002)
            self.assertEqual(fresh.get("require_vc_membership"), 0)

            await db.update_guild_settings(1001, require_vc_membership=1)
            updated_on = await db.get_guild_settings(1001)
            self.assertEqual(updated_on.get("require_vc_membership"), 1)

            await db.update_guild_settings(1001, require_vc_membership=0)
            updated_off = await db.get_guild_settings(1001)
            self.assertEqual(updated_off.get("require_vc_membership"), 0)
        finally:
            await db.close()

    async def test_config_web_api_get_and_post(self):
        """GET/POST /api/config で require_vc_membership の読み書きができること"""
        db = Database(str(self.db_path))
        await db.init_db()

        mock_guild = MagicMock()
        mock_guild.id = 2001
        mock_guild.name = "TestGuild"

        server = WebServer(
            bot=MagicMock(),
            config=AppConfig(),
            database=db,
            cache_manager=MagicMock(),
            tts_manager=MagicMock(),
        )
        server._resolve_guild = MagicMock(return_value=mock_guild)
        test_server = TestServer(server.app, host="127.0.0.1")
        await test_server.start_server()
        client = TestClient(test_server)
        await client.start_server()
        try:
            with patch.object(server, "_require_auth", return_value={"user_id": 1, "is_admin": True}), \
                 patch.object(
                     server,
                     "_get_guild_and_perms",
                     new=AsyncMock(return_value=(mock_guild, {"is_admin": True, "has_role": True})),
                 ):
                res_get = await client.get("/api/config?guild_id=2001")
                self.assertEqual(res_get.status, 200)
                body_get = await res_get.json()
                self.assertIn("require_vc_membership", body_get)
                self.assertFalse(body_get["require_vc_membership"])

                res_post_on = await client.post(
                    "/api/config",
                    json={"guild_id": 2001, "require_vc_membership": True},
                )
                self.assertEqual(res_post_on.status, 200)

                res_get2 = await client.get("/api/config?guild_id=2001")
                self.assertTrue((await res_get2.json())["require_vc_membership"])

                res_post_off = await client.post(
                    "/api/config",
                    json={"guild_id": 2001, "require_vc_membership": False},
                )
                self.assertEqual(res_post_off.status, 200)

                res_get3 = await client.get("/api/config?guild_id=2001")
                self.assertFalse((await res_get3.json())["require_vc_membership"])
        finally:
            await client.close()
            await test_server.close()
            await db.close()


class TestRequireVcMembershipFilter(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmpdir.name) / "filter.db"
        self.db = Database(str(self.db_path))
        await self.db.init_db()

        self.bot = MagicMock()
        self.cog = VCHandlerCog(
            bot=self.bot,
            config=AppConfig(),
            database=self.db,
            cache_manager=MagicMock(),
            tts_manager=MagicMock(),
        )
        self.cog.text_cleaner.should_ignore = MagicMock(return_value=False)
        self.cog._enqueue_tts_message = AsyncMock()
        self.cog._notify_user_speech = MagicMock()

        state = self.cog.get_guild_state(3001)
        state.bound_text_channel_id = 4001

    async def asyncTearDown(self):
        self.cog.cog_unload()
        await self.db.close()
        self._tmpdir.cleanup()

    def _make_message(self, author_vc_channel_id: int | None, bot_vc_channel_id: int = 5001, is_member: bool = True):
        vc_channel = MagicMock()
        vc_channel.id = bot_vc_channel_id

        vc = MagicMock()
        vc.is_connected.return_value = True
        vc.channel = vc_channel

        guild = MagicMock()
        guild.id = 3001
        guild.voice_client = vc

        channel = MagicMock()
        channel.id = 4001

        if is_member:
            author = MagicMock(spec=discord.Member)
        else:
            author = MagicMock(spec=discord.User)
        author.id = 9001
        author.bot = False
        author.display_name = "Tester"

        if author_vc_channel_id is not None:
            author_vc_chan = MagicMock()
            author_vc_chan.id = author_vc_channel_id
            voice_state = MagicMock()
            voice_state.channel = author_vc_chan
        else:
            voice_state = None

        if is_member:
            author.voice = voice_state
        else:
            author.voice = None
            member = MagicMock(spec=discord.Member)
            member.voice = voice_state
            guild.get_member = MagicMock(return_value=member)

        msg = MagicMock(spec=discord.Message)
        msg.id = 7001
        msg.guild = guild
        msg.channel = channel
        msg.author = author
        msg.content = "こんにちは"
        msg.created_at = datetime.now(timezone.utc)
        return msg

    async def test_default_off_reads_non_vc_member(self):
        """デフォルト OFF では VC 不参加メンバーのメッセージも読み上げること"""
        msg = self._make_message(author_vc_channel_id=None)
        with patch("src.cogs.vc_handler.cache_member_info_and_avatar", new=AsyncMock()):
            await self.cog.on_message(msg)
        self.cog._enqueue_tts_message.assert_awaited_once()

    async def test_enabled_ignores_non_vc_and_other_vc_member(self):
        """ON のとき VC 未参加・別 VC 参加メンバーは無視し、同一 VC メンバーのみ読み上げること"""
        await self.db.update_guild_settings(3001, require_vc_membership=1)

        with patch("src.cogs.vc_handler.cache_member_info_and_avatar", new=AsyncMock()), \
             patch.object(self.db, "get_dictionary_words", wraps=self.db.get_dictionary_words) as spy_dict:
            # 1) VC未参加 -> 無視（辞書クエリ前に関数抜けること）
            msg_none = self._make_message(author_vc_channel_id=None)
            await self.cog.on_message(msg_none)
            self.cog._enqueue_tts_message.assert_not_awaited()
            spy_dict.assert_not_awaited()

            # 2) 別VC参加中 -> 無視
            msg_other = self._make_message(author_vc_channel_id=5999)
            await self.cog.on_message(msg_other)
            self.cog._enqueue_tts_message.assert_not_awaited()
            spy_dict.assert_not_awaited()

            # 3) 同一VC参加中 -> 読み上げ実行
            msg_same = self._make_message(author_vc_channel_id=5001)
            await self.cog.on_message(msg_same)
            self.cog._enqueue_tts_message.assert_awaited_once()
            spy_dict.assert_awaited_once()

    async def test_enabled_member_fallback_lookup_for_user_author(self):
        """author が discord.User の場合でも guild.get_member で同一VC判定できること"""
        await self.db.update_guild_settings(3001, require_vc_membership=1)

        msg_user_in_vc = self._make_message(author_vc_channel_id=5001, is_member=False)
        await self.cog.on_message(msg_user_in_vc)
        self.cog._enqueue_tts_message.assert_awaited_once()

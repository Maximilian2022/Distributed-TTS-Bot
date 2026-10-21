import asyncio
import logging
from typing import Dict, List, Optional, Tuple, Set
import discord
from discord.ext import commands

from src.config import AppConfig
from src.database import Database
from src.cache_manager import CacheManager
from src.tts.manager import TTSManager

logger = logging.getLogger(__name__)

class BotPoolManager:
    """
    複数Discord Bot（1〜4号機）のライフサイクル、動的VCルーティング、プール管理を一元統括するマネージャー
    """

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        cache_manager: CacheManager,
        tts_manager: TTSManager
    ):
        self.config = config
        self.db = database
        self.cache_manager = cache_manager
        self.tts_manager = tts_manager
        self.bots: Dict[int, commands.Bot] = {}
        self.tokens: List[Tuple[int, str]] = config.get_discord_tokens()
        self._connecting_vcs: Dict[Tuple[int, int], int] = {}  # (guild_id, vc_id) -> bot_index
        self._vc_locks: Dict[Tuple[int, int], asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()
        self._manually_disconnected_vcs: Set[Tuple[int, int]] = set()  # (guild_id, vc_id) 手動切断フラグ

    def mark_vc_manually_disconnected(self, guild_id: int, voice_channel_id: int) -> None:
        """指定VCが手動切断されたことをマーク (通話終了まで自動再接続を抑止)"""
        key = (guild_id, voice_channel_id)
        self._manually_disconnected_vcs.add(key)
        logger.info(f"[BotPool] Marked VC {voice_channel_id} in guild {guild_id} as manually disconnected (auto-join suppressed until empty).")

    def is_vc_manually_disconnected(self, guild_id: int, voice_channel_id: int) -> bool:
        """指定VCが手動切断により自動再接続抑止中かどうか"""
        return (guild_id, voice_channel_id) in self._manually_disconnected_vcs

    def clear_vc_manual_disconnect(self, guild_id: int, voice_channel_id: int) -> None:
        """指定VCの手動切断フラグを解除 (全員退出または手動/join時)"""
        key = (guild_id, voice_channel_id)
        if key in self._manually_disconnected_vcs:
            self._manually_disconnected_vcs.discard(key)
            logger.info(f"[BotPool] Cleared manual disconnect suppression for VC {voice_channel_id} in guild {guild_id}.")

    def get_vc_lock(self, guild_id: int, voice_channel_id: int) -> asyncio.Lock:
        """指定ギルド・指定VC専用の非同期排他ロックを取得 (原子的作成)"""
        key = (guild_id, voice_channel_id)
        if key not in self._vc_locks:
            self._vc_locks[key] = asyncio.Lock()
        return self._vc_locks[key]

    def is_vc_connecting(self, guild_id: int, voice_channel_id: int) -> bool:
        """指定ギルド・指定VCが現在いずれかのBotによって接続ハンドシェイク中かどうか"""
        return (guild_id, voice_channel_id) in self._connecting_vcs

    async def reserve_vc_connection(self, guild_id: int, voice_channel_id: int, bot_index: int) -> bool:
        """
        指定VCへの接続権を原子的に予約 (In-Flight Reservation)。
        既にいずれかのBotが接続中、または予約中の場合は False を返す。
        """
        async with self._pool_lock:
            # 1. 既にいずれかの号機が接続済みかチェック
            if self.find_connected_bot_in_vc(guild_id, voice_channel_id) is not None:
                logger.warning(
                    f"[BotPool] Reserve rejected: VC {voice_channel_id} in guild {guild_id} "
                    f"already has an active bot connected."
                )
                return False

            # 2. 既にいずれかの号機が接続処理中 (インフライト) かチェック
            key = (guild_id, voice_channel_id)
            if key in self._connecting_vcs:
                existing_bot = self._connecting_vcs[key]
                logger.warning(
                    f"[BotPool] Reserve rejected: VC {voice_channel_id} in guild {guild_id} "
                    f"is currently being connected by Bot #{existing_bot}."
                )
                return False

            # 3. 予約を確保
            self._connecting_vcs[key] = bot_index
            logger.info(f"[BotPool] In-Flight Reservation ACQUIRED: VC {voice_channel_id} reserved for Bot #{bot_index}")
            return True

    def release_vc_reservation(self, guild_id: int, voice_channel_id: int, bot_index: int) -> None:
        """指定VCのインフライト接続予約を解除"""
        key = (guild_id, voice_channel_id)
        if self._connecting_vcs.get(key) == bot_index:
            self._connecting_vcs.pop(key, None)
            logger.debug(f"[BotPool] In-Flight Reservation RELEASED: VC {voice_channel_id} by Bot #{bot_index}")

    def find_connected_or_connecting_bot_in_vc(
        self, guild_id: int, voice_channel_id: int
    ) -> Optional[Tuple[int, commands.Bot]]:
        """指定ギルド・指定VCに接続完了済み、または現在ハンドシェイク処理中のBotを探索"""
        connected = self.find_connected_bot_in_vc(guild_id, voice_channel_id)
        if connected:
            return connected

        key = (guild_id, voice_channel_id)
        connecting_idx = self._connecting_vcs.get(key)
        if connecting_idx and connecting_idx in self.bots:
            return connecting_idx, self.bots[connecting_idx]

        return None

    def get_pool_user_ids(self) -> Dict[int, int]:
        """自プールの全Botの Discord User ID -> bot_index のマップを取得"""
        mapping = {}
        for idx, bot in self.bots.items():
            if bot.user:
                mapping[bot.user.id] = idx
        return mapping

    def find_all_bots_in_vc(self, guild_id: int, voice_channel_id: int) -> List[Tuple[int, commands.Bot]]:
        """指定ギルド・指定VCに接続している自プールの全Bot一覧を取得 (VoiceClientおよびChannel Member双方で走査)"""
        results_map = {}
        # 1. 各Botのローカル VoiceClient から走査
        for idx in sorted(self.bots.keys()):
            bot = self.bots[idx]
            guild = bot.get_guild(guild_id)
            if guild and guild.voice_client and guild.voice_client.is_connected() and guild.voice_client.channel:
                if guild.voice_client.channel.id == voice_channel_id:
                    results_map[idx] = bot

        # 2. Discordチャンネル上の実際のメンバー (channel.members) からも自プールBotを走査 (残留・ゾンビ接続検知)
        user_id_map = self.get_pool_user_ids()
        for idx in sorted(self.bots.keys()):
            bot = self.bots[idx]
            guild = bot.get_guild(guild_id)
            if guild:
                ch = guild.get_channel(voice_channel_id)
                if ch and hasattr(ch, "members"):
                    for m in ch.members:
                        if m.id in user_id_map:
                            b_idx = user_id_map[m.id]
                            if b_idx in self.bots and b_idx not in results_map:
                                results_map[b_idx] = self.bots[b_idx]

        return [(idx, results_map[idx]) for idx in sorted(results_map.keys())]

    def register_bot(self, bot_index: int, bot: commands.Bot) -> None:
        """号機番号とBotインスタンスをプールに登録"""
        self.bots[bot_index] = bot
        logger.info(f"Registered Bot #{bot_index} into BotPoolManager.")

    def get_main_bot(self) -> Optional[commands.Bot]:
        """メインBot (1号機) を取得"""
        return self.bots.get(1)

    def get_bot(self, bot_index: int) -> Optional[commands.Bot]:
        """指定号機のBotを取得"""
        return self.bots.get(bot_index)

    def get_all_bots(self) -> List[commands.Bot]:
        """登録されている全Botを取得"""
        return list(self.bots.values())

    def get_pool_status(self) -> List[Dict[str, any]]:
        """全Botの現在の接続ステータス一覧を取得 (WebUIおよび管理用)"""
        status_list = []
        for idx in sorted(self.bots.keys()):
            b = self.bots[idx]
            connected_vcs = []
            for g in b.guilds:
                if g.voice_client and g.voice_client.is_connected() and g.voice_client.channel:
                    vc_cog = b.get_cog("VCHandlerCog")
                    bound_text_id = None
                    if vc_cog:
                        state = vc_cog.get_guild_state(g.id)
                        bound_text_id = state.bound_text_channel_id
                    text_ch = g.get_channel(bound_text_id) if bound_text_id else None

                    connected_vcs.append({
                        "guild_id": g.id,
                        "guild_name": g.name,
                        "voice_channel_id": g.voice_client.channel.id,
                        "voice_channel_name": g.voice_client.channel.name,
                        "text_channel_id": bound_text_id,
                        "text_channel_name": text_ch.name if text_ch else None
                    })

            status_list.append({
                "bot_index": idx,
                "is_ready": b.is_ready(),
                "user": str(b.user) if b.user else f"Bot #{idx}",
                "user_id": b.user.id if b.user else None,
                "guild_count": len(b.guilds) if b.is_ready() else 0,
                "connected_vcs": connected_vcs,
                "is_busy": len(connected_vcs) > 0 or self.is_bot_busy(b)
            })
        return status_list

    def is_bot_busy(self, bot: commands.Bot) -> bool:
        """
        BotがいずれかのサーバーのVCに接続中（または接続処理中）かどうかを判定。
        Discord API仕様上、1つのBotアカウントは全サーバーを通じて同時に1つのVCにしか接続できない。
        """
        # 1. 予約リスト (インフライト) の確認
        bot_idx = next((idx for idx, b in self.bots.items() if b == bot), None)
        if bot_idx is not None and bot_idx in self._connecting_vcs.values():
            return True

        # 2. bot.voice_clients リストの確認 (discord.Client が保持する全接続中VoiceClient)
        for vc in getattr(bot, "voice_clients", []):
            if vc and (vc.is_connected() or getattr(vc, "_connecting", False)):
                return True

        # 3. 各ギルドの voice_client のフォールバック確認
        for g in getattr(bot, "guilds", []):
            if g.voice_client and (g.voice_client.is_connected() or getattr(g.voice_client, "_connecting", False)):
                return True
        return False

    def find_connected_bot_in_vc(self, guild_id: int, voice_channel_id: int) -> Optional[Tuple[int, commands.Bot]]:
        """指定ギルド・指定VCに既に接続しているBotを探索"""
        for idx, bot in self.bots.items():
            guild = bot.get_guild(guild_id)
            if guild and guild.voice_client and guild.voice_client.is_connected() and guild.voice_client.channel:
                if guild.voice_client.channel.id == voice_channel_id:
                    return idx, bot
        return None

    def find_available_bot(self, guild_id: int, target_vc_id: Optional[int] = None) -> Optional[Tuple[int, commands.Bot]]:
        """
        指定ギルドに参加しており、かつ全サーバーを通じて現在VC未接続の空きBotを優先順（1号機優先）で探索。
        他サーバーで既にVC接続中、または接続処理中の号機は多重接続競合防止のため確実に除外する。
        もし target_vc_id が指定されており既に当該VCに接続/予約中のBotがあれば None を返す。
        """
        if target_vc_id is not None:
            if self.find_connected_or_connecting_bot_in_vc(guild_id, target_vc_id) is not None:
                return None

        for idx in sorted(self.bots.keys()):
            bot = self.bots[idx]
            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            # 全サーバーを通じていずれかのVCに接続中または予約中の号機はスキップ
            if not self.is_bot_busy(bot):
                return idx, bot
        return None

    def find_bot_for_text_channel(self, guild_id: int, text_channel_id: int) -> Optional[Tuple[int, commands.Bot]]:
        """指定テキストチャンネルに紐付いているBotを探索"""
        for idx, bot in self.bots.items():
            vc_cog = bot.get_cog("VCHandlerCog")
            if vc_cog:
                state = vc_cog.get_guild_state(guild_id)
                if state.bound_text_channel_id == text_channel_id:
                    return idx, bot
        return None

    def find_bot_for_member_vc(self, guild_id: int, member: discord.Member) -> Optional[Tuple[int, commands.Bot]]:
        """指定メンバーがいるボイスチャンネルに接続しているBotを探索"""
        if not member.voice or not member.voice.channel:
            return None
        return self.find_connected_or_connecting_bot_in_vc(guild_id, member.voice.channel.id)

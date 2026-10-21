"""
メンバー情報＆アバター画像キャッシュのバックグラウンド・スロー巡回 Cog
VC完全アイドル時（全台無人・通話ゼロ）の安全なタイミングを見計らい、
Discord API レートリミット（429）を完全回避しながら未キャッシュ・期限切れメンバー情報をゆっくり補完する。
"""

import asyncio
import logging
import time
from typing import Optional, List, Dict, Any

import discord
from discord.ext import commands

from src.database import Database
from src.utils.async_task import safe_create_task
from src.utils.avatar_manager import (
    cache_member_info_and_avatar,
    download_and_save_avatar,
    has_avatar
)

logger = logging.getLogger("MemberCacheUpdater")


class MemberCacheUpdaterCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        database: Database,
        bot_pool: Optional[Any] = None,
        min_batch_interval_days: float = 14.0,     # 月2回ペース (14日おき)
        check_interval_seconds: float = 21600.0,   # 6時間おきに「14日経過したか」を判定
        inter_request_delay_seconds: float = 8.0,  # 1人あたりの待機インターバル (超安全設計)
        batch_limit_per_run: int = 50,             # 1回の巡回で処理する最大人数
        max_cache_age_days: int = 30               # キャッシュ有効期限 (30日)
    ):
        self.bot = bot
        self.db = database
        self.bot_pool = bot_pool
        self.min_batch_interval_days = min_batch_interval_days
        self.check_interval_seconds = check_interval_seconds
        self.inter_request_delay_seconds = inter_request_delay_seconds
        self.batch_limit_per_run = batch_limit_per_run
        self.max_cache_age_days = max_cache_age_days

        self._loop_task: Optional[asyncio.Task] = None
        self._is_running: bool = False

        # 起動直後の初期化・VC復旧が完了するまで十分に待機してからループを開始
        self._loop_task = safe_create_task(
            self._updater_loop(),
            name="member_cache_updater_loop"
        )

    def cog_unload(self):
        """Cogアンロード時にバックグラウンドループをキャンセル"""
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            logger.info("MemberCacheUpdaterCog background task cancelled.")

    def is_vc_active(self) -> bool:
        """
        全Bot（メイン＋BotPool）のいずれかが現在VCに接続・通話中であるか判定。
        1台でも接続中なら本番VCへの干渉を避けるため True を返す。
        """
        try:
            # 1. メインBotの接続判定
            for vc in getattr(self.bot, "voice_clients", []):
                if vc and vc.is_connected():
                    return True

            # 2. サブBotプール各機の接続判定
            if self.bot_pool:
                sub_bots = getattr(self.bot_pool, "bots", [])
                for s_bot in sub_bots:
                    for vc in getattr(s_bot, "voice_clients", []):
                        if vc and vc.is_connected():
                            return True
        except Exception as e:
            logger.debug(f"Error checking vc active state: {e}")
            return True  # 安全側に倒してビジー判定

        return False

    async def _updater_loop(self) -> None:
        """
        メインバックグラウンドループ
        起動後60秒待機し、DBの最終実行タイムスタンプを確認。
        前回実行から14日（月2回仕様）以上経過しており、かつVC完全アイドル時のみ1回実行。
        """
        # 起動直後のセッション復帰・他タスクへの配慮で初回は60秒スリープ
        await asyncio.sleep(60.0)
        logger.info("MemberCacheUpdater background loop activated (bi-weekly mode).")

        while True:
            try:
                # 1. DBから前回完了タイムスタンプを取得し、14日経過しているか判定
                last_run_raw = await self.db.get_system_kv("last_member_cache_batch_timestamp")
                now = time.time()
                min_interval_seconds = self.min_batch_interval_days * 86400.0

                if last_run_raw:
                    try:
                        last_run_time = float(last_run_raw)
                        elapsed = now - last_run_time
                        if elapsed < min_interval_seconds:
                            remaining_days = (min_interval_seconds - elapsed) / 86400.0
                            logger.info(
                                f"Member cache slow batch is in cooldown. Last run was {elapsed / 86400.0:.1f} days ago "
                                f"(next run in {remaining_days:.1f} days). Skipping cycle."
                            )
                            await asyncio.sleep(self.check_interval_seconds)
                            continue
                    except ValueError:
                        pass

                # 2. VC通話中チェック (通話中なら巡回せずスキップして次回または15分後に再確認)
                if self.is_vc_active():
                    logger.debug("Member cache update is due, but VC is currently active. Waiting 15 minutes...")
                    await asyncio.sleep(900.0)  # 15分後に再確認
                    continue

                # 3. 未キャッシュまたは30日以上未更新の対象者をDBから抽出
                stale_users = await self.db.get_stale_or_uncached_guild_users(
                    max_age_days=self.max_cache_age_days,
                    limit=self.batch_limit_per_run
                )

                if not stale_users:
                    logger.info("No stale or uncached guild members found. Updating last run timestamp.")
                    await self.db.set_system_kv("last_member_cache_batch_timestamp", str(now))
                    await asyncio.sleep(self.check_interval_seconds)
                    continue

                logger.info(
                    f"Starting bi-weekly member cache slow batch for {len(stale_users)} members (VC idle). "
                    f"Processing slowly with {self.inter_request_delay_seconds}s intervals..."
                )

                # 4. 1人ずつ安全に情報取得＆画像更新
                interrupted = False
                for item in stale_users:
                    # 割り込みチェック: もし処理中に誰かがVCに入ってきたら即座に中断
                    if self.is_vc_active():
                        logger.info("VC call started during member cache update. Pausing batch immediately.")
                        interrupted = True
                        break

                    gid = item["guild_id"]
                    uid = item["user_id"]
                    await self._process_single_member(gid, uid)

                    # Discord REST API レートリミット完全回避のためのインターバル待機
                    await asyncio.sleep(self.inter_request_delay_seconds)

                if not interrupted:
                    # バッチが完了したら最終実行タイムスタンプをDBに永続化 (次回は14日後まで実行されない)
                    await self.db.set_system_kv("last_member_cache_batch_timestamp", str(time.time()))
                    logger.info(
                        f"Finished bi-weekly member cache update batch successfully. "
                        f"Next scheduled run will be in {self.min_batch_interval_days} days."
                    )
                else:
                    # VC通話により中断された場合はタイムスタンプを更新せず、通話終了後に再開できるよう15分待機
                    logger.info("Batch interrupted by VC activity. Will resume when VC becomes idle.")
                    await asyncio.sleep(900.0)
                    continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in member cache updater loop: {e}", exc_info=True)

            # 次回巡回チェックまで待機 (6時間)
            await asyncio.sleep(self.check_interval_seconds)

    async def _process_single_member(self, guild_id: int, user_id: int) -> None:
        """
        単一メンバーのプロフィール＆アバター確認・更新
        """
        try:
            guild = self.bot.get_guild(guild_id)
            if not guild:
                return

            # まずBotのローカルメモリキャッシュを確認 (API消費0)
            member = guild.get_member(user_id)

            if member:
                # オンメモリに実在
                await cache_member_info_and_avatar(self.db, member, guild_id=guild_id, is_in_guild=True)
                logger.debug(f"Resolved member {user_id} in {guild_id} via memory cache")
                return

            # オンメモリになければ Discord REST API で fetch_member
            try:
                member = await guild.fetch_member(user_id)
                await cache_member_info_and_avatar(self.db, member, guild_id=guild_id, is_in_guild=True)
                logger.info(f"Fetched and cached member {user_id} ({member.display_name}) for guild {guild_id}")
            except discord.NotFound:
                # サーバーから既に脱退している
                logger.info(f"Member {user_id} not found in guild {guild_id} (left guild). Updating cache.")
                await self.db.upsert_guild_member_cache(
                    guild_id=guild_id,
                    user_id=user_id,
                    username=f"ユーザー ({user_id})",
                    display_name=f"ユーザー ({user_id})",
                    avatar_hash="",
                    is_in_guild=False
                )
            except discord.Forbidden:
                logger.debug(f"Permission denied to fetch member {user_id} in guild {guild_id}")
            except discord.HTTPException as http_err:
                if getattr(http_err, "status", None) == 429:
                    logger.warning(f"Rate limited (429) while fetching member {user_id}. Backing off.")
                    await asyncio.sleep(60.0)  # 429時は1分スリープ
                else:
                    logger.debug(f"HTTPException while fetching member {user_id}: {http_err}")

        except Exception as e:
            logger.debug(f"Failed to process member cache for {user_id} in {guild_id}: {e}")

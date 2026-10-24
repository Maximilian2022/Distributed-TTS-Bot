import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import logging
from typing import Optional, List, Tuple

from src.config import AppConfig
from src.database import Database
from src.cache_manager import CacheManager
from src.cogs.vc_handler import VCHandlerCog, VCConnectAborted

logger = logging.getLogger(__name__)

class VoiceSelectView(discord.ui.View):
    """声質選択用セレクトメニュー"""
    def __init__(self, config: AppConfig, database: Database, user_id: int, guild_id: Optional[int] = None):
        super().__init__(timeout=60)
        self.config = config
        self.db = database
        self.user_id = user_id
        self.guild_id = guild_id

        # ドロップダウンオプションの作成 (Discord API 上限25件のハードガード)
        options = []
        for voice_id, voice in config.voices.items():
            options.append(
                discord.SelectOption(
                    label=f"{voice.name} ({voice.gender})",
                    value=voice_id,
                    description=voice.description[:100]
                )
            )

        total_voices = len(options)
        if total_voices > 25:
            options = options[:25]
            placeholder = f"声を選択 (上位25件表示中 / /voice補完やWebUI推奨)"
        else:
            placeholder = "使用したい声を選択してください..."

        self.select = discord.ui.Select(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def select_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他のユーザーの設定は変更できません。", ephemeral=True)
            return

        chosen_id = self.select.values[0]
        voice_cfg = self.config.voices.get(chosen_id)
        name = voice_cfg.name if voice_cfg else chosen_id

        await self.db.update_user_settings(
            user_id=self.user_id,
            speaker_id=chosen_id,
            guild_id=self.guild_id
        )

        await interaction.response.edit_message(
            content=f"✅ 声質を **{name}** に変更しました！",
            view=None
        )

class CommandsCog(commands.Cog):
    """
    Discordスラッシュコマンド群
    - /join, /leave, /skip
    - /voice
    - /dict add, /dict delete, /dict list
    """

    def __init__(
        self,
        bot: commands.Bot,
        config: AppConfig,
        database: Database,
        cache_manager: CacheManager,
        vc_handler: VCHandlerCog,
        bot_pool=None,
        disaster_service=None
    ):
        self.bot = bot
        self.config = config
        self.db = database
        self.cache_manager = cache_manager
        self.vc_handler = vc_handler
        self.bot_pool = bot_pool
        self.disaster_service = disaster_service

    # --- VC制御コマンド ---

    @app_commands.command(name="join", description="実行者が参加しているVCに接続し、このチャンネルを読み上げ対象にします。")
    async def join(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ まずボイスチャンネルに参加してから実行してください。", ephemeral=True)
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("❌ テキストチャンネルで実行してください。", ephemeral=True)
            return

        voice_channel = interaction.user.voice.channel
        text_channel = interaction.channel

        await interaction.response.defer(ephemeral=False)
        try:
            # VC単位の排他ロックを取得してアサイン・接続処理を完全同期
            vc_lock = self.bot_pool.get_vc_lock(interaction.guild_id, voice_channel.id) if self.bot_pool else asyncio.Lock()
            async with vc_lock:
                assigned_bot = None
                assigned_bot_idx = 1
                if self.bot_pool:
                    # 既に接続済みまたは接続処理中のBotを探索
                    found = self.bot_pool.find_connected_or_connecting_bot_in_vc(interaction.guild_id, voice_channel.id)
                    if found:
                        assigned_bot_idx, assigned_bot = found
                    else:
                        avail = self.bot_pool.find_available_bot(interaction.guild_id, target_vc_id=voice_channel.id)
                        if avail:
                            assigned_bot_idx, assigned_bot = avail
                        else:
                            total_bots = len(self.bot_pool.bots) if hasattr(self.bot_pool, "bots") and self.bot_pool.bots else 5
                            await interaction.followup.send(
                                f"⚠️ 現在利用可能なBot（最大{total_bots}台）はすべて他のVCに接続中です。\n空きができるまでお待ちいただくか、使っていないVCで `/leave` を実行してください。",
                                ephemeral=True
                            )
                            return
                else:
                    assigned_bot = self.bot

                target_vc_cog = assigned_bot.get_cog("VCHandlerCog") if assigned_bot else self.vc_handler
                target_guild = assigned_bot.get_guild(interaction.guild_id) if assigned_bot else interaction.guild
                target_vc = target_guild.get_channel(voice_channel.id) if target_guild else voice_channel
                target_text = target_guild.get_channel(text_channel.id) if target_guild else text_channel

                await target_vc_cog.connect_vc(target_vc, target_text)

            if self.bot_pool and interaction.guild_id:
                self.bot_pool.clear_vc_manual_disconnect(interaction.guild_id, voice_channel.id)

            dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"
            bot_label = f" (Bot {assigned_bot_idx}号機)" if self.bot_pool and len(self.bot_pool.tokens) > 1 else ""
            embed = discord.Embed(
                title=f"🔊 ボイスチャンネル接続完了{bot_label}",
                description=(
                    f"**{voice_channel.name}** に接続しました！\n"
                    f"**#{text_channel.name}** のテキストを読み上げます。\n\n"
                    f"💡 [Webダッシュボード]({dashboard_url}) から声質のカスタマイズや単語辞書の登録が可能です。"
                ),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
        except VCConnectAborted as e:
            logger.info(f"Join VC aborted: {e}")
            await interaction.followup.send(f"ℹ️ ボイスチャンネル接続は安全に中断されました: {e}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error joining VC: {e}")
            await interaction.followup.send(f"❌ 接続中にエラーが発生しました: {e}", ephemeral=True)

    @app_commands.command(name="leave", description="ボイスチャンネルから切断し、読み上げを終了します。")
    async def leave(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        bots_to_disconnect: List[Tuple[int, commands.Bot]] = []
        if self.bot_pool:
            # 1. 実行者がいるVCに接続中の全Botを走査 (重複Botの一網打尽切断)
            if isinstance(interaction.user, discord.Member) and interaction.user.voice and interaction.user.voice.channel:
                bots_to_disconnect = self.bot_pool.find_all_bots_in_vc(
                    interaction.guild_id, interaction.user.voice.channel.id
                )
            # 2. VCにいなければテキストチャンネルに紐付いているBotを探索
            if not bots_to_disconnect:
                found = self.bot_pool.find_bot_for_text_channel(interaction.guild_id, interaction.channel_id)
                if found:
                    bots_to_disconnect = [found]
        else:
            if interaction.guild.voice_client:
                bots_to_disconnect = [(1, self.bot)]

        if not bots_to_disconnect:
            await interaction.response.send_message("⚠️ このVCまたはテキストチャンネルに接続しているBotはいません。", ephemeral=True)
            return

        disconnected_indices = []
        disconnected_vc_ids = set()
        for idx, bot in bots_to_disconnect:
            target_guild = bot.get_guild(interaction.guild_id)
            target_vc_cog = bot.get_cog("VCHandlerCog")
            if target_guild and target_vc_cog:
                vc_id = target_guild.voice_client.channel.id if (target_guild.voice_client and target_guild.voice_client.channel) else None
                try:
                    await target_vc_cog.disconnect_vc(target_guild)
                    disconnected_indices.append(idx)
                    if vc_id:
                        disconnected_vc_ids.add(vc_id)
                except Exception as e:
                    logger.warning(f"Error disconnecting Bot #{idx} via /leave: {e}")

        # 手動切断されたVCをマーク (通話終了・全員退出まで自動再参加ループを抑止)
        if self.bot_pool and interaction.guild_id:
            for v_id in disconnected_vc_ids:
                self.bot_pool.mark_vc_manually_disconnected(interaction.guild_id, v_id)
            if not disconnected_vc_ids and isinstance(interaction.user, discord.Member) and interaction.user.voice and interaction.user.voice.channel:
                self.bot_pool.mark_vc_manually_disconnected(interaction.guild_id, interaction.user.voice.channel.id)

        if disconnected_indices:
            indices_str = ", ".join(f"#{i}" for i in sorted(disconnected_indices))
            bot_label = f" (Bot {indices_str}号機)" if self.bot_pool and len(self.bot_pool.tokens) > 1 else ""
            await interaction.response.send_message(f"👋 ボイスチャンネルから切断しました。{bot_label}")
        else:
            await interaction.response.send_message("⚠️ 切断可能なボイスチャンネルがありません。", ephemeral=True)

    @app_commands.command(name="skip", description="現在再生中の音声をスキップし、次の音声を再生します。")
    async def skip(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        target_bot = None
        if self.bot_pool:
            if isinstance(interaction.user, discord.Member) and interaction.user.voice and interaction.user.voice.channel:
                found = self.bot_pool.find_connected_bot_in_vc(interaction.guild_id, interaction.user.voice.channel.id)
                if found:
                    _, target_bot = found
            if not target_bot:
                found = self.bot_pool.find_bot_for_text_channel(interaction.guild_id, interaction.channel_id)
                if found:
                    _, target_bot = found
        else:
            target_bot = self.bot

        if not target_bot:
            await interaction.response.send_message("⚠️ 現在再生中のBotはいません。", ephemeral=True)
            return

        target_guild = target_bot.get_guild(interaction.guild_id)
        target_vc_cog = target_bot.get_cog("VCHandlerCog")
        vc = target_guild.voice_client if target_guild else None
        state = target_vc_cog.get_guild_state(interaction.guild_id) if target_vc_cog else None

        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭️ 現在の音声をスキップしました。")
        elif state and not state.queue.empty():
            try:
                state.queue.get_nowait()
                state.queue.task_done()
                await interaction.response.send_message("⏭️ 待機キューの音声を1件スキップしました。")
            except Exception:
                await interaction.response.send_message("⚠️ スキップできる音声がありません。", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 現在再生中の音声はありません。", ephemeral=True)

    # --- 声質設定コマンド ---

    async def voice_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str
    ) -> List[app_commands.Choice[str]]:
        """声質のオートコンプリート (インクリメンタル検索・最大25件)"""
        current_lower = current.lower().strip()
        choices = []
        for voice_id, voice in self.config.voices.items():
            name_full = f"{voice.name} ({voice.gender}) [{voice.provider}]"
            search_target = f"{voice.name} {voice.gender} {voice.provider} {voice.description}".lower()
            if not current_lower or current_lower in search_target:
                choices.append(app_commands.Choice(name=name_full[:100], value=voice_id))
                if len(choices) >= 25:
                    break
        return choices

    @app_commands.command(name="voice", description="自分の読み上げ声質・ピッチ・速度・音量を設定・変更します。")
    @app_commands.describe(
        speaker="声質・話者 (文字入力で検索・補完できます)",
        pitch="ピッチ調整 (例: +10%, 0%, -10%)",
        speed="読み上げ速度 (0.5 〜 2.0)",
        volume="音量パーセント (0 〜 200, デフォルト: 80)"
    )
    async def voice(
        self,
        interaction: discord.Interaction,
        speaker: Optional[str] = None,
        pitch: Optional[str] = None,
        speed: Optional[float] = None,
        volume: Optional[int] = None
    ):
        user_id = interaction.user.id
        guild_id = interaction.guild_id
        available_speaker_ids = list(self.config.voices.keys())

        # 既存設定の取得または初期設定 (ギルド個別 & 初回他サーバー引き継ぎ)
        settings = await self.db.get_or_create_user_settings(
            user_id=user_id,
            available_speaker_ids=available_speaker_ids,
            default_pitch=self.config.default_pitch,
            default_speed=self.config.default_speed,
            default_volume=0.8,
            guild_id=guild_id
        )

        # 指定話者のバリデーション
        new_speaker_id = settings["speaker_id"]
        if speaker:
            if speaker in self.config.voices:
                new_speaker_id = speaker
            else:
                # 部分一致検索でフォールバック
                matched = next((vid for vid, v in self.config.voices.items() if speaker.lower() in v.name.lower()), None)
                if matched:
                    new_speaker_id = matched

        new_pitch = pitch if pitch is not None else settings["pitch"]
        new_speed = speed if speed is not None else settings["speed"]
        new_volume = (float(volume) / 100.0) if volume is not None else settings.get("volume", 0.8)

        if speed is not None and not (0.5 <= speed <= 2.0):
            await interaction.response.send_message("❌ 速度は 0.5 〜 2.0 の範囲で指定してください。", ephemeral=True)
            return

        if volume is not None and not (0 <= volume <= 200):
            await interaction.response.send_message("❌ 音量は 0 〜 200 (%) の範囲で指定してください。", ephemeral=True)
            return

        # 話者・ピッチ・スピード・音量が指定されている場合は更新
        if speaker is not None or pitch is not None or speed is not None or volume is not None:
            settings = await self.db.update_user_settings(
                user_id=user_id,
                speaker_id=new_speaker_id,
                pitch=new_pitch,
                speed=new_speed,
                volume=new_volume,
                guild_id=guild_id
            )

        current_speaker_id = settings["speaker_id"]
        speaker_info = self.config.voices.get(current_speaker_id)
        speaker_name = speaker_info.name if speaker_info else current_speaker_id
        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"
        vol_pct = int(round(settings.get("volume", 0.8) * 100))

        embed = discord.Embed(
            title="🎙️ あなたの音声設定",
            description=f"💡 [Webダッシュボード]({dashboard_url}) からも設定・試聴が可能です。",
            color=discord.Color.blue()
        )
        embed.add_field(name="話者 (声質)", value=f"**{speaker_name}**", inline=True)
        embed.add_field(name="ピッチ", value=f"`{settings['pitch']}`", inline=True)
        embed.add_field(name="速度", value=f"`{settings['speed']}x`", inline=True)
        embed.add_field(name="音量", value=f"`{vol_pct}%`", inline=True)
        embed.set_footer(
            text="声質を変更したい場合は /voice の検索補完または下のメニューから選択してください。"
        )

        view = VoiceSelectView(self.config, self.db, user_id, guild_id=guild_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @voice.autocomplete("speaker")
    async def voice_speaker_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self.voice_autocomplete(interaction, current)

    @app_commands.command(name="volume", description="自分の読み上げ音量を設定・確認します (0% 〜 200%, デフォルト: 80%)。")
    @app_commands.describe(
        percent="音量パーセント (0 〜 200, 未指定で現在設定を確認)"
    )
    async def volume_slash(
        self,
        interaction: discord.Interaction,
        percent: Optional[int] = None
    ):
        user_id = interaction.user.id
        guild_id = interaction.guild_id
        available_speaker_ids = list(self.config.voices.keys())

        settings = await self.db.get_or_create_user_settings(
            user_id=user_id,
            available_speaker_ids=available_speaker_ids,
            default_pitch=self.config.default_pitch,
            default_speed=self.config.default_speed,
            default_volume=0.8,
            guild_id=guild_id
        )

        if percent is None:
            current_pct = int(round(settings.get("volume", 0.8) * 100))
            await interaction.response.send_message(
                f"🔊 現在の音量設定は **{current_pct}%** です。(変更例: `/volume 80` または `!volume 80`)",
                ephemeral=True
            )
            return

        if not (0 <= percent <= 200):
            await interaction.response.send_message("❌ 音量は 0 〜 200 (%) の範囲で指定してください。", ephemeral=True)
            return

        new_vol = float(percent) / 100.0
        await self.db.update_user_settings(
            user_id=user_id,
            speaker_id=settings["speaker_id"],
            volume=new_vol,
            guild_id=guild_id
        )
        await interaction.response.send_message(
            f"🔊 読み上げ音量を **{percent}%** に設定しました！",
            ephemeral=True
        )

    @commands.command(name="volume", description="自分の読み上げ音量を設定・確認します (!volume <0-200>)")
    async def volume_prefix(self, ctx: commands.Context, percent: Optional[int] = None):
        user_id = ctx.author.id
        guild_id = ctx.guild.id if ctx.guild else None
        available_speaker_ids = list(self.config.voices.keys())

        settings = await self.db.get_or_create_user_settings(
            user_id=user_id,
            available_speaker_ids=available_speaker_ids,
            default_pitch=self.config.default_pitch,
            default_speed=self.config.default_speed,
            default_volume=0.8,
            guild_id=guild_id
        )

        if percent is None:
            current_pct = int(round(settings.get("volume", 0.8) * 100))
            await ctx.reply(f"🔊 現在の音量設定は **{current_pct}%** です。(変更例: `!volume 80`)")
            return

        if not (0 <= percent <= 200):
            await ctx.reply("❌ 音量は 0 〜 200 (%) の範囲で指定してください。")
            return

        new_vol = float(percent) / 100.0
        await self.db.update_user_settings(
            user_id=user_id,
            speaker_id=settings["speaker_id"],
            volume=new_vol,
            guild_id=guild_id
        )
        await ctx.reply(f"🔊 読み上げ音量を **{percent}%** に設定しました！")

    # --- 単語辞書コマンド (/dict) ---

    dict_group = app_commands.Group(name="dict", description="単語辞書の登録・削除・一覧表示を行います。")

    @dict_group.command(name="add", description="単語とその読み方をサーバー辞書に登録します。")
    @app_commands.describe(word="登録する単語", reading="読み方 (ひらがな・カタカナ等)")
    async def dict_add(self, interaction: discord.Interaction, word: str, reading: str):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ 辞書コマンドはDiscordサーバー内で実行してください。", ephemeral=True)
            return

        w = word.strip()
        r = reading.strip()
        if not w or not r:
            await interaction.response.send_message("❌ 単語と読み方を正しく入力してください。", ephemeral=True)
            return

        await self.db.add_dictionary_word(interaction.guild_id, w, r)
        # 単語辞書更新時は当該単語を含むキャッシュのみを選択的パージ
        purged = await self.cache_manager.purge_by_word(w)
        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"

        embed = discord.Embed(
            title="📖 辞書登録完了",
            description=f"💡 [Webダッシュボード]({dashboard_url}) からも辞書編集・ユーザー名インポートが可能です。",
            color=discord.Color.green()
        )
        embed.add_field(name="単語", value=w, inline=True)
        embed.add_field(name="読み", value=r, inline=True)
        embed.set_footer(
            text=f"辞書更新に伴い、該当キャッシュ {purged} 件を選択的パージしました。"
        )

        await interaction.response.send_message(embed=embed)

    @dict_group.command(name="delete", description="単語をサーバー辞書から削除します。")
    @app_commands.describe(word="削除する単語")
    async def dict_delete(self, interaction: discord.Interaction, word: str):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ 辞書コマンドはDiscordサーバー内で実行してください。", ephemeral=True)
            return

        w = word.strip()
        if not w:
            await interaction.response.send_message("❌ 削除する単語を指定してください。", ephemeral=True)
            return

        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"
        deleted = await self.db.delete_dictionary_word(interaction.guild_id, w)
        if deleted:
            # 該当単語を含むキャッシュのみを選択的パージ
            purged = await self.cache_manager.purge_by_word(w)
            embed = discord.Embed(
                title="🗑️ 辞書削除完了",
                description=(
                    f"単語 **「{w}」** をサーバー辞書から削除しました。\n\n"
                    f"💡 [Webダッシュボード]({dashboard_url}) からも辞書編集が可能です。"
                ),
                color=discord.Color.orange()
            )
            embed.set_footer(
                text=f"該当キャッシュ {purged} 件を選択的パージしました。"
            )
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                f"⚠️ 単語 **「{w}」** はサーバー辞書に登録されていません。\n"
                f"💡 [Webダッシュボード]({dashboard_url}) で登録状況を確認できます。",
                ephemeral=True
            )

    @dict_group.command(name="list", description="サーバー辞書に登録されている単語一覧を表示します。")
    async def dict_list(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ 辞書コマンドはDiscordサーバー内で実行してください。", ephemeral=True)
            return

        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"
        words = await self.db.get_dictionary_words(interaction.guild_id)
        if not words:
            await interaction.response.send_message(
                f"📖 単語辞書には現在何も登録されていません。\n"
                f"💡 [Webダッシュボード]({dashboard_url}) からメンバー名の一括インポートも可能です。",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📖 単語辞書一覧 ({len(words)}件)",
            color=discord.Color.purple()
        )

        # 文字数制限を考慮して最大25件または分割表示
        lines = [f"• **{w}** → {r}" for w, r in words[:25]]
        desc = "\n".join(lines)
        desc += f"\n\n💡 [Webダッシュボード]({dashboard_url}) からも辞書確認・インポートが可能です。"
        embed.description = desc

        if len(words) > 25:
            embed.set_footer(text=f"他 {len(words) - 25} 件（表示上限により省略）")

        await interaction.response.send_message(embed=embed)

    # --- NGワードコマンド (/ng) ---

    ng_group = app_commands.Group(name="ng", description="NGワードの登録・削除・一覧表示を行います。")

    @ng_group.command(name="add", description="NGワードをサーバーに登録します。発言時にピー音に置き換えられます。")
    @app_commands.describe(word="登録するNGワード")
    async def ng_add(self, interaction: discord.Interaction, word: str):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ NGワードコマンドはDiscordサーバー内で実行してください。", ephemeral=True)
            return

        w = word.strip()
        if not w:
            await interaction.response.send_message("❌ NGワードを正しく入力してください。", ephemeral=True)
            return

        await self.db.add_ng_word(interaction.guild_id, w)
        # NGワード登録時も当該単語を含むキャッシュを選択的パージ
        purged = await self.cache_manager.purge_by_word(w)
        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"

        embed = discord.Embed(
            title="🚫 NGワード登録完了",
            description=(
                f"単語 **「{w}」** をNGワードに登録しました。\n"
                f"今後この単語が使われた発言は、該当部分が自主規制ピー音に置き換えられ、🤫リアクションが付与されます。\n\n"
                f"💡 [Webダッシュボード]({dashboard_url}) からもNGワード管理が可能です。"
            ),
            color=discord.Color.red()
        )
        if purged > 0:
            embed.set_footer(text=f"登録に伴い、該当キャッシュ {purged} 件を選択的パージしました。")

        await interaction.response.send_message(embed=embed)

    @ng_group.command(name="delete", description="NGワードをサーバーから削除します。")
    @app_commands.describe(word="削除するNGワード")
    async def ng_delete(self, interaction: discord.Interaction, word: str):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ NGワードコマンドはDiscordサーバー内で実行してください。", ephemeral=True)
            return

        w = word.strip()
        if not w:
            await interaction.response.send_message("❌ 削除するNGワードを指定してください。", ephemeral=True)
            return

        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"
        deleted = await self.db.delete_ng_word(interaction.guild_id, w)
        if deleted:
            purged = await self.cache_manager.purge_by_word(w)
            embed = discord.Embed(
                title="🗑️ NGワード削除完了",
                description=(
                    f"NGワード **「{w}」** をサーバーから削除しました。\n\n"
                    f"💡 [Webダッシュボード]({dashboard_url}) からも管理が可能です。"
                ),
                color=discord.Color.orange()
            )
            if purged > 0:
                embed.set_footer(text=f"該当キャッシュ {purged} 件を選択的パージしました。")
            await interaction.response.send_message(embed=embed)
        else:
            await interaction.response.send_message(
                f"⚠️ 単語 **「{w}」** はNGワードに登録されていません。\n"
                f"💡 [Webダッシュボード]({dashboard_url}) で登録状況を確認できます。",
                ephemeral=True
            )

    @ng_group.command(name="list", description="サーバーに登録されているNGワード一覧を表示します。")
    async def ng_list(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ NGワードコマンドはDiscordサーバー内で実行してください。", ephemeral=True)
            return

        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"
        words = await self.db.get_ng_words(interaction.guild_id)
        if not words:
            await interaction.response.send_message(
                f"🚫 NGワードは現在何も登録されていません。\n"
                f"💡 [Webダッシュボード]({dashboard_url}) からもNGワードの登録・管理が可能です。",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🚫 NGワード一覧 ({len(words)}件)",
            color=discord.Color.red()
        )

        lines = [f"• **{w}**" for w in words[:30]]
        desc = "\n".join(lines)
        desc += f"\n\n💡 [Webダッシュボード]({dashboard_url}) からもNGワード確認・追加が可能です。"
        embed.description = desc

        if len(words) > 30:
            embed.set_footer(text=f"他 {len(words) - 30} 件（表示上限により省略）")

        await interaction.response.send_message(embed=embed)

    @ng_group.command(name="preset", description="一般的な標準NGワードプリセット（差別用語・露骨な性的語句等）を一括登録します。")
    async def ng_preset(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ NGワードコマンドはDiscordサーバー内で実行してください。", ephemeral=True)
            return

        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"
        count = await self.db.import_default_ng_words(interaction.guild_id)
        embed = discord.Embed(
            title="📋 標準NGワードプリセット適用完了",
            description=(
                f"標準NGワード {count} 件をサーバーに一括登録しました。\n"
                f"登録された単語は `/ng list` または [Webダッシュボード]({dashboard_url}) から確認・個別削除が可能です。"
            ),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="info", description="Botの情報、搭載エンジン、各音声モデルの利用規約・クレジット表記を表示します。")
    async def info(self, interaction: discord.Interaction):
        """Bot情報および各TTSモデルのライセンス・規約・クレジット表記を表示"""
        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"

        embed = discord.Embed(
            title="ℹ️ Distributed TTS Discord Bot 情報 & 利用規約・クレジット",
            description=(
                "本Botは複数の高度な音声合成エンジンを統合した超低遅延読み上げBotです。\n"
                "生成された音声をご利用・配信・公開される際は、各エンジンの**利用規約およびクレジット表記**を必ず遵守してください。"
            ),
            color=discord.Color.blue()
        )

        embed.add_field(
            name="🎙️ 搭載音声合成エンジン (全7系統)",
            value=(
                "• **VOICEVOX**: 33キャラクター / 99スタイル (ローカルCPU)\n"
                "• **SHAREVOX**: 9キャラクター / 14スタイル (ローカルCPU)\n"
                "• **COEIROINK**: 公認14キャラ(78スタイル) + MYCOEIROINK(あみたろ・松嘩りすく) (ローカルCPU)\n"
                "• **OpenJTalk**: 名工大メイ・たくみ・標準男声・ゆっくり風 (完全ローカル・超低遅延)\n"
                "• **Microsoft Edge TTS**: 高品位ニューラル音声 (完全無料)\n"
                "• **Azure AI Speech**: 東日本リージョン高精度ニューラル音声\n"
                "• **Google Cloud TTS**: 東京リージョン高精度音声 (Neural2/WaveNet/Standard)"
            ),
            inline=False
        )

        embed.add_field(
            name="📜 ローカルTTS クレジット表記ルール (必須)",
            value=(
                "ローカルエンジン（VOICEVOX / SHAREVOX / COEIROINK / OpenJTalk）の音声を利用・公開する際は、**クレジット表記が必須**です。\n"
                "• **VOICEVOX**: `VOICEVOX:キャラクター名` ([規約](https://voicevox.hiroshiba.jp/))\n"
                "• **SHAREVOX**: `SHAREVOX:キャラクター名` ([規約](https://sharevox.app/terms))\n"
                "• **COEIROINK**: `COEIROINK:キャラクター名` ([規約](https://coeiroink.com/terms))\n"
                "• **OpenJTalk**: `Open JTalk (名工大 MMDAgent)` ([詳細](http://open-jtalk.sourceforge.net/))\n"
                "*※キャラクターごとに個別規約（商用利用条件等）が異なります。*"
            ),
            inline=False
        )

        embed.add_field(
            name="🌐 クラウドTTS 帰属表記",
            value=(
                "• Powered by Microsoft Azure Speech / Microsoft Edge TTS\n"
                "• Powered by Google Cloud Text-to-Speech"
            ),
            inline=False
        )

        embed.add_field(
            name="🔗 Webダッシュボード & 詳細ヘルプ",
            value=(
                f"全キャラクターの個別利用規約リンク一覧や試聴機能はWebUIに掲載されています。\n"
                f"👉 **[Webダッシュボード（ヘルプ＆規約）]({dashboard_url})**"
            ),
            inline=False
        )

        embed.set_footer(text="超低遅延インメモリ連結・スマート排他制御・多層フォールバック稼働中")
        await interaction.response.send_message(embed=embed)

    # --- 防災速報コマンドグループ ---
    disaster_group = app_commands.Group(name="disaster", description="防災速報（地震・津波・気象警報）の設定・テスト")

    @disaster_group.command(name="status", description="このサーバーでの防災速報（地震・津波・気象警報）の設定を確認します。")
    async def disaster_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.db.get_guild_settings(interaction.guild_id, self.config)
        enabled = bool(settings.get("disaster_alert_enabled", 1))
        min_scale = int(settings.get("disaster_min_scale", 30))
        from src.disaster.parser import format_scale_name
        scale_name = format_scale_name(min_scale)
        pref = settings.get("disaster_pref_filter", "").strip() or "全国（絞り込みなし）"
        weather = bool(settings.get("disaster_alert_weather", 1))
        vol_pct = int(float(settings.get("disaster_volume", 1.0) if settings.get("disaster_volume") is not None else 1.0) * 100)

        embed = discord.Embed(
            title="🚨 防災速報（地震・津波・気象警報）設定状況",
            description="現在Botが接続しているVCおよびペアテキストチャンネルへ速報を配信します。",
            color=discord.Color.green() if enabled else discord.Color.greyple()
        )
        embed.add_field(name="速報配信機能", value="**有効 (ON)** ✅" if enabled else "**無効 (OFF)** ❌", inline=True)
        embed.add_field(name="地震通知の最小震度", value=f"**{scale_name}** 以上", inline=True)
        embed.add_field(name="速報読み上げ音量", value=f"**{vol_pct}%**", inline=True)
        embed.add_field(name="気象警報・特別警報", value="**通知する** ✅" if weather else "**通知しない** ❌", inline=True)
        embed.add_field(name="対象地域フィルター", value=f"`{pref}`", inline=False)
        embed.set_footer(text="緊急地震速報 (EEW) および 津波警報 は命に関わるため設定に関わらず最優先配信されます。")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @disaster_group.command(name="test", description="防災速報の動作テストを行います（現在参加中のVCとチャンネルに配信）。")
    @app_commands.describe(
        alert_type="テストする速報の種類 (earthquake: 地震, weather: 気象警報)"
    )
    @app_commands.choices(alert_type=[
        app_commands.Choice(name="地震速報テスト (震度4)", value="earthquake"),
        app_commands.Choice(name="気象警報テスト (大雨・洪水)", value="weather")
    ])
    async def disaster_test(self, interaction: discord.Interaction, alert_type: str = "earthquake"):
        if not interaction.guild:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ このコマンドを実行するにはサーバー管理権限が必要です。", ephemeral=True)
            return

        if not self.disaster_service:
            await interaction.response.send_message("⚠️ 防災速報サービスが初期化されていません。", ephemeral=True)
            return

        await interaction.response.send_message("🚀 防災速報テストを発信します...", ephemeral=True)
        await self.disaster_service.trigger_test_alert(interaction.guild_id, alert_type=alert_type)

    @disaster_group.command(name="set", description="防災速報の設定を変更します（管理者専用）。")
    @app_commands.describe(
        enabled="速報配信を有効にするか",
        min_scale="地震通知の最小震度 (10: 震度1, 20: 震度2, 30: 震度3, 40: 震度4)",
        pref_filter="対象都道府県（空文字で全国、カンマ区切りで複数可 例: 東京都,神奈川県）",
        weather_alert="気象警報・特別警報を通知するか",
        volume="速報読み上げ音量 (0〜200%, デフォルト: 100%)"
    )
    async def disaster_set(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        min_scale: Optional[int] = None,
        pref_filter: Optional[str] = None,
        weather_alert: Optional[bool] = None,
        volume: Optional[app_commands.Range[int, 0, 200]] = None
    ):
        if not interaction.guild:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ このコマンドを実行するにはサーバー管理権限が必要です。", ephemeral=True)
            return

        update_kwargs = {}
        if enabled is not None:
            update_kwargs["disaster_alert_enabled"] = 1 if enabled else 0
        if min_scale is not None:
            if min_scale not in [10, 20, 30, 40, 45, 50]:
                await interaction.response.send_message("❌ 最小震度は 10(震度1), 20(震度2), 30(震度3), 40(震度4) のいずれかで指定してください。", ephemeral=True)
                return
            update_kwargs["disaster_min_scale"] = min_scale
        if pref_filter is not None:
            update_kwargs["disaster_pref_filter"] = pref_filter.strip()
        if weather_alert is not None:
            update_kwargs["disaster_alert_weather"] = 1 if weather_alert else 0
        if volume is not None:
            update_kwargs["disaster_volume"] = max(0.0, min(2.0, float(volume) / 100.0))

        if not update_kwargs:
            await interaction.response.send_message("⚠️ 変更する設定項目を指定してください。", ephemeral=True)
            return

        await self.db.update_guild_settings(interaction.guild_id, **update_kwargs)
        await interaction.response.send_message("✅ 防災速報の設定を更新しました！ `/disaster status` で確認できます。", ephemeral=True)

    @app_commands.command(name="reload", description="ボットの設定ファイル (config.yaml) および環境変数をホットリロードします（管理者専用）。")
    async def reload_cmd(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ このコマンドを実行するにはサーバー管理者権限が必要です。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            from src.config import reload_config
            reload_config(self.config)

            if hasattr(self.db, "invalidate_guild_cache"):
                self.db.invalidate_guild_cache()
            if hasattr(self.db, "invalidate_user_cache"):
                self.db.invalidate_user_cache()

            logger.info(f"Configuration reloaded by {interaction.user} (ID: {interaction.user.id}) in guild {interaction.guild_id}")
            await interaction.followup.send("✅ 設定（config.yaml / .env）をホットリロードしました！", ephemeral=True)
        except Exception as e:
            logger.error(f"Failed to reload config via Discord command: {e}")
            await interaction.followup.send(f"❌ 設定のリロードに失敗しました: {e}", ephemeral=True)

    # --- 読み上げ除外プレフィックス設定 (/ignore) ---
    ignore_group = app_commands.Group(name="ignore", description="特定の文字から始まるテキストの読み上げ除外設定")

    @ignore_group.command(name="list", description="このサーバーで読み上げを行わないプレフィックス（開始文字）の一覧を表示します。")
    async def ignore_list(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.db.get_guild_settings(interaction.guild_id, self.config)
        prefixes = settings.get("ignore_prefixes", [";", "；"])
        dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8080") or "http://localhost:8080"

        embed = discord.Embed(
            title="🚫 読み上げ除外プレフィックス設定状況",
            description="これらの文字・記号から始まるメッセージは、読み上げを行わずにスキップされます。",
            color=discord.Color.blue()
        )
        if prefixes:
            items_str = " ".join([f"`{p}`" for p in prefixes])
            embed.add_field(name=f"登録済みプレフィックス ({len(prefixes)}件)", value=items_str, inline=False)
        else:
            embed.add_field(name="登録済みプレフィックス", value="*(なし - 全てのメッセージが読み上げられます)*", inline=False)

        embed.add_field(
            name="💡 コマンド一覧",
            value=(
                "• 追加: `/ignore add prefix:;`\n"
                "• 削除: `/ignore delete prefix:;`\n"
                "• 一括設定: `/ignore set prefixes:; , ； , //`\n"
                "• 初期化: `/ignore reset`\n"
                f"• WebUI: **[Webダッシュボード]({dashboard_url})** からも設定可能"
            ),
            inline=False
        )
        await interaction.response.send_message(embed=embed)

    @ignore_group.command(name="add", description="読み上げを除外する開始文字・記号を追加します（管理者専用）。")
    @app_commands.describe(prefix="除外する開始文字（例: ; や // など）")
    async def ignore_add(self, interaction: discord.Interaction, prefix: str):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.db.get_guild_settings(interaction.guild_id, self.config)
        admin_role_id = int(settings.get("admin_role_id", 0) or 0)
        has_admin_role = any(r.id == admin_role_id for r in getattr(interaction.user, "roles", [])) if admin_role_id > 0 else False
        is_admin = interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild or has_admin_role
        if not is_admin:
            await interaction.response.send_message("❌ このコマンドを実行するにはサーバー管理権限（または管理者ロール）が必要です。", ephemeral=True)
            return

        p = prefix.strip()
        if not p:
            await interaction.response.send_message("❌ 有効なプレフィックス文字を入力してください。", ephemeral=True)
            return
        if len(p) > 20:
            await interaction.response.send_message("❌ プレフィックスは最大20文字までです。", ephemeral=True)
            return

        current_prefixes = list(settings.get("ignore_prefixes", [";", "；"]))
        if p in current_prefixes:
            await interaction.response.send_message(f"⚠️ `{p}` は既に除外プレフィックスに登録されています。", ephemeral=True)
            return

        current_prefixes.append(p)
        await self.db.update_guild_settings(interaction.guild_id, ignore_prefixes=current_prefixes)

        items_str = " ".join([f"`{x}`" for x in current_prefixes])
        embed = discord.Embed(
            title="✅ 読み上げ除外プレフィックスを追加しました",
            description=f"メッセージの先頭が `{p}` で始まる場合は読み上げをスキップします。",
            color=discord.Color.green()
        )
        embed.add_field(name="現在の登録一覧", value=items_str, inline=False)
        await interaction.response.send_message(embed=embed)

    @ignore_group.command(name="delete", description="読み上げを除外する開始文字・記号を削除します（管理者専用）。")
    @app_commands.describe(prefix="削除する開始文字")
    async def ignore_delete(self, interaction: discord.Interaction, prefix: str):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.db.get_guild_settings(interaction.guild_id, self.config)
        admin_role_id = int(settings.get("admin_role_id", 0) or 0)
        has_admin_role = any(r.id == admin_role_id for r in getattr(interaction.user, "roles", [])) if admin_role_id > 0 else False
        is_admin = interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild or has_admin_role
        if not is_admin:
            await interaction.response.send_message("❌ このコマンドを実行するにはサーバー管理権限（または管理者ロール）が必要です。", ephemeral=True)
            return

        p = prefix.strip()
        current_prefixes = list(settings.get("ignore_prefixes", [";", "；"]))
        if p not in current_prefixes:
            await interaction.response.send_message(f"❌ `{p}` は除外プレフィックスに登録されていません。", ephemeral=True)
            return

        current_prefixes.remove(p)
        await self.db.update_guild_settings(interaction.guild_id, ignore_prefixes=current_prefixes)

        items_str = " ".join([f"`{x}`" for x in current_prefixes]) if current_prefixes else "*(なし)*"
        embed = discord.Embed(
            title="🗑️ 読み上げ除外プレフィックスを削除しました",
            description=f"`{p}` を除外リストから削除しました。",
            color=discord.Color.orange()
        )
        embed.add_field(name="現在の登録一覧", value=items_str, inline=False)
        await interaction.response.send_message(embed=embed)

    @ignore_group.command(name="set", description="読み上げを除外するプレフィックスを一括設定します（管理者専用）。")
    @app_commands.describe(prefixes="カンマや空白で区切って複数指定（例: ; , ； , //）。空にする場合は「none」")
    async def ignore_set(self, interaction: discord.Interaction, prefixes: str):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.db.get_guild_settings(interaction.guild_id, self.config)
        admin_role_id = int(settings.get("admin_role_id", 0) or 0)
        has_admin_role = any(r.id == admin_role_id for r in getattr(interaction.user, "roles", [])) if admin_role_id > 0 else False
        is_admin = interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild or has_admin_role
        if not is_admin:
            await interaction.response.send_message("❌ このコマンドを実行するにはサーバー管理権限（または管理者ロール）が必要です。", ephemeral=True)
            return

        raw = prefixes.strip()
        if raw.lower() in ("none", "なし", "クリア", "clear"):
            new_prefixes = []
        else:
            import re
            parts = [p.strip() for p in re.split(r'[,、\s]+', raw) if p.strip()]
            new_prefixes = []
            for p in parts:
                if p not in new_prefixes:
                    new_prefixes.append(p[:20])

        await self.db.update_guild_settings(interaction.guild_id, ignore_prefixes=new_prefixes)
        items_str = " ".join([f"`{x}`" for x in new_prefixes]) if new_prefixes else "*(なし)*"
        embed = discord.Embed(
            title="✅ 読み上げ除外プレフィックスを一括更新しました",
            color=discord.Color.green()
        )
        embed.add_field(name="現在の登録一覧", value=items_str, inline=False)
        await interaction.response.send_message(embed=embed)

    @ignore_group.command(name="reset", description="読み上げ除外プレフィックスを初期設定（; と ；）にリセットします（管理者専用）。")
    async def ignore_reset(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            await interaction.response.send_message("❌ サーバー内で実行してください。", ephemeral=True)
            return

        settings = await self.db.get_guild_settings(interaction.guild_id, self.config)
        admin_role_id = int(settings.get("admin_role_id", 0) or 0)
        has_admin_role = any(r.id == admin_role_id for r in getattr(interaction.user, "roles", [])) if admin_role_id > 0 else False
        is_admin = interaction.user.guild_permissions.administrator or interaction.user.guild_permissions.manage_guild or has_admin_role
        if not is_admin:
            await interaction.response.send_message("❌ このコマンドを実行するにはサーバー管理権限（または管理者ロール）が必要です。", ephemeral=True)
            return

        default_prefixes = [";", "；"]
        await self.db.update_guild_settings(interaction.guild_id, ignore_prefixes=default_prefixes)
        items_str = " ".join([f"`{x}`" for x in default_prefixes])
        embed = discord.Embed(
            title="🔄 読み上げ除外プレフィックスを初期化しました",
            description="初期値（セミコロン「;」「；」）にリセットしました。",
            color=discord.Color.green()
        )
        embed.add_field(name="現在の登録一覧", value=items_str, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="alert_test", description="【Bot管理者専用】指定した分類のエラー検知DM通知をテスト送信します。")
    @app_commands.describe(target="テスト通知する障害分類を選択してください")
    @app_commands.choices(target=[
        app_commands.Choice(name="🤖 Bot本体のエラー", value="bot"),
        app_commands.Choice(name="🎙️ Masterエンジンのエラー", value="master_engine"),
        app_commands.Choice(name="🖥️ Workerノードのエラー", value="worker"),
        app_commands.Choice(name="⚡ Workerエンジンのエラー", value="worker_engine"),
    ])
    async def alert_test(self, interaction: discord.Interaction, target: str):
        ADMIN_ID = 310034034690359308
        if interaction.user.id != ADMIN_ID:
            await interaction.response.send_message("❌ このコマンドはBot管理者（ユーザーID: 310034034690359308）のみ実行できます。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        from src.utils.alert_service import (
            alert_service,
            CATEGORY_BOT,
            CATEGORY_MASTER_ENGINE,
            CATEGORY_WORKER,
            CATEGORY_WORKER_ENGINE,
            SEVERITY_ERROR,
            SEVERITY_WARNING,
        )

        test_cases = {
            "bot": {
                "title": "[テスト] Bot内部例外の検知",
                "message": "これはBot本体エラー検知のテスト通知です。未処理の例外やイベントエラー、DB接続障害などが検知された際に即座に送信されます。",
                "category": CATEGORY_BOT,
                "severity": SEVERITY_ERROR,
                "details": "Traceback (most recent call last):\n  File \"src/main.py\", line 42, in test_fn\n    raise RuntimeError('Bot internal test exception')"
            },
            "master_engine": {
                "title": "[テスト] Masterエンジン障害の検知",
                "message": "これはMaster自機エンジンのエラー検知テスト通知です。VOICEVOX/SHAREVOX/COEIROINKの応答不能やタイムアウト、Edge-TTSへの退避時に送信されます。",
                "category": CATEGORY_MASTER_ENGINE,
                "severity": SEVERITY_ERROR,
                "node_name": "Master (自機)",
                "engine_name": "VOICEVOX",
                "details": "TTSConnectionError: VOICEVOX synthesis timed out on http://voicevox:50021 after 10.0s"
            },
            "worker": {
                "title": "[テスト] Workerノード途絶の検知",
                "message": "これはWorkerノードエラー検知のテスト通知です。分散Workerマシンからのハートビート途絶やオフライン化、完全再構成失敗時に送信されます。",
                "category": CATEGORY_WORKER,
                "severity": SEVERITY_WARNING,
                "node_name": "GPU-Worker-01 (RTX 3090)",
                "engine_name": "WorkerAgent",
                "details": "HeartbeatTimeout: No heartbeat received from worker_GPU-01 for 68.4s (> 65s)"
            },
            "worker_engine": {
                "title": "[テスト] Workerエンジン障害・隔離の検知",
                "message": "これはWorker上のTTSエンジン障害検知のテスト通知です。外部GPUノード上のVOICEVOX/SHAREVOX/COEIROINKの推論失敗時に送信されます。",
                "category": CATEGORY_WORKER_ENGINE,
                "severity": SEVERITY_ERROR,
                "node_name": "Compute-Worker-02",
                "engine_name": "COEIROINK",
                "details": "aiohttp.ClientResponseError: 500, message='Internal Server Error during inference query'"
            }
        }

        tc = test_cases.get(target, test_cases["bot"])
        success = await alert_service.send_alert(
            title=tc["title"],
            message=tc["message"],
            category=tc["category"],
            severity=tc["severity"],
            node_name=tc.get("node_name"),
            engine_name=tc.get("engine_name"),
            details=tc.get("details"),
            cooldown_seconds=0.0
        )

        if success:
            await interaction.followup.send(f"✅ 管理者DMへ `{tc['category']}` のテスト通知を送信しました！DMをご確認ください。", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ 管理者DMへの送信に失敗しました。Botコンソールログを確認してください。", ephemeral=True)



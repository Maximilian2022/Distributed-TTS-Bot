import os
import sys
from pathlib import Path

# プロジェクトルート (/app) を sys.path の先頭に追加
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import signal
import asyncio
import time
import logging
from logging.handlers import RotatingFileHandler
import discord
from discord.ext import commands
from typing import Optional, List, Tuple, Dict, Any

from src.config import load_config, AppConfig
from src.database import Database
from src.cache_manager import CacheManager
from src.tts.manager import TTSManager
from src.cogs.vc_handler import VCHandlerCog
from src.cogs.commands import CommandsCog
from src.cogs.member_cache_updater import MemberCacheUpdaterCog
from src.web.server import WebServer
from src.disaster.service import DisasterAlertService
from src.utils.log_handler import TwoStageZstdRotatingHandler
from src.resource_manager import LocalEngineResourceManager
from src.utils.async_task import safe_create_task

def setup_logger(bot_log_path: str, web_log_path: str) -> logging.Logger:
    """コンソールと logs/bot.log、さらに WebUI 用の logs/web.log を設定 (2段階ハイブリッドローテーション対応)"""
    Path(bot_log_path).parent.mkdir(parents=True, exist_ok=True)
    Path(web_log_path).parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 1. コンソールハンドラ (標準出力)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # 2. bot.log ハンドラ (最大3000行、直近100行保持、100MB超過時zstd圧縮、10世代、1GB上限清掃)
    bot_file_handler = TwoStageZstdRotatingHandler(
        str(bot_log_path),
        max_lines=3000,
        keep_lines=100,
        archive_max_bytes=100 * 1024 * 1024,
        max_archives=10,
        dir_max_bytes=1024 * 1024 * 1024,
        encoding="utf-8"
    )
    bot_file_handler.setLevel(logging.INFO)
    bot_file_handler.setFormatter(formatter)

    # aiohttp.access のアクセスログは bot.log には含めず web.log のみに記録
    class ExcludeAiohttpAccessFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return record.name != "aiohttp.access"

    bot_file_handler.addFilter(ExcludeAiohttpAccessFilter())
    root_logger.addHandler(bot_file_handler)

    # 3. web.log ハンドラ (WebUIアクセス・認証専用ログ)
    web_file_handler = TwoStageZstdRotatingHandler(
        str(web_log_path),
        max_lines=3000,
        keep_lines=100,
        archive_max_bytes=100 * 1024 * 1024,
        max_archives=10,
        dir_max_bytes=1024 * 1024 * 1024,
        encoding="utf-8"
    )
    web_file_handler.setLevel(logging.INFO)
    web_file_handler.setFormatter(formatter)

    # Web関連ロガーに専用アタッチ
    for logger_name in ["src.web", "aiohttp.access", "aiohttp.server", "aiohttp.web"]:
        w_logger = logging.getLogger(logger_name)
        w_logger.addHandler(web_file_handler)

    # 4. 音声接続リトライ時の一時的エラーを WARNING に正常化するフィルタ
    class VoiceRetryFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if "Failed to connect to voice... Retrying in" in record.getMessage():
                record.levelno = logging.WARNING
                record.levelname = "WARNING"
                record.exc_info = None
            return True

    retry_filter = VoiceRetryFilter()
    root_logger.addFilter(retry_filter)
    console_handler.addFilter(retry_filter)
    bot_file_handler.addFilter(retry_filter)
    logging.getLogger("discord.voice_state").addFilter(retry_filter)

    return root_logger

from src.bot_pool import BotPoolManager

class TTSBot(commands.Bot):
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        cache_manager: CacheManager,
        tts_manager: TTSManager,
        bot_index: int = 1,
        bot_pool: Optional[BotPoolManager] = None
    ):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.messages = True
        intents.message_content = True

        super().__init__(
            command_prefix=config.command_prefix,
            intents=intents,
            help_command=None
        )
        self.app_config = config
        self.db = database
        self.cache_manager = cache_manager
        self.tts_manager = tts_manager
        self.bot_index = bot_index
        self.bot_pool = bot_pool
        self.cleanup_task: Optional[asyncio.Task] = None
        self.disaster_service: Optional[DisasterAlertService] = None
        self.prewarm_service: Optional[Any] = None

        self.web_server = getattr(bot_pool, "web_server", None)
        if self.bot_index == 1:
            from src.cache_prewarm import IntelligentPrewarmService
            self.prewarm_service = IntelligentPrewarmService(
                tts_manager=self.tts_manager,
                cache_manager=self.cache_manager,
                db=self.db,
                config=config
            )
            self.disaster_service = getattr(bot_pool, "disaster_service", None) or DisasterAlertService(bot_pool=bot_pool, db=self.db, config=config)

    async def setup_hook(self) -> None:
        # 1. メインBot (1号機) のみの初期化処理
        if self.bot_index == 1:
            # 障害検知・管理者DM通知サービスの初期化
            from src.utils.alert_service import alert_service
            alert_service.set_bot(self)

            # データベース初期化 (WALモード・マイグレーション)
            await self.db.init_db()
            # レイテンシトラッカーへのDB連携 & 過去計測履歴の復元
            from src.utils.latency_tracker import latency_tracker
            latency_tracker.set_db(self.db)
            saved_latencies = await self.db.get_recent_playback_latencies(limit=200)
            if saved_latencies:
                latency_tracker.restore_from_history(saved_latencies)
            # 定期キャッシュクリーンアップタスク
            self.cleanup_task = safe_create_task(self._periodic_cache_cleanup(), name="periodic_cache_cleanup")
            # 起動時のキャッシュ整合性スキャン（破損・無音の自己修復）を非同期タスクで実行
            safe_create_task(self._startup_cache_integrity_scan(), name="startup_cache_integrity_scan")
            # 防災速報常駐監視サービス起動
            if self.disaster_service:
                self.disaster_service.start()
            # ワーカーノードログ自動集約サービス起動 (各Workerから定期ログ収集・ファイル保存)
            from src.utils.worker_log_collector import worker_log_collector
            safe_create_task(worker_log_collector.start(), name="worker_log_collector_start")
            # 知能的プリキャッシュサービス起動 (アイドル時の自動キャッシュウォーム)
            if self.prewarm_service:
                safe_create_task(self.prewarm_service.start(), name="intelligent_prewarm_start")

        # スラッシュコマンドエラーハンドラを登録
        self.tree.on_error = self.on_tree_error

        # 2. VCHandlerCogの登録 (各Botが自身のVC/キューを独立管理)
        vc_cog = VCHandlerCog(
            bot=self,
            config=self.app_config,
            database=self.db,
            cache_manager=self.cache_manager,
            tts_manager=self.tts_manager,
            bot_index=self.bot_index,
            bot_pool=self.bot_pool
        )
        await self.add_cog(vc_cog)

        # 3. メインBot (1号機) のみの追加コンポーネント
        if self.bot_index == 1:
            # CommandsCog の登録 (スラッシュコマンド受付 & 動的ルーティング)
            cmd_cog = CommandsCog(
                bot=self,
                config=self.app_config,
                database=self.db,
                cache_manager=self.cache_manager,
                vc_handler=vc_cog,
                bot_pool=self.bot_pool,
                disaster_service=self.disaster_service
            )
            await self.add_cog(cmd_cog)

            # メンバーキャッシュスロー巡回タスク (1号機のみが代表してバックグラウンド実行)
            cache_updater_cog = MemberCacheUpdaterCog(
                bot=self,
                database=self.db,
                bot_pool=self.bot_pool
            )
            await self.add_cog(cache_updater_cog)

            # スラッシュコマンドの同期 (1号機のみが代表して同期)
            safe_create_task(self._safe_sync_commands(), name="safe_sync_commands")

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        """Discord.py イベント内の未処理エラーをキャッチして管理者DM通知"""
        import traceback
        tb = traceback.format_exc()
        logging.error(f"Unhandled error in event {event_method}:\n{tb}")
        try:
            from src.utils.alert_service import alert_service, CATEGORY_BOT, SEVERITY_ERROR
            alert_service.notify(
                title=f"Botイベント未処理例外 ({event_method})",
                message=f"Bot #{self.bot_index} のイベント `{event_method}` 処理中に未処理の例外が発生しました。",
                category=CATEGORY_BOT,
                severity=SEVERITY_ERROR,
                details=tb
            )
        except Exception as e:
            logging.debug(f"Failed to send on_error alert: {e}")

    async def on_tree_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError) -> None:
        """スラッシュコマンド実行時エラーをキャッチして管理者DM通知"""
        import traceback
        tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        cmd_name = interaction.command.name if interaction.command else "不明なコマンド"
        logging.error(f"Slash command error in /{cmd_name}: {error}\n{tb}")
        try:
            from src.utils.alert_service import alert_service, CATEGORY_BOT, SEVERITY_ERROR
            alert_service.notify(
                title=f"スラッシュコマンド例外 (/{cmd_name})",
                message=f"ユーザー `{interaction.user}` による `/{cmd_name}` の実行中にエラーが発生しました。\n**エラー**: {error}",
                category=CATEGORY_BOT,
                severity=SEVERITY_ERROR,
                details=tb
            )
        except Exception as e:
            logging.debug(f"Failed to send on_tree_error alert: {e}")

        # ユーザー側へのエラー応答
        try:
            msg = "❌ コマンドの実行中にエラーが発生しました。管理者に障害通知が送信されました。"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """プレフィックスコマンド実行時エラーのハンドリング"""
        if isinstance(error, commands.CommandNotFound):
            # 未登録コマンド（例: !shw や他Bot向けプレフィックス）は無害なため無視
            return
        logging.warning(f"Command error in {ctx.command}: {error}")

    async def _safe_sync_commands(self) -> None:
        """スラッシュコマンドを非同期で安全に同期 (レートリミット時も他処理を阻害しない)"""
        logging.info("Syncing application slash commands in background...")
        try:
            synced = await self.tree.sync()
            logging.info(f"Successfully synced {len(synced)} slash commands.")
        except Exception as e:
            logging.error(f"Failed to sync slash commands: {e}")

    async def _periodic_cache_cleanup(self) -> None:
        """バックグラウンドで定期的にキャッシュの容量制限 (10GB) および DB WAL チェックポイント・最適化を実行"""
        interval = self.app_config.cache_cleanup_interval_seconds
        while not self.is_closed():
            try:
                await asyncio.sleep(interval)
                await self.cache_manager.enforce_capacity_limit()
                if hasattr(self, "db") and self.db:
                    await self.db.maintenance()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logging.error(f"Error in periodic cache cleanup task: {e}")

    async def _startup_cache_integrity_scan(self) -> None:
        """起動後にバックグラウンドでキャッシュ健全性スキャン（破損・無音検出＆自己修復）を実行"""
        await asyncio.sleep(5)  # 起動直後のCPU負荷分散
        try:
            logging.info("Starting background cache integrity check (corrupted & silent files)...")
            res = await self.cache_manager.scan_and_cleanup_corrupted()
            logging.info(
                f"Cache integrity check completed: {res['total_scanned']} scanned, "
                f"{res['healthy']} healthy, {res['corrupted']} corrupted, {res['silent']} silent, {res['removed']} removed."
            )
        except Exception as e:
            logging.warning(f"Error during startup cache integrity scan: {e}")

    async def on_ready(self) -> None:
        logging.info(f"=== [Bot #{self.bot_index}] connected as {self.user} (ID: {self.user.id}) ===")
        logging.info(f"[Bot #{self.bot_index}] Connected to {len(self.guilds)} guilds.")
        for guild in self.guilds:
            logging.info(f" - [Bot #{self.bot_index}] Guild: {guild.name} (ID: {guild.id})")

    async def close(self) -> None:
        logging.info(f"Shutting down Bot #{self.bot_index} safely...")

        # 再生待ちキューの安全な退避保全 (Graceful Shutdown)
        vc_cog = self.get_cog("VCHandlerCog")
        if vc_cog:
            if hasattr(vc_cog, "drain_and_save_all"):
                try:
                    await vc_cog.drain_and_save_all()
                except Exception as e:
                    logging.error(f"Error draining and preserving queues for Bot #{self.bot_index}: {e}")
            elif hasattr(vc_cog, "save_all_pending_queues"):
                try:
                    await vc_cog.save_all_pending_queues()
                except Exception as e:
                    logging.error(f"Error preserving pending queues for Bot #{self.bot_index}: {e}")

        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
        if self.bot_index == 1:
            if self.prewarm_service:
                try:
                    await self.prewarm_service.stop()
                except Exception as e:
                    logging.error(f"Error stopping IntelligentPrewarmService: {e}")
        await super().close()
        logging.info(f"Bot #{self.bot_index} shutdown complete.")

async def main():
    config = load_config()
    setup_logger(config.log_file, config.web_log_file)

    logging.info("Starting Discord TTS Bot Pool (Python 3.14)...")

    # 1. 共有リソースの生成 & データベース先行初期化
    db = Database(config.db_path)
    await db.init_db()

    # DBに保存された認証情報およびBotトークンを config に動的適用
    try:
        db_tokens = await db.get_bot_tokens()
        credentials = await db.get_all_credentials()
        config.apply_db_credentials(credentials, db_tokens)
    except Exception as e:
        logging.warning(f"Failed to load credentials from DB: {e}")

    cache_manager = CacheManager(
        cache_dir=config.cache_dir,
        max_size_bytes=config.cache_max_size_bytes,
        target_size_bytes=config.cache_target_size_bytes,
        voicevox_cache_dir=config.voicevox_cache_dir,
        voicevox_max_size_bytes=config.voicevox_cache_max_size_bytes,
        voicevox_target_size_bytes=config.voicevox_cache_target_size_bytes
    )
    resource_manager = LocalEngineResourceManager(bot=None, config=config, db=db)
    tts_manager = TTSManager(config, cache_manager, db, resource_manager=resource_manager)
    resource_manager.tts_manager = tts_manager

    # 2. BotPoolManager の初期化 & Botインスタンス動的生成ファクトリの登録
    bot_pool = BotPoolManager(config, db, cache_manager, tts_manager)

    def create_bot(bot_idx: int) -> TTSBot:
        return TTSBot(
            config=config,
            database=db,
            cache_manager=cache_manager,
            tts_manager=tts_manager,
            bot_index=bot_idx,
            bot_pool=bot_pool
        )
    bot_pool.set_bot_factory(create_bot)
    resource_manager.bot_pool = bot_pool

    # 3. 防災速報常駐監視サービス & WebServer の先行起動 (トークン0件でも常時起動)
    disaster_service = DisasterAlertService(bot_pool=bot_pool, db=db, config=config)
    bot_pool.disaster_service = disaster_service

    web_server = WebServer(
        bot=None,
        config=config,
        database=db,
        cache_manager=cache_manager,
        tts_manager=tts_manager,
        bot_pool=bot_pool,
        disaster_service=disaster_service
    )
    bot_pool.web_server = web_server

    try:
        await web_server.start()
        logging.info(f"🌐 [WebUI] Server successfully started on http://{config.web_host}:{config.web_port}")
    except Exception as e:
        logging.error(f"Failed to start WebUI server: {e}")

    # トークン確認 (0件でも即終了せずWebUIでのセットアップを案内)
    tokens = config.get_discord_tokens()
    if not tokens:
        logging.warning("⚠️  [SETUP WIZARD REQUIRED] Discord Bot トークンが登録されていません。")
        logging.warning(f"👉 ブラウザで http://localhost:{config.web_port} にアクセスし、管理パスワードでログインしてBotトークンを登録してください。")
    else:
        logging.info(f"Configured Bot count: {len(tokens)} (Indices: {[t[0] for t in tokens]})")

    # 個人設定ピン留めモデル (リリンちゃん等) を接続中全Workerへブロードキャスト先行昇格
    async def broadcast_pinned_models_to_workers():
        try:
            await asyncio.sleep(8.0)  # Worker自動登録の完了を待機
            async with db.get_connection() as conn:
                async with conn.execute(
                    "SELECT DISTINCT speaker_id FROM ("
                    "  SELECT speaker_id FROM user_settings WHERE speaker_id LIKE 'ci_%'"
                    "  UNION"
                    "  SELECT speaker_id FROM guild_user_settings WHERE speaker_id LIKE 'ci_%'"
                    ")"
                ) as cursor:
                    rows = await cursor.fetchall()
            pinned_models = set()
            for (spk_id,) in rows:
                v_info = config.voices.get(spk_id)
                if v_info and getattr(v_info, "voice_name", None):
                    spk_uuid = v_info.voice_name.split("_")[0]
                    pinned_models.add(spk_uuid)
                else:
                    pinned_models.add(spk_id)
            if pinned_models:
                logging.info(f"📌 [WorkerRAM] 個人設定ピン留めモデルを全Workerへブロードキャスト: {list(pinned_models)}")
                for p_model in pinned_models:
                    await resource_manager.broadcast_ram_promote(p_model, pinned=True)
        except Exception as e:
            logging.debug(f"Broadcast pinned models exception: {e}")

    # バックグラウンドで全モデルの試聴用サンプル音声を事前キャッシュ生成（Gateway接続安定後に低優先度で実行）
    async def delayed_precache_previews():
        try:
            await asyncio.sleep(15.0)  # BotのGateway接続完了を待機
            await tts_manager.precache_all_previews(interval_delay=0.1)
        except Exception as e:
            logging.debug(f"Background preview pre-caching exception: {e}")

    def start_active_background_tasks():
        """Active昇格時または通常起動時にバックグラウンド監視・事前ウォームタスクを開始"""
        resource_manager.set_standby_mode(False)
        resource_manager.start_idle_monitor()
        safe_create_task(broadcast_pinned_models_to_workers(), name="broadcast_pinned_models_to_workers")
        safe_create_task(delayed_precache_previews(), name="delayed_precache_previews")

    # ローカルTTSエンジン（VOICEVOX / SHAREVOX / COEIROINK）の話者・スタイルを動的取得して config.voices に登録
    for engine_name, init_fn in [
        ("VOICEVOX", tts_manager.initialize_voicevox),
        ("SHAREVOX", tts_manager.initialize_sharevox),
        ("COEIROINK", tts_manager.initialize_coeiroink),
    ]:
        try:
            await init_fn()
        except Exception as e:
            logging.warning(f"Could not connect to {engine_name} on startup (engine might be stopped): {e}")

    # シグナルハンドラ設定 (全Botおよび全サービスを協調して安全に終了)
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def loop_exception_handler(l, context):
        msg = context.get("message", "Unhandled asyncio exception")
        exc = context.get("exception")
        import traceback
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc else str(context)
        logging.error(f"Asyncio Loop Unhandled Exception: {msg}\n{details}")
        try:
            from src.utils.alert_service import alert_service, CATEGORY_BOT, SEVERITY_ERROR
            alert_service.notify(
                title="非同期タスク未処理例外",
                message=f"イベントループで未処理例外が発生しました: {msg}",
                category=CATEGORY_BOT,
                severity=SEVERITY_ERROR,
                details=details
            )
        except Exception:
            pass

    loop.set_exception_handler(loop_exception_handler)

    async def shutdown_all():
        logging.info("Received termination signal. Closing all bots and services safely...")
        # 1. WebServer の停止
        try:
            await web_server.stop()
        except Exception as e:
            logging.error(f"Error stopping WebServer: {e}")

        # 2. 防災サービスの停止
        try:
            disaster_service.stop()
        except Exception:
            pass

        # 3. 稼働中Botの全停止
        running_indices = list(bot_pool.bots.keys())
        if running_indices:
            await asyncio.gather(*(bot_pool.stop_bot(idx) for idx in running_indices), return_exceptions=True)

        # 4. TTSマネージャーおよびDBのクローズ
        try:
            await tts_manager.close()
        except Exception:
            pass
        try:
            await db.close()
        except Exception:
            pass
        logging.info("Process resources closed safely.")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: safe_create_task(shutdown_all(), name="shutdown_all"))
        except (NotImplementedError, RuntimeError):
            pass

    # スタンバイモード判定
    standby_trigger_file = Path("/app/data/standby_trigger")
    has_standby_trigger = standby_trigger_file.exists()
    env_standby = os.getenv("TTSBOT_STANDBY", "0").lower() in ("1", "true", "yes")

    current_node = os.getenv("TTSBOT_NODE", "ubuntu").lower()
    current_slot = os.getenv("TTSBOT_SLOT", "").lower()
    current_target_id = f"{current_node}:{current_slot}" if current_node and current_slot else current_slot
    active_slot_file = Path("/app/data/active_slot.txt")

    if has_standby_trigger:
        # rolling_update.sh による明示的なスタンバイ起動: 確実にスタンバイに入りウォームアップを実施
        is_standby = True
        logging.info("⏸️  [STANDBY TRIGGER] /app/data/standby_trigger を検知したため、スタンバイ(ウォームアップ)モードで起動します。")
    elif env_standby:
        # 環境変数のみでスタンバイ指定されている場合: ホストOS再起動時の巻き込まれトラップを検証
        is_standby = True
        if current_target_id and active_slot_file.exists():
            try:
                saved_active = active_slot_file.read_text(encoding="utf-8").strip().lower()
                # 副系ノード（Debian等）は環境変数 TTSBOT_STANDBY=1 の場合は常にスタンバイ起動とする (Ubuntu主系のみ自動復旧)
                if current_node == "ubuntu" and (saved_active == current_target_id or saved_active == current_slot):
                    logging.info(f"✨ [AUTO RECOVERY] 自ノード・スロット ({current_target_id}) は永続化されたアクティブ情報と一致するため、スタンバイをスキップして即時アクティブ起動します。")
                    is_standby = False
            except Exception as e:
                logging.warning(f"Failed to read active_slot.txt: {e}")
    else:
        # 通常起動
        is_standby = False

    # 形態素解析エンジン (SudachiPy / Janome) の事前ウォームアップを別スレッドで安全に実行 (PERF-WARMUP)
    try:
        from src.utils.text_cleaner import warmup_morphological_tokenizers
        await asyncio.to_thread(warmup_morphological_tokenizers)
    except Exception as e:
        logging.warning(f"Failed to pre-warm morphological tokenizers: {e}")

    # リソースマネージャのスタンバイモード設定 & 通常起動時のバックグラウンドタスク開始
    resource_manager.set_standby_mode(is_standby)
    if not is_standby:
        start_active_background_tasks()

    if is_standby:
        status_file = Path("/tmp/ttsbot_ready")
        activate_file = Path("/tmp/ttsbot_activate")
        active_status_file = Path("/tmp/ttsbot_active")

        # 前回の残留フラグ（アクティブ、アクティベート、レディ）があれば安全に消去
        for f in (active_status_file, activate_file, status_file):
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    pass

        try:
            status_file.write_text("ready", encoding="utf-8")
        except Exception as e:
            logging.warning(f"Failed to write ready status file: {e}")

        logging.info("⏸️  [STANDBY MODE] Warm-up completed! Waiting for activation signal (SIGUSR1 or /tmp/ttsbot_activate)...")

        activate_event = asyncio.Event()

        def handle_activate_signal():
            logging.info("⚡ [STANDBY MODE] Received SIGUSR1 activation signal!")
            activate_event.set()

        try:
            loop.add_signal_handler(signal.SIGUSR1, handle_activate_signal)
        except (AttributeError, NotImplementedError):
            pass

        # 自律昇格ガード:
        # 副系ノード（Debian等）は主系（Ubuntu）の障害発生時に HA Watchdog から明示的な
        # シグナル（SIGUSR1 または /tmp/ttsbot_activate）を受けるまで永久に待機しなければならない。
        # したがって自律タイムアウト昇格は、主系ノード（Ubuntu）のローリングアップデート時（has_standby_trigger）のみに限定する。
        allow_auto_promote = (current_node == "ubuntu") and has_standby_trigger
        STANDBY_AUTO_PROMOTE_TIMEOUT_SECONDS = 60.0

        async def watch_activate_file():
            start_wait_ts = time.time()
            while not activate_event.is_set():
                if activate_file.exists():
                    logging.info("⚡ [STANDBY MODE] Detected /tmp/ttsbot_activate file!")
                    try:
                        activate_file.unlink()
                    except Exception:
                        pass
                    activate_event.set()
                    break

                # 60秒放置自律昇格ガード (Ubuntu内ローリングアップデート時の切り替えスクリプト異常終了セーフティネット)
                if allow_auto_promote and (time.time() - start_wait_ts) >= STANDBY_AUTO_PROMOTE_TIMEOUT_SECONDS:
                    logging.warning(
                        f"⏰ [STANDBY SAFETY TIMEOUT] スタンバイ待機が {STANDBY_AUTO_PROMOTE_TIMEOUT_SECONDS:.0f}秒 を超過しました。"
                        "孤立防止セーフティネットに基づき、自律的に Active へ昇格します！"
                    )
                    activate_event.set()
                    break

                await asyncio.sleep(0.3)

        file_watch_task = safe_create_task(watch_activate_file(), name="watch_activate_file")

        # 終了シグナルが来た場合はスタンバイ中でも安全終了
        standby_shutdown_event = asyncio.Event()
        original_shutdown = shutdown_all
        async def shutdown_during_standby():
            standby_shutdown_event.set()
            activate_event.set()
            await original_shutdown()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: safe_create_task(shutdown_during_standby(), name="shutdown_during_standby"))
            except (NotImplementedError, RuntimeError):
                pass

        await activate_event.wait()
        if not file_watch_task.done():
            file_watch_task.cancel()

        if standby_shutdown_event.is_set():
            logging.info("Terminated during standby mode.")
            return

        try:
            if status_file.exists():
                status_file.unlink()
            Path("/tmp/ttsbot_active").write_text("active", encoding="utf-8")
            # スタンバイトリガーファイルを確実に削除
            if standby_trigger_file.exists():
                standby_trigger_file.unlink()
        except Exception:
            pass

        # シグナルハンドラを通常の shutdown_all に戻す
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: safe_create_task(shutdown_all(), name="shutdown_all"))
            except (NotImplementedError, RuntimeError):
                pass

        # Active昇格: スタンバイ解除とバックグラウンドタスク群を開始
        start_active_background_tasks()

        logging.info("🚀 [STANDBY MODE -> ACTIVE] Promoting to active! Connecting all bots to Discord Gateway...")


    # アクティブノード・スロットとして永続記録 (次回ホスト再起動時の自動復旧用)
    if current_target_id:
        try:
            active_slot_file.write_text(current_target_id, encoding="utf-8")
        except Exception as e:
            logging.warning(f"Failed to record active_slot.txt: {e}")

    # 複数Botの段階的起動 (通話中VCセッション担当Botおよび1号機を最優先 0秒起動)
    active_bot_indices = {1}  # 1号機はWebServer・管理代表のため最優先
    try:
        saved_vcs = await db.get_all_active_vcs()
        for svc in (saved_vcs or []):
            b_idx = svc.get("bot_index", 1)
            active_bot_indices.add(b_idx)
        if len(active_bot_indices) > 1:
            logging.info(f"⚡ [Fast Recovery] 通話中VC担当Bot ({list(active_bot_indices)}) を最優先(0秒遅延)で即座にGateway接続します。")
    except Exception as e:
        logging.debug(f"Failed to query active_vcs for priority startup: {e}")

    async def start_worker(idx: int, token_str: str, delay: float):
        if delay > 0:
            await asyncio.sleep(delay)
        logging.info(f"Starting connection for Bot #{idx} (delay={delay:.1f}s)...")
        try:
            await bot_pool.start_bot(idx, token_str)
        except Exception as e:
            logging.error(f"Error starting Bot #{idx}: {e}")

    # 通話中担当Botは 0秒遅延、その他アイドルBotは1.5秒ずつずらしてレートリミット回避
    idle_delay_counter = 0
    for bot_idx, tok in tokens:
        if bot_idx in active_bot_indices:
            bot_delay = 0.0
        else:
            idle_delay_counter += 1
            bot_delay = idle_delay_counter * 1.5
        safe_create_task(start_worker(bot_idx, tok, bot_delay), name=f"start_bot_{bot_idx}")

    # プロセス常駐維持 (終了シグナル受信まで待機)
    await shutdown_event.wait()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Process terminated.")
    except Exception as e:
        logging.exception(f"Fatal unhandled exception in main: {e}")
        sys.exit(1)

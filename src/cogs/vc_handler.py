import asyncio
import collections
import re
import json
import time
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional, Dict, List, Set, Any, Tuple
import discord
from discord.ext import commands

from src.config import AppConfig
from src.database import Database
from src.cache_manager import CacheManager
from src.tts.manager import TTSManager
from src.utils.text_cleaner import TextCleaner
from src.utils.deduplicator import Deduplicator
from src.utils.async_task import safe_create_task
from src.utils.latency_tracker import latency_tracker
from src.utils.latency_profiler import latency_profiler
from src.utils.stream_planner import plan_head_chars
from src.utils.streaming_audio import StreamingAudioSource, get_beep_pcm
from src.utils.gemini_tutor import GeminiTutorClient
from src.utils.avatar_manager import cache_member_info_and_avatar

logger = logging.getLogger(__name__)

# 発言者表示名の括弧除去用 事前コンパイル正規表現 (PERF: ホットパスでのインラインコンパイル排除)
_RE_STRIP_NAME_BRACKETS = re.compile(r"[（\(【\[〔〈《「『][^）\)】\]〕〉》」』]*[）\)】\]〕〉》」』]")
_RE_STRIP_TRAILING_BRACKETS = re.compile(r"[（\(【\[「][^）\)】\]」]*[）\)】\]」]?$")

@dataclass
class QueueItem:
    text: str
    user_id: int
    user_name: str
    speaker_id: str
    pitch: str
    speed: float
    volume: float = 0.8
    author_prefix: Optional[str] = None
    discord_created_at: float = 0.0
    received_at: float = 0.0
    queued_at: float = 0.0
    synth_task: Optional[asyncio.Task] = None
    speculative_timeout: Optional[float] = None
    # 先行合成と再生側で共有する分割結果 (キャッシュキー一致のため。永続化対象外)
    chunks: Optional[List[Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("synth_task", None)
        d.pop("chunks", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueItem":
        valid_keys = set(cls.__annotations__.keys()) - {"synth_task", "chunks"}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

CONSERVATIVE_PLAY_SEC_PER_CHAR = 0.12  # 未投入パーツの再生長見積り (Azure 実測下限。COEIROINK は 0.14〜0.21)
MIN_LOCAL_TRY_SEC = 0.3                # 投機締切の下限 (合成開始からの最低試行時間)


def _conservative_play_sec(chunk: Any) -> float:
    if getattr(chunk, "is_beep", False):
        return float(getattr(chunk, "beep_duration_sec", 0.4) or 0.4)
    return len(getattr(chunk, "text", "") or "") * CONSERVATIVE_PLAY_SEC_PER_CHAR + (getattr(chunk, "pause_after_ms", 0) or 0) / 1000.0


class PlaybackTimeline:
    """
    再生タイムライン基準の投機締切 (最大許容沈黙)。
    各パーツの締切 = そのパーツの再生予定時刻 - クラウド応答マージン。
    - 再生開始前: 順番が来た時刻 + 最大許容沈黙 + 投入済み音声長 + 間の未投入パーツ見積り
    - 再生開始後: 現在 + 未再生バッファ残量 + 間の未投入パーツ見積り
    締切は再生の進行で変わるため、呼び出し側は毎回再評価する。
    """

    def __init__(self, t_turn: float, max_silence: float, az_margin: float, est_play: List[float], stream_source: Any):
        self.t_turn = t_turn
        self.max_silence = max_silence
        self.az_margin = az_margin
        self.est_play = est_play
        self.stream_source = stream_source
        self.fed: Dict[int, float] = {}
        self.t_play: Optional[float] = None

    def mark_fed(self, idx: int, duration_sec: float) -> None:
        self.fed[idx] = duration_sec

    def deadline_for(self, idx: int) -> float:
        unfed_before = sum(self.est_play[j] for j in range(min(idx, len(self.est_play))) if j not in self.fed)
        if self.t_play is None:
            base = self.t_turn + self.max_silence + sum(self.fed.values())
        else:
            base = time.time() + float(getattr(self.stream_source, "buffer_remaining_seconds", 0.0) or 0.0)
        return base + unfed_before - self.az_margin


class SafeAsyncQueue:
    """
    優先割り込み (push_front) と FIFO キュー操作をサポートする安全な非同期キュー。
    asyncio.Queue のプライベート変数（_queue, _getters, _unfinished_tasks等）への
    直接アクセスを完全に排除し、将来の Python アップデート破損リスクを根絶 (B-1)。
    """
    def __init__(self, maxsize: int = 0):
        self.maxsize: int = maxsize
        self._deque: collections.deque = collections.deque()
        self._getters: collections.deque = collections.deque()
        self._unfinished_tasks: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

    def qsize(self) -> int:
        return len(self._deque)

    def empty(self) -> bool:
        return len(self._deque) == 0

    def get_items(self) -> List[Any]:
        """内部デックの安全なリストスナップショット"""
        if not self._deque:
            return []
        return list(self._deque)

    def snapshot(self) -> List[Any]:
        """get_items のエイリアス"""
        if not self._deque:
            return []
        return list(self._deque)

    def clear(self) -> List[Any]:
        """キュー内の全アイテムをクリアして取り出して返す"""
        items = list(self._deque)
        self._deque.clear()
        self._unfinished_tasks = 0
        return items

    def put_nowait(self, item: Any) -> None:
        """キュー末尾にアイテムを即座に追加"""
        if self.maxsize > 0 and len(self._deque) >= self.maxsize:
            raise asyncio.QueueFull()
        self._unfinished_tasks += 1
        while self._getters:
            getter = self._getters.popleft()
            if not getter.done():
                getter.set_result(item)
                return
        self._deque.append(item)

    async def put(self, item: Any) -> None:
        """キュー末尾にアイテムを追加"""
        async with self._lock:
            self.put_nowait(item)

    def push_front(self, item: Any) -> None:
        """キュー最前列にアイテムを優先挿入 (O(1))"""
        self._unfinished_tasks += 1
        while self._getters:
            getter = self._getters.popleft()
            if not getter.done():
                getter.set_result(item)
                return
        self._deque.appendleft(item)

    async def get(self) -> Any:
        """キューからアイテムを取り出し。空の場合は待機（タスクキャンセル耐性付き）"""
        async with self._lock:
            if self._deque:
                return self._deque.popleft()
            loop = asyncio.get_running_loop()
            getter = loop.create_future()
            self._getters.append(getter)

        try:
            return await getter
        except (Exception, asyncio.CancelledError):
            getter.cancel()
            async with self._lock:
                try:
                    self._getters.remove(getter)
                except ValueError:
                    pass
                # キャンセル直前にアイテムが set_result されていた場合、アイテムロストを防ぐためキュー先頭に戻す
                if getter.done() and not getter.cancelled():
                    try:
                        item = getter.result()
                        self._deque.appendleft(item)
                    except Exception:
                        pass
            raise

    def get_nowait(self) -> Any:
        """待機せずに即座にアイテムを取り出し"""
        if self._deque:
            return self._deque.popleft()
        raise asyncio.QueueEmpty()

    def task_done(self) -> None:
        """タスク完了を記録"""
        if self._unfinished_tasks > 0:
            self._unfinished_tasks -= 1


class GuildVCState:
    """ギルドごとのVC接続・キュー状態を管理"""
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.bound_text_channel_id: Optional[int] = None
        self.queue: SafeAsyncQueue = SafeAsyncQueue()
        self.is_playing: bool = False
        self.current_item: Optional[QueueItem] = None
        self.play_task: Optional[asyncio.Task] = None
        self.deduplicator = Deduplicator()
        self.play_finished_event = asyncio.Event()
        # 接続世代カウンタ: disconnect_vc のたびに +1。進行中の connect_vc リトライが
        # 自分の開始時世代と異なることを検知したら後追い接続を中止する (無人VC取り残し対策)
        self.connect_epoch: int = 0
        # 無人VC Watchdog: 最初に無人を検知した時刻 (0.0 = 有人/未検知)
        self.empty_since: float = 0.0

    def get_all_items(self) -> List[QueueItem]:
        """現在キューにある全アイテムを抽出 (シャットダウン退避用)"""
        items = []
        if self.current_item:
            items.append(self.current_item)
        items.extend(self.queue.get_items())
        return items

    def get_queue_texts(self) -> List[str]:
        """現在キューにあるテキスト一覧を取得 (重複抑制判定用)"""
        return [item.text for item in self.queue.get_items()]

    def push_front(self, item: QueueItem) -> None:
        """キューの最前列にアイテムを優先挿入 (O(1))"""
        self.queue.push_front(item)

    def clear(self) -> None:
        """キューと状態のクリア"""
        while not self.queue.empty():
            try:
                item = self.queue.get_nowait()
                if item.synth_task and not item.synth_task.done():
                    item.synth_task.cancel()
                self.queue.task_done()
            except Exception:
                break
        if self.current_item and self.current_item.synth_task and not self.current_item.synth_task.done():
            self.current_item.synth_task.cancel()
        self.is_playing = False
        self.current_item = None
        if self.play_task and not self.play_task.done():
            self.play_task.cancel()
            self.play_task = None

class VCConnectAborted(Exception):
    """接続処理中に VC が無人化した / 切断要求が入ったため接続を中止したことを示す"""


class VCHandlerCog(commands.Cog):
    """
    VC接続・切断制御、メッセージ監視、超低遅延再生キューを統括するCog
    """

    def __init__(
        self,
        bot: commands.Bot,
        config: AppConfig,
        database: Database,
        cache_manager: CacheManager,
        tts_manager: TTSManager,
        bot_index: int = 1,
        bot_pool: Optional[Any] = None
    ):
        self.bot = bot
        self.config = config
        self.db = database
        self.cache_manager = cache_manager
        self.tts_manager = tts_manager
        self.bot_index = bot_index
        self.bot_pool = bot_pool
        self.text_cleaner = TextCleaner(max_length=config.max_text_length)
        self.gemini_tutor = GeminiTutorClient(api_key=config.gemini_api_key, model=config.gemini_model)
        self.guild_states: Dict[int, GuildVCState] = {}

        # 入室プリウォーム・バッチ集約用ステート (同時多人数入室ストーム対策)
        self._pending_prewarm_members: Dict[int, Set[int]] = {}  # guild_id -> set(member_ids)
        self._prewarm_batch_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> debounce task
        self._name_cache_semaphore = asyncio.Semaphore(1)  # 発言者名事前キャッシュ合成の直列化 (通常合成への影響ゼロ化)
        self._auto_leave_task: Optional[asyncio.Task] = None
        # 動的サーバーネーム（ニックネーム）管理ステート
        self._nick_sync_locks: Dict[int, asyncio.Lock] = {}
        self._warned_nick_perms: Set[int] = set()
        self._nick_reconcile_task: Optional[asyncio.Task] = None
        self._auto_join_reconcile_task: Optional[asyncio.Task] = None
        self._pending_bot_leave_tasks: Dict[int, asyncio.Task] = {}
        # 遅延インシデント発生時のワーカーノード状態スナップショット取得元を登録 (1号機のみで十分)
        if bot_index == 1:
            latency_tracker.set_node_snapshot_provider(self._snapshot_worker_nodes)

    # --- 動的サーバーネーム（ニックネーム）同期ロジック ---

    def _default_nick(self, guild: Optional[discord.Guild] = None) -> str:
        """VC未接続時の標準ニックネーム: '{サーバー名} TTS #{self.bot_index}' (最大32文字安全トリム)"""
        suffix = f" TTS #{self.bot_index}"
        max_total_len = 32
        guild_name = guild.name if guild else "サーバー"

        if len(guild_name) + len(suffix) <= max_total_len:
            return f"{guild_name}{suffix}"

        keep_len = max_total_len - len(suffix) - 1
        trimmed = guild_name[:keep_len]
        return f"{trimmed}…{suffix}"

    def _build_vc_nick(self, channel_name: str) -> str:
        """VC名から 32文字以内の '{VC名} TTS' を生成 (末尾超過時は安全にトリム)"""
        suffix = " TTS"
        max_total_len = 32
        if len(channel_name) + len(suffix) <= max_total_len:
            return f"{channel_name}{suffix}"

        # 32文字超過時: 先頭 (32 - 4 - 1 = 27文字) + '…' + ' TTS' (合計32文字)
        keep_len = max_total_len - len(suffix) - 1
        trimmed = channel_name[:keep_len]
        return f"{trimmed}…{suffix}"

    def _get_nick_sync_lock(self, guild_id: int) -> asyncio.Lock:
        """ギルド単位のニックネーム変更排他ロック"""
        if guild_id not in self._nick_sync_locks:
            self._nick_sync_locks[guild_id] = asyncio.Lock()
        return self._nick_sync_locks[guild_id]

    async def _resolve_bound_text_channel(self, guild: discord.Guild, vc_id: int) -> Optional[int]:
        """VCに対応する自動ペア設定テキストチャンネルIDを解決"""
        try:
            guild_settings = await self.db.get_guild_settings(guild.id, self.config)
            pairs = guild_settings.get("auto_join_pairs", [])
            if not pairs:
                avc = int(guild_settings.get("auto_join_vc_id", 0) or 0)
                atxt = int(guild_settings.get("auto_join_text_channel_id", 0) or 0)
                if avc > 0 and atxt > 0:
                    pairs = [{"vc_id": avc, "text_id": atxt}]

            for p in pairs:
                if int(p.get("vc_id", 0)) == vc_id:
                    return int(p.get("text_id", 0))
        except Exception as e:
            logger.warning(f"[Bot #{self.bot_index}] Error resolving bound text channel for VC {vc_id} in '{guild.name}': {e}")
        return None

    async def sync_nickname(self, guild: discord.Guild) -> None:
        """
        対象ギルドにおけるBotのニックネームを「現在のVC接続状態」に合わせて同期。
        VC接続中: '{VC名} TTS'
        VC未接続: '{サーバー名} TTS #{self.bot_index}'
        同名ならAPI呼び出しをスキップし、例外（権限不足やレート制限）発生時も通常処理は絶対に阻害しない。
        """
        if not guild:
            return

        async with self._get_nick_sync_lock(guild.id):
            me = guild.me or (guild.get_member(self.bot.user.id) if self.bot.user else None)
            if not me:
                return

            # 最新のVC状態を多角的に解決:
            # 1. VoiceClient (接続中またはハンドシェイク中)
            # 2. Discord Gateway ボイスステート (me.voice.channel)
            # 3. DB active_vc_sessions (起動時復元進行中のフォールバック)
            active_channel = None
            vc = guild.voice_client
            if vc is not None and vc.channel and (vc.is_connected() or getattr(vc, "_connecting", False)):
                active_channel = vc.channel
            elif me.voice and me.voice.channel:
                active_channel = me.voice.channel
            else:
                # 起動時VC復旧中または接続処理中かをDBセッションから確認
                try:
                    active_vcs = await self.db.get_all_active_vcs()
                    for s in (active_vcs or []):
                        if s.get("guild_id") == guild.id and s.get("bot_index", 1) == self.bot_index:
                            target_vc_id = s.get("voice_channel_id")
                            if target_vc_id:
                                ch = guild.get_channel(target_vc_id)
                                if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                                    if any(not m.bot for m in ch.members):
                                        active_channel = ch
                                        break
                except Exception as e:
                    logger.debug(f"[Bot #{self.bot_index}] Error checking active DB VC in sync_nickname: {e}")

            if active_channel:
                target_nick = self._build_vc_nick(active_channel.name)
            else:
                target_nick = self._default_nick(guild)

            if me.nick == target_nick:
                return

            old_nick = me.nick
            try:
                await me.edit(nick=target_nick, reason="TTS Bot dynamic VC nickname sync")
                logger.info(
                    f"🏷️ [Bot #{self.bot_index}] Synced nickname in '{guild.name}': '{old_nick}' -> '{target_nick}'"
                )
            except discord.Forbidden:
                if guild.id not in self._warned_nick_perms:
                    self._warned_nick_perms.add(guild.id)
                    logger.warning(
                        f"[Bot #{self.bot_index}] Cannot change nickname in '{guild.name}': Missing 'Change Nickname' or 'Manage Nicknames' permission."
                    )
                else:
                    logger.debug(f"[Bot #{self.bot_index}] Missing nickname permission in '{guild.name}' (suppressed)")
            except discord.HTTPException as e:
                logger.warning(
                    f"[Bot #{self.bot_index}] HTTP error syncing nickname in '{guild.name}' from '{old_nick}' to '{target_nick}': {e}"
                )
            except Exception as e:
                logger.warning(
                    f"[Bot #{self.bot_index}] Unexpected error syncing nickname in '{guild.name}' from '{old_nick}' to '{target_nick}': {e}"
                )

    def schedule_nickname_sync(self, guild: Optional[discord.Guild]) -> None:
        """非同期バックグラウンドタスクとしてニックネーム同期をスケジュール (メインフロー遅延ゼロ)"""
        if guild:
            safe_create_task(
                self.sync_nickname(guild),
                name=f"sync_nick_{guild.id}_bot{self.bot_index}_{int(time.time()*1000)}"
            )

    async def _nickname_reconcile_loop(self) -> None:
        """定期整合性チェックループ (5分周期): 予期せぬ切断やAPIエラーで残った名前を自己修復"""
        await self.bot.wait_until_ready()
        logger.info(f"🔄 [Bot #{self.bot_index}] Nickname reconcile loop started (interval=300s)")
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(300.0)
                for guild in list(self.bot.guilds):
                    await self.sync_nickname(guild)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Bot #{self.bot_index}] Error in nickname reconcile loop: {e}")

    def _snapshot_worker_nodes(self) -> List[Dict[str, Any]]:
        """全エンジンプールのノード状態をコンパクトに収集 (インシデント原因調査用)"""
        snap: List[Dict[str, Any]] = []
        for attr in ("voicevox_pool", "sharevox_pool", "coeiroink_pool"):
            pool = getattr(self.tts_manager, attr, None)
            for n in getattr(pool, "nodes", []) or []:
                res = (n.specs or {}).get("resources", {}) or {}
                snap.append({
                    "id": n.id,
                    "hostname": n.hostname,
                    "available": n.is_available,
                    "draining": getattr(n, "draining", False),
                    "active": n.active_count,
                    "max": n.max_concurrent,
                    "engine_running": (n.specs or {}).get("engine_running"),
                    "engine_healthy": (n.specs or {}).get("engine_healthy"),
                    "last_latency_ms": round(getattr(n, "last_latency_ms", 0.0), 1),
                    "cpu": res.get("cpu_percent"),
                    "mem": res.get("mem_percent"),
                    "gpu": res.get("gpu_util_percent"),
                    "vram_mb": res.get("gpu_mem_used_mb"),
                    "ram_models": len((n.specs or {}).get("ram_models", []) or []),
                })
        return snap

    # 無人VC Watchdog 設定
    AUTO_LEAVE_CHECK_INTERVAL = 15.0   # 巡回周期 (秒) - 重複調停を迅速化
    AUTO_LEAVE_GRACE_SECONDS = 25.0    # 無人継続がこの秒数を超えたら退出 (= 2回連続無人検知)

    @staticmethod
    def _count_humans(channel: Optional[discord.abc.GuildChannel]) -> int:
        if channel is None:
            return 0
        return sum(1 for m in getattr(channel, "members", []) if not m.bot)

    def _select_primary_bot_in_vc(
        self, all_bots: List[Tuple[int, commands.Bot]], guild_id: int
    ) -> Tuple[int, commands.Bot]:
        """同一VCに重複接続したBot群から、残すべき最優先Bot（1台）を選定"""
        # 1. 現在再生中 (is_playing) の号機を最優先
        playing_bots = []
        for idx, b in all_bots:
            g = b.get_guild(guild_id)
            if g and g.voice_client and g.voice_client.is_playing():
                playing_bots.append((idx, b))
        if playing_bots:
            return min(playing_bots, key=lambda x: x[0])

        # 2. キューにアイテムがある号機を優先
        queued_bots = []
        for idx, b in all_bots:
            cog = b.get_cog("VCHandlerCog")
            if cog:
                st = cog.get_guild_state(guild_id)
                if not st.queue.empty():
                    queued_bots.append((idx, b))
        if queued_bots:
            return min(queued_bots, key=lambda x: x[0])

        # 3. それ以外は最小号機（1号機優先）
        return min(all_bots, key=lambda x: x[0])

    async def _delayed_duplicate_vc_guard(self, guild: discord.Guild, channel_id: int) -> None:
        """
        VC接続後遅延チェック＆重複排除機構 (接続約4秒後に同一VC内の自プールBot重複を検査・自動調停)
        タイミングのズレや前プロセス残存により同一VCに自プールBotが複数入った場合、
        プライマリ以外の号機を自律的に即時退出させる。
        """
        try:
            await asyncio.sleep(4.0)  # 接続完了から4秒待機 (接続安定待ち)
            if not self.bot_pool:
                return

            guild_obj = self.bot.get_guild(guild.id) or guild
            vc = guild_obj.voice_client

            # 自Botがすでに退出・別チャンネルへ移動している場合は何もしない
            if not vc or not vc.is_connected() or not vc.channel or vc.channel.id != channel_id:
                return

            all_bots = self.bot_pool.find_all_bots_in_vc(guild_obj.id, channel_id)
            if len(all_bots) <= 1:
                return

            # プライマリ号機を決定 (再生中 > キューあり > 最小号機番号)
            primary_idx, primary_bot = self._select_primary_bot_in_vc(all_bots, guild_obj.id)

            if self.bot_index != primary_idx:
                logger.warning(
                    f"🚨 [DelayedDuplicateGuard] Bot #{self.bot_index} detected duplicate bot(s) in VC '{vc.channel.name}' "
                    f"(Active in VC: {[b[0] for b in all_bots]}). "
                    f"Yielding to primary Bot #{primary_idx}. Auto-disconnecting self..."
                )
                await self.disconnect_vc(guild_obj)
            else:
                logger.info(
                    f"✨ [DelayedDuplicateGuard] Bot #{self.bot_index} confirmed as primary in VC '{vc.channel.name}'. "
                    f"Duplicate peers: {[b[0] for b in all_bots if b[0] != primary_idx]}"
                )
                # プライマリ号機側からも、もしVoiceClient未接続なのにMemberとして残存している余剰Botがあれば安全にクリーンアップ
                for extra_idx, extra_bot in all_bots:
                    if extra_idx != primary_idx:
                        extra_cog = extra_bot.get_cog("VCHandlerCog")
                        if extra_cog:
                            extra_g = extra_bot.get_guild(guild_obj.id)
                            if extra_g and extra_g.voice_client:
                                safe_create_task(
                                    extra_cog.disconnect_vc(extra_g),
                                    name=f"force_disconnect_dup_bot{extra_idx}_{guild_obj.id}"
                                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[Bot #{self.bot_index}] Delayed duplicate guard error: {e}")

    async def _auto_leave_watchdog_loop(self) -> None:
        """
        無人VC Watchdog & 重複Bot自動調停 (Deduplication Reconcile):
        1. 同一VCへの自プール複数Bot接続を検知した場合、発話中号機を保護し余剰Botを安全切断。
        2. 無人VCに居座った場合の自動退出セーフティネット。
        """
        await self.bot.wait_until_ready()
        logger.info(f"🐕 [Bot #{self.bot_index}] Auto-leave watchdog started (interval={self.AUTO_LEAVE_CHECK_INTERVAL:.0f}s, grace={self.AUTO_LEAVE_GRACE_SECONDS:.0f}s)")
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(self.AUTO_LEAVE_CHECK_INTERVAL)
                now = time.time()
                for guild in list(self.bot.guilds):
                    vc = guild.voice_client
                    if vc is None or vc.channel is None:
                        continue
                    state = self.get_guild_state(guild.id)

                    # 重複Bot自動調停 (Deduplication Reconcile)
                    if self.bot_pool:
                        all_bots = self.bot_pool.find_all_bots_in_vc(guild.id, vc.channel.id)
                        if len(all_bots) > 1:
                            primary_idx, primary_bot = self._select_primary_bot_in_vc(all_bots, guild.id)
                            if self.bot_index != primary_idx:
                                logger.warning(
                                    f"🚨 [Deduplication Reconcile] Multiple bots detected in VC '{vc.channel.name}' "
                                    f"(Active: {[b[0] for b in all_bots]}). "
                                    f"Bot #{self.bot_index} yielding to primary Bot #{primary_idx}. Auto-disconnecting..."
                                )
                                # キューのアイテムをプライマリBotへ引き継ぎ
                                target_cog = primary_bot.get_cog("VCHandlerCog")
                                if target_cog:
                                    target_state = target_cog.get_guild_state(guild.id)
                                    while not state.queue.empty():
                                        try:
                                            item = state.queue.get_nowait()
                                            await target_state.queue.put(item)
                                        except Exception:
                                            break
                                await self.disconnect_vc(guild)
                                continue

                    if self._count_humans(vc.channel) > 0:
                        state.empty_since = 0.0
                        continue
                    if state.empty_since == 0.0:
                        state.empty_since = now
                        continue
                    if now - state.empty_since >= self.AUTO_LEAVE_GRACE_SECONDS:
                        logger.warning(
                            f"🧹 [Bot #{self.bot_index}] Auto-leave watchdog: VC '{vc.channel.name}' in '{guild.name}' "
                            f"has had no human members for {now - state.empty_since:.0f}s. Disconnecting..."
                        )
                        state.empty_since = 0.0
                        await self.disconnect_vc(guild)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[Bot #{self.bot_index}] Auto-leave watchdog error: {e}")

    def get_guild_state(self, guild_id: int) -> GuildVCState:
        if guild_id not in self.guild_states:
            self.guild_states[guild_id] = GuildVCState(guild_id)
        return self.guild_states[guild_id]

    # --- VC接続/切断のコアメソッド ---

    async def connect_vc(self, channel: discord.VoiceChannel, text_channel: discord.TextChannel) -> discord.VoiceClient:
        guild_id = channel.guild.id
        state = self.get_guild_state(guild_id)
        state.bound_text_channel_id = text_channel.id

        vc_lock = self.bot_pool.get_vc_lock(guild_id, channel.id) if self.bot_pool else asyncio.Lock()
        async with vc_lock:
            vc = channel.guild.voice_client

            # すでに接続中の場合
            if vc is not None and vc.is_connected():
                if vc.channel.id != channel.id:
                    try:
                        await vc.move_to(channel)
                    except Exception as e:
                        logger.warning(f"[Bot #{self.bot_index}] Failed to move VC: {e}. Reconnecting...")
                        try:
                            await vc.disconnect(force=True)
                        except Exception:
                            pass
                        vc = None
                else:
                    return vc

            # 新規または再接続が必要な場合 (リトライループ付き)
            if vc is None or not vc.is_connected():
                # 0. 自プール内重複接続ガード (同一VCに別号機が既に接続中または接続中なら接続を即座に拒否)
                if self.bot_pool:
                    existing = self.bot_pool.find_connected_or_connecting_bot_in_vc(guild_id, channel.id)
                    if existing and existing[0] != self.bot_index:
                        logger.warning(
                            f"[Bot #{self.bot_index}] Aborting connect to '{channel.name}' in '{channel.guild.name}': "
                            f"Bot #{existing[0]} is already connected or in-flight connecting."
                        )
                        raise VCConnectAborted(f"Bot #{existing[0]} is already in VC '{channel.name}'")

                    # インフライト予約 (他号機および同一号機並列の割り込みを原子的に遮断)
                    reserved = await self.bot_pool.reserve_vc_connection(guild_id, channel.id, self.bot_index)
                    if not reserved:
                        existing = self.bot_pool.find_connected_or_connecting_bot_in_vc(guild_id, channel.id)
                        ext_idx = existing[0] if existing else "unknown"
                        raise VCConnectAborted(f"VC '{channel.name}' is already reserved/connected by Bot #{ext_idx}")

                try:
                    # 他ギルドでのVC接続競合ガード (他サーバーで通話中のBotを誤って多重接続させない)
                    for other_vc in getattr(self.bot, "voice_clients", []):
                        if other_vc and other_vc.channel and other_vc.channel.guild.id != guild_id and other_vc.is_connected():
                            logger.warning(
                                f"[Bot #{self.bot_index}] Cannot connect to '{channel.name}' in '{channel.guild.name}': "
                                f"Already connected to '{other_vc.channel.name}' in '{other_vc.channel.guild.name}'."
                            )
                            raise VCConnectAborted(f"Bot #{self.bot_index} is busy in '{other_vc.channel.guild.name}'")

                    max_retries = 3
                    last_err = None
                    start_epoch = state.connect_epoch
                    for attempt in range(1, max_retries + 1):
                        # 試行前ガード: VCが無人化した場合は後追い接続しない (人間がいる場合は接続継続)
                        if self._count_humans(channel) == 0:
                            logger.info(
                                f"[Bot #{self.bot_index}] Aborting connect to '{channel.name}' before attempt {attempt}: no human members."
                            )
                            raise VCConnectAborted(f"connect to '{channel.name}' aborted (empty)")
                        try:
                            # 既存の接続状況を確認 (すでに目的VCに正常接続完了していれば破壊せず採用)
                            old_vc = channel.guild.voice_client
                            if old_vc is not None:
                                if old_vc.is_connected() and old_vc.channel and old_vc.channel.id == channel.id:
                                    vc = old_vc
                                    break
                                try:
                                    await old_vc.disconnect(force=True)
                                    await asyncio.sleep(0.3)
                                except Exception:
                                    pass

                            vc = await channel.connect(timeout=20.0, reconnect=True, self_deaf=True)
                            break
                        except VCConnectAborted:
                            raise
                        except Exception as e:
                            last_err = e
                            logger.warning(
                                f"[Bot #{self.bot_index}] Voice connect attempt {attempt}/{max_retries} failed for '{channel.name}': {e}"
                            )
                            if attempt < max_retries:
                                await asyncio.sleep(1.0 * attempt)
                            else:
                                logger.error(
                                    f"[Bot #{self.bot_index}] All {max_retries} voice connect attempts failed for '{channel.name}' in '{channel.guild.name}': {e}"
                                )
                                raise last_err
                finally:
                    # ハンドシェイク完了/失敗に伴いインフライト予約を解放 (is_connected() が有効になるか失敗確定)
                    if self.bot_pool:
                        self.bot_pool.release_vc_reservation(guild_id, channel.id, self.bot_index)

                # 接続完了後ガード: VCが無人化した場合は退出（人間がいる場合は絶対に切断しない）
                if self._count_humans(channel) == 0:
                    logger.info(
                        f"[Bot #{self.bot_index}] VC '{channel.name}' became empty during handshake. Leaving immediately."
                    )
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass
                    raise VCConnectAborted(f"connect to '{channel.name}' aborted (empty channel)")

        state.empty_since = 0.0
        # 無人VC Watchdog を起動 (未起動 or 終了済みの場合のみ)
        if self._auto_leave_task is None or self._auto_leave_task.done():
            self._auto_leave_task = safe_create_task(
                self._auto_leave_watchdog_loop(),
                name=f"auto_leave_watchdog_bot{self.bot_index}"
            )

        # データベースに接続セッションを保存 (再起動時の自動復旧用・マルチBot対応)
        await self.db.save_active_vc(guild_id, channel.id, text_channel.id, bot_index=self.bot_index)

        # 再生ループタスクが起動していなければ開始
        if state.play_task is None or state.play_task.done():
            state.play_task = safe_create_task(
                self._playback_loop(channel.guild, state),
                name=f"playback_loop_{channel.guild.id}"
            )

        # 再起動前の退避キューがあれば復元投入
        await self.restore_pending_queue(guild_id, state)

        # 先行プリウォーム: 接続先VCにいる非Bot参加者全員の設定を走査して必要なローカルエンジンを先行起動
        if hasattr(self.tts_manager, "resource_manager") and self.tts_manager.resource_manager:
            human_ids = [m.id for m in channel.members if not m.bot]
            if human_ids:
                safe_create_task(
                    self.tts_manager.resource_manager.prewarm_for_users(guild_id, human_ids),
                    name=f"prewarm_{guild_id}"
                )
        # ニックネームを {VC名} TTS に同期 (確実な即時反映)
        try:
            await self.sync_nickname(channel.guild)
        except Exception as e:
            logger.warning(f"[Bot #{self.bot_index}] Error syncing nickname on connect_vc: {e}")

        # 重複接続防止の遅延ガードタスクを発火 (接続4秒後に同一VC内の自プールBot重複を検査し、重複していれば号機の大きい方が自動退出)
        safe_create_task(
            self._delayed_duplicate_vc_guard(channel.guild, channel.id),
            name=f"delayed_dup_guard_{guild_id}_{channel.id}_bot{self.bot_index}"
        )

        return vc

    async def drain_and_save_all(self, wait_playback_timeout: float = 6.0) -> None:
        """シャットダウン時に再生中の音声を少し待ち、全未再生キューをDBに永続化 (Graceful Drain)"""
        start_t = time.time()
        # 再生中のVCがあれば最大 wait_playback_timeout 秒待機
        while time.time() - start_t < wait_playback_timeout:
            has_playing = False
            for guild in self.bot.guilds:
                vc = guild.voice_client
                if vc and vc.is_playing():
                    has_playing = True
                    break
            if not has_playing:
                break
            await asyncio.sleep(0.2)

        await self.save_all_pending_queues()

    async def save_all_pending_queues(self) -> None:
        """シャットダウン時に全ギルドの未再生キューをDBに永続化"""
        for guild_id, state in self.guild_states.items():
            items = state.get_all_items()
            if items:
                items_json = [json.dumps(it.to_dict(), ensure_ascii=False) for it in items]
                try:
                    await self.db.save_pending_queue(guild_id, self.bot_index, items_json)
                    logger.info(f"Preserved {len(items)} pending queue items for guild {guild_id} (Bot #{self.bot_index})")
                except Exception as e:
                    logger.error(f"Failed to save pending queue for guild {guild_id}: {e}")

    async def restore_pending_queue(self, guild_id: int, state: GuildVCState) -> int:
        """ギルドの退避されたキューアイテムを復元投入"""
        try:
            items_json = await self.db.restore_and_clear_pending_queue(guild_id, self.bot_index)
            if items_json:
                restored_count = 0
                for it_json in items_json:
                    try:
                        data = json.loads(it_json)
                        item = QueueItem.from_dict(data)
                        item.synth_task = safe_create_task(
                            self._prefetch_audio(item),
                            name=f"prefetch_{item.user_id}_{int(time.time()*1000)}"
                        )
                        await state.queue.put(item)
                        restored_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to parse restored queue item: {e}")
                if restored_count > 0:
                    logger.info(f"✨ Restored {restored_count} preserved queue items for guild {guild_id} (Bot #{self.bot_index})")
                return restored_count
        except Exception as e:
            logger.error(f"Failed to restore pending queue for guild {guild_id}: {e}")
        return 0

    async def disconnect_vc(self, guild: discord.Guild) -> None:
        state = self.get_guild_state(guild.id)
        # 進行中の connect_vc リトライを無効化 (後追い接続による無人VC取り残し防止)
        state.connect_epoch += 1
        state.empty_since = 0.0
        state.clear()
        state.bound_text_channel_id = None

        # データベースから接続セッションを削除 (この号機のみ)
        await self.db.remove_active_vc(guild.id, bot_index=self.bot_index)

        vc = guild.voice_client
        if vc is not None:
            if vc.is_playing():
                try:
                    vc.stop()
                except Exception:
                    pass
            try:
                await vc.disconnect(force=True)
            except Exception as e:
                logger.warning(f"[Bot #{self.bot_index}] Exception while disconnecting VC in '{guild.name}': {e}")
            logger.info(f"[Bot #{self.bot_index}] Disconnected from VC in '{guild.name}'")
        # ニックネームを TTS #X にリセット (確実な即時反映)
        try:
            await self.sync_nickname(guild)
        except Exception as e:
            logger.warning(f"[Bot #{self.bot_index}] Error syncing nickname on disconnect_vc: {e}")

    async def restore_active_vcs(self) -> None:
        """再起動時に以前接続していたVCセッションへ自動再接続 (担当号機のセッションのみ)"""
        try:
            sessions = await self.db.get_all_active_vcs()
            if not sessions:
                return

            my_sessions = [s for s in sessions if s.get("bot_index", 1) == self.bot_index]
            if not my_sessions:
                return

            logger.info(f"[Bot #{self.bot_index}] Found {len(my_sessions)} saved VC session(s). Restoring...")
            for session in my_sessions:
                guild_id = session["guild_id"]
                vc_id = session["voice_channel_id"]
                text_id = session["text_channel_id"]

                guild = self.bot.get_guild(guild_id)
                if not guild:
                    continue

                vc_channel = guild.get_channel(vc_id)
                text_channel = guild.get_channel(text_id)

                if isinstance(vc_channel, discord.VoiceChannel) and isinstance(text_channel, discord.TextChannel):
                    # 既に別号機が接続済みまたは復元中ならスキップして重複レコードをDBから掃除
                    if self.bot_pool:
                        existing = self.bot_pool.find_connected_or_connecting_bot_in_vc(guild_id, vc_id)
                        if existing and existing[0] != self.bot_index:
                            logger.warning(
                                f"[Bot #{self.bot_index}] Skipping restore for VC '{vc_channel.name}' in '{guild.name}': "
                                f"Bot #{existing[0]} is already connected or in-flight restoring."
                            )
                            await self.db.remove_active_vc(guild_id, bot_index=self.bot_index)
                            continue

                    # 人間が誰もいなければ復旧せずセッションを削除
                    human_members = [m for m in vc_channel.members if not m.bot]
                    if len(human_members) > 0:
                        logger.info(f"[Bot #{self.bot_index}] Restoring VC in '{guild.name}' (VC: {vc_channel.name}, Text: #{text_channel.name})...")
                        try:
                            await self.connect_vc(vc_channel, text_channel)
                        except VCConnectAborted as e:
                            logger.info(f"[Bot #{self.bot_index}] Restore aborted for VC '{vc_channel.name}': {e}")
                            await self.db.remove_active_vc(guild_id, bot_index=self.bot_index)
                    else:
                        logger.info(f"[Bot #{self.bot_index}] No human members left in saved VC '{vc_channel.name}' of '{guild.name}'. Discarding.")
                        await self.db.remove_active_vc(guild_id, bot_index=self.bot_index)
                else:
                    await self.db.remove_active_vc(guild_id, bot_index=self.bot_index)
        except Exception as e:
            logger.error(f"[Bot #{self.bot_index}] Error restoring active VC sessions: {e}")

    # --- バックグラウンド先行合成 (非同期プリフェッチ) ---

    def _share_chunks_for_prefetch(self, item: QueueItem) -> None:
        """シングルフライト有効時、キュー投入時点の分割結果を先行合成と再生側で共有する (分割の揺れによるキャッシュミス防止)"""
        if getattr(self.config, "chunk_singleflight", "off") != "on":
            return
        try:
            voice_cfg = self.tts_manager._get_voice_config(item.speaker_id)
            item.chunks = TextCleaner.split_into_chunks(
                item.text,
                author_prefix=item.author_prefix,
                engine=voice_cfg.provider if voice_cfg else None,
                speaker_id=item.speaker_id
            ) or None
        except Exception as e:
            logger.debug(f"Failed to pre-split chunks for prefetch: {e}")

    async def _prefetch_chunks(self, item: QueueItem, speculative_timeout: Optional[float] = None):
        """
        キュー待機時間中に、後続のストリーミング再生（_stream_play_item）で
        必要となる全チャンクを先行してバックグラウンド合成し、パーツキャッシュ（L1/L2）を温める。
        これにより、自分の再生順が回ってきた瞬間に 0ms キャッシュヒットで即座に音が鳴る。
        """
        try:
            engine_name = None
            try:
                if hasattr(self, "tts_manager") and self.tts_manager:
                    voice_cfg = self.tts_manager._get_voice_config(item.speaker_id)
                    if voice_cfg:
                        engine_name = voice_cfg.provider
            except Exception:
                pass

            chunks = item.chunks or TextCleaner.split_into_chunks(
                item.text,
                author_prefix=item.author_prefix,
                engine=engine_name,
                speaker_id=item.speaker_id
            )
            if not chunks:
                return
            # シングルフライト有効時: 先行合成はローカルのキャッシュ温めに限定 (投機・クラウド退避しない)。
            # 再生の番が来たら再生側が実行中の合成に合流し、再生タイムライン基準で投機を判断する
            local_only = getattr(self.config, "chunk_singleflight", "off") == "on"

            body_indices = [
                i for i, ch in enumerate(chunks)
                if not getattr(ch, "is_beep", False)
                and not getattr(ch, "is_name_prefix", False)
                and ch.text.strip()
            ]
            longest_idx = max(body_indices, key=lambda i: len(chunks[i].text)) if body_indices else -1

            async def _synth_one(idx: int, ch):
                if getattr(ch, "is_beep", False):
                    return
                is_long = (idx == longest_idx)
                try:
                    await self.tts_manager.get_chunk_audio(
                        text=ch.text,
                        speaker_id=item.speaker_id,
                        pitch=item.pitch,
                        speed=item.speed,
                        user_id=item.user_id,
                        is_longest_chunk=is_long,
                        speculative_timeout=speculative_timeout,
                        local_only=local_only
                    )
                except Exception as e:
                    logger.debug(f"Prefetch chunk failed for '{ch.text[:15]}...': {e}")

            # 全チャンクを並行先行合成
            await asyncio.gather(*[_synth_one(i, ch) for i, ch in enumerate(chunks)], return_exceptions=True)
        except Exception as e:
            logger.debug(f"Prefetch chunks task encountered error: {e}")

    async def _add_reaction_safe(self, message: discord.Message, emoji_char: str) -> None:
        """メッセージに安全にリアクションを付与（権限不足やメッセージ削除済みエラーを吸収）"""
        try:
            await message.add_reaction(emoji_char)
        except Exception as e:
            logger.debug(f"Failed to add reaction '{emoji_char}' to message {message.id}: {e}")

    async def _prefetch_audio(self, item: QueueItem):
        """メッセージがキューに入った瞬間にバックグラウンドでTTS合成を開始し、推論時間を再生と並行して隠蔽 (フォールバック用)"""
        t_start = time.time()
        audio_path = await self.tts_manager.get_audio_path(
            user_id=item.user_id,
            speaker_id=item.speaker_id,
            pitch=item.pitch,
            speed=item.speed,
            text=item.text,
            author_prefix=item.author_prefix
        )
        t_end = time.time()
        meta = getattr(self.tts_manager, "last_synthesis_meta", {}) or {}
        return audio_path, t_start, t_end, meta

    def _notify_user_speech(self) -> None:
        """ユーザー発言を検知した際に、知能的プリキャッシュサービスへ通知して即座に待機させる"""
        try:
            if hasattr(self.bot, "prewarm_service") and self.bot.prewarm_service:
                self.bot.prewarm_service.record_user_speech()
            elif self.bot_pool and hasattr(self.bot_pool, "main_bot"):
                main_bot = getattr(self.bot_pool, "main_bot", None)
                if main_bot and hasattr(main_bot, "prewarm_service") and main_bot.prewarm_service:
                    main_bot.prewarm_service.record_user_speech()
        except Exception:
            pass

    def _notify_user_speech_done(self) -> None:
        """ユーザー発言の再生が完了した際に、知能的プリキャッシュサービスへ通知"""
        try:
            if hasattr(self.bot, "prewarm_service") and self.bot.prewarm_service:
                self.bot.prewarm_service.notify_user_speech_done()
            elif self.bot_pool and hasattr(self.bot_pool, "main_bot"):
                main_bot = getattr(self.bot_pool, "main_bot", None)
                if main_bot and hasattr(main_bot, "prewarm_service") and main_bot.prewarm_service:
                    main_bot.prewarm_service.notify_user_speech_done()
        except Exception:
            pass

    # --- 低遅延再生ループ ---

    async def _playback_loop(self, guild: discord.Guild, state: GuildVCState) -> None:
        """ギルド専用のキュー再生ワーカー"""
        logger.debug(f"Starting playback loop for guild {guild.id}")
        try:
            while True:
                item = await state.queue.get()
                t_dequeued = time.time()
                state.current_item = item
                state.is_playing = True

                vc: Optional[discord.VoiceClient] = guild.voice_client
                if vc is None or not vc.is_connected():
                    # ネットワーク揺れや Discord の再接続処理中の可能性があるため、短時間リトライ
                    reconnected = False
                    for retry_idx in range(10):
                        await asyncio.sleep(1.0)
                        vc = guild.voice_client
                        if vc is not None and vc.is_connected():
                            reconnected = True
                            logger.info(f"Voice client reconnected in guild {guild.id} (retry {retry_idx + 1}/10). Resuming playback.")
                            break
                    if not reconnected:
                        logger.warning(f"Voice client permanently disconnected in guild {guild.id}, restoring item to queue and terminating playback loop.")
                        state.queue.task_done()
                        state.push_front(item)
                        state.current_item = None
                        state.is_playing = False
                        break

                try:
                    # 1. 超低遅延ストリーミングパイプライン再生の実行 (名前先行再生 ＆ チャンク逐次供給)
                    stream_ok = False
                    try:
                        stream_ok = await self._stream_play_item(guild, state, vc, item, t_dequeued)
                    except Exception as s_err:
                        logger.warning(f"Streaming playback failed in guild {guild.id} ({s_err}). Falling back to file playback.")
                        stream_ok = False

                    # ストリーミング成功時は未完了の先行合成タスクがあればキャンセルしてGPUリソースを即時解放
                    if stream_ok and item.synth_task and not item.synth_task.done():
                        item.synth_task.cancel()

                    # 2. ストリーミングで未再生の場合のフォールバック (従来の一括ファイル再生)
                    if not stream_ok:
                        if item.synth_task and not item.synth_task.done():
                            try:
                                await asyncio.shield(item.synth_task)
                            except Exception:
                                pass

                        t_synth_start = time.time()
                        audio_path = await self.tts_manager.get_audio_path(
                            user_id=item.user_id,
                            speaker_id=item.speaker_id,
                            pitch=item.pitch,
                            speed=item.speed,
                            text=item.text,
                            author_prefix=item.author_prefix
                        )
                        t_synth_end = time.time()
                        meta = getattr(self.tts_manager, "last_synthesis_meta", {}) or {}

                        state.play_finished_event.clear()
                        def after_playing(error):
                            if error:
                                logger.error(f"Fallback playback error in guild {guild.id}: {error}")
                            self.bot.loop.call_soon_threadsafe(state.play_finished_event.set)

                        audio_source = discord.FFmpegPCMAudio(
                            str(audio_path),
                            before_options="-probesize 32 -analyzeduration 0 -threads 1",
                            options="-vn"
                        )
                        vol = max(0.0, min(float(item.volume), 2.0))
                        audio_source = discord.PCMVolumeTransformer(audio_source, volume=vol)

                        vc.play(audio_source, after=after_playing)
                        t_play_start = time.time()
                        state.deduplicator.record_played(item.text)

                        # レイテンシ計測ログの記録
                        try:
                            if not meta:
                                meta = getattr(self.tts_manager, "last_synthesis_meta", {}) or {}
                            voice_cfg = self.config.voices.get(item.speaker_id)
                            spk_name = voice_cfg.name if voice_cfg else item.speaker_id
                            t_recv = getattr(item, "received_at", t_dequeued)
                            latency_tracker.record(
                                user_name=item.user_name,
                                text=item.text,
                                speaker_name=spk_name,
                                provider=meta.get("provider", "unknown"),
                                worker_label=meta.get("worker", "unknown"),
                                t_discord_created=getattr(item, "discord_created_at", 0.0),
                                t_received=t_recv,
                                t_queued=getattr(item, "queued_at", 0.0),
                                t_dequeued=t_dequeued,
                                t_synth_start=t_synth_start,
                                t_synth_end=t_synth_end,
                                t_play_start=t_play_start,
                                is_cached=meta.get("is_cached", False),
                                is_fallback=meta.get("is_fallback", False),
                                turn_ttfs_ms=max(0.0, (t_play_start - t_dequeued) * 1000.0),
                                e2e_ttfs_ms=max(0.0, (t_play_start - t_recv) * 1000.0),
                                incident_context={
                                    "guild_id": guild.id,
                                    "guild_name": guild.name,
                                    "speaker_id": item.speaker_id,
                                    "expected_provider": getattr(voice_cfg, "provider", None),
                                    "full_text": item.text,
                                },
                            )
                        except Exception as lat_err:
                            logger.warning(f"Failed to record fallback playback latency: {lat_err}")

                        await state.play_finished_event.wait()

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Error synthesizing/playing audio in guild {guild.id}: {e}")
                finally:
                    state.is_playing = False
                    state.current_item = None
                    state.queue.task_done()
                    self._notify_user_speech_done()

        except asyncio.CancelledError:
            logger.debug(f"Playback loop cancelled for guild {guild.id}")
        except Exception as e:
            logger.exception(f"Unexpected fatal error in playback loop for guild {guild.id}: {e}")
        finally:
            state.is_playing = False

    async def _stream_play_item(
        self,
        guild: discord.Guild,
        state: GuildVCState,
        vc: discord.VoiceClient,
        item: QueueItem,
        t_dequeued: float
    ) -> bool:
        """
        超低遅延ストリーミングパイプライン再生 (名前先行再生 ＆ チャンク逐次供給)
        1. StreamingAudioSource を初期化
        2. 発言者名プレフィックスがあれば、キャッシュ/推論して即時 feed
        3. vc.play(stream_source) を即座に開始 (TTFS: 0〜50ms)
        4. 本文を MeCab/句読点でチャンク分割
        5. 先行再生の裏で本文チャンクを並行合成し、順次 stream_source に feed
        6. 全チャンク投入完了時に stream_source.finish()
        7. 再生完了待ち & 次世代レイテンシメトリクスを tracker に記録
        """
        vol = max(0.0, min(float(item.volume), 2.0))
        stream_source = StreamingAudioSource()
        volume_transformer = discord.PCMVolumeTransformer(stream_source, volume=vol)

        chunks_meta = []
        t_play_start = 0.0
        total_audio_duration = 0.0
        min_buffer_remaining_ms = float('inf')
        preroll_wait_ms = 0.0

        state.play_finished_event.clear()
        def after_playing(error):
            if error:
                logger.error(f"Streaming playback error in guild {guild.id}: {error}")
            self.bot.loop.call_soon_threadsafe(state.play_finished_event.set)

        # 話者定義からエンジン（プロバイダー）を特定
        engine_name = None
        try:
            if hasattr(self, "tts_manager") and self.tts_manager:
                voice_cfg = self.tts_manager._get_voice_config(item.speaker_id)
                if voice_cfg:
                    engine_name = voice_cfg.provider
        except Exception:
            pass

        # チャンク分割の作成 (発言者名プレフィックス + 本文チャンク: 各マシン×各エンジン推論性能に応じた動的文字数適応)
        all_chunks = item.chunks or TextCleaner.split_into_chunks(
            item.text,
            author_prefix=item.author_prefix,
            engine=engine_name,
            speaker_id=item.speaker_id
        )
        if not all_chunks:
            return False

        # --- 最大許容沈黙・段階導入フラグ (off / shadow / on) ---
        tl_mode = getattr(self.config, "stream_tl_speculative", "off")
        route_mode = getattr(self.config, "head_route_earliest", "off")
        head_mode = getattr(self.config, "head_dynamic_length", "off")
        is_local_engine = engine_name in ("voicevox", "sharevox", "coeiroink")
        stream_settings = await self.db.get_guild_settings(guild.id, self.config)
        max_silence = float(stream_settings.get("max_silence_seconds") or getattr(self.config, "max_silence_default_seconds", 1.5))
        az_margin = float(getattr(self.config, "az_margin_seconds", 0.45))
        chunk_extra: Dict[int, Dict[str, Any]] = {}
        head_pred: Dict[str, Any] = {}

        def _head_index(chs: List[Any]) -> Optional[int]:
            return next(
                (i for i, c in enumerate(chs) if not c.is_name_prefix and not c.is_beep and c.text.strip()),
                None,
            )

        head_idx = _head_index(all_chunks)
        if is_local_engine and head_idx is not None and (route_mode != "off" or head_mode != "off"):
            try:
                voice_cfg_p = self.tts_manager._get_voice_config(item.speaker_id)
                model_id_p = getattr(voice_cfg_p, "speaker_uuid", "") or getattr(voice_cfg_p, "voice_name", "") or str(item.speaker_id)
                # 先頭本文パーツ長の動的決定 (先行合成済みの発話は分割を変えない: キャッシュキー一致のため)
                if head_mode != "off" and item.chunks is None:
                    head_text = all_chunks[head_idx].text
                    head_key = CacheManager.generate_cache_key(item.speaker_id, item.pitch, item.speed, head_text, user_id=item.user_id)
                    planned = None
                    if await self.tts_manager.cache_manager.get(head_key, provider=engine_name) is None:
                        body_lens = [len(c.text) for c in all_chunks[head_idx:] if not c.is_beep and not c.is_name_prefix and c.text.strip()]
                        name_chunks = [c for c in all_chunks[:head_idx] if c.is_name_prefix]
                        planned = plan_head_chars(
                            body_lens,
                            has_name=bool(name_chunks),
                            name_play_s=sum(_conservative_play_sec(c) for c in name_chunks),
                            nodes=self.tts_manager.get_planning_nodes(engine_name, model_id_p, guild.id),
                            max_silence=max_silence,
                            az_margin=az_margin,
                            k_plan=float(getattr(self.config, "head_plan_pessimism", 1.5)),
                        )
                    applied = bool(planned) and head_mode == "on" and tl_mode == "on"
                    if applied:
                        all_chunks = TextCleaner.apply_head_chunk_limit(all_chunks, planned)
                        head_idx = _head_index(all_chunks)
                    if planned:
                        logger.info(
                            f"✂️ [HeadPlan:{'on' if applied else 'shadow'}] guild={guild.id} head {len(head_text)}→{planned} chars "
                            f"(max_silence={max_silence:.2f}s) text='{head_text[:15]}...'"
                        )
                    chunk_extra.setdefault(head_idx, {}).update({
                        "head_chars_planned": planned,
                        "head_chars_applied": len(all_chunks[head_idx].text) if applied else None,
                    })
                head_pred = self.tts_manager.peek_earliest_finish(
                    engine_name, len(all_chunks[head_idx].text), model_name=model_id_p, guild_id=guild.id
                )
                if head_pred:
                    chunk_extra.setdefault(head_idx, {}).update({
                        "pred_node": head_pred.get("worker_label"),
                        "pred_eta_ms": round(float(head_pred.get("eta_ms", 0.0)), 1),
                    })
            except Exception as plan_err:
                logger.debug(f"Head planning skipped: {plan_err}")

        # 同一発話内チャンクの別ノード分散 (Anti-Affinity) 用セット:
        # 先頭本文パーツが完了最短と予測されたノードを後続チャンクの回避ノードに事前登録し、同一GPU競合を防止
        turn_avoid_nodes: Set[str] = set()
        if head_pred:
            pred_m = head_pred.get("machine_name")
            if pred_m:
                turn_avoid_nodes.add(pred_m)
            elif head_pred.get("node") and getattr(head_pred.get("node"), "machine_name", None):
                turn_avoid_nodes.add(head_pred.get("node").machine_name)

        # 分割パーツの中で推論対象となる本文チャンクの最長チャンクを特定（最強GPUへ優先ルーティングするため）
        # ※ 発言者名プレフィックス (is_name_prefix=True) やピー音 (is_beep=True) は除外し、
        #    エンジン別動的チャンク上限 (8〜30文字) に合わせて固定文字数足切りを行わず最長本文を特定する。
        body_indices = [
            i for i, ch in enumerate(all_chunks)
            if not getattr(ch, "is_beep", False)
            and not getattr(ch, "is_name_prefix", False)
            and ch.text.strip()
        ]
        longest_idx = max(body_indices, key=lambda i: len(all_chunks[i].text)) if body_indices else 0

        if getattr(item, "speculative_timeout", None) is not None:
            spec_timeout = float(item.speculative_timeout)
            explicit_spec_timeout = True
        else:
            guild_settings = await self.db.get_guild_settings(guild.id, self.config)
            spec_timeout = float(guild_settings.get("speculative_timeout_seconds", 2.5) or 2.5)
            explicit_spec_timeout = False

        def _estimate_chunk_play_sec(c_obj) -> float:
            if getattr(c_obj, "is_beep", False):
                dur = float(getattr(c_obj, "beep_duration_sec", 0.4) or 0.4)
            else:
                c_len = max(1, len(getattr(c_obj, "text", "") or ""))
                dur = max(0.25, c_len * 0.14)
            pause_s = float(getattr(c_obj, "pause_after_ms", 0) or 0) / 1000.0
            return dur + pause_s

        timeline = PlaybackTimeline(
            t_turn=t_dequeued,
            max_silence=max_silence,
            az_margin=az_margin,
            est_play=[_conservative_play_sec(c) for c in all_chunks],
            stream_source=stream_source,
        )

        async def _synth_chunk(
            ch,
            is_longest: bool,
            chunk_idx: int = 0,
            est_headroom_sec: float = 0.0,
            avoid_nodes: Optional[Set[str]] = None
        ):
            t_s = time.time()
            if getattr(ch, "is_beep", False):
                beep_pcm = get_beep_pcm(getattr(ch, "beep_duration_sec", 0.4))
                t_e = time.time()
                return ch, beep_pcm, "beep", "local", True, t_s, t_e, False, []

            live_buf_sec = getattr(stream_source, "buffer_remaining_seconds", 0.0)
            total_headroom = max(est_headroom_sec, live_buf_sec)
            if explicit_spec_timeout and chunk_idx == 0 and total_headroom <= 0.2:
                chunk_spec_timeout = spec_timeout
            else:
                try:
                    chunk_spec_timeout = latency_profiler.calculate_dynamic_speculative_timeout(
                        base_timeout=spec_timeout,
                        engine=engine_name,
                        text_len=len(ch.text),
                        chunk_index=chunk_idx,
                        buffer_headroom_sec=total_headroom,
                    )
                except Exception:
                    chunk_spec_timeout = spec_timeout

            # 再生タイムライン基準の投機締切 (shadow は記録のみ)
            extra = chunk_extra.setdefault(chunk_idx, {})
            deadline_fn = None
            if tl_mode != "off" and is_local_engine:
                def _deadline(idx: int = chunk_idx, t0: float = t_s) -> float:
                    return max(timeline.deadline_for(idx), t0 + MIN_LOCAL_TRY_SEC)
                extra["tl_deadline_rel_ms"] = round((_deadline() - t_s) * 1000.0, 1)
                extra["legacy_spec_ms"] = round(chunk_spec_timeout * 1000.0, 1)
                if tl_mode == "on":
                    deadline_fn = _deadline

            is_head = (route_mode == "on" and chunk_idx == head_idx)
            chunk_avoid = None if is_head else avoid_nodes

            b, p, w, c = await self.tts_manager.get_chunk_audio(
                text=ch.text,
                speaker_id=item.speaker_id,
                pitch=item.pitch,
                speed=item.speed,
                user_id=item.user_id,
                is_longest_chunk=is_longest,
                speculative_timeout=chunk_spec_timeout,
                speculative_deadline_fn=deadline_fn,
                prefer_earliest_finish=is_head,
                avoid_nodes=chunk_avoid
            )
            t_e = time.time()
            c_meta = (
                self.tts_manager.get_last_chunk_meta(ch.text, item.speaker_id)
                if hasattr(self.tts_manager, "get_last_chunk_meta")
                else {}
            )
            is_fb = (not c) and (
                bool(c_meta.get("is_fallback"))
                or (bool(engine_name) and p not in ("beep", engine_name))
            )
            failed_provs = list(c_meta.get("failed_providers", [])) if not c else []
            if not c and c_meta.get("joined_inflight"):
                extra["joined_inflight"] = c_meta.get("joined_inflight")
            return ch, b, p, w, c, t_s, t_e, is_fb, failed_provs

        # 最初のチャンク（プレフィックスまたは第1文）
        first_chunk = all_chunks[0]
        first_is_longest = (longest_idx == 0)

        # チャンクが複数ある場合は、第1チャンクと並行して後続チャンクの合成タスクも直ちにバックグラウンド起動！
        remaining_tasks = []
        all_created_tasks: List[asyncio.Task] = []

        try:
            # 名前なしで先頭が本文の場合、先頭パーツを最初に起動して完了予測最短ノードを確実に確保する
            head_first_task: Optional[asyncio.Task] = None
            if route_mode == "on" and head_idx == 0:
                head_first_task = asyncio.create_task(_synth_chunk(
                    first_chunk,
                    first_is_longest,
                    chunk_idx=0,
                    est_headroom_sec=0.0,
                    avoid_nodes=turn_avoid_nodes
                ))
                all_created_tasks.append(head_first_task)

            if len(all_chunks) > 1:
                cum_headroom_sec = _estimate_chunk_play_sec(first_chunk)
                for idx, ch in enumerate(all_chunks[1:], start=1):
                    is_long = (idx == longest_idx)
                    t = asyncio.create_task(_synth_chunk(
                        ch,
                        is_long,
                        chunk_idx=idx,
                        est_headroom_sec=cum_headroom_sec,
                        avoid_nodes=turn_avoid_nodes
                    ))
                    remaining_tasks.append(t)
                    all_created_tasks.append(t)
                    cum_headroom_sec += _estimate_chunk_play_sec(ch)

            # 第1チャンクの取得
            if head_first_task is not None:
                c0_ch, c0_bytes, c0_prov, c0_worker, c0_cached, c0_synth_start, c0_synth_end, c0_fb, c0_failed_provs = await head_first_task
            else:
                c0_ch, c0_bytes, c0_prov, c0_worker, c0_cached, c0_synth_start, c0_synth_end, c0_fb, c0_failed_provs = await _synth_chunk(
                    first_chunk,
                    first_is_longest,
                    chunk_idx=0,
                    est_headroom_sec=0.0,
                    avoid_nodes=turn_avoid_nodes
                )

            # 第1チャンクをストリームに投入
            if getattr(first_chunk, "is_beep", False):
                dur0 = stream_source.feed_raw_pcm(c0_bytes, pause_after_ms=first_chunk.pause_after_ms)
            else:
                dur0 = stream_source.feed_wav_bytes(
                    c0_bytes,
                    pause_after_ms=first_chunk.pause_after_ms,
                    is_followed_by_beep=getattr(first_chunk, "is_followed_by_beep", False)
                )
            total_audio_duration += dur0
            timeline.mark_fed(0, dur0)

            c0_synth_duration = round((c0_synth_end - c0_synth_start) * 1000, 1)
            c0_audio_duration = round(dur0 * 1000, 1)
            chunks_meta.append({
                "chunk_index": 0,
                "text": first_chunk.text if not getattr(first_chunk, "is_beep", False) else "🚫 [自主規制ピー音]",
                "char_count": len(first_chunk.text) if not getattr(first_chunk, "is_beep", False) else 3,
                "role": "name" if getattr(first_chunk, "is_name_prefix", False) else ("beep" if getattr(first_chunk, "is_beep", False) else "body"),
                "provider": c0_prov,
                "worker": c0_worker,
                "is_cached": c0_cached,
                "is_fallback": c0_fb,
                "failed_providers": c0_failed_provs,
                "synth_ms": c0_synth_duration,
                "synthesis_ms": c0_synth_duration,
                "duration_ms": c0_audio_duration,
                "t_synth_start": c0_synth_start,
                "t_synth_end": c0_synth_end,
                "buffer_remaining_ms": c0_audio_duration,
                "buffer_headroom_ms": c0_audio_duration,
            })

            # 統計ベース動的 Adaptive Preroll 制御 (音飛び/アンダーランの根絶 & 初動ラグ極小化):
            # 第0パーツ実測再生長(dur0)および先行並行進捗(l0_elapsed)、後続パーツの推定推論時間・推定音声長から、
            # 最適な「いい塩梅」の Preroll 待機時間 を動的に算出する。
            if remaining_tasks:
                voice_cfg = self.tts_manager._get_voice_config(item.speaker_id)
                target_prov = getattr(voice_cfg, "provider", "voicevox")
                model_id = getattr(voice_cfg, "speaker_uuid", "") or getattr(voice_cfg, "voice_name", "") or str(item.speaker_id)

                if hasattr(self.tts_manager, "get_expected_worker_info"):
                    first_body_len = len(all_chunks[1].text) if len(all_chunks) > 1 else 10
                    w_info = self.tts_manager.get_expected_worker_info(
                        target_prov,
                        model_name=model_id,
                        text_len=first_body_len,
                        guild_id=guild.id,
                    )
                    est_worker_label = w_info.get("worker_label", "")
                    est_has_gpu = w_info.get("has_gpu")
                    is_loaded = w_info.get("is_model_loaded")
                    est_node_ms = w_info.get("estimated_node_ms")
                else:
                    est_worker_label = self.tts_manager._get_worker_label_for_provider(target_prov) if hasattr(self.tts_manager, "_get_worker_label_for_provider") else ""
                    est_has_gpu = None
                    est_node_ms = None
                    is_loaded = None
                    if target_prov.lower() == "coeiroink":
                        try:
                            c_pool = getattr(self.tts_manager, "coeiroink_pool", None)
                            if c_pool and hasattr(c_pool, "nodes"):
                                is_loaded = any(n.has_model_in_ram(model_id) for n in c_pool.nodes if getattr(n, "is_healthy", False))
                        except Exception as e:
                            logger.debug(f"Failed to check coeiroink model RAM state: {e}")

                # 先頭本文の推定は割当予測 (完了予測最短ノード) と同じ値を使う (Preroll と割当の推定統一)
                if route_mode == "on" and head_idx == 1 and head_pred:
                    est_node_ms = float(head_pred.get("eta_ms", 0.0)) or est_node_ms

                # 第0パーツの生成所要時間 (並行タスクがすでに消化したリードタイム)
                l0_elapsed = max(0.0, c0_synth_end - c0_synth_start)
                item_speed = max(0.5, min(float(item.speed or 1.0), 2.0))

                # 発言者名プレフィックスかどうかの判定:
                # 名前直後の音飛び(全体の98%を占める)を根絶するため、名前時は第1文(Chunk #1)を待機する安全マージン・上限を最適化
                is_name = getattr(first_chunk, "is_name_prefix", False)
                if is_name:
                    safety_margin_s = 0.25  # 250ms 安全マージン (ネットワーク・デコードの揺らぎを確実に吸収)
                    # Edge-TTS や COEIROINK 等の重量プロバイダ時は待機上限を緩和してアンダーラン防止
                    if target_prov in ("edge", "coeiroink") or not est_has_gpu:
                        max_cap_s = 1.35   # 重量プロバイダ時は最大1.35秒まで待機してアンダーラン防止
                    else:
                        max_cap_s = 0.85   # 高速GPU/Azure等は0.85秒キャップ
                    if tl_mode == "on":
                        max_cap_s = max_silence  # 最大許容沈黙: 順番が来てから最初の音までの上限
                    min_wait_s = 0.15      # 第1文がまだ完了していない場合の最低待機枠 (150ms)
                else:
                    safety_margin_s = 0.15  # 150ms 安全マージン
                    max_cap_s = 0.40       # 通常文パーツは即応性最優先 (400msキャップ)
                    min_wait_s = 0.0

                # 各チャンクの必要待機時間 W_k を計算
                w_candidates = [0.0]
                cum_est_audio_dur = dur0  # 第0パーツの実測再生時間からスタート

                for idx, ch in enumerate(all_chunks[1:], start=1):
                    # 推定推論時間 (本文プロバイダと現在稼働中の最優先ワーカーに基づいて正確に算出)
                    est_synth = latency_tracker.get_estimated_synthesis_time(
                        provider=target_prov,
                        char_count=len(ch.text),
                        worker_label=est_worker_label,
                        is_model_loaded=is_loaded,
                        has_gpu=est_has_gpu,
                        estimated_node_ms=est_node_ms if idx == 1 else None,
                    )
                    # 第0パーツ取得中にすでに並行して進んだ時間を控除
                    rem_synth = max(0.0, est_synth - l0_elapsed)

                    if idx == 1:
                        # 第1パーツ: 名前または短文直後の不自然な沈黙を完全根絶するため最優先
                        w_1 = rem_synth + safety_margin_s - dur0
                        # 名前プレフィックスで第1文がまだ未完了なら最低待機枠を確保
                        if is_name and not remaining_tasks[0].done():
                            w_1 = max(w_1, min_wait_s)
                        w_candidates.append(w_1)
                    else:
                        # 第2パーツ以降: 文間の息継ぎ・句点ポーズ許容枠 (300ms) を差し引いた減衰モデル
                        w_k = rem_synth + safety_margin_s - cum_est_audio_dur - 0.30
                        w_candidates.append(w_k)

                    # 後続パーツのための推定音声長を加算 (1文字あたり約140ms / speed + ポーズ)
                    est_chunk_audio = (len(ch.text) * 0.14) / item_speed + (ch.pause_after_ms / 1000.0)
                    cum_est_audio_dur += est_chunk_audio

                needed_wait_s = max(w_candidates)
                dynamic_wait_timeout = max(0.0, min(needed_wait_s, max_cap_s))
                logger.debug(
                    f"Adaptive Preroll calc (is_name={is_name}): l0={l0_elapsed*1000:.0f}ms, dur0={dur0*1000:.0f}ms, "
                    f"needed_wait={needed_wait_s*1000:.0f}ms, dynamic_wait_timeout={dynamic_wait_timeout*1000:.0f}ms"
                )


                t_preroll_start = time.time()
                if dynamic_wait_timeout > 0.03:
                    # 不足分だけプレフィックス発声を遅らせて第1文を待機 (重い推論のアンダーラン完全防止)
                    try:
                        ch1, b1, p1, w1, c1, ts1, te1, fb1, fp1 = await asyncio.wait_for(
                            asyncio.shield(remaining_tasks[0]),
                            timeout=dynamic_wait_timeout
                        )
                        if getattr(ch1, "is_beep", False):
                            dur1 = stream_source.feed_raw_pcm(b1, pause_after_ms=ch1.pause_after_ms)
                        else:
                            dur1 = stream_source.feed_wav_bytes(
                                b1,
                                pause_after_ms=ch1.pause_after_ms,
                                is_followed_by_beep=getattr(ch1, "is_followed_by_beep", False)
                            )
                        total_audio_duration += dur1
                        timeline.mark_fed(1, dur1)
                        ch1_synth_dur = round((te1 - ts1) * 1000, 1)
                        ch1_audio_dur = round(dur1 * 1000, 1)
                        init_buffer_ms = round(total_audio_duration * 1000, 1)
                        chunks_meta.append({
                            "chunk_index": 1,
                            "text": ch1.text,
                            "char_count": len(ch1.text),
                            "role": "body",
                            "provider": p1,
                            "worker": w1,
                            "is_cached": c1,
                            "is_fallback": fb1,
                            "failed_providers": fp1,
                            "synth_ms": ch1_synth_dur,
                            "synthesis_ms": ch1_synth_dur,
                            "duration_ms": ch1_audio_dur,
                            "t_synth_start": ts1,
                            "t_synth_end": te1,
                            "buffer_remaining_ms": init_buffer_ms,
                            "buffer_headroom_ms": init_buffer_ms,
                        })
                        # 第1文タスクは処理済み
                        remaining_tasks = remaining_tasks[1:]
                    except asyncio.TimeoutError:
                        logger.info(
                            f"Dynamic preroll wait ({dynamic_wait_timeout:.2f}s) finished for guild {guild.id}. "
                            f"Starting playback with prefix buffer ({dur0*1000:.0f}ms) to let body chunk catch up."
                        )
                else:
                    # プレフィックス音声長が十分にあるため待機ゼロで即座に発声開始可能！
                    # ただし既に第1文タスクが完了していれば直ちにストリームに投入
                    if remaining_tasks[0].done():
                        try:
                            ch1, b1, p1, w1, c1, ts1, te1, fb1, fp1 = remaining_tasks[0].result()
                            if getattr(ch1, "is_beep", False):
                                dur1 = stream_source.feed_raw_pcm(b1, pause_after_ms=ch1.pause_after_ms)
                            else:
                                dur1 = stream_source.feed_wav_bytes(
                                    b1,
                                    pause_after_ms=ch1.pause_after_ms,
                                    is_followed_by_beep=getattr(ch1, "is_followed_by_beep", False)
                                )
                            total_audio_duration += dur1
                            timeline.mark_fed(1, dur1)
                            ch1_synth_dur = round((te1 - ts1) * 1000, 1)
                            ch1_audio_dur = round(dur1 * 1000, 1)
                            init_buffer_ms = round(total_audio_duration * 1000, 1)
                            chunks_meta.append({
                                "chunk_index": 1,
                                "text": ch1.text,
                                "char_count": len(ch1.text),
                                "role": "body",
                                "provider": p1,
                                "worker": w1,
                                "is_cached": c1,
                                "is_fallback": fb1,
                                "failed_providers": fp1,
                                "synth_ms": ch1_synth_dur,
                                "synthesis_ms": ch1_synth_dur,
                                "duration_ms": ch1_audio_dur,
                                "t_synth_start": ts1,
                                "t_synth_end": te1,
                                "buffer_remaining_ms": init_buffer_ms,
                                "buffer_headroom_ms": init_buffer_ms,
                            })
                            remaining_tasks = remaining_tasks[1:]
                        except Exception:
                            pass

                preroll_wait_ms = (time.time() - t_preroll_start) * 1000.0

            # vc.play を開始！ (プレフィックスまたは第1文までの最適バッファでアンダーランゼロ再生)
            vc.play(volume_transformer, after=after_playing)
            t_play_start = time.time()
            timeline.t_play = t_play_start

            # 残りの後続チャンクを完了次第、パイプライン逐次投入 (全タスク完了待ちによるアンダーランを完全防止)
            for task in remaining_tasks:
                ch, b, p, w, c, t_s, t_e, is_fb, failed_provs = await task

                # チャンクfeed前のバッファ余裕度 (正=余裕, 負=ギャップ/アンダーラン)
                elapsed_play = time.time() - t_play_start
                buffer_remaining_s = total_audio_duration - elapsed_play
                buffer_remaining_ms = buffer_remaining_s * 1000
                min_buffer_remaining_ms = min(min_buffer_remaining_ms, buffer_remaining_ms)

                if getattr(ch, "is_beep", False):
                    dur = stream_source.feed_raw_pcm(b, pause_after_ms=ch.pause_after_ms)
                else:
                    dur = stream_source.feed_wav_bytes(
                        b,
                        pause_after_ms=ch.pause_after_ms,
                        is_followed_by_beep=getattr(ch, "is_followed_by_beep", False)
                    )
                total_audio_duration += dur
                timeline.mark_fed(len(chunks_meta), dur)

                task_synth_dur = round((t_e - t_s) * 1000, 1)
                task_audio_dur = round(dur * 1000, 1)
                buf_rem_rounded = round(buffer_remaining_ms, 1)
                chunks_meta.append({
                    "chunk_index": len(chunks_meta),
                    "text": ch.text if not getattr(ch, "is_beep", False) else "🚫 [自主規制ピー音]",
                    "char_count": len(ch.text) if not getattr(ch, "is_beep", False) else 3,
                    "role": "beep" if getattr(ch, "is_beep", False) else "body",
                    "provider": p,
                    "worker": w,
                    "is_cached": c,
                    "is_fallback": is_fb,
                    "failed_providers": failed_provs,
                    "synth_ms": task_synth_dur,
                    "synthesis_ms": task_synth_dur,
                    "duration_ms": task_audio_dur,
                    "t_synth_start": t_s,
                    "t_synth_end": t_e,
                    "buffer_remaining_ms": buf_rem_rounded,
                    "buffer_headroom_ms": buf_rem_rounded,
                })

            # すべてのチャンク投入完了
            stream_source.finish()
            state.deduplicator.record_played(item.text)

            # 推論隠蔽率: 再生中に並行完了した後続チャンクの推論時間 / 全後続推論時間
            total_synth_ms = sum(c["synth_ms"] for c in chunks_meta)
            if t_play_start > 0 and len(chunks_meta) > 1:
                masked_ms = 0.0
                playback_cursor_ms = 0.0
                for ci, ch_m in enumerate(chunks_meta):
                    if ci == 0:
                        playback_cursor_ms += ch_m["duration_ms"]
                        continue
                    t_se = ch_m.get("t_synth_end")
                    if t_se:
                        synth_done_offset_ms = (t_se - t_play_start) * 1000
                        if synth_done_offset_ms <= playback_cursor_ms:
                            masked_ms += ch_m["synth_ms"]
                    playback_cursor_ms += ch_m["duration_ms"]
                post_c0_synth = total_synth_ms - (chunks_meta[0]["synth_ms"] if chunks_meta else 0)
                masking_ratio = round((masked_ms / post_c0_synth * 100), 1) if post_c0_synth > 0 else 100.0
            else:
                masking_ratio = 100.0 if total_synth_ms > 0 else 0.0

            # 再生終了を待機 (アンダーラン集計が確定する)
            await state.play_finished_event.wait()

            # メトリクス記録
            try:
                voice_cfg = self.config.voices.get(item.speaker_id)
                spk_name = voice_cfg.name if voice_cfg else item.speaker_id
                # キャッシュされていない（実推論を行った）チャンクを代表ワーカー・プロバイダとする
                active_chunk = next((c for c in chunks_meta if not c.get("is_cached")), chunks_meta[0] if chunks_meta else None)
                primary_worker = active_chunk["worker"] if active_chunk else "unknown"
                primary_prov = active_chunk["provider"] if active_chunk else "unknown"

                # 全チャンクがキャッシュヒットしていた時のみ全体キャッシュ判定とする (名前のキャッシュで本文推論が隠蔽されないよう防止)
                is_all_cached = all(c.get("is_cached", False) for c in chunks_meta) if chunks_meta else False
                has_any_fallback = any(c.get("is_fallback", False) for c in chunks_meta) if chunks_meta else False

                t_recv = getattr(item, "received_at", t_dequeued)
                turn_ttfs = max(0.0, (t_play_start - t_dequeued) * 1000.0)
                e2e_ttfs = max(0.0, (t_play_start - t_recv) * 1000.0)
                core_ttfs = max(0.0, (c0_synth_end - t_dequeued) * 1000.0)
                underrun_ms = getattr(stream_source, "underrun_ms", 0.0)
                split_ms = getattr(all_chunks[0], "split_ms", 0.0) if all_chunks else 0.0
                calc_headroom = round(min_buffer_remaining_ms if min_buffer_remaining_ms != float('inf') else 0.0, 1)
                gap_lag = round(abs(min(0.0, min_buffer_remaining_ms)) if min_buffer_remaining_ms != float('inf') else 0.0, 1)

                for cm in chunks_meta:
                    cm.update({k: v for k, v in chunk_extra.get(cm.get("chunk_index"), {}).items() if v is not None})

                latency_tracker.record(
                    user_name=item.user_name,
                    text=item.text,
                    speaker_name=spk_name,
                    provider=primary_prov,
                    worker_label=primary_worker,
                    t_discord_created=getattr(item, "discord_created_at", 0.0),
                    t_received=t_recv,
                    t_queued=getattr(item, "queued_at", 0.0),
                    t_dequeued=t_dequeued,
                    t_synth_start=c0_synth_start,
                    t_synth_end=c0_synth_end,
                    t_play_start=t_play_start,
                    is_cached=is_all_cached,
                    is_fallback=has_any_fallback,
                    chunks=chunks_meta,
                    ttfs_ms=round(turn_ttfs, 1),
                    turn_ttfs_ms=round(turn_ttfs, 1),
                    e2e_ttfs_ms=round(e2e_ttfs, 1),
                    underrun_ms=round(underrun_ms, 1),
                    split_ms=round(split_ms, 1),
                    buffer_headroom_ms=calc_headroom,
                    gap_lag_ms=gap_lag,
                    masking_ratio=masking_ratio,
                    total_audio_duration_ms=round(total_audio_duration * 1000.0, 1),
                    preroll_wait_ms=round(preroll_wait_ms, 1),
                    core_ttfs_ms=round(core_ttfs, 1),
                    initial_synth_ms=round(max(0.0, (c0_synth_end - c0_synth_start) * 1000.0), 1),
                    incident_context={
                        "guild_id": guild.id,
                        "guild_name": guild.name,
                        "speaker_id": item.speaker_id,
                        "expected_provider": getattr(voice_cfg, "provider", None),
                        "full_text": item.text,
                    },
                )
            except Exception as lat_err:
                logger.warning(f"Failed to record streaming latency: {lat_err}")

        finally:
            # 孤児タスク防止: 未完了のタスク（タイムアウトや切断・例外で残ったもの）を確実にキャンセル
            for t in all_created_tasks:
                if not t.done():
                    t.cancel()
            # 再生完了・中断・例外発生を問わず確実にバッファメモリを解放 (B-2)
            stream_source.cleanup()

        return True

    async def speak_alert(self, guild: discord.Guild, text: str, volume: float = 1.0) -> None:
        """防災速報等の最優先音声をVCで即時再生（現在の再生を停止して優先割り込み再生）"""
        state = self.get_guild_state(guild.id)
        vc: Optional[discord.VoiceClient] = guild.voice_client
        if not vc or not vc.is_connected():
            return

        # クリアで聞き取りやすい標準ボイスを選定 (Azure Nanami または初期話者)
        alert_speaker = "nanami" if "nanami" in self.config.voices else ("edge_nanami" if "edge_nanami" in self.config.voices else list(self.config.voices.keys())[0])

        now_ts = time.time()
        vol = max(0.0, min(float(volume), 2.0))
        alert_item = QueueItem(
            text=text,
            user_id=0,
            user_name="防災速報",
            speaker_id=alert_speaker,
            pitch="0%",
            speed=1.05,
            volume=vol,
            author_prefix=None,
            discord_created_at=now_ts,
            received_at=now_ts,
            queued_at=now_ts
        )

        # 現在再生中なら停止して割り込み
        if vc.is_playing():
            vc.stop()

        # キューの先頭に最優先で挿入 (O(1))
        state.push_front(alert_item)

    # --- メッセージ監視イベント ---

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        t_received = time.time()
        discord_created = message.created_at.timestamp() if message.created_at else t_received

        if not message.guild:
            return

        state = self.get_guild_state(message.guild.id)
        # VCに接続されていない、または紐付けテキストチャンネル以外の発言は無視
        if not state.bound_text_channel_id or message.channel.id != state.bound_text_channel_id:
            return

        vc = message.guild.voice_client
        if vc is None or not vc.is_connected():
            return

        # ユーザー発言を検知したため、アイドル時の知能的プリキャッシュを即座に中断＆タイマーリセット
        self._notify_user_speech()

        # 1. 無視判定 (Bot自身の発言、他Bot、コマンド行)
        if self.text_cleaner.should_ignore(message):
            return

        # メンバー情報＆アバターキャッシュを非同期で更新 (当該ギルドのみ・Discord API消費ゼロ)
        if isinstance(message.author, discord.Member):
            safe_create_task(
                cache_member_info_and_avatar(self.db, message.author, guild_id=message.guild.id, is_in_guild=True),
                name=f"cache_avatar_msg_{message.author.id}"
            )

        # 2. ギルド設定および辞書データの取得
        guild_settings = await self.db.get_guild_settings(message.guild.id, self.config)
        ignore_prefixes = guild_settings.get("ignore_prefixes")
        if ignore_prefixes is None:
            ignore_prefixes = getattr(self.config, "ignore_prefixes", [";", "；"])
        if ignore_prefixes and message.content.strip().startswith(tuple(ignore_prefixes)):
            logger.debug(f"Ignored message with prefix from {message.author.display_name}: {message.content[:20]}")
            return

        max_length = guild_settings.get("max_text_length", self.config.max_text_length)
        dict_words, ng_words = await asyncio.gather(
            self.db.get_dictionary_words(message.guild.id),
            self.db.get_ng_words(message.guild.id)
        )
        read_username = bool(guild_settings.get("read_username", 0))

        # 本文のサニタイズ (NGワードはピー音プレースホルダー <<BEEP>> に置換)
        cleaned_body = self.text_cleaner.clean(
            raw_text=message.content,
            guild=message.guild,
            dictionary_entries=dict_words,
            ng_words=ng_words,
            max_length=max_length
        )

        if not cleaned_body:
            return

        # NGワード検知リストの収集（本文）
        detected_ng_words: List[str] = list(self.text_cleaner.last_detected_ng_words)

        # 辞書が使用された場合、非同期で last_used_at を更新 (LRU管理用)
        if dict_words:
            used_words = [w for w, _ in dict_words if w in message.content]
            if used_words:
                safe_create_task(self.db.touch_dictionary_words(message.guild.id, used_words), name="touch_dictionary_words")

        # 未知語が検知された場合、バックグラウンドで自己学習タスクを発火 (音声再生は待たない)
        if self.text_cleaner.last_detected_unknown_words and self.config.gemini_api_key:
            unknown_to_learn = list(self.text_cleaner.last_detected_unknown_words)
            safe_create_task(self._learn_unknown_words(message.guild.id, unknown_to_learn, message.content), name="learn_unknown_words")

        # 3. スマート重複抑制 (本文の短文について指定秒数またはキュー内と重複していればスキップ)
        dedup_sec = guild_settings.get("dedup_window_seconds", self.config.dedup_window_seconds)
        dedup_len = guild_settings.get("dedup_max_length", self.config.dedup_max_length)
        current_queue_texts = state.get_queue_texts()
        if state.deduplicator.is_duplicate(cleaned_body, current_queue_texts, window_seconds=dedup_sec, max_length=dedup_len):
            logger.info(f"Skipped duplicate short phrase: '{cleaned_body}' from {message.author.display_name}")
            return

        # 4. ユーザー声質設定の取得 (ギルド個別設定 & 初回他サーバー引き継ぎ & 自動選定)
        available_speaker_ids = list(self.config.voices.keys())
        user_settings = await self.db.get_or_create_user_settings(
            user_id=message.author.id,
            available_speaker_ids=available_speaker_ids,
            default_pitch=self.config.default_pitch,
            default_speed=self.config.default_speed,
            guild_id=message.guild.id
        )

        # サーバーネーム読み上げがONの場合は、発言者表示名をサニタイズして独立プレフィックスパーツとして保持
        author_prefix = None
        if read_username:
            raw_display_name = message.author.display_name
            # 個人設定で「括弧除去 (【】（）等)」がONの場合 (デフォルトON: 例「【称号】ユーザー名【属性】」→「ユーザー名」)
            if user_settings.get("strip_name_brackets", True):
                stripped = _RE_STRIP_NAME_BRACKETS.sub("", raw_display_name).strip()
                if stripped:
                    raw_display_name = stripped

            cleaned_author = self.text_cleaner.clean(
                raw_text=raw_display_name,
                guild=message.guild,
                dictionary_entries=dict_words,
                ng_words=ng_words,
                max_length=20
            )
            if self.text_cleaner.last_detected_ng_words:
                for w in self.text_cleaner.last_detected_ng_words:
                    if w not in detected_ng_words:
                        detected_ng_words.append(w)

            # 「以下略」がついた場合は取り除く
            cleaned_author = cleaned_author.replace(" 以下略", "").strip()
            if cleaned_author:
                author_prefix = f"{cleaned_author}、"

        # NGワードが検知された場合（本文または発言者名）、メッセージに🤫リアクションを付与＆last_used_at更新
        if detected_ng_words:
            safe_create_task(self._add_reaction_safe(message, "🤫"), name=f"ng_react_{message.id}")
            safe_create_task(self.db.touch_ng_words(message.guild.id, detected_ng_words), name="touch_ng_words")

        # 5. 再生キューに追加 (本文と名前プレフィックスをスマート分離)
        spec_timeout = float(guild_settings.get("speculative_timeout_seconds", 2.5) or 2.5)
        item = QueueItem(
            text=cleaned_body,
            user_id=message.author.id,
            user_name=message.author.display_name,
            speaker_id=user_settings["speaker_id"],
            pitch=user_settings["pitch"],
            speed=user_settings["speed"],
            volume=float(user_settings.get("volume", 0.8)),
            author_prefix=author_prefix,
            discord_created_at=discord_created,
            received_at=t_received,
            queued_at=time.time(),
            speculative_timeout=spec_timeout,
        )

        state.deduplicator.record_added(cleaned_body)

        # バックグラウンド先行合成 (非同期プリフェッチ) のインテリジェント制御:
        # 1. 現在再生中ではなくキューも空の場合:
        #    直ちに _playback_loop -> _stream_play_item が起動して最速で第1文を合成・再生開始できるため、
        #    プリフェッチタスクは起動しない（同一リクエストの二重並行実行によるGPUセマフォ競合・自爆遅延を完全排除）。
        # 2. 現在他の発言が再生中またはキュー待ちの場合:
        #    前の発言の再生時間（待ち時間）を利用し、後続のストリーミング再生で必要となる全チャンクを
        #    文分割単位で並行先行合成してパーツキャッシュ（L1/L2）に温めておく。
        is_busy = state.is_playing or not state.queue.empty()
        if is_busy:
            self._share_chunks_for_prefetch(item)
            item.synth_task = safe_create_task(
                self._prefetch_chunks(item, speculative_timeout=spec_timeout),
                name=f"prefetch_chunks_{item.user_id}_{int(time.time()*1000)}"
            )
        else:
            item.synth_task = None

        await state.queue.put(item)

        # 再生ループタスクが停止していれば自己修復 (自動再起動)
        if state.play_task is None or state.play_task.done():
            logger.info(f"🔄 [Self-Healing] Playback loop for guild '{message.guild.name}' was inactive. Auto-restarting...")
            state.play_task = safe_create_task(
                self._playback_loop(message.guild, state),
                name=f"playback_loop_{message.guild.id}"
            )

        log_text = f"[{author_prefix}]{cleaned_body}" if author_prefix else cleaned_body
        logger.debug(f"Queued message from {message.author.display_name}: '{log_text}' (Queue size: {state.queue.qsize()})")

    def _schedule_prewarm_batch(self, guild: discord.Guild, member: discord.Member) -> None:
        """複数ユーザー同時入室時の過負荷を防ぐため、2.0秒のデバウンスバッチで事前ウォームアップをスケジュール"""
        guild_id = guild.id
        if guild_id not in self._pending_prewarm_members:
            self._pending_prewarm_members[guild_id] = set()
        self._pending_prewarm_members[guild_id].add(member.id)

        # すでにバッチ待機タスクが走っていれば追加メンバーも一緒に処理されるため待機
        existing_task = self._prewarm_batch_tasks.get(guild_id)
        if existing_task and not existing_task.done():
            return

        self._prewarm_batch_tasks[guild_id] = safe_create_task(
            self._execute_prewarm_batch(guild),
            name=f"prewarm_batch_guild_{guild_id}"
        )

    async def _execute_prewarm_batch(self, guild: discord.Guild) -> None:
        """2.0秒待機後に蓄積された入室メンバーのモデルウォームアップおよび発言者名事前キャッシュを一括実行"""
        guild_id = guild.id
        try:
            # 2.0秒待機（この間に連続入室したメンバーを1つのバッチに集約）
            await asyncio.sleep(2.0)

            member_ids = list(self._pending_prewarm_members.pop(guild_id, set()))
            if not member_ids:
                return

            logger.info(f"🚀 [Pre-warm Batch] Processing aggregated pre-warm for {len(member_ids)} member(s) in '{guild.name}' (IDs: {member_ids})")

            # 1. ローカルエンジン起動・外部GPUワーカーへのモデル事前ロード（一括重複排除実行）
            if hasattr(self.tts_manager, "resource_manager") and self.tts_manager.resource_manager:
                await self.tts_manager.resource_manager.prewarm_for_users(guild_id, member_ids)

            # 2. 発言者名（「〇〇さん、」）の事前キャッシュを低優先度（セマフォ直列）で安全に生成
            await asyncio.sleep(1.0)
            for uid in member_ids:
                m = guild.get_member(uid)
                if not m or m.bot:
                    continue
                vc = guild.voice_client
                if not vc or not vc.channel or m not in vc.channel.members:
                    continue

                safe_create_task(
                    self._cache_member_name_prefix(guild, m),
                    name=f"cache_name_{m.id}"
                )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Error in prewarm batch for guild {guild_id}: {e}")
        finally:
            self._prewarm_batch_tasks.pop(guild_id, None)

    async def _cache_member_name_prefix(self, guild: discord.Guild, member: discord.Member) -> None:
        """発言者名（「〇〇さん、」）のキャッシュ生成（セマフォで直列化し、通常合成への影響を最小化）"""
        async with self._name_cache_semaphore:
            try:
                available_speaker_ids = list(self.config.voices.keys())
                settings = await self.db.get_or_create_guild_user_settings(
                    guild_id=guild.id,
                    user_id=member.id,
                    available_speaker_ids=available_speaker_ids,
                    default_pitch=self.config.default_pitch,
                    default_speed=self.config.default_speed
                )
                spk_id = settings.get("speaker_id")
                if not spk_id:
                    return

                raw_display_name = member.display_name
                if settings.get("strip_name_brackets", True):
                    stripped = _RE_STRIP_NAME_BRACKETS.sub("", raw_display_name).strip()
                    if stripped:
                        raw_display_name = stripped
                ng_words = await self.db.get_ng_words(guild.id)
                cleaned_author = self.text_cleaner.clean(
                    raw_text=raw_display_name,
                    guild=guild,
                    dictionary_entries=[],
                    ng_words=ng_words,
                    max_length=20
                ).replace(" 以下略", "").strip()

                if cleaned_author:
                    # NGワードピー音プレースホルダーが含まれる場合は事前キャッシュを行わない（発言時に動的PCMフィードするため）
                    if '<<__BEEP' in cleaned_author or '<<ピー音' in cleaned_author:
                        return
                    name_prefix = f"{cleaned_author}、"
                    # 全文キャッシュの有無を事前チェック（存在すれば合成不要）
                    cache_key = CacheManager.generate_cache_key(
                        speaker_id=spk_id,
                        pitch=settings.get("pitch", "0%"),
                        speed=float(settings.get("speed", 1.0)),
                        text=name_prefix,
                        user_id=member.id
                    )
                    voice_cfg = self.config.voices.get(spk_id)
                    provider = getattr(voice_cfg, "provider", "")
                    cached = await self.cache_manager.get(cache_key, provider=provider)
                    if cached:
                        logger.debug(f"[Pre-warm] Name prefix '{name_prefix}' already in cache for {member.display_name}")
                        return

                    await asyncio.wait_for(
                        self.tts_manager.get_audio_path(
                            user_id=member.id,
                            speaker_id=spk_id,
                            pitch=settings.get("pitch", "0%"),
                            speed=float(settings.get("speed", 1.0)),
                            text=name_prefix
                        ),
                        timeout=5.0
                    )
                    logger.info(f"✨ [Pre-warm] Cached author prefix for '{member.display_name}': '{name_prefix}' ({spk_id})")
            except asyncio.TimeoutError:
                logger.debug(f"[Pre-warm] Name cache timed out (5s) for member {member.id} ({member.display_name}), skipping pre-warm")
            except Exception as e:
                logger.debug(f"Pre-warm name cache failed for member {member.id}: {e}")

    async def _enqueue_vc_event_announcement(
        self,
        guild: discord.Guild,
        member: discord.Member,
        event_type: str  # "join" または "leave"
    ) -> None:
        """VC入退室イベントのアナウンス音声を再生キューに投入 (announce_vc_events == 1 の場合のみ)"""
        try:
            guild_settings = await self.db.get_guild_settings(guild.id, self.config)
            if not guild_settings.get("announce_vc_events", 0):
                return

            vc = guild.voice_client
            if vc is None or not vc.is_connected():
                return

            state = self.get_guild_state(guild.id)

            # 単語辞書を取得して名前に適用
            dict_words = await self.db.get_dictionary_words(guild.id)
            cleaned_name = self.text_cleaner.clean(
                raw_text=member.display_name,
                guild=guild,
                dictionary_entries=dict_words,
                max_length=20
            )
            cleaned_name = cleaned_name.replace(" 以下略", "").strip()
            cleaned_name = _RE_STRIP_TRAILING_BRACKETS.sub("", cleaned_name).strip()
            if not cleaned_name:
                cleaned_name = "誰か"

            action = "入室しました" if event_type == "join" else "退出しました"
            announce_text = f"{cleaned_name}さんが{action}"

            speaker_id = guild_settings.get("default_speaker", self.config.default_speaker)
            if speaker_id not in self.config.voices:
                speaker_id = self.config.default_speaker

            t_now = time.time()
            spec_timeout = float(guild_settings.get("speculative_timeout_seconds", 2.5) or 2.5)
            item = QueueItem(
                text=announce_text,
                user_id=member.id,
                user_name="システム",
                speaker_id=speaker_id,
                pitch="0",
                speed=1.05,
                volume=0.8,
                author_prefix="",
                discord_created_at=t_now,
                received_at=t_now,
                queued_at=t_now,
                speculative_timeout=spec_timeout,
            )

            # キュー待機が生じる場合のみバックグラウンド先行合成
            if state.is_playing or not state.queue.empty():
                self._share_chunks_for_prefetch(item)
                item.synth_task = safe_create_task(
                    self._prefetch_chunks(item, speculative_timeout=spec_timeout),
                    name=f"prefetch_vc_event_{item.user_id}_{int(t_now*1000)}"
                )
            await state.queue.put(item)

            if state.play_task is None or state.play_task.done():
                state.play_task = safe_create_task(
                    self._playback_loop(guild, state),
                    name=f"playback_loop_{guild.id}"
                )
            logger.info(f"📢 [VC Event] Queued {event_type} announcement: '{announce_text}' in guild '{guild.name}'")
        except Exception as e:
            logger.warning(f"Failed to queue VC event announcement for {member.display_name}: {e}")

    async def _handle_bot_voice_leave(self, guild: discord.Guild, before_channel: discord.abc.GuildChannel) -> None:
        """Bot自身がVCから退出したイベントの検証（回線瞬断再接続と管理者キックの分離調停）"""
        try:
            # 3.5秒待機して discord.py の VoiceClient 再接続処理の成否を待つ
            await asyncio.sleep(3.5)

            vc = guild.voice_client
            # 1. すでに再接続が成功している、または接続処理中であれば何もしない
            if vc and (vc.is_connected() or getattr(vc, "_connecting", False)):
                ch_name = vc.channel.name if vc.channel else "unknown"
                logger.info(f"✨ [Bot #{self.bot_index}] Voice connection preserved/reconnected to '{ch_name}' in '{guild.name}'. Cleanup aborted.")
                return

            # 2. まだ再接続できていない場合、VCに人間が残っているかチェック
            ch = guild.get_channel(before_channel.id) if hasattr(before_channel, "id") else None
            human_count = len([m for m in ch.members if not m.bot]) if ch and hasattr(ch, "members") else 0

            if human_count > 0:
                # 人間がまだVCにいる！これは管理者キックではなく一時的な回線切断事故。
                # セッションを破棄せず、自動再接続を試みる
                logger.warning(
                    f"🚨 [Bot #{self.bot_index}] Disconnected from VC '{before_channel.name}' in '{guild.name}', but {human_count} human(s) still present! "
                    f"Attempting self-healing auto-reconnect..."
                )
                state = self.get_guild_state(guild.id)
                bound_text_id = state.bound_text_channel_id
                if not bound_text_id:
                    bound_text_id = await self._resolve_bound_text_channel(guild, before_channel.id)
                text_ch = guild.get_channel(bound_text_id) if bound_text_id else None

                if ch and text_ch and isinstance(ch, (discord.VoiceChannel, discord.StageChannel)) and isinstance(text_ch, discord.TextChannel):
                    try:
                        await self.connect_vc(ch, text_ch)
                        logger.info(f"✨ [Bot #{self.bot_index}] Auto-reconnected to VC '{ch.name}' in '{guild.name}' successfully.")
                        return
                    except Exception as e:
                        logger.error(f"[Bot #{self.bot_index}] Failed to auto-reconnect to VC '{ch.name}': {e}")
                return

            # 3. 人間が0人、または再接続不要の場合のみ正式クリーンアップ
            logger.info(
                f"🛑 [Bot #{self.bot_index}] Confirmed disconnection from VC '{before_channel.name}' in '{guild.name}' (0 humans). Cleaning up..."
            )
            await self.disconnect_vc(guild)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error in _handle_bot_voice_leave for guild {guild.id}: {e}")
        finally:
            self._pending_bot_leave_tasks.pop(guild.id, None)

    async def _auto_join_reconcile_loop(self) -> None:
        """自動参加対象VCの定期整合性チェック（人がいるのにBotがいないVCへ自動接続）"""
        await self.bot.wait_until_ready()
        logger.info(f"🚀 [AutoJoinReconcile] Started auto-join reconcile loop on Bot #{self.bot_index}")
        while not self.bot.is_closed():
            try:
                await asyncio.sleep(15)  # 15秒周期で走査
                if self.bot_index != 1:  # 1号機のみが集中調停
                    continue

                for guild in list(self.bot.guilds):
                    guild_settings = await self.db.get_guild_settings(guild.id, self.config)
                    pairs = guild_settings.get("auto_join_pairs", [])
                    if not pairs:
                        avc = int(guild_settings.get("auto_join_vc_id", 0) or 0)
                        atxt = int(guild_settings.get("auto_join_text_channel_id", 0) or 0)
                        if avc > 0 and atxt > 0:
                            pairs = [{"vc_id": avc, "text_id": atxt}]

                    for p in pairs:
                        target_vc_id = int(p.get("vc_id", 0))
                        target_text_id = int(p.get("text_id", 0))
                        if not target_vc_id or not target_text_id:
                            continue

                        vc_ch = guild.get_channel(target_vc_id)
                        if not isinstance(vc_ch, (discord.VoiceChannel, discord.StageChannel)):
                            continue

                        # 人間（非Bot）がいるか？
                        human_count = len([m for m in vc_ch.members if not m.bot])
                        if human_count == 0:
                            # 通話終了: 手動切断フラグをリセット (次回新しい通話時には再度自動参加可能に)
                            if self.bot_pool:
                                self.bot_pool.clear_vc_manual_disconnect(guild.id, target_vc_id)
                            continue

                        # 手動切断されたVCは、通話が終了する（人間が0人になる）まで自動再接続を抑止
                        if self.bot_pool and self.bot_pool.is_vc_manually_disconnected(guild.id, target_vc_id):
                            continue

                        # すでに自プールのBotが接続中またはインフライト接続中か？
                        if self.bot_pool:
                            if self.bot_pool.find_connected_or_connecting_bot_in_vc(guild.id, target_vc_id):
                                continue
                        else:
                            vc = guild.voice_client
                            if vc and vc.is_connected() and vc.channel and vc.channel.id == target_vc_id:
                                continue

                        # 人がいるのにBotがいない！空いているBotを探索
                        assigned_idx = 1
                        assigned_bot = None
                        if self.bot_pool:
                            avail = self.bot_pool.find_available_bot(guild.id, target_vc_id=target_vc_id)
                            if avail:
                                assigned_idx, assigned_bot = avail
                            else:
                                continue
                        else:
                            if guild.voice_client is None or not guild.voice_client.is_connected():
                                assigned_bot = self.bot
                                assigned_idx = 1

                        if not assigned_bot:
                            continue

                        target_vc_cog = assigned_bot.get_cog("VCHandlerCog") if assigned_bot else self
                        target_guild = assigned_bot.get_guild(guild.id) if assigned_bot else guild
                        bot_vc = target_guild.get_channel(target_vc_id) if target_guild else vc_ch
                        bot_text = target_guild.get_channel(target_text_id) if target_guild else guild.get_channel(target_text_id)

                        if isinstance(bot_vc, (discord.VoiceChannel, discord.StageChannel)) and isinstance(bot_text, discord.TextChannel):
                            logger.info(
                                f"🔄 [AutoJoinReconcile] Detected {human_count} human(s) in auto-join VC '{bot_vc.name}' of '{guild.name}' without Bot. "
                                f"Auto-joining with Bot #{assigned_idx} (bound text: #{bot_text.name})..."
                            )
                            try:
                                await target_vc_cog.connect_vc(bot_vc, bot_text)
                            except VCConnectAborted as e:
                                logger.info(f"[AutoJoinReconcile] Join to VC '{bot_vc.name}' with Bot #{assigned_idx} cancelled: {e}")
                                continue
                            except Exception as e:
                                logger.error(f"[AutoJoinReconcile] Failed to join VC '{bot_vc.name}' with Bot #{assigned_idx}: {e}")
                                continue

                            dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8088") or "http://localhost:8088"
                            bot_label = f" (Bot {assigned_idx}号機)" if self.bot_pool and len(self.bot_pool.tokens) > 1 else ""
                            embed = discord.Embed(
                                title=f"🔊 ボイスチャンネル自動接続完了{bot_label}",
                                description=(
                                    f"**{bot_vc.name}** に自動接続しました！\n"
                                    f"**#{bot_text.name}** のテキストを読み上げます。\n\n"
                                    f"💡 [Webダッシュボード]({dashboard_url}) から声質のカスタマイズや単語辞書の登録が可能です。"
                                ),
                                color=discord.Color.green()
                            )
                            try:
                                await bot_text.send(embed=embed)
                            except Exception as e:
                                logger.warning(f"[AutoJoinReconcile] Failed to send announcement in {bot_text.name}: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in _auto_join_reconcile_loop: {e}")

    # --- 自動参加・自動退出イベント ---

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState
    ) -> None:
        guild = member.guild

        # 0. Bot自身のボイス状態更新ハンドリング (管理者による強制移動・キック対応)
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None:
                # Discordの回線瞬断やVoice WebSocket再接続中の一時的イベントを誤認しないよう、
                # 即時切断せず遅延検証を行う
                vc_ch = before.channel
                logger.info(
                    f"⚠️ [Bot #{self.bot_index}] Received voice leave event from VC '{vc_ch.name}' in '{guild.name}'. Verifying if temporary reconnection..."
                )
                prev_task = self._pending_bot_leave_tasks.get(guild.id)
                if prev_task and not prev_task.done():
                    prev_task.cancel()
                self._pending_bot_leave_tasks[guild.id] = safe_create_task(
                    self._handle_bot_voice_leave(guild, vc_ch),
                    name=f"verify_bot_leave_{guild.id}_{self.bot_index}"
                )
            elif after.channel is not None and before.channel is not None and before.channel.id != after.channel.id:
                # 管理者等により別VCへ強制移動された
                logger.info(
                    f"🔀 [Bot #{self.bot_index}] Moved to VC '{after.channel.name}' in '{guild.name}'. Updating state..."
                )
                state = self.get_guild_state(guild.id)
                # ペア設定があるか確認
                new_text_id = await self._resolve_bound_text_channel(guild, after.channel.id)
                if new_text_id:
                    state.bound_text_channel_id = new_text_id
                    new_text_ch = guild.get_channel(new_text_id)
                    if new_text_ch and isinstance(new_text_ch, discord.TextChannel):
                        try:
                            embed = discord.Embed(
                                title=f"🔀 ボイスチャンネル移動検知 (Bot #{self.bot_index})",
                                description=(
                                    f"**{after.channel.name}** への移動に伴い、"
                                    f"読み上げ対象チャンネルを **#{new_text_ch.name}** に切り替えました。"
                                ),
                                color=discord.Color.blue()
                            )
                            safe_create_task(
                                new_text_ch.send(embed=embed),
                                name=f"announce_move_{guild.id}_{int(time.time()*1000)}"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send text channel switch announcement: {e}")

                # DBセッション情報更新
                if state.bound_text_channel_id:
                    await self.db.save_active_vc(
                        guild.id, after.channel.id, state.bound_text_channel_id, bot_index=self.bot_index
                    )
                # ニックネーム同期
                self.schedule_nickname_sync(guild)
            return

        if member.bot:
            return

        # メンバー情報＆アバターキャッシュを非同期で更新 (当該ギルドのみ・Discord API消費ゼロ)
        safe_create_task(
            cache_member_info_and_avatar(self.db, member, guild_id=guild.id, is_in_guild=True),
            name=f"cache_avatar_vs_{member.id}"
        )

        # 1. 自動退出判定: Botが参加中のVCから人間が0人になったら退出
        vc: Optional[discord.VoiceClient] = guild.voice_client

        if vc and vc.channel:
            # Bot以外の非Botメンバー数をカウント
            human_members = [m for m in vc.channel.members if not m.bot]
            if len(human_members) == 0:
                logger.info(f"No human members left in VC '{vc.channel.name}' of '{guild.name}'. Auto-disconnecting...")
                if self.bot_pool:
                    self.bot_pool.clear_vc_manual_disconnect(guild.id, vc.channel.id)
                await self.disconnect_vc(guild)
                return

            # ユーザーがBotと同じVCに入室した瞬間なら先行プリウォーム＆名前事前キャッシュをバッチ実行 (2.0秒デバウンス)
            if after.channel and after.channel.id == vc.channel.id and (before.channel is None or before.channel.id != after.channel.id):
                self._schedule_prewarm_batch(guild, member)
                # VC入室アナウンス (announce_vc_events == 1 の場合のみ)
                safe_create_task(
                    self._enqueue_vc_event_announcement(guild, member, "join"),
                    name=f"vc_announce_join_{member.id}_{int(time.time()*1000)}"
                )

            # ユーザーがBotと同じVCから退出した瞬間（かつまだ人間が残っている）
            elif before.channel and before.channel.id == vc.channel.id and (after.channel is None or after.channel.id != vc.channel.id):
                # VC退出アナウンス (announce_vc_events == 1 の場合のみ)
                safe_create_task(
                    self._enqueue_vc_event_announcement(guild, member, "leave"),
                    name=f"vc_announce_leave_{member.id}_{int(time.time()*1000)}"
                )

        # 人間がVCから退出した際、もしそのVCの人間が0人になっていれば手動切断フラグをリセット (通話セッション終了)
        if before.channel and (after.channel is None or before.channel.id != after.channel.id):
            remaining = [m for m in before.channel.members if not m.bot]
            if len(remaining) == 0 and self.bot_pool:
                self.bot_pool.clear_vc_manual_disconnect(guild.id, before.channel.id)

        # 2. 自動参加判定: メインBot (1号機) が集中監視し、空いている号機を動的アサイン
        if self.bot_index == 1:
            # ユーザーがVCに参加した瞬間 (before と after でチャンネルが異なる)
            if after.channel and (before.channel is None or before.channel.id != after.channel.id):
                guild_settings = await self.db.get_guild_settings(guild.id, self.config)
                pairs = guild_settings.get("auto_join_pairs", [])
                if not pairs:
                    avc = int(guild_settings.get("auto_join_vc_id", 0) or 0)
                    atxt = int(guild_settings.get("auto_join_text_channel_id", 0) or 0)
                    if avc > 0 and atxt > 0:
                        pairs = [{"vc_id": avc, "text_id": atxt}]

                matched_pair = None
                for p in pairs:
                    if int(p.get("vc_id", 0)) == after.channel.id:
                        matched_pair = p
                        break

                if matched_pair:
                    target_text_id = int(matched_pair.get("text_id", 0))
                    target_vc_id = after.channel.id

                    # 手動切断により抑止中かチェック (通話中に /leave された場合は通話終了まで再入室しない)
                    if self.bot_pool and self.bot_pool.is_vc_manually_disconnected(guild.id, target_vc_id):
                        logger.debug(f"[AutoJoin] Auto-join suppressed for VC {target_vc_id} in '{guild.name}' (manually disconnected).")
                        return

                    # 対象VCにいずれかのBotがすでに接続中、またはインフライト接続中かチェック
                    already_connected = False
                    if self.bot_pool:
                        if self.bot_pool.find_connected_or_connecting_bot_in_vc(guild.id, target_vc_id):
                            already_connected = True
                    else:
                        if vc is not None and vc.is_connected() and vc.channel and vc.channel.id == target_vc_id:
                            already_connected = True

                    if not already_connected:
                        assigned_bot = None
                        assigned_idx = 1
                        if self.bot_pool:
                            avail = self.bot_pool.find_available_bot(guild.id, target_vc_id=target_vc_id)
                            if avail:
                                assigned_idx, assigned_bot = avail
                            else:
                                logger.warning(f"All bots in pool are busy. Cannot auto-join VC '{after.channel.name}' in '{guild.name}'.")
                        else:
                            if vc is None or not vc.is_connected():
                                assigned_bot = self.bot
                                assigned_idx = 1

                        if assigned_bot:
                            target_vc_cog = assigned_bot.get_cog("VCHandlerCog") if assigned_bot else self
                            target_guild = assigned_bot.get_guild(guild.id) if assigned_bot else guild
                            bot_vc = target_guild.get_channel(target_vc_id) if target_guild else after.channel
                            bot_text = target_guild.get_channel(target_text_id) if target_guild else guild.get_channel(target_text_id)

                            if isinstance(bot_vc, (discord.VoiceChannel, discord.StageChannel)) and isinstance(bot_text, discord.TextChannel):
                                logger.info(
                                    f"User {member.display_name} joined auto-join target VC '{bot_vc.name}' in '{guild.name}'. "
                                    f"Auto-joining with Bot #{assigned_idx} (bound text: #{bot_text.name})..."
                                )
                                try:
                                    await target_vc_cog.connect_vc(bot_vc, bot_text)
                                except VCConnectAborted as e:
                                    logger.info(f"Auto-join to VC '{bot_vc.name}' with Bot #{assigned_idx} cancelled: {e}")
                                    return
                                except Exception as e:
                                    logger.error(
                                        f"Failed to auto-join VC '{bot_vc.name}' with Bot #{assigned_idx}: {e}"
                                    )
                                    return

                            dashboard_url = getattr(self.config, "web_dashboard_url", "http://localhost:8088") or "http://localhost:8088"
                            bot_label = f" (Bot {assigned_idx}号機)" if self.bot_pool and len(self.bot_pool.tokens) > 1 else ""
                            embed = discord.Embed(
                                title=f"🔊 ボイスチャンネル自動接続完了{bot_label}",
                                description=(
                                    f"**{bot_vc.name}** に自動接続しました！\n"
                                    f"**#{bot_text.name}** のテキストを読み上げます。\n\n"
                                    f"💡 [Webダッシュボード]({dashboard_url}) から声質のカスタマイズや単語辞書の登録が可能です。"
                                ),
                                color=discord.Color.green()
                            )
                            try:
                                await bot_text.send(embed=embed)
                            except Exception as e:
                                logger.warning(f"Failed to send auto-join announcement in {bot_text.name}: {e}")

    # --- 起動時VC復旧リスナー ---

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Bot起動完了時に前回のVCセッションを復旧"""
        # 内部ギルドキャッシュが完全に揃うまで少し待機
        await asyncio.sleep(2)
        await self.restore_active_vcs()

        # 起動時ニックネーム整合性チェック (全ギルド走査・未接続Botの取り残し修復)
        for guild in list(self.bot.guilds):
            self.schedule_nickname_sync(guild)

        # ニックネーム定期整合性修復ループを起動 (未起動時のみ)
        if self._nick_reconcile_task is None or self._nick_reconcile_task.done():
            self._nick_reconcile_task = safe_create_task(
                self._nickname_reconcile_loop(),
                name=f"nick_reconcile_loop_bot{self.bot_index}"
            )

        # 自動参加定期整合性修復ループを起動 (1号機かつ未起動時のみ)
        if self.bot_index == 1:
            if self._auto_join_reconcile_task is None or self._auto_join_reconcile_task.done():
                self._auto_join_reconcile_task = safe_create_task(
                    self._auto_join_reconcile_loop(),
                    name=f"auto_join_reconcile_loop_bot{self.bot_index}"
                )

    @commands.Cog.listener()
    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel
    ) -> None:
        """VC名変更のリアルタイム検知とニックネーム即時追従 (完全イベント駆動)"""
        if not isinstance(after, (discord.VoiceChannel, discord.StageChannel)):
            return
        if before.name == after.name:
            return
        guild = after.guild
        vc = guild.voice_client
        if vc and vc.channel and vc.channel.id == after.id:
            logger.info(
                f"📝 [Bot #{self.bot_index}] VC renamed from '{before.name}' to '{after.name}' in '{guild.name}'. Syncing nickname..."
            )
            self.schedule_nickname_sync(guild)

    @commands.Cog.listener()
    async def on_guild_update(
        self,
        before: discord.Guild,
        after: discord.Guild
    ) -> None:
        """サーバー名変更のリアルタイム検知と通常時ニックネーム即時追従 (完全イベント駆動)"""
        if before.name == after.name:
            return
        logger.info(
            f"📝 [Bot #{self.bot_index}] Guild renamed from '{before.name}' to '{after.name}'. Syncing nickname..."
        )
        self.schedule_nickname_sync(after)

    # --- Gemini 2.5 Flash 自己学習パイプライン ---

    async def _learn_unknown_words(self, guild_id: int, words: List[str], context_text: str) -> None:
        """未知語のカタカナ読みを Gemini 2.5 Flash で非同期推論し、AI提案テーブルへ格納"""
        try:
            for word in words:
                res = await self.gemini_tutor.infer_unknown_word(word, context_text)
                if res and "reading" in res:
                    added = await self.db.add_ai_suggestion(
                        guild_id=guild_id,
                        word=res["word"],
                        suggested_reading=res["reading"],
                        reason=res.get("reason", "未知語からの推論"),
                        source="unknown_word"
                    )
                    if added:
                        logger.info(f"💡 [GeminiTutor] Learned unknown word '{res['word']}' -> '{res['reading']}' for guild {guild_id}")
        except Exception as e:
            logger.debug(f"[GeminiTutor] _learn_unknown_words error: {e}")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """ユーザーが ❌ (✕印) で誤読指摘リアクションをした場合にAI自己学習を発火"""
        if not self.config.gemini_api_key:
            return

        # Bot自身のリアクションは無視
        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        emoji_name = str(payload.emoji.name)
        if emoji_name not in ("❌", "✖️", "✖", "❎", "✕"):
            return

        if not payload.guild_id:
            return

        state = self.guild_states.get(payload.guild_id)
        if not state or not state.bound_text_channel_id:
            return

        # 読み上げ対象テキストチャンネルか確認
        if payload.channel_id != state.bound_text_channel_id:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        try:
            message = await channel.fetch_message(payload.message_id)
            if not message or message.author.bot or not message.content.strip():
                return

            # 非同期で誤読分析を実行 (イベントループをブロックしない)
            asyncio.create_task(self._learn_from_reaction(payload.guild_id, message))
        except Exception as e:
            logger.debug(f"[GeminiTutor] on_raw_reaction_add error: {e}")

    async def _learn_from_reaction(self, guild_id: int, message: discord.Message) -> None:
        """リアクション指摘メッセージから難読語・固有名詞を特定し、AI提案テーブルへ格納"""
        try:
            results = await self.gemini_tutor.analyze_misread_text(message.content)
            learned_count = 0
            for item in results:
                added = await self.db.add_ai_suggestion(
                    guild_id=guild_id,
                    word=item["word"],
                    suggested_reading=item["reading"],
                    reason=item.get("reason", "リアクション指摘からの推論"),
                    source="reaction"
                )
                if added:
                    learned_count += 1

            if learned_count > 0:
                logger.info(f"💡 [GeminiTutor] Extracted {learned_count} misread suggestions from reaction on msg {message.id}")
                # 学習完了のフィードバックとして 💡 を付与
                try:
                    await message.add_reaction("💡")
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[GeminiTutor] _learn_from_reaction error: {e}")

    def cog_unload(self) -> None:
        """Cogアンロード時のバックグラウンドタスク完全キャンセル"""
        if self._auto_join_reconcile_task and not self._auto_join_reconcile_task.done():
            self._auto_join_reconcile_task.cancel()
        if self._nick_reconcile_task and not self._nick_reconcile_task.done():
            self._nick_reconcile_task.cancel()
        for t in self._pending_bot_leave_tasks.values():
            if t and not t.done():
                t.cancel()
        self._pending_bot_leave_tasks.clear()


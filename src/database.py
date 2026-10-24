import aiosqlite
import sqlite3
import asyncio
import logging
import hashlib
import random
import json
import time
import re
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

# 初回自動選定用のクラウド話者プール (Azure & GCP)
CLOUD_INITIAL_SPEAKER_POOL = [
    "nanami", "keita", "aoi", "daichi", "mayu", "naoki", "shiori",
    "g_neural_b", "g_neural_c", "g_neural_d",
    "g_wavenet_a", "g_wavenet_c", "g_wavenet_d", "g_wavenet_b",
    "g_standard_a", "g_standard_b", "g_standard_c", "g_standard_d"
]


class SQLitePool:
    """
    SQLite WALモード用持続的コネクションプール
    ファイルオープンやPRAGMA設定のオーバーヘッドをゼロにし、DB遅延を30〜50%削減
    """
    def __init__(self, db_path: str, max_connections: int = 5):
        self.db_path = db_path
        self.max_connections = max_connections
        self._pool: asyncio.Queue = asyncio.Queue()
        self._size = 0
        self._lock = asyncio.Lock()
        self._is_closed = False

    async def _create_connection(self) -> aiosqlite.Connection:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.db_path, timeout=30.0)
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA busy_timeout=30000;")
        conn._last_used_at = time.monotonic()
        return conn

    @asynccontextmanager
    async def acquire(self):
        if self._is_closed:
            raise RuntimeError("Connection pool is closed")

        conn: Optional[aiosqlite.Connection] = None
        try:
            conn = self._pool.get_nowait()
        except asyncio.QueueEmpty:
            async with self._lock:
                if self._size < self.max_connections:
                    conn = await self._create_connection()
                    self._size += 1

            if conn is None:
                try:
                    conn = await asyncio.wait_for(self._pool.get(), timeout=10.0)
                except asyncio.TimeoutError:
                    raise TimeoutError("Database connection pool acquire timed out")

        # アイドルタイムアウト式死活チェック (PERF)
        # 直近30秒以内に使用された接続は健全とみなし、毎クエリ 0.3〜0.5ms の不要な SELECT 1 直列遅延を排除
        now = time.monotonic()
        last_used = getattr(conn, "_last_used_at", 0.0)
        is_healthy = True

        if (now - last_used) > 30.0:
            try:
                await conn.execute("SELECT 1")
            except Exception:
                is_healthy = False

        if not is_healthy:
            try:
                await conn.close()
            except Exception:
                pass
            async with self._lock:
                self._size -= 1
            conn = None
            try:
                new_conn = await self._create_connection()
                async with self._lock:
                    self._size += 1
                conn = new_conn
            except Exception:
                raise

        try:
            yield conn
        finally:
            if conn is not None:
                conn._last_used_at = time.monotonic()

            if not self._is_closed and conn is not None:
                try:
                    self._pool.put_nowait(conn)
                except Exception:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                    async with self._lock:
                        self._size -= 1
            elif conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass
                async with self._lock:
                    self._size -= 1

    async def close(self):
        self._is_closed = True
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                await conn.close()
            except Exception:
                pass
        self._size = 0


class Database:
    def __init__(self, db_path: str, max_connections: int = 5):
        self.db_path = db_path
        self.pool = SQLitePool(db_path, max_connections=max_connections)
        self._guild_settings_cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}
        self._dict_cache: Dict[int, Tuple[float, List[Tuple[str, str]]]] = {}
        self._ng_words_cache: Dict[int, Tuple[float, List[str]]] = {}
        self._user_settings_cache: Dict[Tuple[int, int], Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._cache_ttl: float = 300.0  # 5分間キャッシュ
        self._metric_delete_counter: int = 0
        self._latency_delete_counter: int = 0

    def get_connection(self):
        """プールから持続的DBコネクションを取得する非同期コンテキストマネージャー"""
        return self.pool.acquire()

    @property
    def is_closed(self) -> bool:
        """接続プールが閉じられているかどうか"""
        return getattr(self.pool, "_is_closed", False)

    async def close(self):
        """プール内の全接続を安全に解放 (直前に WAL チェックポイントを実行)"""
        try:
            async with self.get_connection() as db:
                await db.execute("PRAGMA wal_checkpoint(PASSIVE);")
                await db.execute("PRAGMA optimize;")
        except Exception:
            pass
        await self.pool.close()


    def invalidate_guild_cache(self, guild_id: Optional[int] = None) -> None:
        """ギルド設定、辞書、NGワードキャッシュを無効化"""
        if guild_id is None:
            self._guild_settings_cache.clear()
            self._dict_cache.clear()
            self._ng_words_cache.clear()
        else:
            self._guild_settings_cache.pop(guild_id, None)
            self._dict_cache.pop(guild_id, None)
            self._ng_words_cache.pop(guild_id, None)
            if guild_id == 0:
                self._dict_cache.clear()

    def invalidate_user_cache(self, user_id: Optional[int] = None, guild_id: Optional[int] = None) -> None:
        """ユーザー設定キャッシュを無効化"""
        if user_id is None:
            self._user_settings_cache.clear()
        elif guild_id is None:
            keys = [k for k in self._user_settings_cache if k[1] == user_id]
            for k in keys:
                self._user_settings_cache.pop(k, None)
        else:
            self._user_settings_cache.pop((guild_id, user_id), None)

    async def init_db(self) -> None:
        """データベースとテーブルの初期化およびマイグレーション"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self.get_connection() as db:
            # WALモード有効化 (マルチBot同時書き込み耐性 & ロック競合ゼロ)
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA busy_timeout=30000;")

            # 1. ユーザー設定 (グローバル/ベース)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    speaker_id TEXT NOT NULL,
                    pitch TEXT NOT NULL DEFAULT '0%',
                    speed REAL NOT NULL DEFAULT 1.0,
                    volume REAL NOT NULL DEFAULT 0.8,
                    strip_name_brackets INTEGER NOT NULL DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 1-2. ギルド別ユーザー設定 (サーバーごとに個別の声質を設定可能 & 初期設定引き継ぎ)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_user_settings (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    speaker_id TEXT NOT NULL,
                    pitch TEXT NOT NULL DEFAULT '0%',
                    speed REAL NOT NULL DEFAULT 1.0,
                    volume REAL NOT NULL DEFAULT 0.8,
                    strip_name_brackets INTEGER NOT NULL DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

            # user_settings & guild_user_settings の strip_name_brackets / volume マイグレーション
            async with db.execute("PRAGMA table_info(user_settings)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
                if "strip_name_brackets" not in cols:
                    await db.execute("ALTER TABLE user_settings ADD COLUMN strip_name_brackets INTEGER DEFAULT 1")
                    logger.info("Migrated user_settings: added strip_name_brackets column")
                if "volume" not in cols:
                    await db.execute("ALTER TABLE user_settings ADD COLUMN volume REAL DEFAULT 0.8")
                    logger.info("Migrated user_settings: added volume column")

            async with db.execute("PRAGMA table_info(guild_user_settings)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
                if "strip_name_brackets" not in cols:
                    await db.execute("ALTER TABLE guild_user_settings ADD COLUMN strip_name_brackets INTEGER DEFAULT 1")
                    logger.info("Migrated guild_user_settings: added strip_name_brackets column")
                if "volume" not in cols:
                    await db.execute("ALTER TABLE guild_user_settings ADD COLUMN volume REAL DEFAULT 0.8")
                    logger.info("Migrated guild_user_settings: added volume column")

            # 1-3. ギルドメンバーキャッシュ (WebUI表示用・アバターAVIF・在室判定・最新表示名)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_member_cache (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    avatar_hash TEXT DEFAULT '',
                    is_in_guild INTEGER NOT NULL DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_guild_member_cache_guild ON guild_member_cache(guild_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_guild_member_cache_user ON guild_member_cache(user_id);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_guild_member_cache_updated ON guild_member_cache(updated_at);")

            # 1-3-2. システム設定・メタ情報キーバリューストア (月2回バッチ最終実行日時等の永続化)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS system_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 1-4. キャッシュメタデータ (発言内容・話者・ヒット数・パーツ判定管理用)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_metadata (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    text TEXT NOT NULL,
                    speaker_id TEXT,
                    speaker_name TEXT,
                    is_chunk INTEGER DEFAULT 0,
                    duration REAL DEFAULT 0.0,
                    size_bytes INTEGER DEFAULT 0,
                    hit_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_hit_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_text ON cache_metadata(text);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_provider ON cache_metadata(provider);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_created ON cache_metadata(created_at);")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_cache_hits ON cache_metadata(hit_count);")

            # 2. ギルド別単語辞書
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_dictionary (
                    guild_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    reading TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, word)
                )
            """)

            # guild_dictionary の last_used_at マイグレーション (LRU管理用)
            async with db.execute("PRAGMA table_info(guild_dictionary)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
                if "last_used_at" not in cols:
                    await db.execute("ALTER TABLE guild_dictionary ADD COLUMN last_used_at TIMESTAMP")
                    await db.execute("UPDATE guild_dictionary SET last_used_at = created_at WHERE last_used_at IS NULL")
                    logger.info("Migrated guild_dictionary: added last_used_at column for LRU management")

            # 2-2. AI提案単語テーブル (自己学習パイプライン & 辞書汚染防止)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ai_suggested_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    suggested_reading TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    source TEXT DEFAULT 'unknown_word',
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, word)
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_ai_suggested_status ON ai_suggested_words (guild_id, status);")

            # 2-3. ギルド別NGワードテーブル
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_ng_words (
                    guild_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, word)
                )
            """)

            # 3. ギルド個別設定
            await db.execute("""
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    command_prefix TEXT DEFAULT '!',
                    max_text_length INTEGER DEFAULT 50,
                    dedup_window_seconds REAL DEFAULT 5.0,
                    dedup_max_length INTEGER DEFAULT 10,
                    auto_join_vc_id INTEGER DEFAULT 0,
                    auto_join_text_channel_id INTEGER DEFAULT 0,
                    admin_role_id INTEGER DEFAULT 0,
                    default_speaker TEXT DEFAULT 'nanami',
                    read_username INTEGER DEFAULT 0,
                    announce_vc_events INTEGER DEFAULT 0,
                    speculative_timeout_seconds REAL DEFAULT 1.5,
                    ignore_prefixes TEXT DEFAULT '[";", "；"]',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 既存の guild_settings に read_username, auto_join_pairs, announce_vc_events 等がない場合のマイグレーション
            async with db.execute("PRAGMA table_info(guild_settings)") as cur:
                cols = [row[1] for row in await cur.fetchall()]
                if "read_username" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN read_username INTEGER DEFAULT 0")
                    logger.info("Migrated guild_settings table: added read_username column")
                if "announce_vc_events" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN announce_vc_events INTEGER DEFAULT 0")
                    logger.info("Migrated guild_settings table: added announce_vc_events column")
                if "auto_join_pairs" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN auto_join_pairs TEXT DEFAULT '[]'")
                    logger.info("Migrated guild_settings table: added auto_join_pairs column")
                if "disaster_alert_enabled" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN disaster_alert_enabled INTEGER DEFAULT 1")
                    logger.info("Migrated guild_settings table: added disaster_alert_enabled column")
                if "disaster_min_scale" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN disaster_min_scale INTEGER DEFAULT 30")
                    logger.info("Migrated guild_settings table: added disaster_min_scale column")
                if "disaster_pref_filter" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN disaster_pref_filter TEXT DEFAULT ''")
                    logger.info("Migrated guild_settings table: added disaster_pref_filter column")
                if "disaster_alert_weather" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN disaster_alert_weather INTEGER DEFAULT 1")
                    logger.info("Migrated guild_settings table: added disaster_alert_weather column")
                if "disaster_volume" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN disaster_volume REAL DEFAULT 1.0")
                    logger.info("Migrated guild_settings table: added disaster_volume column")
                if "ignore_prefixes" not in cols:
                    await db.execute('ALTER TABLE guild_settings ADD COLUMN ignore_prefixes TEXT DEFAULT \'[";", "；"]\'')
                    logger.info("Migrated guild_settings table: added ignore_prefixes column")
                if "speculative_timeout_seconds" not in cols:
                    await db.execute("ALTER TABLE guild_settings ADD COLUMN speculative_timeout_seconds REAL DEFAULT 1.5")
                    logger.info("Migrated guild_settings table: added speculative_timeout_seconds column")

            # 4. 月別TTS利用量トラッキング
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tts_usage (
                    month TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    characters INTEGER DEFAULT 0,
                    requests INTEGER DEFAULT 0,
                    PRIMARY KEY (month, provider)
                )
            """)

            # 5. 接続中VCセッション（再起動時の自動復旧用・マルチBot対応）
            async with db.execute("PRAGMA table_info(active_vc_sessions)") as cur:
                rows = await cur.fetchall()
                cols = [row[1] for row in rows]
                if not rows:
                    await db.execute("""
                        CREATE TABLE active_vc_sessions (
                            guild_id INTEGER NOT NULL,
                            bot_index INTEGER NOT NULL DEFAULT 1,
                            voice_channel_id INTEGER NOT NULL,
                            text_channel_id INTEGER NOT NULL,
                            connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, bot_index)
                        )
                    """)
                elif "bot_index" not in cols:
                    logger.info("Migrating active_vc_sessions table to multi-bot schema...")
                    await db.execute("""
                        CREATE TABLE active_vc_sessions_new (
                            guild_id INTEGER NOT NULL,
                            bot_index INTEGER NOT NULL DEFAULT 1,
                            voice_channel_id INTEGER NOT NULL,
                            text_channel_id INTEGER NOT NULL,
                            connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (guild_id, bot_index)
                        )
                    """)
                    await db.execute("""
                        INSERT OR IGNORE INTO active_vc_sessions_new (guild_id, bot_index, voice_channel_id, text_channel_id, connected_at)
                        SELECT guild_id, 1, voice_channel_id, text_channel_id, connected_at FROM active_vc_sessions
                    """)
                    await db.execute("DROP TABLE active_vc_sessions")
                    await db.execute("ALTER TABLE active_vc_sessions_new RENAME TO active_vc_sessions")
                    logger.info("Successfully migrated active_vc_sessions table to multi-bot schema.")

            # 6. 月別キャッシュ節約統計トラッキング (クラウド / ローカル分離)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tts_cache_savings (
                    month TEXT PRIMARY KEY,
                    saved_characters INTEGER DEFAULT 0,
                    hits INTEGER DEFAULT 0,
                    saved_cloud_characters INTEGER DEFAULT 0,
                    hits_cloud INTEGER DEFAULT 0,
                    saved_local_characters INTEGER DEFAULT 0,
                    hits_local INTEGER DEFAULT 0
                )
            """)

            # 既存テーブルがある場合のカラム自動マイグレーション
            async with db.execute("PRAGMA table_info(tts_cache_savings)") as cursor:
                cols = [row[1] for row in await cursor.fetchall()]
                if "saved_cloud_characters" not in cols:
                    await db.execute("ALTER TABLE tts_cache_savings ADD COLUMN saved_cloud_characters INTEGER DEFAULT 0")
                if "hits_cloud" not in cols:
                    await db.execute("ALTER TABLE tts_cache_savings ADD COLUMN hits_cloud INTEGER DEFAULT 0")
                if "saved_local_characters" not in cols:
                    await db.execute("ALTER TABLE tts_cache_savings ADD COLUMN saved_local_characters INTEGER DEFAULT 0")
                if "hits_local" not in cols:
                    await db.execute("ALTER TABLE tts_cache_savings ADD COLUMN hits_local INTEGER DEFAULT 0")

                # 過去の未分類キャッシュ節約データをクラウドに初期割り当て
                await db.execute("""
                    UPDATE tts_cache_savings
                    SET saved_cloud_characters = saved_characters,
                        hits_cloud = hits
                    WHERE saved_cloud_characters = 0 AND hits_cloud = 0 AND (saved_characters > 0 OR hits > 0)
                """)

            # 既存の旧 dictionary テーブルが存在する場合は移行 (guild_id=0: グローバルとして移行)
            async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dictionary'") as cursor:
                old_table = await cursor.fetchone()
                if old_table:
                    await db.execute("""
                        INSERT OR IGNORE INTO guild_dictionary (guild_id, word, reading, created_at)
                        SELECT 0, word, reading, created_at FROM dictionary
                    """)

            # 7. システムメトリクス履歴 (再起動後のWebUIグラフ復元・アラート用)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS metrics_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    time_str TEXT NOT NULL,
                    cpu_percent REAL NOT NULL,
                    mem_percent REAL NOT NULL,
                    data_json TEXT NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_metrics_timestamp ON metrics_history(timestamp);")

            # 8. Graceful Shutdown 再生キュー保全 (再起動時の復元用)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS pending_playback_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    bot_index INTEGER NOT NULL DEFAULT 1,
                    item_order INTEGER NOT NULL,
                    item_json TEXT NOT NULL,
                    saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_pending_queue_guild ON pending_playback_queue(guild_id, bot_index);")

            # 9. 発話〜再生レイテンシ履歴 (再起動後のWebUI復元用)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS playback_latency_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    record_json TEXT NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_playback_latency_created ON playback_latency_history(created_at);")

            # 9-2. 遅延・アンダーラン等の状態悪化インシデント (原因調査用・最大1000件/30日保持)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS latency_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    guild_id INTEGER,
                    incident_types TEXT,
                    root_causes TEXT,
                    incident_json TEXT NOT NULL
                )
            """)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_latency_incidents_created ON latency_incidents(created_at);")

            # 10. スキーママイグレーション管理テーブル
            await db.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 11. システム認証情報・APIキー・設定 (WebUIで管理可能)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS system_credentials (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 12. Discord Botトークンプール (最大10台動的スケーリング)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_tokens (
                    bot_index INTEGER PRIMARY KEY,
                    token TEXT NOT NULL,
                    label TEXT DEFAULT '',
                    is_enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # マイグレーション: 全ユーザーの話者を Azure / GCP クラウドプールから再選定
            async with db.execute("SELECT 1 FROM schema_migrations WHERE version = '20261002_reassign_cloud_speakers'") as cur:
                if not await cur.fetchone():
                    logger.info("Applying migration: reassigning all users to Azure/GCP cloud speakers...")
                    pitch_choices = ["-15%", "-10%", "-5%", "0%", "+5%", "+10%", "+15%"]
                    speed_choices = [0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]
                    async with db.execute("SELECT DISTINCT user_id FROM user_settings UNION SELECT DISTINCT user_id FROM guild_user_settings") as ucur:
                        u_rows = await ucur.fetchall()
                        for u_row in u_rows:
                            uid = u_row[0]
                            c_spk = random.choice(CLOUD_INITIAL_SPEAKER_POOL)
                            c_pitch = random.choice(pitch_choices)
                            c_speed = random.choice(speed_choices)
                            await db.execute(
                                "UPDATE user_settings SET speaker_id = ?, pitch = ?, speed = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                                (c_spk, c_pitch, c_speed, uid)
                            )
                            await db.execute(
                                "UPDATE guild_user_settings SET speaker_id = ?, pitch = ?, speed = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                                (c_spk, c_pitch, c_speed, uid)
                            )
                    await db.execute("INSERT INTO schema_migrations (version) VALUES ('20261002_reassign_cloud_speakers')")
                    logger.info(f"Migration completed: reassigned {len(u_rows)} users to Azure/GCP cloud speakers.")

            # マイグレーション: 実在しない ja-JP-Neural2-F (g_neural_f) を設定しているユーザーを実在するクラウド女声へ再選定
            async with db.execute("SELECT 1 FROM schema_migrations WHERE version = '20261003_fix_invalid_g_neural_f'") as cur:
                if not await cur.fetchone():
                    logger.info("Applying migration: replacing invalid 'g_neural_f' with valid cloud female voices...")
                    valid_pool = ["g_neural_b", "nanami", "mayu", "aoi", "g_wavenet_b", "shiori"]
                    async with db.execute("SELECT user_id FROM user_settings WHERE speaker_id = 'g_neural_f'") as ucur:
                        for row in await ucur.fetchall():
                            new_spk = random.choice(valid_pool)
                            await db.execute(
                                "UPDATE user_settings SET speaker_id = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                                (new_spk, row[0])
                            )
                    async with db.execute("SELECT guild_id, user_id FROM guild_user_settings WHERE speaker_id = 'g_neural_f'") as gcur:
                        for row in await gcur.fetchall():
                            new_spk = random.choice(valid_pool)
                            await db.execute(
                                "UPDATE guild_user_settings SET speaker_id = ?, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND user_id = ?",
                                (new_spk, row[0], row[1])
                            )
                    await db.execute("INSERT INTO schema_migrations (version) VALUES ('20261003_fix_invalid_g_neural_f')")
                    logger.info("Migration '20261003_fix_invalid_g_neural_f' completed successfully.")

            await db.commit()
        logger.info(f"Database initialized with guild isolation & usage tracking at {self.db_path}")

    # --- ユーザー設定 (グローバル & ギルド個別 & 設定引き継ぎ・同期) ---

    async def get_user_settings(self, user_id: int) -> Optional[Dict[str, Any]]:
        """グローバル（ベース）のユーザー音声設定を取得"""
        now = time.time()
        ckey = (0, user_id)
        if ckey in self._user_settings_cache:
            ts, val = self._user_settings_cache[ckey]
            if now - ts < self._cache_ttl:
                return dict(val) if val is not None else None

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT user_id, speaker_id, pitch, speed, volume, strip_name_brackets FROM user_settings WHERE user_id = ?",
                (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    cols = row.keys()
                    res = {
                        "user_id": row["user_id"],
                        "speaker_id": row["speaker_id"],
                        "pitch": row["pitch"],
                        "speed": row["speed"],
                        "volume": float(row["volume"]) if "volume" in cols and row["volume"] is not None else 0.8,
                        "strip_name_brackets": bool(row["strip_name_brackets"]) if "strip_name_brackets" in cols else True
                    }
                    self._user_settings_cache[ckey] = (now, dict(res))
                    return res
        self._user_settings_cache[ckey] = (now, None)
        return None

    async def get_guild_user_settings(self, guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """指定ギルドでのユーザー音声設定を取得"""
        now = time.time()
        ckey = (guild_id, user_id)
        if ckey in self._user_settings_cache:
            ts, val = self._user_settings_cache[ckey]
            if now - ts < self._cache_ttl:
                return dict(val) if val is not None else None

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT guild_id, user_id, speaker_id, pitch, speed, volume, strip_name_brackets FROM guild_user_settings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    cols = row.keys()
                    res = {
                        "guild_id": row["guild_id"],
                        "user_id": row["user_id"],
                        "speaker_id": row["speaker_id"],
                        "pitch": row["pitch"],
                        "speed": row["speed"],
                        "volume": float(row["volume"]) if "volume" in cols and row["volume"] is not None else 0.8,
                        "strip_name_brackets": bool(row["strip_name_brackets"]) if "strip_name_brackets" in cols else True
                    }
                    self._user_settings_cache[ckey] = (now, dict(res))
                    return res
        self._user_settings_cache[ckey] = (now, None)
        return None

    async def get_or_create_guild_user_settings(
        self,
        guild_id: int,
        user_id: int,
        available_speaker_ids: List[str],
        default_pitch: str = "0%",
        default_speed: float = 1.0,
        default_volume: float = 0.8,
        default_strip_brackets: bool = True
    ) -> Dict[str, Any]:
        """
        指定ギルドでのユーザー設定を取得。
        未設定の場合、以下の優先度で初期設定を引き継ぐ：
        1. 他のギルドでの最新の個人設定 (直近更新された guild_user_settings)
        2. グローバル設定 (user_settings)
        3. 上記のいずれもない場合はランダムに自動選定 (括弧除去はデフォルトON, 音量はデフォルト80%)
        """
        current = await self.get_guild_user_settings(guild_id, user_id)
        if current:
            return current

        # 1 & 2. 他ギルドまたはグローバル設定からの初期値引き継ぎ判定
        inherited_speaker: Optional[str] = None
        inherited_pitch: Optional[str] = None
        inherited_speed: Optional[float] = None
        inherited_volume: Optional[float] = None
        inherited_strip_brackets: Optional[bool] = None

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            # 他サーバーの最新設定を検索
            async with db.execute(
                "SELECT speaker_id, pitch, speed, volume, strip_name_brackets FROM guild_user_settings WHERE user_id = ? AND guild_id != ? ORDER BY updated_at DESC LIMIT 1",
                (user_id, guild_id)
            ) as cur:
                other_row = await cur.fetchone()
                if other_row:
                    cols = other_row.keys()
                    inherited_speaker = other_row["speaker_id"]
                    inherited_pitch = other_row["pitch"]
                    inherited_speed = other_row["speed"]
                    if "volume" in cols and other_row["volume"] is not None:
                        inherited_volume = float(other_row["volume"])
                    if "strip_name_brackets" in cols and other_row["strip_name_brackets"] is not None:
                        inherited_strip_brackets = bool(other_row["strip_name_brackets"])

            # 他サーバーにない場合はグローバル設定を検索
            if not inherited_speaker:
                async with db.execute(
                    "SELECT speaker_id, pitch, speed, volume, strip_name_brackets FROM user_settings WHERE user_id = ?",
                    (user_id,)
                ) as cur:
                    global_row = await cur.fetchone()
                    if global_row:
                        cols = global_row.keys()
                        inherited_speaker = global_row["speaker_id"]
                        inherited_pitch = global_row["pitch"]
                        inherited_speed = global_row["speed"]
                        if "volume" in cols and global_row["volume"] is not None:
                            inherited_volume = float(global_row["volume"])
                        if "strip_name_brackets" in cols and global_row["strip_name_brackets"] is not None:
                            inherited_strip_brackets = bool(global_row["strip_name_brackets"])

        if inherited_speaker and (not available_speaker_ids or inherited_speaker in available_speaker_ids):
            # 継承した設定を採用
            chosen_speaker = inherited_speaker
            chosen_pitch = inherited_pitch if inherited_pitch is not None else default_pitch
            chosen_speed = inherited_speed if inherited_speed is not None else default_speed
            chosen_volume = inherited_volume if inherited_volume is not None else default_volume
            chosen_strip = inherited_strip_brackets if inherited_strip_brackets is not None else default_strip_brackets
            logger.info(f"Inherited voice settings for user {user_id} in guild {guild_id}: speaker={chosen_speaker}, pitch={chosen_pitch}, speed={chosen_speed}, volume={chosen_volume}, strip_brackets={chosen_strip}")
        else:
            # 初回テキスト発言時: 話者・ピッチ・速度をランダムに自動選定 (Azure & GCP クラウドプール限定)
            cloud_candidates = [
                s for s in available_speaker_ids
                if s in CLOUD_INITIAL_SPEAKER_POOL or s.startswith("g_") or s in ("nanami", "keita", "aoi", "daichi", "mayu", "naoki", "shiori")
            ]
            selection_pool = cloud_candidates if cloud_candidates else CLOUD_INITIAL_SPEAKER_POOL
            chosen_speaker = random.choice(selection_pool)

            pitch_choices = ["-15%", "-10%", "-5%", "0%", "+5%", "+10%", "+15%"]
            chosen_pitch = random.choice(pitch_choices)

            speed_choices = [0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]
            chosen_speed = random.choice(speed_choices)
            chosen_volume = default_volume
            chosen_strip = default_strip_brackets
            logger.info(f"Auto-assigned random voice '{chosen_speaker}' to user {user_id} in guild {guild_id} (volume={chosen_volume}, strip_brackets={chosen_strip})")

        new_settings = {
            "guild_id": guild_id,
            "user_id": user_id,
            "speaker_id": chosen_speaker,
            "pitch": chosen_pitch,
            "speed": chosen_speed,
            "volume": chosen_volume,
            "strip_name_brackets": chosen_strip
        }

        strip_int = 1 if chosen_strip else 0
        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO guild_user_settings (guild_id, user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (guild_id, user_id, chosen_speaker, chosen_pitch, chosen_speed, chosen_volume, strip_int)
            )
            # グローバル user_settings も最新値として同期
            await db.execute(
                """
                INSERT OR REPLACE INTO user_settings (user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (user_id, chosen_speaker, chosen_pitch, chosen_speed, chosen_volume, strip_int)
            )
            await db.commit()

        self._user_settings_cache[(guild_id, user_id)] = (time.time(), dict(new_settings))
        self._user_settings_cache[(0, user_id)] = (time.time(), dict(new_settings))

        return new_settings

    async def update_guild_user_settings(
        self,
        guild_id: int,
        user_id: int,
        speaker_id: str,
        pitch: Optional[str] = None,
        speed: Optional[float] = None,
        volume: Optional[float] = None,
        strip_name_brackets: Optional[bool] = None
    ) -> Dict[str, Any]:
        """指定ギルドでのユーザー個別音声設定を更新"""
        current = await self.get_guild_user_settings(guild_id, user_id)
        if not current:
            base = await self.get_user_settings(user_id)
            current_pitch = base["pitch"] if base else "0%"
            current_speed = base["speed"] if base else 1.0
            current_volume = base["volume"] if base else 0.8
            current_strip = base["strip_name_brackets"] if base else True
        else:
            current_pitch = current["pitch"]
            current_speed = current["speed"]
            current_volume = current.get("volume", 0.8)
            current_strip = current.get("strip_name_brackets", True)

        new_pitch = pitch if pitch is not None else current_pitch
        new_speed = speed if speed is not None else current_speed
        new_volume = volume if volume is not None else current_volume
        new_strip = strip_name_brackets if strip_name_brackets is not None else current_strip
        strip_int = 1 if new_strip else 0

        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO guild_user_settings (guild_id, user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    speaker_id=excluded.speaker_id,
                    pitch=excluded.pitch,
                    speed=excluded.speed,
                    volume=excluded.volume,
                    strip_name_brackets=excluded.strip_name_brackets,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (guild_id, user_id, speaker_id, new_pitch, new_speed, new_volume, strip_int)
            )
            # グローバル user_settings も更新
            await db.execute(
                """
                INSERT INTO user_settings (user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    speaker_id=excluded.speaker_id,
                    pitch=excluded.pitch,
                    speed=excluded.speed,
                    volume=excluded.volume,
                    strip_name_brackets=excluded.strip_name_brackets,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, speaker_id, new_pitch, new_speed, new_volume, strip_int)
            )
            await db.commit()

        self.invalidate_user_cache(user_id=user_id)
        logger.info(f"Updated guild voice settings for user {user_id} in guild {guild_id}: speaker={speaker_id}, pitch={new_pitch}, speed={new_speed}, volume={new_volume}, strip_brackets={new_strip}")
        return {
            "guild_id": guild_id,
            "user_id": user_id,
            "speaker_id": speaker_id,
            "pitch": new_pitch,
            "speed": new_speed,
            "volume": new_volume,
            "strip_name_brackets": new_strip
        }

    async def get_all_guild_user_settings(self, user_id: int) -> List[Dict[str, Any]]:
        """ユーザーが設定を持つ全ギルドの設定一覧を取得"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT guild_id, user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at FROM guild_user_settings WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                result = []
                for row in rows:
                    cols = row.keys()
                    result.append({
                        "guild_id": row["guild_id"],
                        "user_id": row["user_id"],
                        "speaker_id": row["speaker_id"],
                        "pitch": row["pitch"],
                        "speed": row["speed"],
                        "volume": float(row["volume"]) if "volume" in cols and row["volume"] is not None else 0.8,
                        "strip_name_brackets": bool(row["strip_name_brackets"]) if "strip_name_brackets" in cols else True,
                        "updated_at": row["updated_at"]
                    })
                return result

    async def get_all_guild_user_settings_for_guild(self, guild_id: int) -> List[Dict[str, Any]]:
        """指定ギルドで個人設定が保存されている全ユーザーの設定一覧を取得"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT guild_id, user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at "
                "FROM guild_user_settings WHERE guild_id = ? ORDER BY updated_at DESC",
                (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                result = []
                for row in rows:
                    cols = row.keys()
                    result.append({
                        "guild_id": row["guild_id"],
                        "user_id": row["user_id"],
                        "speaker_id": row["speaker_id"],
                        "pitch": row["pitch"],
                        "speed": row["speed"],
                        "volume": float(row["volume"]) if "volume" in cols and row["volume"] is not None else 0.8,
                        "strip_name_brackets": bool(row["strip_name_brackets"]) if "strip_name_brackets" in cols else True,
                        "updated_at": row["updated_at"]
                    })
                return result

    async def delete_guild_user_settings(self, guild_id: int, user_id: int) -> bool:
        """指定ギルドでのユーザー個人設定を削除（デフォルト/初期選定状態へリセット）"""
        async with self.get_connection() as db:
            cur = await db.execute(
                "DELETE FROM guild_user_settings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            )
            await db.commit()
            deleted = cur.rowcount > 0
            if deleted:
                self.invalidate_user_cache(user_id=user_id, guild_id=guild_id)
            return deleted

    # --- ギルドメンバーキャッシュ (WebUI・アバターAVIF・在室状況・表示名) ---

    async def upsert_guild_member_cache(
        self,
        guild_id: int,
        user_id: int,
        username: str,
        display_name: str,
        avatar_hash: Optional[str] = None,
        is_in_guild: bool = True
    ) -> None:
        """ギルドメンバーキャッシュを登録または更新"""
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO guild_member_cache (guild_id, user_id, username, display_name, avatar_hash, is_in_guild, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    avatar_hash = excluded.avatar_hash,
                    is_in_guild = excluded.is_in_guild,
                    updated_at = CURRENT_TIMESTAMP
            """, (guild_id, user_id, username or "", display_name or "", avatar_hash or "", 1 if is_in_guild else 0))
            await db.commit()

    async def get_guild_member_cache(self, guild_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        """指定ギルド・ユーザーのキャッシュを取得"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT guild_id, user_id, username, display_name, avatar_hash, is_in_guild, updated_at "
                "FROM guild_member_cache WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return {
                        "guild_id": row["guild_id"],
                        "user_id": row["user_id"],
                        "username": row["username"],
                        "display_name": row["display_name"],
                        "avatar_hash": row["avatar_hash"],
                        "is_in_guild": bool(row["is_in_guild"]),
                        "updated_at": row["updated_at"]
                    }
                return None

    async def get_all_guild_member_caches_for_guild(self, guild_id: int) -> Dict[int, Dict[str, Any]]:
        """指定ギルドの全メンバーキャッシュを user_id をキーとする辞書で取得（WebUI高速表示用）"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT guild_id, user_id, username, display_name, avatar_hash, is_in_guild, updated_at "
                "FROM guild_member_cache WHERE guild_id = ?",
                (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                result = {}
                for row in rows:
                    uid = row["user_id"]
                    result[uid] = {
                        "guild_id": row["guild_id"],
                        "user_id": uid,
                        "username": row["username"],
                        "display_name": row["display_name"],
                        "avatar_hash": row["avatar_hash"],
                        "is_in_guild": bool(row["is_in_guild"]),
                        "updated_at": row["updated_at"]
                    }
                return result

    async def get_stale_or_uncached_guild_users(
        self,
        max_age_days: int = 30,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        個人設定が存在するが、未キャッシュまたは max_age_days 日以上更新されていないギルド・ユーザーペアを取得
        スローバッチ更新用
        """
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT gus.guild_id, gus.user_id, gmc.updated_at
                FROM guild_user_settings gus
                LEFT JOIN guild_member_cache gmc 
                    ON gus.guild_id = gmc.guild_id AND gus.user_id = gmc.user_id
                WHERE gmc.updated_at IS NULL 
                   OR datetime(gmc.updated_at) < datetime('now', ?)
                ORDER BY CASE WHEN gmc.updated_at IS NULL THEN 0 ELSE 1 END, gmc.updated_at ASC
                LIMIT ?
            """
            async with db.execute(query, (f"-{max_age_days} days", limit)) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "guild_id": row["guild_id"],
                        "user_id": row["user_id"],
                        "updated_at": row["updated_at"]
                    }
                    for row in rows
                ]

    # --- システム設定・メタ情報 KVS ---

    async def get_system_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """システムKVSから値を取得"""
        async with self.get_connection() as db:
            async with db.execute("SELECT value FROM system_kv WHERE key = ?", (key,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return str(row[0])
                return default

    async def set_system_kv(self, key: str, value: str) -> None:
        """システムKVSに値を登録または更新"""
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO system_kv (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, str(value)))
            await db.commit()

    async def sync_guild_user_settings(
        self,
        user_id: int,
        from_guild_id: int,
        to_guild_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        あるサーバー (from_guild_id) の個人音声設定を別のサーバー (to_guild_id) に同期・コピーする。
        """
        source = await self.get_guild_user_settings(from_guild_id, user_id)
        if not source:
            source = await self.get_user_settings(user_id)
            if not source:
                return None

        updated = await self.update_guild_user_settings(
            guild_id=to_guild_id,
            user_id=user_id,
            speaker_id=source["speaker_id"],
            pitch=source["pitch"],
            speed=source["speed"],
            volume=source.get("volume", 0.8),
            strip_name_brackets=source.get("strip_name_brackets", True)
        )
        logger.info(f"Synced voice settings for user {user_id}: guild {from_guild_id} -> guild {to_guild_id}")
        return updated

    async def get_or_create_user_settings(
        self,
        user_id: int,
        available_speaker_ids: List[str],
        default_pitch: str = "0%",
        default_speed: float = 1.0,
        default_volume: float = 0.8,
        strip_name_brackets: bool = True,
        guild_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        ユーザー設定を取得または初期化。guild_id が指定されている場合はギルド個別設定（他ギルド引き継ぎ付き）を使用。
        """
        if guild_id:
            return await self.get_or_create_guild_user_settings(
                guild_id=guild_id,
                user_id=user_id,
                available_speaker_ids=available_speaker_ids,
                default_pitch=default_pitch,
                default_speed=default_speed,
                default_volume=default_volume,
                default_strip_brackets=strip_name_brackets
            )

        settings = await self.get_user_settings(user_id)
        if settings:
            return settings

        # 初回テキスト発言時: 話者・ピッチ・速度をランダムに自動選定 (Azure & GCP クラウドプール限定)
        cloud_candidates = [
            s for s in available_speaker_ids
            if s in CLOUD_INITIAL_SPEAKER_POOL or s.startswith("g_") or s in ("nanami", "keita", "aoi", "daichi", "mayu", "naoki", "shiori")
        ]
        selection_pool = cloud_candidates if cloud_candidates else CLOUD_INITIAL_SPEAKER_POOL
        chosen_speaker = random.choice(selection_pool)

        pitch_choices = ["-15%", "-10%", "-5%", "0%", "+5%", "+10%", "+15%"]
        chosen_pitch = random.choice(pitch_choices)

        speed_choices = [0.90, 0.95, 1.00, 1.05, 1.10, 1.15, 1.20]
        chosen_speed = random.choice(speed_choices)
        chosen_volume = default_volume

        new_settings = {
            "user_id": user_id,
            "speaker_id": chosen_speaker,
            "pitch": chosen_pitch,
            "speed": chosen_speed,
            "volume": chosen_volume,
            "strip_name_brackets": strip_name_brackets
        }

        strip_int = 1 if strip_name_brackets else 0
        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO user_settings (user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (user_id, chosen_speaker, chosen_pitch, chosen_speed, chosen_volume, strip_int)
            )
            await db.commit()

        self._user_settings_cache[(0, user_id)] = (time.time(), dict(new_settings))
        logger.info(f"Auto-assigned random voice '{chosen_speaker}' (pitch: {chosen_pitch}, speed: {chosen_speed}x, volume: {chosen_volume}, strip_brackets={strip_name_brackets}) to user {user_id}")
        return new_settings

    async def update_user_settings(
        self,
        user_id: int,
        speaker_id: str,
        pitch: Optional[str] = None,
        speed: Optional[float] = None,
        volume: Optional[float] = None,
        strip_name_brackets: Optional[bool] = None,
        guild_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        ユーザー設定を更新。guild_id が指定されている場合はギルド個別設定を更新。
        """
        if guild_id:
            return await self.update_guild_user_settings(
                guild_id=guild_id,
                user_id=user_id,
                speaker_id=speaker_id,
                pitch=pitch,
                speed=speed,
                volume=volume,
                strip_name_brackets=strip_name_brackets
            )

        current = await self.get_user_settings(user_id)
        current_pitch = current["pitch"] if current else "0%"
        current_speed = current["speed"] if current else 1.0
        current_volume = current["volume"] if current else 0.8
        current_strip = current["strip_name_brackets"] if current else True

        new_pitch = pitch if pitch is not None else current_pitch
        new_speed = speed if speed is not None else current_speed
        new_volume = volume if volume is not None else current_volume
        new_strip = strip_name_brackets if strip_name_brackets is not None else current_strip
        strip_int = 1 if new_strip else 0

        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO user_settings (user_id, speaker_id, pitch, speed, volume, strip_name_brackets, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    speaker_id=excluded.speaker_id,
                    pitch=excluded.pitch,
                    speed=excluded.speed,
                    volume=excluded.volume,
                    strip_name_brackets=excluded.strip_name_brackets,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (user_id, speaker_id, new_pitch, new_speed, new_volume, strip_int)
            )
            await db.commit()

        self.invalidate_user_cache(user_id=user_id)
        logger.info(f"Updated voice settings for user {user_id}: speaker={speaker_id}, pitch={new_pitch}, speed={new_speed}, volume={new_volume}, strip_brackets={new_strip}")
        return {
            "user_id": user_id,
            "speaker_id": speaker_id,
            "pitch": new_pitch,
            "speed": new_speed,
            "volume": new_volume,
            "strip_name_brackets": new_strip
        }

    # --- ギルド別単語辞書 ---

    async def add_dictionary_word(self, guild_id: int, word: str, reading: str) -> None:
        """指定ギルドの単語辞書に追加 (guild_id=0はグローバル共通)"""
        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO guild_dictionary (guild_id, word, reading, created_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, word) DO UPDATE SET
                    reading=excluded.reading,
                    created_at=CURRENT_TIMESTAMP
                """,
                (guild_id, word.strip(), reading.strip())
            )
            await db.commit()
        self.invalidate_guild_cache(guild_id)
        logger.info(f"Added dictionary word for guild {guild_id}: '{word}' -> '{reading}'")

    async def add_dictionary_words_batch(self, guild_id: int, items: List[Tuple[str, str]]) -> int:
        """指定ギルドの単語辞書に複数単語を一括追加・更新"""
        if not items:
            return 0
        imported_count = 0
        async with self.get_connection() as db:
            for word, reading in items:
                w = word.strip()
                r = reading.strip()
                if not w or not r:
                    continue
                await db.execute(
                    """
                    INSERT INTO guild_dictionary (guild_id, word, reading, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, word) DO UPDATE SET
                        reading=excluded.reading,
                        created_at=CURRENT_TIMESTAMP
                    """,
                    (guild_id, w, r)
                )
                imported_count += 1
            await db.commit()
        if imported_count > 0:
            self.invalidate_guild_cache(guild_id)
        logger.info(f"Batch imported {imported_count} dictionary words for guild {guild_id}")
        return imported_count

    async def delete_dictionary_word(self, guild_id: int, word: str) -> bool:
        """指定ギルドの単語辞書から削除"""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM guild_dictionary WHERE (guild_id = ? OR guild_id = 0) AND word = ?",
                (guild_id, word.strip())
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self.invalidate_guild_cache(guild_id)
            logger.info(f"Deleted dictionary word '{word}' from guild {guild_id}")
        return deleted

    async def update_dictionary_word(self, guild_id: int, old_word: str, new_word: str, new_reading: str) -> bool:
        """指定ギルドの単語辞書エントリを更新"""
        old_w = old_word.strip()
        new_w = new_word.strip()
        new_r = new_reading.strip()
        if not old_w or not new_w or not new_r:
            return False

        async with self.get_connection() as db:
            if old_w == new_w:
                cursor = await db.execute(
                    """
                    UPDATE guild_dictionary
                    SET reading = ?, created_at = CURRENT_TIMESTAMP
                    WHERE (guild_id = ? OR guild_id = 0) AND word = ?
                    """,
                    (new_r, guild_id, old_w)
                )
                await db.commit()
                updated = cursor.rowcount > 0
            else:
                await db.execute(
                    "DELETE FROM guild_dictionary WHERE (guild_id = ? OR guild_id = 0) AND word = ?",
                    (guild_id, old_w)
                )
                await db.execute(
                    """
                    INSERT INTO guild_dictionary (guild_id, word, reading, created_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(guild_id, word) DO UPDATE SET
                        reading = excluded.reading,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (guild_id, new_w, new_r)
                )
                await db.commit()
                updated = True

        if updated:
            self.invalidate_guild_cache(guild_id)
            logger.info(f"Updated dictionary word for guild {guild_id}: '{old_w}' -> '{new_w}': '{new_r}'")
        return updated

    async def get_dictionary_words(self, guild_id: int = 0) -> List[Tuple[str, str]]:
        """指定ギルド（およびグローバル0）の辞書一覧を取得（文字数降順）"""
        now = time.time()
        if guild_id in self._dict_cache:
            ts, cached = self._dict_cache[guild_id]
            if now - ts < self._cache_ttl:
                return list(cached)

        async with self.get_connection() as db:
            async with db.execute(
                """
                SELECT word, reading FROM guild_dictionary
                WHERE guild_id = ? OR guild_id = 0
                ORDER BY LENGTH(word) DESC, word ASC
                """,
                (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                # 重複した単語はギルド固有のものを優先
                seen = set()
                result = []
                for w, r in rows:
                    if w not in seen:
                        seen.add(w)
                        result.append((w, r))
                self._dict_cache[guild_id] = (now, list(result))
                return result

    async def import_user_names(self, guild_id: int, names: List[str]) -> int:
        """メンバー表示名を単語辞書に一括インポート"""
        imported_count = 0
        async with self.get_connection() as db:
            for name in names:
                clean_name = name.strip()
                if not clean_name:
                    continue
                # 既に登録されているか確認
                async with db.execute(
                    "SELECT 1 FROM guild_dictionary WHERE guild_id = ? AND word = ?",
                    (guild_id, clean_name)
                ) as cur:
                    if await cur.fetchone() is None:
                        await db.execute(
                            """
                            INSERT INTO guild_dictionary (guild_id, word, reading, created_at)
                            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                            """,
                            (guild_id, clean_name, clean_name)
                        )
                        imported_count += 1
            await db.commit()
        if imported_count > 0:
            self.invalidate_guild_cache(guild_id)
        logger.info(f"Imported {imported_count} member display names for guild {guild_id}")
        return imported_count

    async def touch_dictionary_words(self, guild_id: int, words: List[str]) -> None:
        """読み上げに使用された単語の last_used_at を非同期更新 (LRU管理用)"""
        if not words:
            return
        clean_words = [w.strip() for w in words if w.strip()]
        if not clean_words:
            return
        try:
            async with self.get_connection() as db:
                placeholders = ",".join("?" for _ in clean_words)
                query = f"""
                    UPDATE guild_dictionary
                    SET last_used_at = CURRENT_TIMESTAMP
                    WHERE (guild_id = ? OR guild_id = 0) AND word IN ({placeholders})
                """
                await db.execute(query, [guild_id] + clean_words)
                await db.commit()
        except Exception as e:
            logger.debug(f"[Database] touch_dictionary_words error: {e}")

    async def prune_dictionary_lru(self, guild_id: int, max_entries: int = 5000) -> int:
        """ギルド単語辞書の件数が上限を超えた場合、最も古く使われていない単語を自動パージ"""
        async with self.get_connection() as db:
            async with db.execute("SELECT COUNT(*) FROM guild_dictionary WHERE guild_id = ?", (guild_id,)) as cur:
                row = await cur.fetchone()
                count = row[0] if row else 0

            if count <= max_entries:
                return 0

            prune_count = count - max_entries
            cursor = await db.execute(
                """
                DELETE FROM guild_dictionary
                WHERE (guild_id, word) IN (
                    SELECT guild_id, word FROM guild_dictionary
                    WHERE guild_id = ?
                    ORDER BY COALESCE(last_used_at, created_at) ASC
                    LIMIT ?
                )
                """,
                (guild_id, prune_count)
            )
            await db.commit()
            deleted = cursor.rowcount
        if deleted > 0:
            self.invalidate_guild_cache(guild_id)
            logger.info(f"Pruned {deleted} LRU dictionary entries for guild {guild_id} (max: {max_entries})")
        return deleted

    # --- ギルド別 NGワード管理 ---

    async def get_ng_words(self, guild_id: int) -> List[str]:
        """指定ギルドのNGワード一覧を取得（文字数降順）"""
        now = time.time()
        if guild_id in self._ng_words_cache:
            ts, cached = self._ng_words_cache[guild_id]
            if now - ts < self._cache_ttl:
                return list(cached)

        async with self.get_connection() as db:
            async with db.execute(
                """
                SELECT word FROM guild_ng_words
                WHERE guild_id = ?
                ORDER BY LENGTH(word) DESC, word ASC
                """,
                (guild_id,)
            ) as cursor:
                rows = await cursor.fetchall()
                result = [row[0] for row in rows]
                self._ng_words_cache[guild_id] = (now, list(result))
                return result

    async def add_ng_word(self, guild_id: int, word: str) -> None:
        """ギルドにNGワードを追加"""
        w = word.strip()
        if not w:
            return
        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO guild_ng_words (guild_id, word, created_at, last_used_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, word) DO UPDATE SET last_used_at = CURRENT_TIMESTAMP
                """,
                (guild_id, w)
            )
            await db.commit()
        self.invalidate_guild_cache(guild_id)

    async def delete_ng_word(self, guild_id: int, word: str) -> bool:
        """ギルドからNGワードを削除"""
        w = word.strip()
        if not w:
            return False
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM guild_ng_words WHERE guild_id = ? AND word = ?",
                (guild_id, w)
            )
            await db.commit()
            deleted = cursor.rowcount > 0
        if deleted:
            self.invalidate_guild_cache(guild_id)
        return deleted

    async def touch_ng_words(self, guild_id: int, words: List[str]) -> None:
        """検知されたNGワードの last_used_at を非同期更新 (LRU管理用)"""
        if not words:
            return
        clean_words = [w.strip() for w in words if w.strip()]
        if not clean_words:
            return
        try:
            async with self.get_connection() as db:
                placeholders = ",".join("?" for _ in clean_words)
                query = f"""
                    UPDATE guild_ng_words
                    SET last_used_at = CURRENT_TIMESTAMP
                    WHERE guild_id = ? AND word IN ({placeholders})
                """
                await db.execute(query, [guild_id] + clean_words)
                await db.commit()
        except Exception as e:
            logger.debug(f"[Database] touch_ng_words error: {e}")

    async def import_default_ng_words(self, guild_id: int) -> int:
        """標準NGワードプリセットをギルドに一括インポート"""
        from src.utils.default_ng_words import DEFAULT_NG_WORDS
        imported_count = 0
        async with self.get_connection() as db:
            for w in DEFAULT_NG_WORDS:
                clean_w = w.strip()
                if not clean_w:
                    continue
                async with db.execute(
                    "SELECT 1 FROM guild_ng_words WHERE guild_id = ? AND word = ?",
                    (guild_id, clean_w)
                ) as cur:
                    if await cur.fetchone() is None:
                        await db.execute(
                            """
                            INSERT INTO guild_ng_words (guild_id, word, created_at, last_used_at)
                            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                            """,
                            (guild_id, clean_w)
                        )
                        imported_count += 1
            await db.commit()
        if imported_count > 0:
            self.invalidate_guild_cache(guild_id)
        logger.info(f"Imported {imported_count} default NG words for guild {guild_id}")
        return imported_count

    # --- AI学習提案辞書 (自己学習・辞書汚染防止) ---

    async def add_ai_suggestion(
        self,
        guild_id: int,
        word: str,
        suggested_reading: str,
        reason: str = "",
        source: str = "unknown_word"
    ) -> bool:
        """AIが学習した単語提案を登録 (すでに本辞書にある場合は除外)"""
        w = word.strip()
        r = suggested_reading.strip()
        if not w or not r:
            return False

        async with self.get_connection() as db:
            # すでに本辞書にあるか確認
            async with db.execute(
                "SELECT 1 FROM guild_dictionary WHERE (guild_id = ? OR guild_id = 0) AND word = ?",
                (guild_id, w)
            ) as cur:
                if await cur.fetchone() is not None:
                    return False

            cursor = await db.execute(
                """
                INSERT INTO ai_suggested_words (guild_id, word, suggested_reading, reason, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, word) DO UPDATE SET
                    suggested_reading = excluded.suggested_reading,
                    reason = excluded.reason,
                    source = excluded.source,
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'pending'
                """,
                (guild_id, w, r, reason.strip(), source)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def get_ai_suggestions(self, guild_id: int, status: str = "pending", limit: int = 50) -> List[Dict[str, Any]]:
        """AI提案単語リストを取得"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT id, guild_id, word, suggested_reading, reason, source, status, created_at, updated_at
                FROM ai_suggested_words
                WHERE guild_id = ? AND status = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (guild_id, status, limit)
            ) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def approve_ai_suggestion(self, suggestion_id: int) -> Optional[Dict[str, Any]]:
        """AI提案を承認し、本辞書 (guild_dictionary) へ登録"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM ai_suggested_words WHERE id = ?",
                (suggestion_id,)
            ) as cur:
                item = await cur.fetchone()
                if not item:
                    return None
                data = dict(item)

            guild_id = data["guild_id"]
            word = data["word"]
            reading = data["suggested_reading"]

            # 本辞書に追加・更新
            await db.execute(
                """
                INSERT INTO guild_dictionary (guild_id, word, reading, created_at, last_used_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id, word) DO UPDATE SET
                    reading = excluded.reading,
                    last_used_at = CURRENT_TIMESTAMP
                """,
                (guild_id, word, reading)
            )
            # ステータスを approved に更新
            await db.execute(
                "UPDATE ai_suggested_words SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (suggestion_id,)
            )
            await db.commit()

        self.invalidate_guild_cache(guild_id)
        logger.info(f"Approved AI suggestion #{suggestion_id} for guild {guild_id}: '{word}' -> '{reading}'")
        return data

    async def reject_ai_suggestion(self, suggestion_id: int) -> bool:
        """AI提案を却下 (ステータスを rejected に変更)"""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "UPDATE ai_suggested_words SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (suggestion_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_ai_suggestion(self, suggestion_id: int) -> bool:
        """AI提案を物理削除"""
        async with self.get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM ai_suggested_words WHERE id = ?",
                (suggestion_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    # --- ギルド個別設定 ---

    async def get_guild_settings(self, guild_id: int, default_cfg: Any = None) -> Dict[str, Any]:
        """ギルド設定を取得 (存在しなければデフォルト値で初期化)"""
        now = time.time()
        if guild_id in self._guild_settings_cache:
            ts, cached = self._guild_settings_cache[guild_id]
            if now - ts < self._cache_ttl:
                return dict(cached)

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    res = dict(row)
                    # auto_join_pairs のデコード
                    pairs_raw = res.get("auto_join_pairs")
                    pairs = []
                    if pairs_raw:
                        try:
                            pairs = json.loads(pairs_raw)
                            if not isinstance(pairs, list):
                                pairs = []
                        except Exception:
                            pairs = []
                    if not pairs:
                        avc = int(res.get("auto_join_vc_id", 0) or 0)
                        atxt = int(res.get("auto_join_text_channel_id", 0) or 0)
                        if avc > 0 and atxt > 0:
                            pairs = [{"vc_id": avc, "text_id": atxt}]
                    res["auto_join_pairs"] = pairs

                    # ignore_prefixes のデコード
                    prefixes_raw = res.get("ignore_prefixes")
                    prefixes = None
                    if prefixes_raw is not None:
                        try:
                            parsed = json.loads(prefixes_raw) if isinstance(prefixes_raw, str) else prefixes_raw
                            if isinstance(parsed, list):
                                prefixes = [str(p) for p in parsed if str(p)]
                        except Exception:
                            pass
                    if prefixes is None:
                        def_pref = getattr(default_cfg, "ignore_prefixes", [";", "；"]) if default_cfg else [";", "；"]
                        prefixes = list(def_pref)
                    res["ignore_prefixes"] = prefixes
                    res["announce_vc_events"] = int(res.get("announce_vc_events", 0) or 0)
                    res["speculative_timeout_seconds"] = float(res.get("speculative_timeout_seconds", 1.5) or 1.5)
                    res["disaster_volume"] = float(res.get("disaster_volume", 1.0) if res.get("disaster_volume") is not None else 1.0)

                    self._guild_settings_cache[guild_id] = (now, dict(res))
                    return res

        # 未保存の場合はデフォルト設定を作成して返却
        prefix = getattr(default_cfg, "command_prefix", "!") if default_cfg else "!"
        max_len = getattr(default_cfg, "max_text_length", 50) if default_cfg else 50
        dedup_sec = getattr(default_cfg, "dedup_window_seconds", 5.0) if default_cfg else 5.0
        dedup_len = getattr(default_cfg, "dedup_max_length", 10) if default_cfg else 10
        auto_vc = getattr(default_cfg, "auto_join_vc_id", 0) if default_cfg else 0
        auto_text = getattr(default_cfg, "auto_join_text_channel_id", 0) if default_cfg else 0
        def_speaker = getattr(default_cfg, "default_speaker", "nanami") if default_cfg else "nanami"
        def_prefixes = getattr(default_cfg, "ignore_prefixes", [";", "；"]) if default_cfg else [";", "；"]
        def_spec_timeout = float(getattr(default_cfg, "speculative_timeout_seconds", 1.5)) if default_cfg else 1.5

        initial_pairs = [{"vc_id": auto_vc, "text_id": auto_text}] if auto_vc > 0 and auto_text > 0 else []

        new_settings = {
            "guild_id": guild_id,
            "command_prefix": prefix,
            "max_text_length": max_len,
            "dedup_window_seconds": dedup_sec,
            "dedup_max_length": dedup_len,
            "auto_join_vc_id": auto_vc,
            "auto_join_text_channel_id": auto_text,
            "auto_join_pairs": initial_pairs,
            "admin_role_id": 0,
            "default_speaker": def_speaker,
            "read_username": 0,
            "announce_vc_events": 0,
            "speculative_timeout_seconds": def_spec_timeout,
            "ignore_prefixes": list(def_prefixes),
            "disaster_alert_enabled": 1,
            "disaster_min_scale": 30,
            "disaster_pref_filter": "",
            "disaster_alert_weather": 1,
            "disaster_volume": 1.0
        }

        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO guild_settings
                (guild_id, command_prefix, max_text_length, dedup_window_seconds, dedup_max_length, auto_join_vc_id, auto_join_text_channel_id, auto_join_pairs, admin_role_id, default_speaker, read_username, announce_vc_events, speculative_timeout_seconds, ignore_prefixes, disaster_alert_enabled, disaster_min_scale, disaster_pref_filter, disaster_alert_weather, disaster_volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 1, 30, '', 1, 1.0)
                """,
                (guild_id, prefix, max_len, dedup_sec, dedup_len, auto_vc, auto_text, json.dumps(initial_pairs), 0, def_speaker, def_spec_timeout, json.dumps(list(def_prefixes)))
            )
            await db.commit()

        self._guild_settings_cache[guild_id] = (now, dict(new_settings))
        return new_settings

    async def update_guild_settings(self, guild_id: int, **kwargs) -> Dict[str, Any]:
        """ギルド設定を更新"""
        current = await self.get_guild_settings(guild_id)

        # auto_join_pairs が指定された場合のバリデーション & 下位互換処理
        if "auto_join_pairs" in kwargs:
            raw_pairs = kwargs["auto_join_pairs"]
            valid_pairs = []
            if isinstance(raw_pairs, list):
                for p in raw_pairs[:5]:  # 最大5件
                    try:
                        vc_id = int(p.get("vc_id", 0))
                        text_id = int(p.get("text_id", 0))
                        if vc_id > 0 and text_id > 0:
                            valid_pairs.append({"vc_id": vc_id, "text_id": text_id})
                    except (ValueError, TypeError):
                        pass
            current["auto_join_pairs"] = valid_pairs
            if valid_pairs:
                current["auto_join_vc_id"] = valid_pairs[0]["vc_id"]
                current["auto_join_text_channel_id"] = valid_pairs[0]["text_id"]
            else:
                current["auto_join_vc_id"] = 0
                current["auto_join_text_channel_id"] = 0
            del kwargs["auto_join_pairs"]

        if "ignore_prefixes" in kwargs:
            raw_prefixes = kwargs["ignore_prefixes"]
            valid_prefixes = []
            if isinstance(raw_prefixes, list):
                for p in raw_prefixes:
                    p_str = str(p).strip()
                    if p_str and p_str not in valid_prefixes:
                        valid_prefixes.append(p_str[:20])
            elif isinstance(raw_prefixes, str):
                parts = [p.strip() for p in re.split(r'[,、\s\n]+', raw_prefixes) if p.strip()]
                for p in parts:
                    if p and p not in valid_prefixes:
                        valid_prefixes.append(p[:20])
            current["ignore_prefixes"] = valid_prefixes[:30]
            del kwargs["ignore_prefixes"]

        if "disaster_volume" in kwargs:
            try:
                current["disaster_volume"] = max(0.0, min(float(kwargs["disaster_volume"]), 2.0))
            except (ValueError, TypeError):
                current["disaster_volume"] = 1.0
            del kwargs["disaster_volume"]

        current.update(kwargs)

        async with self.get_connection() as db:
            await db.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, command_prefix, max_text_length, dedup_window_seconds,
                    dedup_max_length, auto_join_vc_id, auto_join_text_channel_id,
                    auto_join_pairs, admin_role_id, default_speaker, read_username,
                    announce_vc_events, speculative_timeout_seconds, ignore_prefixes,
                    disaster_alert_enabled, disaster_min_scale, disaster_pref_filter,
                    disaster_alert_weather, disaster_volume, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(guild_id) DO UPDATE SET
                    command_prefix=excluded.command_prefix,
                    max_text_length=excluded.max_text_length,
                    dedup_window_seconds=excluded.dedup_window_seconds,
                    dedup_max_length=excluded.dedup_max_length,
                    auto_join_vc_id=excluded.auto_join_vc_id,
                    auto_join_text_channel_id=excluded.auto_join_text_channel_id,
                    auto_join_pairs=excluded.auto_join_pairs,
                    admin_role_id=excluded.admin_role_id,
                    default_speaker=excluded.default_speaker,
                    read_username=excluded.read_username,
                    announce_vc_events=excluded.announce_vc_events,
                    speculative_timeout_seconds=excluded.speculative_timeout_seconds,
                    ignore_prefixes=excluded.ignore_prefixes,
                    disaster_alert_enabled=excluded.disaster_alert_enabled,
                    disaster_min_scale=excluded.disaster_min_scale,
                    disaster_pref_filter=excluded.disaster_pref_filter,
                    disaster_alert_weather=excluded.disaster_alert_weather,
                    disaster_volume=excluded.disaster_volume,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    current.get("command_prefix", "!"),
                    current.get("max_text_length", 50),
                    current.get("dedup_window_seconds", 5.0),
                    current.get("dedup_max_length", 10),
                    current.get("auto_join_vc_id", 0),
                    current.get("auto_join_text_channel_id", 0),
                    json.dumps(current.get("auto_join_pairs", [])),
                    current.get("admin_role_id", 0),
                    current.get("default_speaker", "nanami"),
                    current.get("read_username", 0),
                    1 if current.get("announce_vc_events", 0) else 0,
                    float(current.get("speculative_timeout_seconds", 1.5) or 1.5),
                    json.dumps(current.get("ignore_prefixes", [";", "；"])),
                    1 if current.get("disaster_alert_enabled", 1) else 0,
                    int(current.get("disaster_min_scale", 30)),
                    str(current.get("disaster_pref_filter", "")),
                    1 if current.get("disaster_alert_weather", 1) else 0,
                    float(current.get("disaster_volume", 1.0) if current.get("disaster_volume") is not None else 1.0)
                )
            )
            await db.commit()

        self._guild_settings_cache[guild_id] = (time.time(), dict(current))
        logger.info(f"Updated guild settings for guild {guild_id}: {kwargs}")
        return current

    # --- TTS 利用量 & キャッシュ節約トラッキング ---

    async def record_tts_usage(self, provider: str, char_count: int) -> None:
        """今月のTTS利用文字数を記録"""
        if self.is_closed:
            return
        month = datetime.now().strftime("%Y-%m")
        try:
            async with self.get_connection() as db:
                await db.execute(
                    """
                    INSERT INTO tts_usage (month, provider, characters, requests)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(month, provider) DO UPDATE SET
                        characters = characters + excluded.characters,
                        requests = requests + 1
                    """,
                    (month, provider.lower(), char_count)
                )
                await db.commit()
        except RuntimeError as e:
            if "Connection pool is closed" in str(e):
                return
            raise
        except Exception as e:
            if self.is_closed:
                return
            logger.warning(f"Failed to record TTS usage: {e}")

    async def record_cache_hit(self, arg: Any, is_local: bool = False) -> None:
        """
        キャッシュヒット記録の統合エントリーポイント:
        - 第1引数が数値 (int): 節約文字数とヒット回数を記録 (tts_cache_savings)
        - 第1引数が文字列 (str): キャッシュキーの hit_count を記録 (cache_metadata)
        """
        if isinstance(arg, (int, float)):
            await self.record_cache_savings(int(arg), is_local=is_local)
        elif isinstance(arg, str):
            await self.record_cache_metadata_hit(arg)

    async def record_cache_savings(self, char_count: int, is_local: bool = False) -> None:
        """キャッシュヒット時に節約できた文字数とヒット回数を記録 (クラウド / ローカル分離)"""
        if char_count <= 0 or self.is_closed:
            return
        month = datetime.now().strftime("%Y-%m")
        try:
            async with self.get_connection() as db:
                if is_local:
                    await db.execute(
                        """
                        INSERT INTO tts_cache_savings (
                            month, saved_characters, hits,
                            saved_cloud_characters, hits_cloud,
                            saved_local_characters, hits_local
                        )
                        VALUES (?, ?, 1, 0, 0, ?, 1)
                        ON CONFLICT(month) DO UPDATE SET
                            saved_characters = saved_characters + excluded.saved_characters,
                            hits = hits + 1,
                            saved_local_characters = saved_local_characters + excluded.saved_local_characters,
                            hits_local = hits_local + 1
                        """,
                        (month, char_count, char_count)
                    )
                else:
                    await db.execute(
                        """
                        INSERT INTO tts_cache_savings (
                            month, saved_characters, hits,
                            saved_cloud_characters, hits_cloud,
                            saved_local_characters, hits_local
                        )
                        VALUES (?, ?, 1, ?, 1, 0, 0)
                        ON CONFLICT(month) DO UPDATE SET
                            saved_characters = saved_characters + excluded.saved_characters,
                            hits = hits + 1,
                            saved_cloud_characters = saved_cloud_characters + excluded.saved_cloud_characters,
                            hits_cloud = hits_cloud + 1
                        """,
                        (month, char_count, char_count)
                    )
                await db.commit()
        except RuntimeError as e:
            if "Connection pool is closed" in str(e):
                return
            raise
        except Exception as e:
            if self.is_closed:
                return
            logger.warning(f"Failed to record cache savings: {e}")

    async def get_monthly_tts_usage(self, month: Optional[str] = None) -> Dict[str, Any]:
        """指定月 (デフォルト当月) のプロバイダー別利用状況およびキャッシュ節約統計 (Cloud / Local) を取得"""
        if not month:
            month = datetime.now().strftime("%Y-%m")

        usage = {
            "month": month,
            "azure": {"characters": 0, "requests": 0, "limit": 500000},
            "google": {"characters": 0, "requests": 0, "limit": 1000000},
            "edge": {"characters": 0, "requests": 0, "limit": 0},
            "voicevox": {"characters": 0, "requests": 0, "limit": 0},  # 完全無料・無制限
            "sharevox": {"characters": 0, "requests": 0, "limit": 0},
            "coeiroink": {"characters": 0, "requests": 0, "limit": 0},
            "openjtalk": {"characters": 0, "requests": 0, "limit": 0},
            "savings": {
                "saved_characters": 0,
                "hits": 0,
                "hit_rate_percent": 0.0,
                "cloud": {
                    "saved_characters": 0,
                    "hits": 0,
                    "hit_rate_percent": 0.0
                },
                "local": {
                    "saved_characters": 0,
                    "hits": 0,
                    "hit_rate_percent": 0.0
                }
            }
        }

        async with self.get_connection() as db:
            async with db.execute(
                "SELECT provider, characters, requests FROM tts_usage WHERE month = ?",
                (month,)
            ) as cursor:
                rows = await cursor.fetchall()
                for prov, chars, reqs in rows:
                    p = prov.lower()
                    if p in usage:
                        usage[p]["characters"] = chars
                        usage[p]["requests"] = reqs

            async with db.execute(
                """
                SELECT saved_characters, hits, saved_cloud_characters, hits_cloud, saved_local_characters, hits_local
                FROM tts_cache_savings WHERE month = ?
                """,
                (month,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    saved_chars, hits, c_saved, c_hits, l_saved, l_hits = row
                    usage["savings"]["saved_characters"] = saved_chars or 0
                    usage["savings"]["hits"] = hits or 0
                    usage["savings"]["cloud"]["saved_characters"] = c_saved or 0
                    usage["savings"]["cloud"]["hits"] = c_hits or 0
                    usage["savings"]["local"]["saved_characters"] = l_saved or 0
                    usage["savings"]["local"]["hits"] = l_hits or 0

        # クラウド試行数 (Azure + Google + Edge + Cloud Hits)
        cloud_reqs = usage["azure"]["requests"] + usage["google"]["requests"] + usage["edge"]["requests"]
        cloud_hits = usage["savings"]["cloud"]["hits"]
        cloud_total = cloud_reqs + cloud_hits
        if cloud_total > 0:
            usage["savings"]["cloud"]["hit_rate_percent"] = round((cloud_hits / cloud_total) * 100, 1)

        # ローカル試行数 (VOICEVOX + SHAREVOX + COEIROINK + OpenJTalk + Local Hits)
        local_reqs = (
            usage["voicevox"]["requests"]
            + usage["sharevox"]["requests"]
            + usage["coeiroink"]["requests"]
            + usage["openjtalk"]["requests"]
        )
        local_hits = usage["savings"]["local"]["hits"]
        local_total = local_reqs + local_hits
        if local_total > 0:
            usage["savings"]["local"]["hit_rate_percent"] = round((local_hits / local_total) * 100, 1)

        # 全体試行数
        total_reqs = cloud_reqs + local_reqs
        total_hits = usage["savings"]["hits"]
        total_all = total_reqs + total_hits
        if total_all > 0:
            usage["savings"]["hit_rate_percent"] = round((total_hits / total_all) * 100, 1)

        return usage

    async def get_provider_monthly_characters(self, provider: str, month: Optional[str] = None) -> int:
        """指定プロバイダーの当月消費文字数を高速取得 (無料枠ガード用)"""
        if not month:
            month = datetime.now().strftime("%Y-%m")
        async with self.get_connection() as db:
            async with db.execute(
                "SELECT characters FROM tts_usage WHERE month = ? AND provider = ?",
                (month, provider.lower())
            ) as cursor:
                row = await cursor.fetchone()
                return int(row[0]) if row else 0

    # --- 接続中VCセッション管理 (再起動時復旧用・マルチBot対応) ---

    async def save_active_vc(
        self,
        guild_id: int,
        voice_channel_id: int,
        text_channel_id: int,
        bot_index: int = 1
    ) -> None:
        """接続中のVCセッションを保存 (同一VCへの別号機レコードは自動パージして重複を完全防止)"""
        async with self.get_connection() as db:
            # 同一ギルド・同一VCに他号機のセッションが残っていれば即座に削除 (物理的二重化を遮断)
            await db.execute(
                "DELETE FROM active_vc_sessions WHERE guild_id = ? AND voice_channel_id = ? AND bot_index != ?",
                (guild_id, voice_channel_id, bot_index)
            )
            await db.execute(
                """
                INSERT OR REPLACE INTO active_vc_sessions (guild_id, bot_index, voice_channel_id, text_channel_id, connected_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (guild_id, bot_index, voice_channel_id, text_channel_id)
            )
            await db.commit()
        logger.debug(f"Saved active VC session for guild {guild_id} (bot #{bot_index}): VC={voice_channel_id}, Text={text_channel_id}")

    async def remove_active_vc(self, guild_id: int, bot_index: Optional[int] = None) -> None:
        """VC切断時にセッション情報を削除 (bot_index指定時はその号機のみ、未指定時はギルド全体)"""
        async with self.get_connection() as db:
            if bot_index is not None:
                await db.execute("DELETE FROM active_vc_sessions WHERE guild_id = ? AND bot_index = ?", (guild_id, bot_index))
            else:
                await db.execute("DELETE FROM active_vc_sessions WHERE guild_id = ?", (guild_id,))
            await db.commit()
        logger.debug(f"Removed active VC session for guild {guild_id} (bot #{bot_index if bot_index else 'all'})")

    async def get_all_active_vcs(self) -> List[Dict[str, int]]:
        """再起動時に復旧すべき全接続中VCセッションを取得 (同一VCの重複を自動修復)"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT guild_id, bot_index, voice_channel_id, text_channel_id FROM active_vc_sessions") as cur:
                rows = await cur.fetchall()

            # 同一 (guild_id, voice_channel_id) に対する重複レコードを自己修復
            unique_sessions: Dict[Tuple[int, int], Dict[str, int]] = {}
            duplicates_to_delete: List[Tuple[int, int]] = []
            for r in rows:
                d = dict(r)
                key = (d["guild_id"], d["voice_channel_id"])
                if key not in unique_sessions:
                    unique_sessions[key] = d
                else:
                    # 最小の号機番号を優先して残す
                    if d["bot_index"] < unique_sessions[key]["bot_index"]:
                        duplicates_to_delete.append((unique_sessions[key]["guild_id"], unique_sessions[key]["bot_index"]))
                        unique_sessions[key] = d
                    else:
                        duplicates_to_delete.append((d["guild_id"], d["bot_index"]))

            if duplicates_to_delete:
                for g_id, b_idx in duplicates_to_delete:
                    await db.execute("DELETE FROM active_vc_sessions WHERE guild_id = ? AND bot_index = ?", (g_id, b_idx))
                await db.commit()
                logger.warning(f"Self-healed {len(duplicates_to_delete)} duplicate active VC session(s) from database.")

            return list(unique_sessions.values())

    # --- キャッシュメタデータ管理 (エクスプローラー & スマートパーツ可視化) ---

    async def record_cache_saved(
        self,
        cache_key: str,
        provider: str,
        text: str,
        speaker_id: Optional[str] = None,
        speaker_name: Optional[str] = None,
        is_chunk: bool = False,
        duration: float = 0.0,
        size_bytes: int = 0
    ) -> None:
        """新規作成または更新された音声キャッシュのメタデータを記録（ロック競合時は最大3回リトライ）"""
        if self.is_closed:
            return
        for attempt in range(3):
            try:
                async with self.get_connection() as db:
                    await db.execute(
                        """
                        INSERT INTO cache_metadata (
                            cache_key, provider, text, speaker_id, speaker_name, is_chunk, duration, size_bytes, hit_count, created_at, last_hit_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            provider=excluded.provider,
                            text=excluded.text,
                            speaker_id=excluded.speaker_id,
                            speaker_name=excluded.speaker_name,
                            is_chunk=excluded.is_chunk,
                            duration=excluded.duration,
                            size_bytes=excluded.size_bytes,
                            last_hit_at=CURRENT_TIMESTAMP
                        """,
                        (
                            cache_key,
                            provider.lower(),
                            text.strip(),
                            speaker_id or "",
                            speaker_name or "",
                            1 if is_chunk else 0,
                            round(duration, 2),
                            size_bytes
                        )
                    )
                    await db.commit()
                    return
            except (sqlite3.OperationalError, aiosqlite.OperationalError) as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.1 * (2 ** attempt) + random.uniform(0.01, 0.05))
                    continue
                if self.is_closed or "Connection pool is closed" in str(e):
                    return
                logger.warning(f"Failed to record cache metadata for {cache_key} due to DB lock: {e}")
                return
            except Exception as e:
                if self.is_closed or "Connection pool is closed" in str(e):
                    return
                logger.warning(f"Failed to record cache metadata for {cache_key}: {e}")
                return

    async def record_cache_metadata_hit(self, cache_key: str) -> None:
        """キャッシュヒット時に再利用回数 (hit_count) と最終ヒット日時をインクリメント（ロック競合時は最大3回リトライ、非ブロッキング）"""
        if self.is_closed:
            return
        for attempt in range(3):
            try:
                async with self.get_connection() as db:
                    await db.execute(
                        """
                        UPDATE cache_metadata
                        SET hit_count = hit_count + 1, last_hit_at = CURRENT_TIMESTAMP
                        WHERE cache_key = ?
                        """,
                        (cache_key,)
                    )
                    await db.commit()
                    return
            except (sqlite3.OperationalError, aiosqlite.OperationalError) as e:
                if "locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.1 * (2 ** attempt) + random.uniform(0.01, 0.05))
                    continue
                if self.is_closed or "Connection pool is closed" in str(e):
                    return
                logger.debug(f"Cache hit recording skipped due to db lock ({e})")
                return
            except Exception as e:
                if self.is_closed or "Connection pool is closed" in str(e):
                    return
                logger.warning(f"Failed to record cache metadata hit for {cache_key}: {e}")
                return

    async def get_cache_metadata(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """特定のキャッシュファイルのメタデータを取得"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT cache_key, provider, text, speaker_id, speaker_name, is_chunk, duration, size_bytes, hit_count, created_at, last_hit_at FROM cache_metadata WHERE cache_key = ?",
                (cache_key,)
            ) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def list_cache_metadata(
        self,
        target: str = "cloud",
        search: str = "",
        sort_by: str = "mtime",
        sort_order: str = "desc",
        page: int = 1,
        per_page: int = 30
    ) -> Dict[str, Any]:
        """
        キャッシュエクスプローラー用: 検索・ソート・ページネーション付きでメタデータ一覧を取得。
        target: 'cloud' (azure/google/edge等) または 'local' (voicevox/sharevox/coeiroink/openjtalk等)
        """
        local_providers = ('voicevox', 'sharevox', 'coeiroink', 'openjtalk')
        
        # ターゲット条件
        if target == "local":
            target_filter = "provider IN ('voicevox', 'sharevox', 'coeiroink', 'openjtalk') OR provider LIKE 'vv_%'"
        else:
            target_filter = "provider NOT IN ('voicevox', 'sharevox', 'coeiroink', 'openjtalk') AND provider NOT LIKE 'vv_%'"

        # 検索条件
        where_clauses = [f"({target_filter})"]
        params = []

        if search:
            s = f"%{search.strip()}%"
            where_clauses.append("(text LIKE ? OR speaker_name LIKE ? OR cache_key LIKE ?)")
            params.extend([s, s, s])

        where_sql = " AND ".join(where_clauses)

        # ソート条件
        sort_order_sql = "ASC" if sort_order.lower() == "asc" else "DESC"
        if sort_by == "hits":
            order_sql = f"hit_count {sort_order_sql}, last_hit_at DESC"
        elif sort_by == "size":
            order_sql = f"size_bytes {sort_order_sql}"
        elif sort_by == "duration":
            order_sql = f"duration {sort_order_sql}"
        elif sort_by == "text":
            order_sql = f"text {sort_order_sql}"
        else:  # mtime / created_at
            order_sql = f"created_at {sort_order_sql}"

        offset = max(0, (page - 1) * per_page)

        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row

            # 1. 総件数取得
            count_query = f"SELECT COUNT(*) FROM cache_metadata WHERE {where_sql}"
            async with db.execute(count_query, params) as cur:
                total_count = (await cur.fetchone())[0]

            # 2. 一覧取得
            list_query = f"""
                SELECT cache_key, provider, text, speaker_id, speaker_name, is_chunk, duration, size_bytes, hit_count, created_at, last_hit_at
                FROM cache_metadata
                WHERE {where_sql}
                ORDER BY {order_sql}
                LIMIT ? OFFSET ?
            """
            async with db.execute(list_query, params + [per_page, offset]) as cur:
                rows = await cur.fetchall()

            total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 1

            return {
                "items": [dict(r) for r in rows],
                "total": total_count,
                "total_pages": total_pages,
                "page": page,
                "per_page": per_page,
                "target": target
            }

    async def delete_cache_metadata(self, cache_key: str) -> bool:
        """単一のキャッシュメタデータを削除"""
        async with self.get_connection() as db:
            cur = await db.execute("DELETE FROM cache_metadata WHERE cache_key = ?", (cache_key,))
            await db.commit()
            return cur.rowcount > 0

    async def delete_cache_metadata_batch(self, cache_keys: List[str]) -> int:
        """複数のキャッシュメタデータを一括削除"""
        if not cache_keys:
            return 0
        async with self.get_connection() as db:
            placeholders = ",".join(["?"] * len(cache_keys))
            cur = await db.execute(f"DELETE FROM cache_metadata WHERE cache_key IN ({placeholders})", cache_keys)
            await db.commit()
            return cur.rowcount

    async def purge_cache_metadata(self, target: str = "all") -> int:
        """キャッシュメタデータを一括パージ (all / cloud / local)"""
        async with self.get_connection() as db:
            if target == "local":
                cur = await db.execute("DELETE FROM cache_metadata WHERE provider IN ('voicevox', 'sharevox', 'coeiroink', 'openjtalk') OR provider LIKE 'vv_%'")
            elif target == "cloud":
                cur = await db.execute("DELETE FROM cache_metadata WHERE provider NOT IN ('voicevox', 'sharevox', 'coeiroink', 'openjtalk') AND provider NOT LIKE 'vv_%'")
            else:
                cur = await db.execute("DELETE FROM cache_metadata")
            await db.commit()
            return cur.rowcount

    async def find_cache_keys_by_word(self, word: str) -> List[Tuple[str, str]]:
        """特定の単語を含むキャッシュメタデータ (cache_key, provider) を検索"""
        if not word or not word.strip():
            return []
        pattern = f"%{word.strip()}%"
        async with self.get_connection() as db:
            cur = await db.execute(
                "SELECT cache_key, provider FROM cache_metadata WHERE text LIKE ?",
                (pattern,)
            )
            rows = await cur.fetchall()
            return [(r[0], r[1]) for r in rows]

    async def get_all_cache_keys_with_provider(self) -> List[Tuple[str, str]]:
        """全キャッシュメタデータの (cache_key, provider) 一覧を取得"""
        async with self.get_connection() as db:
            cur = await db.execute("SELECT cache_key, provider FROM cache_metadata")
            rows = await cur.fetchall()
            return [(r[0], r[1]) for r in rows]

    # --- システムメトリクス永続化 ---

    async def record_metric(self, entry: Dict[str, Any]) -> None:
        """メトリクススナップショットをDBに永続化 (24時間以上前は自動クリーンアップ)"""
        ts = int(entry.get("timestamp") or time.time())
        t_str = entry.get("time", "")
        cpu = float(entry.get("host", {}).get("cpu_percent", 0.0))
        mem = float(entry.get("host", {}).get("mem_percent", 0.0))
        data_json = json.dumps(entry, ensure_ascii=False)

        try:
            async with self.get_connection() as db:
                await db.execute(
                    """
                    INSERT INTO metrics_history (timestamp, time_str, cpu_percent, mem_percent, data_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ts, t_str, cpu, mem, data_json)
                )
                # 30回に1回のみ古いメトリクスをクリーンアップ（高頻度走査を削減）
                self._metric_delete_counter += 1
                if self._metric_delete_counter >= 30:
                    cutoff = ts - 86400
                    await db.execute("DELETE FROM metrics_history WHERE timestamp < ?", (cutoff,))
                    self._metric_delete_counter = 0
                await db.commit()
        except (RuntimeError, Exception) as e:
            # シャットダウン時のプールクローズ等は無視
            if "pool is closed" in str(e).lower() or "closed" in str(e).lower():
                return
            logger.debug(f"Failed to record metric (shutting down?): {e}")

    async def get_recent_metrics(self, limit: int = 60) -> List[Dict[str, Any]]:
        """直近のメトリクス履歴を復元用として取得（時系列昇順）"""
        async with self.get_connection() as db:
            cur = await db.execute(
                """
                SELECT data_json FROM metrics_history
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,)
            )
            rows = await cur.fetchall()
            results = []
            for r in reversed(rows):
                try:
                    results.append(json.loads(r[0]))
                except Exception:
                    pass
            return results

    # --- Graceful Shutdown 再生キュー保全 ---

    async def save_pending_queue(self, guild_id: int, bot_index: int, items_json: List[str]) -> None:
        """シャットダウン時にギルドの残キューアイテムをDBに退避 (executemany で高速バッチ保存)"""
        if not items_json:
            return
        async with self.get_connection() as db:
            # 既存の残キューがあれば削除
            await db.execute(
                "DELETE FROM pending_playback_queue WHERE guild_id = ? AND bot_index = ?",
                (guild_id, bot_index)
            )
            params = [(guild_id, bot_index, idx, it_json) for idx, it_json in enumerate(items_json)]
            await db.executemany(
                """
                INSERT INTO pending_playback_queue (guild_id, bot_index, item_order, item_json)
                VALUES (?, ?, ?, ?)
                """,
                params
            )
            await db.commit()

    async def restore_and_clear_pending_queue(self, guild_id: int, bot_index: int = 1) -> List[str]:
        """ギルドの退避された残キューアイテムを復元し、DBから削除"""
        async with self.get_connection() as db:
            cur = await db.execute(
                """
                SELECT item_json FROM pending_playback_queue
                WHERE guild_id = ? AND bot_index = ?
                ORDER BY item_order ASC
                """,
                (guild_id, bot_index)
            )
            rows = await cur.fetchall()
            if rows:
                await db.execute(
                    "DELETE FROM pending_playback_queue WHERE guild_id = ? AND bot_index = ?",
                    (guild_id, bot_index)
                )
                await db.commit()
            return [r[0] for r in rows]

    async def save_playback_latency(self, record_dict: Dict[str, Any]) -> None:
        """レイテンシ計測レコードをDBに保存 (直近200件保持)"""
        try:
            raw_json = json.dumps(record_dict, ensure_ascii=False)
            async with self.get_connection() as db:
                await db.execute(
                    "INSERT INTO playback_latency_history (record_json) VALUES (?)",
                    (raw_json,)
                )
                # 20回に1回のみ200件超過分をクリーンアップ（毎回サブクエリDELETEを削減）
                self._latency_delete_counter += 1
                if self._latency_delete_counter >= 20:
                    await db.execute("""
                        DELETE FROM playback_latency_history
                        WHERE id NOT IN (
                            SELECT id FROM playback_latency_history ORDER BY id DESC LIMIT 200
                        )
                    """)
                    self._latency_delete_counter = 0
                await db.commit()
        except Exception as e:
            logger.debug(f"Failed to save playback latency to DB: {e}")

    async def get_recent_playback_latencies(self, limit: int = 60) -> List[Dict[str, Any]]:
        """直近のレイテンシ計測レコードを取得 (Bot起動時の復元用)"""
        try:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT record_json FROM playback_latency_history ORDER BY id DESC LIMIT ?",
                    (limit,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    records = []
                    for row in reversed(rows):
                        try:
                            records.append(json.loads(row[0]))
                        except Exception:
                            continue
                    return records
        except Exception as e:
            logger.debug(f"Failed to load playback latencies from DB: {e}")
            return []

    async def save_latency_incident(self, incident: Dict[str, Any]) -> None:
        """状態悪化インシデントを保存 (最大1000件 / 30日保持)"""
        try:
            async with self.get_connection() as db:
                await db.execute(
                    "INSERT INTO latency_incidents (guild_id, incident_types, root_causes, incident_json) VALUES (?, ?, ?, ?)",
                    (
                        int(incident.get("guild_id") or 0),
                        ",".join(incident.get("incident_types", [])),
                        ",".join(incident.get("root_causes", [])),
                        json.dumps(incident, ensure_ascii=False, default=str),
                    )
                )
                self._incident_delete_counter = getattr(self, "_incident_delete_counter", 0) + 1
                if self._incident_delete_counter >= 50:
                    await db.execute("DELETE FROM latency_incidents WHERE created_at < datetime('now', '-30 days')")
                    await db.execute("""
                        DELETE FROM latency_incidents
                        WHERE id NOT IN (SELECT id FROM latency_incidents ORDER BY id DESC LIMIT 1000)
                    """)
                    self._incident_delete_counter = 0
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to save latency incident to DB: {e}")

    async def get_latency_incidents(self, limit: int = 50) -> List[Dict[str, Any]]:
        """直近の状態悪化インシデントを新しい順で取得"""
        try:
            async with self.get_connection() as db:
                async with db.execute(
                    "SELECT id, incident_json FROM latency_incidents ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit), 500)),)
                ) as cursor:
                    rows = await cursor.fetchall()
            result = []
            for row in rows:
                try:
                    item = json.loads(row[1])
                    item["id"] = row[0]
                    result.append(item)
                except Exception:
                    continue
            return result
        except Exception as e:
            logger.warning(f"Failed to load latency incidents from DB: {e}")
            return []

    # --- D-2: キャッシュ統計・TTS利用サマリー集計 ---

    async def get_cache_stats_summary(self) -> Dict[str, Any]:
        """キャッシュ統計（プロバイダー別ファイル数・サイズ・ヒット数、トップ単語、TTS利用量）を集計"""
        async with self.get_connection() as db:
            # プロバイダー別キャッシュ集計
            cur = await db.execute("""
                SELECT provider, COUNT(*), SUM(size_bytes), SUM(hit_count)
                FROM cache_metadata
                GROUP BY provider
            """)
            provider_stats: Dict[str, Dict[str, Any]] = {}
            total_files = 0
            total_bytes = 0
            total_hits = 0
            for row in await cur.fetchall():
                p, count, size, hits = row[0], row[1] or 0, row[2] or 0, row[3] or 0
                provider_stats[p] = {
                    "files": count,
                    "bytes": size,
                    "hits": hits
                }
                total_files += count
                total_bytes += size
                total_hits += hits

            # トップヒット単語 TOP 10
            cur = await db.execute("""
                SELECT text, provider, hit_count, size_bytes
                FROM cache_metadata
                ORDER BY hit_count DESC
                LIMIT 10
            """)
            top_words = [
                {"text": r[0], "provider": r[1], "hits": r[2], "bytes": r[3]}
                for r in await cur.fetchall()
            ]

            # 今月のTTS利用量
            cur = await db.execute("""
                SELECT provider, characters, requests
                FROM tts_usage
                WHERE month = strftime('%Y-%m', 'now', 'localtime')
            """)
            monthly_usage = {
                r[0]: {"characters": r[1], "requests": r[2]}
                for r in await cur.fetchall()
            }

            return {
                "total_files": total_files,
                "total_bytes": total_bytes,
                "total_hits": total_hits,
                "by_provider": provider_stats,
                "top_cached_phrases": top_words,
                "monthly_tts_usage": monthly_usage
            }

    async def maintenance(self) -> Dict[str, Any]:
        """
        SQLite WAL ファイルの完全チェックポイント & クエリオプティマイザの統計更新
        - PRAGMA wal_checkpoint(TRUNCATE): WAL ファイルをメインDBに反映しサイズを0に切り詰め、肥大化とリード遅延を防止
        - PRAGMA optimize: クエリ実行計画の最適化統計情報を更新
        """
        result: Dict[str, Any] = {"checkpoint": None, "optimized": False}
        try:
            async with self.get_connection() as db:
                cur = await db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                row = await cur.fetchone()
                if row:
                    result["checkpoint"] = {"busy": row[0], "log": row[1], "checkpointed": row[2]}
                await db.execute("PRAGMA optimize;")
                result["optimized"] = True
                logger.info(f"SQLite maintenance completed: checkpoint={result['checkpoint']}")
        except Exception as e:
            logger.warning(f"Error during SQLite maintenance: {e}")
        return result

    # --- システム認証情報 & 動的Botトークン管理 ---

    async def get_credential(self, key: str, default: str = "") -> str:
        """システム設定・認証情報値を取得"""
        async with self.get_connection() as db:
            async with db.execute("SELECT value FROM system_credentials WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
                return str(row[0]) if row else default

    async def set_credential(self, key: str, value: str, is_secret: bool = False) -> None:
        """システム設定・認証情報値を保存・更新"""
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO system_credentials (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """, (key, str(value)))
            await db.commit()

    async def get_all_credentials(self) -> Dict[str, str]:
        """全システム認証情報・キー設定を取得"""
        async with self.get_connection() as db:
            async with db.execute("SELECT key, value FROM system_credentials") as cur:
                rows = await cur.fetchall()
                return {r[0]: r[1] for r in rows}

    async def get_bot_tokens(self) -> List[Dict[str, Any]]:
        """登録済みの全Botトークン一覧を取得 (1〜10号機昇順)"""
        async with self.get_connection() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM bot_tokens ORDER BY bot_index ASC") as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]

    async def save_bot_token(self, bot_index: int, token: str, label: str = "", is_enabled: bool = True) -> None:
        """Botトークンを保存・更新 (1〜10号機)"""
        if not (1 <= bot_index <= 10):
            raise ValueError("bot_index must be between 1 and 10")
        async with self.get_connection() as db:
            await db.execute("""
                INSERT INTO bot_tokens (bot_index, token, label, is_enabled, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(bot_index) DO UPDATE SET
                    token=excluded.token,
                    label=excluded.label,
                    is_enabled=excluded.is_enabled,
                    updated_at=CURRENT_TIMESTAMP
            """, (bot_index, str(token).strip(), label, 1 if is_enabled else 0))
            await db.commit()

    async def delete_bot_token(self, bot_index: int) -> bool:
        """Botトークンを削除"""
        async with self.get_connection() as db:
            cur = await db.execute("DELETE FROM bot_tokens WHERE bot_index = ?", (bot_index,))
            await db.commit()
            return cur.rowcount > 0

    async def toggle_bot_token(self, bot_index: int, is_enabled: bool) -> bool:
        """Botトークンの有効/無効を切り替え"""
        async with self.get_connection() as db:
            cur = await db.execute("UPDATE bot_tokens SET is_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE bot_index = ?", (1 if is_enabled else 0, bot_index))
            await db.commit()
            return cur.rowcount > 0








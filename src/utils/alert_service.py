"""
src/utils/alert_service.py
エラー検知・障害通知サブシステム。
Bot本体、Masterエンジン、Workerノード、Workerエンジンの異常を検知し、
指定の管理者ユーザーへDiscord DM経由でリアルタイム通知（JST日時・リッチEmbed・スパム抑制付き）を行います。
"""

import os
import time
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import discord

logger = logging.getLogger(__name__)

# JSTタイムゾーン定義 (UTC+9)
JST = timezone(timedelta(hours=9))

# 初期・フォールバック用管理者ユーザーID (環境変数または未設定0)
try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", os.getenv("DISCORD_OWNER_USER_ID", "0")) or 0)
except ValueError:
    ADMIN_USER_ID = 0

# エラー分類定義
CATEGORY_BOT = "Bot本体"
CATEGORY_MASTER_ENGINE = "Masterエンジン"
CATEGORY_WORKER = "Workerノード"
CATEGORY_WORKER_ENGINE = "Workerエンジン"

# 重大度定義
SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"
SEVERITY_INFO = "INFO"


class AlertService:
    def __init__(self):
        self.bot: Optional[discord.Client] = None
        self.db: Any = None
        self.default_contact_id: int = ADMIN_USER_ID
        # クールダウン管理: error_key -> {"last_sent": float, "suppressed_count": int}
        self._cooldowns: Dict[str, Dict[str, Any]] = {}
        self._default_cooldown_seconds = 300.0  # 同一エラーのスパム抑制: 5分間
        self._lock = asyncio.Lock()

    def set_bot(self, bot: discord.Client, db: Any = None):
        """Bot インスタンスおよび DB を登録"""
        self.bot = bot
        if db is not None:
            self.db = db
        logger.info(f"📢 [AlertService] 障害DM通知サービスが初期化されました (デフォルト連絡先: {self.default_contact_id})。")

    def set_default_contact_id(self, user_id: int):
        """デフォルトの連絡先ユーザーIDを動的更新"""
        if user_id > 0:
            self.default_contact_id = user_id
            logger.info(f"📢 [AlertService] デフォルト連絡先ユーザーIDを {user_id} に更新しました。")

    async def resolve_target_user_id(self, guild_id: Optional[int] = None, explicit_target_id: Optional[int] = None) -> int:
        """ギルド設定またはシステム設定から通知先DiscordユーザーIDを安全に解決"""
        if explicit_target_id and explicit_target_id > 0:
            return explicit_target_id

        # 1. ギルド設定の連絡先を確認
        if self.db and guild_id:
            try:
                g_cfg = await self.db.get_guild_settings(guild_id)
                cid = g_cfg.get("error_contact_user_id")
                if cid and int(cid) > 0:
                    return int(cid)
            except Exception as e:
                logger.debug(f"[AlertService] Failed to get guild error_contact_user_id: {e}")

        # 2. システム設定 (system_credentials) の連絡先を確認
        if self.db and hasattr(self.db, "get_all_credentials"):
            try:
                creds = await self.db.get_all_credentials()
                cid = creds.get("error_contact_user_id")
                if cid and int(cid) > 0:
                    return int(cid)
            except Exception as e:
                logger.debug(f"[AlertService] Failed to get system error_contact_user_id: {e}")

        # 3. フォールバック
        return self.default_contact_id

    def notify(
        self,
        title: str,
        message: str,
        category: str = CATEGORY_BOT,
        severity: str = SEVERITY_ERROR,
        node_name: Optional[str] = None,
        engine_name: Optional[str] = None,
        details: Optional[str] = None,
        cooldown_seconds: Optional[float] = None,
        guild_id: Optional[int] = None,
        target_user_id: Optional[int] = None
    ):
        """
        同期・非同期のあらゆるコンテキストから安全に呼び出せる通知エントリポイント。
        イベントループが存在すれば非同期タスクとしてDM送信を即座にスケジュールします。
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self.send_alert(
                    title=title,
                    message=message,
                    category=category,
                    severity=severity,
                    node_name=node_name,
                    engine_name=engine_name,
                    details=details,
                    cooldown_seconds=cooldown_seconds,
                    guild_id=guild_id,
                    target_user_id=target_user_id
                )
            )
        except RuntimeError:
            # イベントループが動いていない場合は警告ログのみ
            logger.warning(f"⚠️ [AlertService] イベントループ外からの通知リクエスト: {title} - {message}")

    async def send_alert(
        self,
        title: str,
        message: str,
        category: str = CATEGORY_BOT,
        severity: str = SEVERITY_ERROR,
        node_name: Optional[str] = None,
        engine_name: Optional[str] = None,
        details: Optional[str] = None,
        cooldown_seconds: Optional[float] = None,
        guild_id: Optional[int] = None,
        target_user_id: Optional[int] = None
    ) -> bool:
        """
        管理者・エラー連絡先へ Discord DM を送信（スパム抑制付き）
        """
        if not self.bot or not self.bot.is_ready():
            logger.debug(f"[AlertService] Bot 未準備のため通知をスキップ: {title}")
            return False

        # 通知先DiscordユーザーIDの解決
        contact_id = await self.resolve_target_user_id(guild_id=guild_id, explicit_target_id=target_user_id)
        if not contact_id or contact_id <= 0:
            logger.warning(f"[AlertService] 有効なエラー連絡先ユーザーIDが未設定のため通知をスキップ: {title}")
            return False

        cd = cooldown_seconds if cooldown_seconds is not None else self._default_cooldown_seconds
        now = time.time()
        
        # エラーシグネチャの生成（同一エンジン・種別の障害通知ストームを確実に防止）
        # 本文の対象テキストや秒数の差異に影響されないよう、分類・エンジン名・タイトルで集約
        clean_engine = (engine_name or "").upper().strip()
        clean_title = title.split("・")[0].strip() if "・" in title else title.strip()
        signature = f"{contact_id}:{category}:{clean_engine}:{clean_title}"
        
        async with self._lock:
            # メモリ肥大化防止（1時間以上前の古いシグネチャを削除）
            if len(self._cooldowns) > 100:
                self._cooldowns = {
                    k: v for k, v in self._cooldowns.items()
                    if (now - v.get("last_sent", 0)) < 3600.0
                }

            state = self._cooldowns.get(signature)
            if state:
                time_since = now - state["last_sent"]
                if time_since < cd:
                    state["suppressed_count"] += 1
                    logger.debug(f"[AlertService] 同一エラーがクールダウン中のため抑制 (抑止件数: {state['suppressed_count']}): {title}")
                    return False
                else:
                    # クールダウン経過後の再通知
                    suppressed = state["suppressed_count"]
                    state["last_sent"] = now
                    state["suppressed_count"] = 0
            else:
                suppressed = 0
                self._cooldowns[signature] = {"last_sent": now, "suppressed_count": 0}

        try:
            # 連絡先ユーザーの取得
            admin_user = self.bot.get_user(contact_id)
            if not admin_user:
                try:
                    admin_user = await self.bot.fetch_user(contact_id)
                except Exception as e:
                    logger.error(f"❌ [AlertService] 連絡先ユーザー (ID: {contact_id}) の取得に失敗しました: {e}")
                    return False

            # Embed の構築
            color_map = {
                SEVERITY_CRITICAL: 0xed4245,  # 赤
                SEVERITY_ERROR:    0xfaa61a,  # 橙
                SEVERITY_WARNING:  0xfee75c,  # 黄
                SEVERITY_INFO:     0x5865f2,  # 青
            }
            icon_map = {
                SEVERITY_CRITICAL: "🚨",
                SEVERITY_ERROR:    "❌",
                SEVERITY_WARNING:  "⚠️",
                SEVERITY_INFO:     "ℹ️",
            }
            embed_color = color_map.get(severity.upper(), 0xed4245)
            icon = icon_map.get(severity.upper(), "⚠️")

            jst_now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
            embed = discord.Embed(
                title=f"{icon} [{severity.upper()}] {title}",
                description=message,
                color=embed_color,
                timestamp=datetime.now(timezone.utc)
            )

            # フィールド追加
            embed.add_field(name="📂 エラー分類", value=f"`{category}`", inline=True)
            if node_name:
                embed.add_field(name="🖥️ 対象ノード", value=f"`{node_name}`", inline=True)
            if engine_name:
                embed.add_field(name="🎙️ 対象エンジン", value=f"`{engine_name}`", inline=True)
            embed.add_field(name="🕒 発生時刻 (JST)", value=f"`{jst_now}`", inline=True)

            if suppressed > 0:
                embed.add_field(
                    name="🔁 連続発生",
                    value=f"前回の通知からさらに **{suppressed} 回** 同一エラーが発生しました。",
                    inline=False
                )

            # スタックトレース・詳細情報
            if details:
                clean_details = str(details).strip()
                if len(clean_details) > 1000:
                    clean_details = clean_details[-1000:]
                embed.add_field(
                    name="📜 詳細 / スタックトレース",
                    value=f"```python\n{clean_details}\n```",
                    inline=False
                )

            embed.set_footer(text="Distributed TTS 監視サブシステム (JST)")

            # DM送信
            await admin_user.send(embed=embed)
            logger.info(f"📨 [AlertService] 連絡先 ({contact_id}) へエラーDM通知を送信しました: [{severity}] {title}")
            return True

        except discord.Forbidden:
            logger.error(f"❌ [AlertService] 連絡先 ({contact_id}) へのDM送信が拒否されました (DM受信許可設定を確認してください)")
            return False
        except Exception as e:
            logger.error(f"❌ [AlertService] DM通知送信中にエラーが発生しました: {e}", exc_info=True)
            return False


# シングルトンインスタンス
alert_service = AlertService()

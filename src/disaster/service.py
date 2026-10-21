"""
緊急地震速報・地震情報・津波警報・気象特別警報の常時監視 & VC/テキスト速報通知サービス
"""

import asyncio
import collections
import logging
import time
from typing import Dict, Any, List, Set, Optional
import aiohttp
import discord

from src.disaster.parser import (
    parse_earthquake_event,
    parse_eew_event,
    parse_tsunami_event,
    parse_weather_warning_item,
    format_scale_name
)

logger = logging.getLogger(__name__)

P2P_QUAKE_URL = "https://api.p2pquake.net/v2/history?codes=551&codes=552&codes=554&codes=556&limit=10"
JMA_MAP_URL = "https://www.jma.go.jp/bosai/warning/data/r8/map.json"


class DisasterAlertService:
    def __init__(self, bot_pool, db, config):
        self.bot_pool = bot_pool
        self.db = db
        self.config = config
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._seen_ids: collections.OrderedDict = collections.OrderedDict()
        self._seen_eew_events: collections.OrderedDict = collections.OrderedDict()
        self._last_eew_556_time = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._initialized = False
        self._p2p_failure_count = 0
        self._jma_failure_count = 0
        self._last_p2p_warning_time = 0.0
        self._last_jma_warning_time = 0.0
        self._jma_etag: Optional[str] = None
        self._jma_last_modified: Optional[str] = None

    def _mark_seen(self, event_id: str) -> None:
        """イベントIDを既読に登録（最新2000件をFIFOで保持）"""
        self._seen_ids[event_id] = True
        if len(self._seen_ids) > 2000:
            self._seen_ids.popitem(last=False)

    def _get_session(self) -> aiohttp.ClientSession:
        """永続的な aiohttp.ClientSession を取得（コネクションプーリング有効）"""
        if self._session is None or self._session.closed:
            headers = {"User-Agent": "Discord-TTS-Bot-Disaster/1.0"}
            connector = aiohttp.TCPConnector(limit=10, keepalive_timeout=30)
            self._session = aiohttp.ClientSession(headers=headers, connector=connector)
        return self._session

    def start(self) -> None:
        """バックグラウンド監視タスクを開始"""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("DisasterAlertService started (monitoring P2PQuake & JMA warnings).")

    def stop(self) -> None:
        """監視タスクを停止"""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._session and not self._session.closed:
            asyncio.create_task(self._session.close())
        logger.info("DisasterAlertService stopped.")

    async def _monitor_loop(self) -> None:
        """15秒周期のメインポーリングループ"""
        # 初回起動時は既存の直近データを取得して既読化（誤爆通知を防止）
        await self._initial_sync()

        jma_check_counter = 0

        while self._is_running:
            try:
                # 1. 地震・津波・EEW (15秒ごと)
                await self._check_p2p_quake()

                # 2. 気象警報・特別警報 (約60秒ごと: 4ループに1回)
                jma_check_counter += 1
                if jma_check_counter >= 4:
                    jma_check_counter = 0
                    await self._check_jma_weather()

            except asyncio.CancelledError:
                break
            except Exception as e:
                err_type = type(e).__name__
                logger.error(f"Error in disaster monitor loop ({err_type}): {e}", exc_info=True)

            await asyncio.sleep(15)

    async def _initial_sync(self) -> None:
        """初回起動時に直近のイベントIDを学習し、過去の地震や警報を通知しないようにする"""
        session = self._get_session()
        # P2P
        try:
            async with session.get(P2P_QUAKE_URL, timeout=aiohttp.ClientTimeout(total=5.0, connect=3.0)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for item in data:
                        iid = item.get("id") or item.get("_id")
                        if iid:
                            self._mark_seen(str(iid))
        except Exception as e:
            logger.debug(f"Initial P2PQuake sync skipped ({type(e).__name__}): {e}")

        # JMA
        try:
            headers = {}
            if self._jma_etag:
                headers["If-None-Match"] = self._jma_etag
            if self._jma_last_modified:
                headers["If-Modified-Since"] = self._jma_last_modified
            async with session.get(JMA_MAP_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=8.0, connect=3.0)) as resp:
                if resp.status == 200:
                    if "ETag" in resp.headers:
                        self._jma_etag = resp.headers["ETag"]
                    if "Last-Modified" in resp.headers:
                        self._jma_last_modified = resp.headers["Last-Modified"]
                    data = await resp.json()
                    for item in data:
                        ev = parse_weather_warning_item(item)
                        if ev and ev.get("id"):
                            self._mark_seen(ev["id"])
        except Exception as e:
            logger.debug(f"Initial JMA warning sync skipped ({type(e).__name__}): {e}")

        self._initialized = True
        logger.info(f"DisasterAlertService initialized: pre-loaded {len(self._seen_ids)} existing event IDs.")

    async def _check_p2p_quake(self) -> None:
        """P2P地震情報 API から最新情報を取得・判定"""
        session = self._get_session()
        try:
            timeout = aiohttp.ClientTimeout(total=5.0, connect=3.0)
            async with session.get(P2P_QUAKE_URL, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.debug(f"P2PQuake API returned HTTP {resp.status}")
                    return
                data = await resp.json()

            if self._p2p_failure_count > 0:
                logger.info(f"✨ P2PQuake API connection recovered (was failing {self._p2p_failure_count} times).")
                self._p2p_failure_count = 0

            has_556_in_batch = any(item.get("code") == 556 for item in data)
            now = time.time()

            for item in reversed(data):  # 古い順に処理
                item_id = str(item.get("id") or item.get("_id") or "")
                code = item.get("code")

                # code 554 (発表検知) の重複抑制:
                # バッチ内に code 556 が含まれているか、または直近30秒以内に code 556 を処理済みの場合は
                # 554 は冗長（556の電文に含まれるか先行/追従電文）なためスキップ
                if code == 554 and (has_556_in_batch or (now - self._last_eew_556_time < 30.0)):
                    logger.debug(f"Skipping redundant EEW detection (code 554, id={item_id}) due to code 556 presence.")
                    continue

                event = None
                if code in [554, 556]:
                    event = parse_eew_event(item)
                elif code == 551:
                    event = parse_earthquake_event(item)
                elif code == 552:
                    event = parse_tsunami_event(item)

                if not event:
                    continue

                event_id = event["id"]
                if event_id in self._seen_ids:
                    continue

                # EEW (code 556) の同一イベント制御
                if code == 556:
                    self._last_eew_556_time = time.time()
                    eew_ev_id = event.get("event_id")
                    is_cancelled = event.get("is_cancelled", False)

                    if eew_ev_id:
                        last_status = self._seen_eew_events.get(eew_ev_id)
                        if is_cancelled:
                            # 取消報: すでに取消報を発報済みでなければ最優先で発報
                            if last_status == "cancelled":
                                self._mark_seen(event_id)
                                continue
                            self._seen_eew_events[eew_ev_id] = "cancelled"
                        else:
                            # 警報: すでに同イベントで警報を発報済みの場合は続報
                            # 続報は同一内容での多重読み上げ防止のためVC発話を抑制（初回のみ発話）
                            if last_status == "alert":
                                logger.info(f"Skipping duplicate EEW voice alert for serial {event.get('serial')} of event {eew_ev_id}")
                                self._mark_seen(event_id)
                                continue
                            self._seen_eew_events[eew_ev_id] = "alert"

                        if len(self._seen_eew_events) > 200:
                            self._seen_eew_events.popitem(last=False)

                self._mark_seen(event_id)

                logger.info(f"Detected new disaster event: {event.get('title')}")
                await self.dispatch_alert(event)

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            self._p2p_failure_count += 1
            now = time.time()
            if now - self._last_p2p_warning_time > 300:  # 5分に1回のみ警告
                err_type = type(e).__name__
                logger.warning(
                    f"⚠️ P2PQuake API is currently unreachable ({err_type}) - "
                    f"retrying in background (failed {self._p2p_failure_count} times)."
                )
                self._last_p2p_warning_time = now
            else:
                logger.debug(f"P2PQuake check failed ({type(e).__name__}): {e}")
        except Exception as e:
            logger.error(f"Unexpected error in _check_p2p_quake: {e}", exc_info=True)

    async def _check_jma_weather(self) -> None:
        """気象庁 map.json から最新の特別警報・警報の新規発表を取得・判定"""
        session = self._get_session()
        try:
            timeout = aiohttp.ClientTimeout(total=8.0, connect=3.0)
            headers = {}
            if self._jma_etag:
                headers["If-None-Match"] = self._jma_etag
            if self._jma_last_modified:
                headers["If-Modified-Since"] = self._jma_last_modified

            async with session.get(JMA_MAP_URL, headers=headers, timeout=timeout) as resp:
                if resp.status == 304:
                    # 304 Not Modified: 更新なし。930KBの受信とJSONデコードを完全スキップ
                    if self._jma_failure_count > 0:
                        logger.info(f"✨ JMA Warning API connection recovered (was failing {self._jma_failure_count} times).")
                        self._jma_failure_count = 0
                    return

                if resp.status != 200:
                    logger.debug(f"JMA Warning API returned HTTP {resp.status}")
                    return

                if "ETag" in resp.headers:
                    self._jma_etag = resp.headers["ETag"]
                if "Last-Modified" in resp.headers:
                    self._jma_last_modified = resp.headers["Last-Modified"]

                data = await resp.json()

            if self._jma_failure_count > 0:
                logger.info(f"✨ JMA Warning API connection recovered (was failing {self._jma_failure_count} times).")
                self._jma_failure_count = 0

            for item in data:
                event = parse_weather_warning_item(item)
                if not event:
                    continue

                event_id = event["id"]
                if event_id in self._seen_ids:
                    continue

                self._mark_seen(event_id)

                logger.info(f"Detected new weather warning: {event.get('title')}")
                await self.dispatch_alert(event)

        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            self._jma_failure_count += 1
            now = time.time()
            if now - self._last_jma_warning_time > 300:  # 5分に1回のみ警告
                err_type = type(e).__name__
                logger.warning(
                    f"⚠️ JMA Warning API is currently unreachable ({err_type}) - "
                    f"retrying in background (failed {self._jma_failure_count} times)."
                )
                self._last_jma_warning_time = now
            else:
                logger.debug(f"JMA Warning check failed ({type(e).__name__}): {e}")
        except Exception as e:
            logger.error(f"Unexpected error in _check_jma_weather: {e}", exc_info=True)

    async def dispatch_alert(self, event: Dict[str, Any], target_guild_id: Optional[int] = None) -> int:
        """
        現在BotがVC接続中のギルドを巡回し、設定に合致したVCとペアテキストチャンネルへ速報を配信
        戻り値: 配信したVC数
        """
        sent_count = 0
        is_test = bool(event.get("is_test", False))
        # 利用可能な全Botインスタンス
        if self.bot_pool:
            if hasattr(self.bot_pool, "get_all_bots"):
                bots = self.bot_pool.get_all_bots()
            elif isinstance(getattr(self.bot_pool, "bots", None), dict):
                bots = list(self.bot_pool.bots.values())
            elif isinstance(getattr(self.bot_pool, "bots", None), list):
                bots = self.bot_pool.bots
            else:
                bots = [self.bot_pool]
        else:
            bots = []

        for bot_idx, bot in enumerate(bots, start=1):
            for guild in bot.guilds:
                if target_guild_id and guild.id != target_guild_id:
                    continue

                # 1. BotがVCに接続中か確認
                vc: Optional[discord.VoiceClient] = guild.voice_client
                if not vc or not vc.is_connected() or not vc.channel:
                    continue

                # 2. VC内に人間（非Bot）が1人以上いるか確認 (テスト時は無人でもテスト再生)
                humans = [m for m in vc.channel.members if not m.bot]
                if len(humans) == 0 and not is_test:
                    continue

                # 3. ギルド設定の取得 (テスト時は無効化設定でもテスト実行)
                guild_settings = await self.db.get_guild_settings(guild.id, self.config)
                if not is_test and not bool(guild_settings.get("disaster_alert_enabled", 1)):
                    continue

                # 4. 閾値・地域判定 (テスト時はスキップ)
                if not is_test and not self._should_notify_guild(guild_settings, event):
                    continue

                # 5. ペアテキストチャンネルの特定
                target_text_channel = await self._find_paired_text_channel(guild, vc.channel.id, guild_settings)

                # 6. テキストチャンネルへ Embed 送信
                if target_text_channel:
                    embed = self._create_alert_embed(event)
                    try:
                        await target_text_channel.send(embed=embed)
                    except Exception as e:
                        logger.warning(f"Failed to send disaster alert embed to channel {target_text_channel.id}: {e}")

                # 7. VCへ音声速報を即時割り込み再生
                vc_cog = bot.get_cog("VCHandlerCog")
                if vc_cog:
                    try:
                        alert_vol = float(guild_settings.get("disaster_volume", 1.0) if guild_settings.get("disaster_volume") is not None else 1.0)
                        await vc_cog.speak_alert(guild, event["voice_text"], volume=alert_vol)
                        logger.info(f"Broadcasted disaster audio alert to VC '{vc.channel.name}' in '{guild.name}' (Bot #{bot_idx}, Vol: {alert_vol})")
                        sent_count += 1
                    except Exception as e:
                        logger.error(f"Failed to play disaster audio alert in guild {guild.id}: {e}")

        return sent_count

    def _should_notify_guild(self, settings: Dict[str, Any], event: Dict[str, Any]) -> bool:
        """ギルドの設定（震度閾値・地域フィルタ・警報トグル）に合致するか判定"""
        ev_type = event.get("type")

        # 地震情報
        if ev_type == "earthquake":
            min_scale = int(settings.get("disaster_min_scale", 30))  # デフォルト震度3 (30)
            if event.get("max_scale", 0) < min_scale:
                return False

            pref_filter = settings.get("disaster_pref_filter", "").strip()
            if pref_filter:
                prefs = [p.strip() for p in pref_filter.replace("、", ",").split(",") if p.strip()]
                affected = event.get("affected_prefs", [])
                hypo = event.get("hypocenter", "")
                # 指定地域が震度観測されたか、または震源地に含まれているか
                if not any(p in hypo or p in affected for p in prefs):
                    return False

        # 気象警報・特別警報
        elif ev_type == "weather":
            if not bool(settings.get("disaster_alert_weather", 1)):
                return False

            pref_filter = settings.get("disaster_pref_filter", "").strip()
            if pref_filter:
                prefs = [p.strip() for p in pref_filter.replace("、", ",").split(",") if p.strip()]
                pname = event.get("pref_name", "")
                if not any(p in pname for p in prefs):
                    return False

        # EEW (緊急地震速報) や 津波警報 は命に関わるためフィルターせず最優先配信
        return True

    async def _find_paired_text_channel(
        self,
        guild: discord.Guild,
        vc_id: int,
        settings: Dict[str, Any]
    ) -> Optional[discord.TextChannel]:
        """VCに対応するペアテキストチャンネルを解決"""
        # 1. auto_join_pairs から検索
        pairs = settings.get("auto_join_pairs", [])
        for p in pairs:
            if int(p.get("vc_id", 0)) == vc_id:
                txt_id = int(p.get("text_id", 0))
                ch = guild.get_channel(txt_id)
                if ch and isinstance(ch, discord.TextChannel):
                    return ch

        # 2. auto_join_text_channel_id
        atxt_id = int(settings.get("auto_join_text_channel_id", 0) or 0)
        if atxt_id > 0:
            ch = guild.get_channel(atxt_id)
            if ch and isinstance(ch, discord.TextChannel):
                return ch

        # 3. ギルド内の最初の送信可能なテキストチャンネル
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                return ch

        return None

    def _create_alert_embed(self, event: Dict[str, Any]) -> discord.Embed:
        """速報用のリッチな Discord Embed を生成"""
        ev_type = event.get("type")

        if ev_type == "earthquake":
            color = discord.Color.red() if event.get("max_scale", 0) >= 45 else discord.Color.orange()
            embed = discord.Embed(
                title=event.get("title"),
                description=f"**{event.get('time')} 頃発生**",
                color=color
            )
            embed.add_field(name="最大震度", value=f"**{event.get('scale_name')}**", inline=True)
            embed.add_field(name="震源地", value=event.get("hypocenter"), inline=True)
            embed.add_field(name="規模 / 深さ", value=f"{event.get('magnitude')} / {event.get('depth')}", inline=True)
            if event.get("tsunami_desc"):
                embed.add_field(name="津波情報", value=f"🌊 {event.get('tsunami_desc')}", inline=False)
            if event.get("pref_summary"):
                embed.add_field(name="各地の震度", value=event.get("pref_summary"), inline=False)
            embed.set_footer(text="情報元: 気象庁 / P2P地震情報 | TTS Bot 速報配信")

        elif ev_type == "eew":
            is_cancelled = bool(event.get("is_cancelled", False))
            is_alert = bool(event.get("is_alert", False))

            if is_cancelled:
                embed = discord.Embed(
                    title=event.get("title", "ℹ️ 【緊急地震速報 (取消)】先ほどの速報は取り消されました"),
                    description="**気象庁より先ほどの緊急地震速報の取り消しが発表されました。**\n揺れの心配はありません。",
                    color=discord.Color.blue()
                )
                if event.get("event_id"):
                    embed.add_field(name="対象イベントID", value=f"`{event.get('event_id')}`", inline=True)
                if event.get("time"):
                    embed.add_field(name="取消発表時刻", value=event.get("time"), inline=True)
                embed.set_footer(text="情報元: 気象庁 緊急地震速報 (EEW取消)")

            elif is_alert:
                embed = discord.Embed(
                    title=event.get("title", "⚠️ 【緊急地震速報 (警報)】強い揺れに警戒してください！"),
                    description="**強い揺れに警戒してください。頭を保護し、丈夫な机の下などに隠れて身の安全を確保してください！**",
                    color=discord.Color.red()
                )
                if event.get("hypocenter"):
                    embed.add_field(name="震源地", value=event.get("hypocenter"), inline=True)
                if event.get("magnitude") or event.get("depth"):
                    mag_depth = f"{event.get('magnitude', '不明')} / {event.get('depth', '不明')}"
                    embed.add_field(name="規模 / 深さ", value=mag_depth, inline=True)
                target_prefs = event.get("target_prefs", [])
                if target_prefs:
                    prefs_display = ", ".join(target_prefs[:15])
                    if len(target_prefs) > 15:
                        prefs_display += f" 他 {len(target_prefs) - 15} 都道府県"
                    embed.add_field(name="警戒対象地域", value=prefs_display, inline=False)
                if event.get("time"):
                    embed.add_field(name="検知時刻", value=event.get("time"), inline=True)
                embed.set_footer(text="情報元: 気象庁 緊急地震速報 (EEW警報)")

            else:
                embed = discord.Embed(
                    title=event.get("title", "⚠️ 【緊急地震速報】緊急地震速報が発表されました"),
                    description="気象庁より緊急地震速報が発表されました。今後の揺れにご注意ください。",
                    color=discord.Color.gold()
                )
                if event.get("time"):
                    embed.add_field(name="検知時刻", value=event.get("time"), inline=True)
                embed.set_footer(text="情報元: 気象庁 緊急地震速報 (EEW発表検出)")

        elif ev_type == "tsunami":
            embed = discord.Embed(
                title=event.get("title"),
                description=f"**{event.get('time')} 発表**\n沿岸部や河口付近の方は直ちに高台や避難ビル等の安全な場所へ避難してください！",
                color=discord.Color.purple() if "大津波" in event.get("top_grade", "") else discord.Color.red()
            )
            embed.add_field(name="対象沿岸", value=", ".join(event.get("areas", [])[:8]), inline=False)
            embed.set_footer(text="情報元: 気象庁 津波警報・注意報")

        elif ev_type == "weather":
            is_special = event.get("is_special", False)
            color = discord.Color.purple() if is_special else discord.Color.gold()
            embed = discord.Embed(
                title=event.get("title"),
                description=f"**発表地域: {event.get('pref_name')}**\n{event.get('headline')}",
                color=color
            )
            embed.add_field(name="発表警報", value="、".join(event.get("warnings", [])), inline=False)
            embed.set_footer(text="情報元: 気象庁 防災気象情報 (特別警報・警報)")

        else:
            embed = discord.Embed(
                title="🚨 防災速報",
                description=event.get("voice_text", ""),
                color=discord.Color.orange()
            )

        return embed

    async def trigger_test_alert(
        self,
        guild_id: Optional[int] = None,
        alert_type: str = "earthquake",
        target_guild: Optional[Any] = None,
        **kwargs
    ) -> int:
        """テスト用の速報を即座にシミュレート発信（動作確認用）"""
        if guild_id is None and target_guild is not None:
            guild_id = getattr(target_guild, "id", None)

        if alert_type == "weather":
            test_event = {
                "type": "weather",
                "id": f"test_weather_{int(time.time())}",
                "time": "テスト発表",
                "pref_name": "東京都地方",
                "is_special": False,
                "is_test": True,
                "warnings": ["大雨警報", "洪水警報"],
                "headline": "これはテスト通知です。低い土地の浸水や土砂崩れに警戒してください。",
                "voice_text": "【気象速報テスト】東京都地方に、大雨警報と洪水警報が発表されました。これは動作テストの読み上げです。",
                "title": "⚠️ 【気象警報 (テスト)】東京都地方に大雨警報・洪水警報が発表されました"
            }
        elif alert_type == "tsunami":
            test_event = {
                "type": "tsunami",
                "id": f"test_tsunami_{int(time.time())}",
                "time": "テスト発表",
                "is_test": True,
                "top_grade": "大津波警報",
                "headline": "これはテスト通知です。ただちに高台や避難ビルなど安全な場所へ避難してください。",
                "voice_text": "【大津波警報テスト】沿岸部に大津波警報が発表されました。ただちに高台や避難ビルなど安全な場所へ避難してください。これは動作テストの読み上げです。",
                "title": "🌊 【大津波警報 (テスト)】沿岸部に大津波警報が発表されました",
                "areas": ["太平洋沿岸", "オホーツク海沿岸", "日本海沿岸"]
            }
        else:
            test_event = {
                "type": "earthquake",
                "id": f"test_eq_{int(time.time())}",
                "time": "テスト速報",
                "max_scale": 40,
                "scale_name": "震度4",
                "hypocenter": "東京湾",
                "depth": "約30km",
                "magnitude": "M4.8",
                "is_test": True,
                "tsunami_desc": "この地震による津波の心配はありません。",
                "voice_text": "【地震速報テスト】東京湾を震源とする最大震度4の地震が発生しました。これは動作確認用のテスト読み上げです。津波の心配はありません。",
                "title": "🚨 【地震情報 (テスト)】東京湾で最大震度4 (M4.8)",
                "pref_summary": "東京都: 震度4, 神奈川県: 震度3, 千葉県: 震度3",
                "affected_prefs": ["東京都", "神奈川県", "千葉県"]
            }

        sent_count = await self.dispatch_alert(test_event, target_guild_id=guild_id)
        return sent_count

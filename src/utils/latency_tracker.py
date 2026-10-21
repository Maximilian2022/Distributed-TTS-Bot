"""
TTSBot - 発話〜再生レイテンシ計測・追跡モジュール (LatencyTracker)
Discord上でのメッセージ送信から、Bot受信、キュー待機、TTS推論、FFmpeg準備、音声送出(vc.play)までの
各フェーズの所要時間（ミリ秒）を正確に記録・集計します。
"""

import json
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)

# --- 状態悪化インシデント専用ロガー (logs/latency_incidents.log, JSONL, 10MB x 5世代) ---
_incident_logger = logging.getLogger("ttsbot.latency_incidents")
_incident_logger.propagate = False  # bot.log を汚さない
if not _incident_logger.handlers:
    try:
        _log_dir = Path(__file__).resolve().parents[2] / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _h = RotatingFileHandler(_log_dir / "latency_incidents.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
        _h.setFormatter(logging.Formatter("%(message)s"))
        _incident_logger.addHandler(_h)
        _incident_logger.setLevel(logging.INFO)
    except Exception as _e:  # ログ出力不可でも本体動作は継続
        logger.warning(f"latency_incidents.log handler init failed: {_e}")

# インシデント判定閾値
INCIDENT_TURN_TTFS_MS = 1500.0      # Type C: 初動ラグ
INCIDENT_PREROLL_MS = 1900.0        # Type B: プリロール上限(2.0s)到達
INCIDENT_RTF = 1.2                  # Type D: 推論時間/音声尺
INCIDENT_QUEUE_WAIT_MS = 3000.0     # Type F: キュー滞留
INCIDENT_DISCORD_LAG_MS = 1000.0    # 参考タグ: Discord Gateway 配信遅延
CLOUD_PROVIDERS = {"edge", "azure", "google", "openjtalk"}
COLD_LOAD_SYNTH_MS = 3000.0         # COEIROINK でこれを超える単一チャンク推論はモデルロード待ちを疑う

# --- 合成カテゴリ分類 (動的 Adaptive Preroll 用) ---
CAT_CLOUD = "CLOUD"                  # Google, Edge, Azure
CAT_GPU_FAST = "LOCAL_GPU_FAST"      # VOICEVOX / SHAREVOX on RTX 3090 / GTX 1080 Ti
CAT_CPU = "LOCAL_CPU"                # VOICEVOX / SHAREVOX / OpenJTalk on CPU
CAT_COEIROINK_WARM = "COEIROINK_WARM" # RAMDisk/VRAM常駐・ロード済み
CAT_COEIROINK_COLD = "COEIROINK_COLD" # 未ロード初回 (コールドロード)

# カテゴリ別事前基準値 (Prior) (秒)
CATEGORY_PRIORS: Dict[str, float] = {
    CAT_CLOUD: 0.35,
    CAT_GPU_FAST: 0.15,
    CAT_CPU: 0.50,
    CAT_COEIROINK_WARM: 0.30,
    CAT_COEIROINK_COLD: 2.50,
}


def get_synthesis_category(
    provider: str,
    worker_label: str = "",
    is_model_loaded: Optional[bool] = None
) -> str:
    """プロバイダー、Workerラベル、モデルロード状態から推論カテゴリを判定"""
    prov = (provider or "").lower()
    if prov in ("edge", "google", "azure"):
        return CAT_CLOUD
    if prov == "openjtalk":
        return CAT_CPU

    if prov == "coeiroink":
        if is_model_loaded is False:
            return CAT_COEIROINK_COLD
        elif is_model_loaded is True:
            return CAT_COEIROINK_WARM
        return CAT_COEIROINK_WARM

    # VOICEVOX / SHAREVOX 等
    w_clean = (worker_label or "").lower()
    if any(k in w_clean for k in ("3090", "1080", "2022", "2017", "gpu")):
        return CAT_GPU_FAST
    elif any(k in w_clean for k in ("cpu", "ubuntu", "debian", "master", "7700k")):
        return CAT_CPU

    return CAT_CPU


@dataclass
class ChunkLatencyInfo:
    chunk_index: int             # 0: 名前, 1: チャンク1, ...
    text: str                    # パーツテキスト抜粋
    char_count: int              # 文字数
    role: str                    # "name" (発言者名) | "body" (本文)
    provider: str                # voicevox, coeiroink, etc.
    worker_label: str            # MASTERSOMEN2022, local_master, etc.
    synthesis_ms: float          # 生成時間 (ms)
    duration_ms: float           # 再生時間 (ms)
    deadline_ms: float = 0.0     # 再生目標時刻 (前のパーツ再生終了時刻)
    ready_offset_ms: float = 0.0 # 生成完了時刻 (再生開始からのオフセット)
    buffer_headroom_ms: float = 0.0 # 余裕度 (+ms: 先行完了, -ms: 途切れ)
    buffer_remaining_ms: float = 0.0 # feed時点のバッファ残量 (+: 余裕, -: 途切れ)
    synth_ms: float = 0.0        # synthesis_msのエイリアス
    t_synth_start: float = 0.0   # 合成開始時刻
    t_synth_end: float = 0.0     # 合成完了時刻
    is_cached: bool = False      # キャッシュヒットか
    is_fallback: bool = False    # フォールバックか


@dataclass
class PlaybackLatencyRecord:
    timestamp: str               # 発生日時 (HH:MM:SS)
    user_name: str               # 発言者名
    text_preview: str            # 発言テキスト抜粋 (最大25文字)
    char_count: int              # 文字数
    speaker_name: str            # 話者名
    provider: str                # 音声プロバイダー (voicevox, coeiroink, etc.)
    worker_label: str            # 処理ノード名 (例: OMEN2022 (RTX 3090) / VOICEVOX)
    discord_latency_ms: float    # Discord Gateway配信遅延 (Bot受信 - Discord送信)
    queue_wait_ms: float         # 再生キュー待機時間 (取り出し - キュー投入)
    synthesis_ms: float          # TTS音声合成推論時間 (取得完了 - リクエスト開始)
    play_prep_ms: float          # 再生準備時間 (vc.play - 取得完了)
    total_latency_ms: float      # 総体感ラグ (vc.play - Discord送信)
    system_latency_ms: float     # システム総処理時間 (vc.play - Bot受信)
    is_cached: bool              # キャッシュヒットしたか
    is_fallback: bool            # フォールバック発生したか
    status_level: str            # "ultra" (<250ms), "good" (<600ms), "fair" (<1500ms), "slow" (>=1500ms)
    ttfs_ms: float = 0.0         # 初動ラグ (互換用、turn_ttfs_msと同値)
    turn_ttfs_ms: float = 0.0    # 発話権獲得ラグ (発話権獲得〜初音再生: 目標<250ms)
    e2e_ttfs_ms: float = 0.0     # 投稿起点初動ラグ (Discord送信/Bot受信〜初音再生)
    underrun_ms: float = 0.0     # 音飛び・アンダーラン実時間 (0msなら完全シームレス)
    rtf: float = 0.0             # Real-Time Factor (推論時間 / 再生時間, <1.0でリアルタイム)
    split_ms: float = 0.0        # 形態素解析・文章分割所要時間 (ms)
    worker_machine: str = ""     # 整形済みマシン名 (例: OMEN2022 (RTX 3090))
    worker_engine: str = ""      # 整形済みエンジン名 (例: VOICEVOX)
    buffer_headroom_ms: float = 0.0 # 最小バッファ余裕度 (+: シームレス余裕, -: ギャップ遅延)
    gap_lag_ms: float = 0.0      # 音途切れ時間 (0msなら完全シームレス)
    masking_ratio: float = 100.0 # 推論隠蔽率 (%)
    preroll_wait_ms: float = 0.0 # Adaptive Preroll待機時間 (ms)
    core_ttfs_ms: float = 0.0   # 推論起因ラグ (Preroll除外, dequeue→第1チャンク合成完了)
    chunks: List[Dict[str, Any]] = None  # パーツ別詳細リスト

    def __post_init__(self):
        if self.chunks is None:
            self.chunks = []
        if self.turn_ttfs_ms == 0.0 and self.ttfs_ms != 0.0:
            self.turn_ttfs_ms = self.ttfs_ms
        if self.ttfs_ms == 0.0:
            self.ttfs_ms = self.turn_ttfs_ms if self.turn_ttfs_ms > 0 else self.system_latency_ms
        if self.e2e_ttfs_ms == 0.0:
            self.e2e_ttfs_ms = self.total_latency_ms
        if self.core_ttfs_ms == 0.0 and self.turn_ttfs_ms > 0.0:
            self.core_ttfs_ms = max(0.0, round(self.turn_ttfs_ms - self.preroll_wait_ms, 1))


from src.utils.async_task import safe_create_task


def parse_smart_worker_label(raw_label: str, provider: str = "") -> tuple[str, str]:
    """生URLやノード名から (スマートマシン名, スマートエンジン名) を抽出"""
    if not raw_label:
        eng = (provider or "VOICEVOX").upper()
        return ("Master (i7-7700K)", eng)

    raw_clean = raw_label.strip()

    # パターン1: 既に "Machine / Engine" 形式の場合
    if " / " in raw_clean:
        parts = raw_clean.split(" / ", 1)
        return (parts[0].strip(), parts[1].strip().upper())

    # マシン名の推定
    machine = "MasterDebian (i7-7700K)"
    if "2022" in raw_clean or "192.168.11.200" in raw_clean:
        machine = "OMEN2022 (RTX 3090)"
    elif "2017" in raw_clean or "192.168.11.207" in raw_clean or "192.168.11.201" in raw_clean:
        machine = "OMEN2017 (GTX 1080 Ti)"
    elif "ubuntu" in raw_clean.lower() or "192.168.11.245" in raw_clean:
        machine = "MasterUbuntu (8 Core)"
    elif "debian" in raw_clean.lower() or "192.168.11.248" in raw_clean:
        machine = "MasterDebian (i7-7700K)"
    elif "local" in raw_clean.lower() or "127.0.0.1" in raw_clean:
        machine = "Master (CPU)"

    # エンジン名の推定
    engine = (provider or "").upper()
    if ":5002" in raw_clean or "voicevox" in raw_clean.lower():
        engine = "VOICEVOX"
    elif ":5003" in raw_clean or "coeiroink" in raw_clean.lower():
        engine = "COEIROINK"
    elif ":10101" in raw_clean or "aivis" in raw_clean.lower():
        engine = "AIVISSPEECH"
    elif ":8000" in raw_clean or "sharevox" in raw_clean.lower():
        engine = "SHAREVOX"
    elif not engine:
        engine = "VOICEVOX"

    return (machine, engine)


class LatencyTracker:
    IDLE_SESSION_GAP_SECONDS = 600.0  # 10分以上の無発話で新セッション判定

    def __init__(self, max_history: int = 60):
        self._history: deque[PlaybackLatencyRecord] = deque(maxlen=max_history)
        self._worker_stats: Dict[str, Dict[str, Any]] = {}
        self._db: Optional[Any] = None
        self._last_record_ts: float = 0.0
        self._session_id: int = 1
        self._session_record_count: int = 0
        self._incidents: deque[Dict[str, Any]] = deque(maxlen=50)
        self._node_snapshot_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None

        # 動的 Adaptive Preroll 用: カテゴリ別独立推論実績バッファ (各カテゴリ直近30件)
        # 頻繁に使われるエンジン (Google等) による他エンジン (COEIROINK/GPU等) のサンプル押し出し・飢餓を完全防止
        self._category_samples: Dict[str, deque[float]] = {
            CAT_CLOUD: deque(maxlen=30),
            CAT_GPU_FAST: deque(maxlen=30),
            CAT_CPU: deque(maxlen=30),
            CAT_COEIROINK_WARM: deque(maxlen=30),
            CAT_COEIROINK_COLD: deque(maxlen=30),
        }

    def _add_chunk_sample(self, ch: Dict[str, Any]) -> None:
        """非キャッシュチャンクの推論所要時間を該当カテゴリの独立バッファへ蓄積"""
        if ch.get("is_cached", False):
            return
        synth_ms = float(ch.get("synth_ms") or ch.get("synthesis_ms") or 0.0)
        if synth_ms <= 0:
            return
        prov = str(ch.get("provider", "")).lower()
        w = str(ch.get("worker", "") or ch.get("worker_label", "")).lower()

        is_loaded = None
        if prov == "coeiroink":
            is_loaded = (synth_ms < 1500.0)
        cat = get_synthesis_category(prov, w, is_model_loaded=is_loaded)
        if cat in self._category_samples:
            self._category_samples[cat].append(synth_ms / 1000.0)

    def set_node_snapshot_provider(self, fn: Callable[[], List[Dict[str, Any]]]) -> None:
        """インシデント発生時にワーカーノード状態を取得するコールバックを登録"""
        self._node_snapshot_provider = fn

    def get_recent_incidents(self) -> List[Dict[str, Any]]:
        return list(reversed(self._incidents))


    @staticmethod
    def _analyze_incident(record: "PlaybackLatencyRecord", ctx: Dict[str, Any]) -> tuple[List[str], List[str]]:
        """状態悪化の種別 (incident_types) と根本原因タグ (root_causes) を推定"""
        types: List[str] = []
        causes: List[str] = []
        chunks = record.chunks or []
        if record.underrun_ms > 0 or record.buffer_headroom_ms < 0:
            types.append("A_UNDERRUN")
            # バッファが最初に負になった(=間に合わなかった)チャンクを特定
            for c in chunks:
                if float(c.get("buffer_remaining_ms", 0.0)) < 0:
                    causes.append(f"UNDERRUN_CHUNK_{c.get('chunk_index')}")
                    break
        if record.preroll_wait_ms >= INCIDENT_PREROLL_MS:
            types.append("B_PREROLL_TIMEOUT")
        if not record.is_cached and record.turn_ttfs_ms >= INCIDENT_TURN_TTFS_MS:
            types.append("C_SLOW_FIRST_AUDIO")
        if not record.is_cached and record.rtf > INCIDENT_RTF:
            types.append("D_RTF_OVER")
        expected = str(ctx.get("expected_provider") or "").lower()
        fallback_chunks = [
            c for c in chunks
            if not c.get("is_cached") and expected and expected not in CLOUD_PROVIDERS
            and str(c.get("provider", "")).lower() in CLOUD_PROVIDERS
        ]
        if record.is_fallback or fallback_chunks:
            types.append("E_FALLBACK")
            causes.append("SPECULATIVE_FALLBACK")
        if record.queue_wait_ms >= INCIDENT_QUEUE_WAIT_MS:
            types.append("F_QUEUE_WAIT")
            causes.append("QUEUE_CONGESTION")
        if not types:
            return types, causes
        # チャンク単位のボトルネック推定
        for c in chunks:
            if c.get("is_cached"):
                continue
            synth = float(c.get("synth_ms", c.get("synthesis_ms", 0.0)) or 0.0)
            dur = float(c.get("duration_ms", 0.0) or 0.0)
            idx = c.get("chunk_index")
            if str(c.get("provider", "")).lower() == "coeiroink" and synth >= COLD_LOAD_SYNTH_MS:
                causes.append(f"COEIROINK_COLD_LOAD_CHUNK_{idx}")
            elif dur > 0 and synth > max(1500.0, dur * 2.0):
                causes.append(f"WORKER_SLOW_CHUNK_{idx}")
        if record.discord_latency_ms >= INCIDENT_DISCORD_LAG_MS:
            causes.append("DISCORD_GATEWAY_DELAY")
        if not causes:
            causes.append("UNCLASSIFIED")
        # 重複除去 (順序維持)
        return types, list(dict.fromkeys(causes))

    def _record_incident(self, record: "PlaybackLatencyRecord", ctx: Dict[str, Any]) -> None:
        try:
            types, causes = self._analyze_incident(record, ctx)
            if not types:
                return
            nodes: List[Dict[str, Any]] = []
            if self._node_snapshot_provider:
                try:
                    nodes = self._node_snapshot_provider() or []
                except Exception as e:
                    nodes = [{"error": str(e)}]
            incident = {
                "occurred_at": datetime.now().isoformat(timespec="milliseconds"),
                "incident_types": types,
                "root_causes": causes,
                "guild_id": ctx.get("guild_id"),
                "guild_name": ctx.get("guild_name"),
                "speaker_id": ctx.get("speaker_id"),
                "expected_provider": ctx.get("expected_provider"),
                "full_text": ctx.get("full_text"),
                "record": asdict(record),
                "nodes_snapshot": nodes,
            }
            self._incidents.append(incident)
            _incident_logger.info(json.dumps(incident, ensure_ascii=False, default=str))
            logger.warning(
                f"🚨 [LatencyIncident] {','.join(types)} | causes={','.join(causes)} | "
                f"Turn-TTFS={record.turn_ttfs_ms:.0f}ms Underrun={record.underrun_ms:.0f}ms "
                f"Headroom={record.buffer_headroom_ms:.0f}ms | user='{record.user_name}'"
            )
            if self._db and hasattr(self._db, "save_latency_incident"):
                safe_create_task(
                    self._db.save_latency_incident(incident),
                    name=f"db_save_incident_{int(time.time()*1000)}"
                )
        except Exception as e:
            logger.debug(f"Failed to record latency incident: {e}")

    def reset(self, clear_history: bool = False) -> Dict[str, Any]:
        """レイテンシ集計を手動リセット (新セッション開始)"""
        self._worker_stats.clear()
        for q in self._category_samples.values():
            q.clear()
        self._session_id += 1
        self._session_record_count = 0
        self._last_record_ts = 0.0
        if clear_history:
            self._history.clear()
        logger.info(f"🔄 [LatencyTracker] レイテンシ集計を手動リセットしました (session_id={self._session_id}, clear_history={clear_history})。")
        return {"success": True, "message": "レイテンシ集計をリセットしました。", "session_id": self._session_id}

    def set_db(self, db: Any) -> None:
        """データベースインスタンスを設定 (永続化用)"""
        self._db = db

    def restore_from_history(self, records: List[Dict[str, Any]]) -> int:
        """DBから復元した過去レコードをメモリにロード (Bot再起動時の復元)"""
        restored = 0
        for r_dict in records:
            try:
                # 未知フィールドや欠落フィールドがあっても安全にロード
                valid_keys = set(PlaybackLatencyRecord.__annotations__.keys())
                filtered = {k: v for k, v in r_dict.items() if k in valid_keys}
                rec = PlaybackLatencyRecord(**filtered)
                # マシン名・エンジン名が空ならスマートパースして補完
                if not rec.worker_machine or not rec.worker_engine:
                    m, e = parse_smart_worker_label(rec.worker_label, rec.provider)
                    rec.worker_machine = m
                    rec.worker_engine = e
                    rec.worker_label = f"{m} / {e}"
                self._history.append(rec)

                # カテゴリ別独立サンプルバッファへの蓄積
                for ch in (rec.chunks or []):
                    self._add_chunk_sample(ch)

                w_lbl = rec.worker_label or "unknown"
                if w_lbl not in self._worker_stats:
                    self._worker_stats[w_lbl] = {"count": 0, "total_synth_ms": 0.0, "avg_synth_ms": 0.0, "avg_synthesis_ms": 0.0}
                w_stat = self._worker_stats[w_lbl]
                w_stat["count"] += 1
                w_stat["total_synth_ms"] += rec.synthesis_ms
                avg_s = round(w_stat["total_synth_ms"] / w_stat["count"], 1)
                w_stat["avg_synth_ms"] = avg_s
                w_stat["avg_synthesis_ms"] = avg_s
                restored += 1
            except Exception as e:
                logger.debug(f"Failed to restore latency record: {e}")

        if restored > 0:
            logger.info(f"✨ Restored {restored} playback latency history records from database.")
        return restored

    def record(
        self,
        user_name: str,
        text: str,
        speaker_name: str,
        provider: str,
        worker_label: str,
        t_discord_created: float,
        t_received: float,
        t_queued: float,
        t_dequeued: float,
        t_synth_start: float,
        t_synth_end: float,
        t_play_start: float,
        is_cached: bool = False,
        is_fallback: bool = False,
        chunks: Optional[List[Dict[str, Any]]] = None,
        ttfs_ms: Optional[float] = None,
        turn_ttfs_ms: Optional[float] = None,
        e2e_ttfs_ms: Optional[float] = None,
        underrun_ms: Optional[float] = 0.0,
        split_ms: Optional[float] = 0.0,
        buffer_headroom_ms: Optional[float] = None,
        gap_lag_ms: Optional[float] = None,
        masking_ratio: Optional[float] = None,
        total_audio_duration_ms: Optional[float] = 0.0,
        preroll_wait_ms: Optional[float] = 0.0,
        core_ttfs_ms: Optional[float] = None,
        incident_context: Optional[Dict[str, Any]] = None,
    ) -> PlaybackLatencyRecord:
        """1回の音声再生に関する各区間のレイテンシを記録"""
        now_dt = datetime.now()
        timestamp_str = now_dt.strftime("%H:%M:%S")
        now_mono = time.time()

        # 10分以上アイドルだった場合はセッションリセット（古い発話を引きずらないように自動フレッシュ化）
        if self._last_record_ts > 0 and (now_mono - self._last_record_ts) > self.IDLE_SESSION_GAP_SECONDS:
            idle_min = int((now_mono - self._last_record_ts) / 60)
            logger.info(f"🔄 [LatencyTracker] 前回の発話から {idle_min}分 経過したため、レイテンシ集計を新セッションとして自動リセットします。")
            self._worker_stats.clear()
            self._session_id += 1
            self._session_record_count = 0

        self._last_record_ts = now_mono
        self._session_record_count += 1

        # スマートマシン名 / エンジン名のパース
        machine_name, engine_name = parse_smart_worker_label(worker_label, provider)
        smart_worker_label = f"{machine_name} / {engine_name}"

        # 各区間の計算 (負数にならないようガード)
        discord_lat = max(0.0, (t_received - t_discord_created) * 1000.0) if t_discord_created > 0 else 0.0
        if discord_lat > 60000.0 or discord_lat < 0.0:
            discord_lat = 0.0

        queue_wait = max(0.0, (t_dequeued - t_queued) * 1000.0)
        # チャンク情報がある場合は全チャンク合算推論時間、なければ従来のt_synth区間
        if chunks:
            synthesis = sum(c.get("synth_ms", c.get("synthesis_ms", 0.0)) for c in chunks)
        else:
            synthesis = max(0.0, (t_synth_end - t_synth_start) * 1000.0)

        # システム総処理時間 (Bot受信〜再生開始)
        system_total = max(0.0, (t_play_start - t_received) * 1000.0)
        total_lat = (discord_lat + system_total) if discord_lat > 0 else system_total

        # 発話権獲得ラグ (turn_ttfs: 待ち行列から取り出されてから初音再生開始まで)
        # 前の人が喋っていたキュー待ちは除外された純粋なBotレスポンスラグ (目標 < 250ms)
        calc_turn_ttfs = round(turn_ttfs_ms if turn_ttfs_ms is not None else max(0.0, (t_play_start - t_dequeued) * 1000.0), 1)

        # 投稿起点初動ラグ (e2e_ttfs: Discord送信/Bot受信から初音再生開始まで)
        calc_e2e_ttfs = round(e2e_ttfs_ms if e2e_ttfs_ms is not None else system_total, 1)

        # Adaptive Preroll待機時間 & 推論起因ラグ (core_ttfs: Preroll除外の純粋な初動ラグ)
        calc_preroll = round(preroll_wait_ms or 0.0, 1)
        calc_core_ttfs = round(core_ttfs_ms if core_ttfs_ms is not None else max(0.0, calc_turn_ttfs - calc_preroll), 1)

        # 再生準備時間 (vc.play - 第1チャンク合成完了 - Preroll待機)
        # Preroll待機時間を二重計上しないよう純粋な再生準備ラグのみを算出
        raw_play_prep = (t_play_start - t_synth_end) * 1000.0
        play_prep = max(0.0, raw_play_prep - calc_preroll)

        # 互換用 ttfs_ms: 純粋なレスポンスを見るため calc_turn_ttfs を基準とする
        calc_ttfs = calc_turn_ttfs if ttfs_ms is None else round(ttfs_ms, 1)

        # RTF (Real-Time Factor: 推論時間 / 再生時間) の算出
        calc_rtf = 0.0
        tot_duration = total_audio_duration_ms or 0.0
        if chunks:
            c_synth = sum(c.get("synthesis_ms", c.get("synth_ms", 0.0)) for c in chunks)
            c_dur = sum(c.get("duration_ms", 0.0) for c in chunks)
            if c_dur > 0:
                calc_rtf = round(c_synth / c_dur, 2)
            elif tot_duration > 0:
                calc_rtf = round(c_synth / tot_duration, 2)
        elif tot_duration > 0:
            calc_rtf = round(synthesis / tot_duration, 2)

        # 評価判定 (推論起因ラグおよび実質初動ラグを総合評価)
        # ストリーミング再生における意図的なPrerollバッファ蓄積も考慮
        if is_cached or (calc_core_ttfs < 250.0 and calc_turn_ttfs < 500.0):
            level = "ultra"
        elif calc_core_ttfs < 400.0 or calc_turn_ttfs < 600.0:
            level = "good"
        elif calc_turn_ttfs < 1500.0:
            level = "fair"
        else:
            level = "slow"

        preview = text[:25] + ("..." if len(text) > 25 else "")

        # パーツ情報からマージンや隠蔽率を自動計算 (未指定時)
        calc_headroom = buffer_headroom_ms if buffer_headroom_ms is not None else 0.0
        calc_gap = gap_lag_ms if gap_lag_ms is not None else 0.0
        calc_masking = masking_ratio if masking_ratio is not None else (100.0 if is_cached else (100.0 if calc_turn_ttfs < 100.0 else 0.0))

        if chunks and buffer_headroom_ms is None:
            # チャンク情報から算出
            hr_list = [
                c.get("buffer_remaining_ms", c.get("buffer_headroom_ms", 0.0))
                for c in chunks if "buffer_remaining_ms" in c or "buffer_headroom_ms" in c
            ]
            if hr_list:
                calc_headroom = round(min(hr_list), 1)
                calc_gap = round(abs(min(0.0, min(hr_list))), 1)

        record = PlaybackLatencyRecord(
            timestamp=timestamp_str,
            user_name=user_name,
            text_preview=preview,
            char_count=len(text),
            speaker_name=speaker_name,
            provider=provider,
            worker_label=smart_worker_label,
            discord_latency_ms=round(discord_lat, 1),
            queue_wait_ms=round(queue_wait, 1),
            synthesis_ms=round(synthesis, 1),
            play_prep_ms=round(play_prep, 1),
            total_latency_ms=round(total_lat, 1),
            system_latency_ms=round(system_total, 1),
            is_cached=is_cached,
            is_fallback=is_fallback,
            status_level=level,
            ttfs_ms=calc_ttfs,
            turn_ttfs_ms=calc_turn_ttfs,
            e2e_ttfs_ms=calc_e2e_ttfs,
            underrun_ms=round(underrun_ms or 0.0, 1),
            rtf=calc_rtf,
            split_ms=round(split_ms or 0.0, 1),
            worker_machine=machine_name,
            worker_engine=engine_name,
            buffer_headroom_ms=round(calc_headroom, 1),
            gap_lag_ms=round(calc_gap, 1),
            masking_ratio=round(calc_masking, 1),
            preroll_wait_ms=calc_preroll,
            core_ttfs_ms=calc_core_ttfs,
            chunks=chunks or []
        )

        self._history.append(record)

        # 動的 Adaptive Preroll 用: カテゴリ別独立サンプルバッファへの蓄積
        for ch in (chunks or []):
            self._add_chunk_sample(ch)

        # ワーカー別統計の更新
        if smart_worker_label not in self._worker_stats:
            self._worker_stats[smart_worker_label] = {"count": 0, "total_synth_ms": 0.0, "avg_synth_ms": 0.0, "avg_synthesis_ms": 0.0}
        w_stat = self._worker_stats[smart_worker_label]
        w_stat["count"] += 1
        w_stat["total_synth_ms"] += synthesis
        avg_s = round(w_stat["total_synth_ms"] / w_stat["count"], 1)
        w_stat["avg_synth_ms"] = avg_s
        w_stat["avg_synthesis_ms"] = avg_s

        logger.info(
            f"⚡ [Latency] Turn-TTFS: {calc_turn_ttfs:.0f}ms (Core: {calc_core_ttfs:.0f}ms + Preroll: {calc_preroll:.0f}ms | QueueWait: {queue_wait:.0f}ms | Total: {total_lat:.0f}ms) | "
            f"RTF: {calc_rtf:.2f}x | Underrun: {record.underrun_ms:.0f}ms | Headroom: {calc_headroom:.0f}ms | Node: [{smart_worker_label}] | "
            f"user='{user_name}' text='{preview}'"
        )

        # DBへの永続化 (再起動復元用)
        if self._db and hasattr(self._db, "save_playback_latency"):
            safe_create_task(
                self._db.save_playback_latency(asdict(record)),
                name=f"db_save_latency_{int(time.time()*1000)}"
            )

        # 状態悪化インシデントの判定・記録 (原因調査用)
        self._record_incident(record, incident_context or {})

        return record

    def get_summary(self) -> Dict[str, Any]:
        """WebUI向けの要約メトリクスと最新履歴を返却"""
        if not self._history:
            empty_summary = {
                "total_count": 0,
                "sample_count": 0,
                "cache_hits": 0,
                "cache_hit_rate_percent": 0.0,
                "avg_total_latency_ms": 0.0,
                "avg_synthesis_ms": 0.0,
                "avg_queue_wait_ms": 0.0,
                "avg_discord_latency_ms": 0.0,
                "avg_play_prep_ms": 0.0,
                "min_total_latency_ms": 0.0,
                "max_total_latency_ms": 0.0,
                "avg_ttfs_ms": 0.0,
                "avg_turn_ttfs_ms": 0.0,
                "avg_e2e_ttfs_ms": 0.0,
                "avg_rtf": 0.0,
                "avg_underrun_ms": 0.0,
                "underrun_free_rate_percent": 100.0,
                "seamless_rate_percent": 100.0,
                "avg_buffer_headroom_ms": 0.0,
                "avg_masking_ratio": 100.0,
            }
            return {
                "has_data": False,
                "summary": empty_summary,
                "workers": self._worker_stats,
                "history": [],
                "recent_avg_total_ms": 0.0,
                "recent_avg_synth_ms": 0.0,
                "recent_avg_queue_ms": 0.0,
                "recent_min_total_ms": 0.0,
                "recent_max_total_ms": 0.0,
                "cache_hit_rate": 0.0,
                "breakdown": {
                    "discord_ms": 0.0,
                    "queue_ms": 0.0,
                    "synth_ms": 0.0,
                    "prep_ms": 0.0
                },
                "worker_stats": self._worker_stats,
                "recent_events": []
            }

        # 直近20件をサンプリング
        sample = list(self._history)[-20:]
        totals = [r.total_latency_ms for r in sample]
        synths = [r.synthesis_ms for r in sample]
        queues = [r.queue_wait_ms for r in sample]
        discords = [r.discord_latency_ms for r in sample]
        preps = [r.play_prep_ms for r in sample]
        turn_ttfses = [getattr(r, "turn_ttfs_ms", getattr(r, "ttfs_ms", r.system_latency_ms)) for r in sample]
        e2e_ttfses = [getattr(r, "e2e_ttfs_ms", r.total_latency_ms) for r in sample]
        core_ttfses = [getattr(r, "core_ttfs_ms", 0.0) for r in sample]
        prerolls = [getattr(r, "preroll_wait_ms", 0.0) for r in sample]
        headrooms = [getattr(r, "buffer_headroom_ms", 0.0) for r in sample]
        gaps = [getattr(r, "gap_lag_ms", 0.0) for r in sample]
        maskings = [getattr(r, "masking_ratio", 100.0) for r in sample]
        rtfs = [getattr(r, "rtf", 0.0) for r in sample if getattr(r, "rtf", 0.0) > 0]
        underruns = [getattr(r, "underrun_ms", 0.0) for r in sample]

        cached_cnt = sum(1 for r in sample if r.is_cached)
        hit_rate = round((cached_cnt / len(sample)) * 100.0, 1) if sample else 0.0

        avg_total = round(sum(totals) / len(totals), 1)
        avg_synth = round(sum(synths) / len(synths), 1)
        avg_queue = round(sum(queues) / len(queues), 1)
        avg_discord = round(sum(discords) / len(discords), 1)
        avg_prep = round(sum(preps) / len(preps), 1)
        avg_turn_ttfs = round(sum(turn_ttfses) / len(turn_ttfses), 1) if turn_ttfses else 0.0
        avg_e2e_ttfs = round(sum(e2e_ttfses) / len(e2e_ttfses), 1) if e2e_ttfses else 0.0
        avg_core_ttfs = round(sum(core_ttfses) / len(core_ttfses), 1) if core_ttfses else 0.0
        avg_preroll = round(sum(prerolls) / len(prerolls), 1) if prerolls else 0.0
        avg_headroom = round(sum(headrooms) / len(headrooms), 1) if headrooms else 0.0
        avg_masking = round(sum(maskings) / len(maskings), 1) if maskings else 100.0
        avg_rtf = round(sum(rtfs) / len(rtfs), 2) if rtfs else 0.0
        avg_underrun = round(sum(underruns) / len(underruns), 1) if underruns else 0.0
        underrun_free_cnt = sum(1 for u in underruns if u <= 0.0)
        underrun_free_rate = round((underrun_free_cnt / len(underruns)) * 100.0, 1) if underruns else 100.0
        seamless_cnt = sum(1 for g in gaps if g <= 0.0)
        seamless_rate = round((seamless_cnt / len(gaps)) * 100.0, 1) if gaps else 100.0

        recent_events = [asdict(r) for r in reversed(list(self._history)[-15:])]

        summary_data = {
            "total_count": len(self._history),
            "sample_count": len(sample),
            "cache_hits": cached_cnt,
            "cache_hit_rate_percent": hit_rate,
            "avg_total_latency_ms": avg_total,
            "avg_synthesis_ms": avg_synth,
            "avg_queue_wait_ms": avg_queue,
            "avg_discord_latency_ms": avg_discord,
            "avg_play_prep_ms": avg_prep,
            "min_total_latency_ms": min(totals),
            "max_total_latency_ms": max(totals),
            "avg_ttfs_ms": avg_turn_ttfs,
            "avg_turn_ttfs_ms": avg_turn_ttfs,
            "avg_e2e_ttfs_ms": avg_e2e_ttfs,
            "avg_rtf": avg_rtf,
            "avg_underrun_ms": avg_underrun,
            "underrun_free_rate_percent": underrun_free_rate,
            "seamless_rate_percent": seamless_rate,
            "avg_buffer_headroom_ms": avg_headroom,
            "avg_masking_ratio": avg_masking,
            "avg_core_ttfs_ms": avg_core_ttfs,
            "avg_preroll_wait_ms": avg_preroll,
            "session_id": self._session_id,
            "session_record_count": self._session_record_count,
            "idle_seconds_ago": round(time.time() - self._last_record_ts, 1) if self._last_record_ts > 0 else 0.0,
            "idle_reset_minutes": int(self.IDLE_SESSION_GAP_SECONDS / 60),
        }

        return {
            "has_data": True,
            "summary": summary_data,
            "workers": self._worker_stats,
            "history": recent_events,
            # 互換用フラットキー
            "recent_avg_total_ms": avg_total,
            "recent_avg_synth_ms": avg_synth,
            "recent_avg_queue_ms": avg_queue,
            "recent_min_total_ms": min(totals),
            "recent_max_total_ms": max(totals),
            "recent_avg_ttfs_ms": avg_turn_ttfs,
            "recent_avg_turn_ttfs_ms": avg_turn_ttfs,
            "recent_avg_e2e_ttfs_ms": avg_e2e_ttfs,
            "recent_avg_core_ttfs_ms": avg_core_ttfs,
            "recent_avg_preroll_wait_ms": avg_preroll,
            "recent_avg_rtf": avg_rtf,
            "recent_seamless_rate": seamless_rate,
            "recent_avg_headroom_ms": avg_headroom,
            "cache_hit_rate": hit_rate,
            "breakdown": {
                "discord_ms": avg_discord,
                "queue_ms": avg_queue,
                "synth_ms": avg_synth,
                "prep_ms": avg_prep,
                "ttfs_ms": avg_turn_ttfs,
                "turn_ttfs_ms": avg_turn_ttfs,
                "e2e_ttfs_ms": avg_e2e_ttfs,
                "core_ttfs_ms": avg_core_ttfs,
                "preroll_wait_ms": avg_preroll,
                "headroom_ms": avg_headroom,
            },
            "worker_stats": self._worker_stats,
        }

    def get_estimated_synthesis_time(
        self,
        provider: str,
        char_count: int = 10,
        worker_label: str = "",
        is_model_loaded: Optional[bool] = None,
    ) -> float:
        """
        カテゴリ判定と直近推論履歴から、第1文の推定合成所要時間 (秒) を動的に算出する。
        - 物理状態 (コールドロード) が確定している場合は統計を見ずに長めの枠を即座に確保
        - サンプル不足時 (N < 5) は事前基準値 (Prior) とベイズ平滑化ブレンド + 悲観的マージンで安全側に倒す
        - 十分なサンプル (N >= 5) では実績 P90 を信頼採用
        - 文字数に応じたスケール時間を加算
        """
        cat = get_synthesis_category(provider, worker_label, is_model_loaded)
        prior_time = CATEGORY_PRIORS.get(cat, 0.40)

        # 物理的に未ロード初回確定の場合は、統計に関係なく確実に待機枠を確保 (アンダーラン完全防止)
        if cat == CAT_COEIROINK_COLD:
            est = max(prior_time, 2.50)
            if char_count > 15:
                est += (char_count - 15) * 0.020
            return max(2.0, min(est, 4.0))

        # カテゴリ別独立サンプルバッファ (各30件) から非キャッシュ推論実績 (秒) を取得
        samples: List[float] = list(self._category_samples.get(cat, []))

        n = len(samples)
        if n >= 5:
            # 十分な実績がある場合: P90 (遅いケースをカバーする90パーセンタイル)
            sorted_samples = sorted(samples)
            p90_idx = min(len(sorted_samples) - 1, int(len(sorted_samples) * 0.90))
            est = sorted_samples[p90_idx]
        elif n > 0:
            # サンプル数が少ない場合 (1〜4件): ベイズ的平滑化ブレンド + 悲観的マージン
            k = 4.0  # 事前分布の仮想サンプル重み
            sample_val = sorted(samples)[-1]  # サンプルの最大値 (悲観的)
            est = (k / (n + k)) * prior_time + (n / (n + k)) * sample_val + 0.10  # 100ms 安全マージン
        else:
            # 実績ゼロ件: 事前基準値を100%採用
            est = prior_time

        # 文字数が15文字を超える場合は文字数スケールを加算
        if char_count > 15:
            est += (char_count - 15) * 0.012

        # カテゴリに応じたクランプ
        if cat == CAT_GPU_FAST:
            return max(0.10, min(est, 1.20))
        elif cat == CAT_CLOUD:
            return max(0.20, min(est, 1.50))
        else:
            return max(0.20, min(est, 3.00))



# グローバルシングルトン
latency_tracker = LatencyTracker(max_history=60)


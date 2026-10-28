import os
import io
import time
import wave
import array
import shutil
import struct
import hashlib
import uuid
import asyncio
import logging
import collections
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any, Union
from src.utils.text_cleaner import TextCleaner
from src.utils.async_task import safe_create_task
from src.utils.security import sanitize_filename, validate_safe_path

logger = logging.getLogger(__name__)


def detect_effective_ram_bytes() -> int:
    """
    ホスト物理メモリ (/proc/meminfo) およびコンテナ cgroup メモリ上限の小さい方を取得 (bytes)。
    少メモリVPSやコンテナメモリ制限環境でのOOMを防止する。
    """
    host_total = 0
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    host_total = int(line.split()[1]) * 1024
                    break
    except Exception:
        pass

    cgroup_limit = 0
    for cg_path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        if os.path.exists(cg_path):
            try:
                raw = Path(cg_path).read_text(encoding="utf-8").strip()
                if raw and raw != "max" and raw.isdigit():
                    val = int(raw)
                    # 無制限を表す巨大値 (例: > 1PB) は除外
                    if 0 < val < (1 << 50):
                        cgroup_limit = val
                        break
            except Exception:
                pass

    if host_total > 0 and cgroup_limit > 0:
        return min(host_total, cgroup_limit)
    return cgroup_limit or host_total or (4 * 1024 * 1024 * 1024)


def calculate_effective_cache_limits(
    directory: Union[str, Path],
    configured_max_bytes: int,
    configured_target_bytes: int,
    current_cache_size_bytes: int = 0
) -> Tuple[int, int]:
    """
    対象ディレクトリのディスク空き容量 (shutil.disk_usage) に基づき、
    OSやDBを圧迫しない実効キャッシュ上限 (effective_max_bytes, effective_target_bytes) を動的算出。
    """
    target_path = Path(directory)
    check_path = target_path if target_path.exists() else target_path.parent
    try:
        usage = shutil.disk_usage(check_path)
        disk_total = usage.total
        disk_free = usage.free
        if disk_total <= 0:
            return configured_max_bytes, configured_target_bytes

        # 最低限残すべき空き容量安全マージン (通常: max(3GB, 総容量の10%)、小容量ディスクでは総容量の15%)
        if disk_total <= 20 * (1024 ** 3):
            min_free_reserve = max(512 * (1024 ** 2), int(disk_total * 0.15))
        else:
            min_free_reserve = max(3 * (1024 ** 3), int(disk_total * 0.10))

        # キャッシュが使用可能な実質上限 = 現在のキャッシュ使用量 + (現在の空き容量 - 安全マージン)
        max_usable_for_dir = max(64 * (1024 ** 2), current_cache_size_bytes + disk_free - min_free_reserve)
        effective_max = min(configured_max_bytes, max_usable_for_dir)
        ratio = (configured_target_bytes / float(configured_max_bytes)) if configured_max_bytes > 0 else 0.85
        ratio = max(0.5, min(0.95, ratio))
        effective_target = min(configured_target_bytes, int(effective_max * ratio))
        return effective_max, effective_target
    except Exception:
        return configured_max_bytes, configured_target_bytes

def _format_utc_to_jst(dt_raw: Any) -> str:
    """UTCタイムスタンプ文字列をJST (+9時間) に変換して 'YYYY-MM-DD HH:MM:SS' を返す"""
    if not dt_raw:
        return "-"
    s = str(dt_raw).strip()
    if not s:
        return "-"
    try:
        clean = s.replace("T", " ")[:19]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")
        jst_dt = dt + timedelta(hours=9)
        return jst_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s.replace("T", " ")[:19]


def get_wav_duration(audio_data: bytes) -> float:
    """WAVバイト列から再生秒数を計算 (24kHz, 16bit, mono なら 48000 bytes/sec)"""
    try:
        if len(audio_data) <= 44:
            return 0.0
        with wave.open(io.BytesIO(audio_data), 'rb') as w:
            frames = w.getnframes()
            rate = w.getframerate()
            return round(frames / float(rate), 2) if rate > 0 else 0.0
    except Exception:
        pcm_bytes = max(0, len(audio_data) - 44)
        return round(pcm_bytes / 48000.0, 2)

def fix_wav_header(audio_data: bytes) -> bytes:
    """
    FFmpeg等のストリーミング/パイプ出力によって WAV ヘッダーの RIFFサイズ / data チャンクサイズが
    0x7FFFFFFF (2147483647) などの未確定値になっている場合、実際のバイト数に基づいて正規のヘッダーに補正する。
    """
    if len(audio_data) < 44 or not audio_data.startswith(b"RIFF") or audio_data[8:12] != b"WAVE":
        return audio_data

    data_idx = audio_data.find(b"data")
    if data_idx != -1 and data_idx + 8 <= len(audio_data):
        actual_data_len = len(audio_data) - (data_idx + 8)
        actual_riff_len = len(audio_data) - 8
        b = bytearray(audio_data)
        struct.pack_into("<I", b, 4, actual_riff_len)
        struct.pack_into("<I", b, data_idx + 4, actual_data_len)
        return bytes(b)
    return audio_data

def validate_wav_bytes(audio_data: bytes) -> Tuple[bool, str]:
    """
    WAVバイナリの破損・無音チェックを行う。
    - 最小サイズチェック (44バイト以下は破損)
    - WAVフォーマット整合性チェック (PCM, 16bit, 24kHz, Mono)
    - 無音波形チェック (0.1秒以上のデータで振幅が極端に小さい場合は無音判定)
    """
    if not audio_data or len(audio_data) <= 44:
        return False, f"Audio too small ({len(audio_data) if audio_data else 0} bytes <= 44 bytes header)"

    try:
        audio_data = fix_wav_header(audio_data)
        with wave.open(io.BytesIO(audio_data), 'rb') as r:
            nchannels = r.getnchannels()
            sampwidth = r.getsampwidth()
            framerate = r.getframerate()
            nframes = r.getnframes()

            if nframes == 0:
                return False, "WAV contains 0 audio frames"

            # サンプル幅チェック (本Botは 16-bit PCM が標準)
            if sampwidth != 2:
                return False, f"Unexpected sample width: {sampwidth} (expected 2 for 16-bit)"

            # PCMフレームから振幅のピーク（最大絶対値）を検査
            # 検査負荷を抑えるため、先頭最大2秒（48,000フレーム）をサンプリング走査
            check_frames = min(nframes, 48000)
            raw_data = r.readframes(check_frames)
            samples = array.array('h')
            samples.frombytes(raw_data)

            if len(samples) >= 2400:
                # 1パス早期脱出: 振幅30以上のサンプルが現れた瞬間に即時OK判定 (O(1))
                # 無音時も2回目のmax走査を行わず、1パス内で最大振幅を追跡
                max_amp = 0
                has_sound = False
                for s in samples:
                    val = abs(s)
                    if val >= 30:
                        has_sound = True
                        break
                    if val > max_amp:
                        max_amp = val
                if not has_sound:
                    return False, f"Audio is completely silent (peak amplitude: {max_amp} < 30 in {len(samples)} samples)"

    except wave.Error as e:
        return False, f"Invalid WAV structure: {e}"
    except Exception as e:
        return False, f"Failed to parse WAV data: {e}"

    return True, "OK"

def normalize_pitch(pitch: str) -> str:
    """ピッチを5%刻みにスナップ（量子化）してキャッシュ共有率を向上"""
    try:
        val = int(pitch.replace("%", "").replace("+", "").strip())
        rounded = round(val / 5) * 5
        return f"+{rounded}%" if rounded > 0 else f"{rounded}%"
    except Exception:
        return pitch or "0%"

def normalize_speed(speed: float) -> float:
    """速度を0.05刻みにスナップ（量子化）してキャッシュ共有率を向上"""
    try:
        return round(round(float(speed) / 0.05) * 0.05, 2)
    except Exception:
        return 1.0

class CacheManager:
    def __init__(
        self,
        cache_dir: str,
        max_size_bytes: int = 10 * 1024 * 1024 * 1024,          # 10GB (Cloud)
        target_size_bytes: int = int(8.5 * 1024 * 1024 * 1024),   # 8.5GB
        voicevox_cache_dir: Optional[str] = None,
        voicevox_max_size_bytes: int = 20 * 1024 * 1024 * 1024,  # 20GB (VOICEVOX)
        voicevox_target_size_bytes: int = int(17 * 1024 * 1024 * 1024) # 17GB
    ):
        self.cache_dir = Path(cache_dir)
        self.max_size_bytes = max_size_bytes
        self.target_size_bytes = target_size_bytes

        self.voicevox_cache_dir = Path(voicevox_cache_dir) if voicevox_cache_dir else (self.cache_dir.parent / "cache_voicevox")
        self.voicevox_max_size_bytes = voicevox_max_size_bytes
        self.voicevox_target_size_bytes = voicevox_target_size_bytes

        self._lock = asyncio.Lock()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.voicevox_cache_dir.mkdir(parents=True, exist_ok=True)
        self.db: Any = None
        self._last_enforce_time: Dict[str, float] = {}
        self._save_counter: Dict[str, int] = {}

        # L1 インメモリ LRU キャッシュ (定型文・短文のゼロI/O高速化)
        # ホストRAMおよびcgroupメモリ制限に応じて [16MB, 128MB] の範囲で自動スケーリング
        # key: cache_key, value: (provider, audio_bytes, file_path)
        self._l1_cache: collections.OrderedDict = collections.OrderedDict()
        eff_ram = detect_effective_ram_bytes()
        self._l1_max_bytes: int = max(16 * 1024 * 1024, min(128 * 1024 * 1024, eff_ram // 32))
        self._l1_current_bytes: int = 0
        self._l1_max_single_file_bytes: int = 256 * 1024  # 256KB以下の音声のみオンメモリ保持

    def _put_l1(self, cache_key: str, provider: str, audio_bytes: bytes, file_path: Path) -> None:
        """L1 メモリキャッシュに格納 (256KB以下、総量100MB上限でLRU破棄)"""
        data_len = len(audio_bytes)
        if data_len > self._l1_max_single_file_bytes:
            return
        if cache_key in self._l1_cache:
            _, old_bytes, _ = self._l1_cache.pop(cache_key)
            self._l1_current_bytes -= len(old_bytes)
        while self._l1_current_bytes + data_len > self._l1_max_bytes and self._l1_cache:
            _, (_, evicted_bytes, _) = self._l1_cache.popitem(last=False)
            self._l1_current_bytes -= len(evicted_bytes)
        self._l1_cache[cache_key] = (provider, audio_bytes, file_path)
        self._l1_current_bytes += data_len

    def _get_l1(self, cache_key: str) -> Optional[Tuple[str, bytes, Path]]:
        """L1 メモリキャッシュから取得 (LRU更新)"""
        if cache_key in self._l1_cache:
            val = self._l1_cache.pop(cache_key)
            self._l1_cache[cache_key] = val
            return val
        return None

    def _invalidate_l1(self, cache_key: str) -> None:
        """L1 メモリキャッシュから削除"""
        if cache_key in self._l1_cache:
            _, evicted_bytes, _ = self._l1_cache.pop(cache_key)
            self._l1_current_bytes -= len(evicted_bytes)

    def set_database(self, database: Any) -> None:
        """Database インスタンスを注入してメタデータを自動同期"""
        self.db = database

    LOCAL_PROVIDERS = {"voicevox", "sharevox", "coeiroink", "openjtalk"}

    def get_cache_dir(self, provider: str = "azure") -> Path:
        """プロバイダーに応じたキャッシュディレクトリを返却 (ローカル系は20GBボリュームに集約)"""
        p = str(provider).lower()
        if p in self.LOCAL_PROVIDERS or p.startswith("vv_"):
            return self.voicevox_cache_dir
        return self.cache_dir

    @property
    def cloud_dir(self) -> Path:
        return self.cache_dir

    @property
    def local_dir(self) -> Path:
        return self.voicevox_cache_dir

    @property
    def l1_cache(self) -> collections.OrderedDict:
        return self._l1_cache

    @property
    def l1_max_items(self) -> int:
        return 100

    @property
    def l1_current_bytes(self) -> int:
        return self._l1_current_bytes

    def get_total_cache_size(self, path: Optional[Union[str, Path]] = None) -> int:
        """指定ディレクトリ（または全キャッシュ）の合計ファイルサイズ（バイト）を取得"""
        if path is not None:
            target_dir = Path(path)
            if not target_dir.exists():
                return 0
            total = 0
            for f in target_dir.iterdir():
                if f.is_file() and f.suffix == ".wav":
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
            return total
        else:
            return self.get_total_cache_size(self.cache_dir) + self.get_total_cache_size(self.voicevox_cache_dir)

    def get_file_path(self, cache_key: str, provider: str = "azure") -> Path:
        return self.get_cache_dir(provider) / cache_key

    @staticmethod
    def generate_cache_key(
        speaker_id: str,
        pitch: str,
        speed: float,
        text: str,
        user_id: Optional[int] = None
    ) -> str:
        """
        声質・量子化ピッチ・量子化速度・正規化テキストからSHA-256ハッシュファイル名を生成。
        同一ボイス設定であればユーザー間でキャッシュを全体共有し、ヒット率を最大化する。
        文末記号（！・ー・w等）のブレを吸収して同一キーに集約。
        """
        actual_speaker = str(speaker_id)
        actual_pitch = str(pitch)
        try:
            actual_speed = float(speed)
        except (ValueError, TypeError):
            actual_speed = 1.0
        actual_text = str(text)

        norm_pitch = normalize_pitch(actual_pitch)
        norm_speed = normalize_speed(actual_speed)
        norm_text = TextCleaner.normalize_cache_key_text(actual_text)
        raw = f"{actual_speaker}:{norm_pitch}:{norm_speed}:{norm_text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest() + ".wav"

    async def _get_disk_file(self, cache_key: str, provider: str = "azure") -> Optional[Path]:
        """ディスク上のキャッシュファイルを検索・検証し、Pathを返却 (L1チェックなし)"""
        def _find_and_stat() -> Tuple[Optional[Path], Optional[os.stat_result]]:
            p = self.get_file_path(cache_key, provider)
            try:
                return p, p.stat()
            except OSError:
                pass
            alt_dir = self.cache_dir if self.get_cache_dir(provider) == self.voicevox_cache_dir else self.voicevox_cache_dir
            alt_p = alt_dir / cache_key
            try:
                return alt_p, alt_p.stat()
            except OSError:
                return None, None

        file_path, stat = await asyncio.to_thread(_find_and_stat)
        if file_path is None or stat is None:
            return None

        try:
            if stat.st_size <= 44:
                logger.warning(f"Corrupted cache file detected during get ({stat.st_size} bytes): {file_path.name}. Auto-removing.")
                await asyncio.to_thread(file_path.unlink, True)
                self._invalidate_l1(cache_key)
                if self.db:
                    safe_create_task(self.db.delete_cache_metadata(cache_key), name="delete_cache_metadata_corrupted")
                return None

            await asyncio.to_thread(os.utime, file_path, None)
            logger.debug(f"Cache hit ({provider}): {cache_key} (mtime updated)")
            if self.db:
                safe_create_task(
                    self.db.record_cache_metadata_hit(cache_key) if hasattr(self.db, "record_cache_metadata_hit") else self.db.record_cache_hit(cache_key),
                    name="record_cache_hit"
                )
            return file_path
        except OSError as e:
            logger.warning(f"Failed to access cache for {cache_key}: {e}")
            return None

    async def get(self, cache_key: str, provider: str = "azure") -> Optional[Path]:
        """
        キャッシュが存在するか確認。
        システムコールを単一の stat() に集約し、スレッドプールへのディスパッチ負荷を最小化。
        L1 キャッシュに存在する場合はディスクI/Oを行わずに即時パスを返却。
        """
        l1_entry = self._get_l1(cache_key)
        if l1_entry is not None:
            _, _, file_path = l1_entry
            safe_create_task(asyncio.to_thread(os.utime, file_path, None), name="utime_l1")
            if self.db:
                safe_create_task(
                    self.db.record_cache_metadata_hit(cache_key) if hasattr(self.db, "record_cache_metadata_hit") else self.db.record_cache_hit(cache_key),
                    name="record_cache_hit"
                )
            return file_path

        return await self._get_disk_file(cache_key, provider)

    async def get_bytes(self, cache_key: str, provider: str = "azure") -> Optional[bytes]:
        """キャッシュが存在すればバイト列を高速返却（L1メモリキャッシュ優先、L1ミス時は直接ディスク探索）"""
        l1_entry = self._get_l1(cache_key)
        if l1_entry is not None:
            _, audio_bytes, file_path = l1_entry
            safe_create_task(asyncio.to_thread(os.utime, file_path, None), name="utime_l1_bytes")
            if self.db:
                safe_create_task(
                    self.db.record_cache_metadata_hit(cache_key) if hasattr(self.db, "record_cache_metadata_hit") else self.db.record_cache_hit(cache_key),
                    name="record_cache_hit"
                )
            return audio_bytes

        # L1 ミス時: get() を呼ぶと再度 L1 をチェックしてしまうため、直接 _get_disk_file() を呼び出し (A-3)
        file_path = await self._get_disk_file(cache_key, provider)
        if file_path is not None:
            try:
                content = await asyncio.to_thread(file_path.read_bytes)
                is_valid, reason = validate_wav_bytes(content)
                if not is_valid:
                    logger.warning(f"Invalid cached audio detected for {cache_key}: {reason}. Auto-removing.")
                    await asyncio.to_thread(file_path.unlink, True)
                    self._invalidate_l1(cache_key)
                    if self.db:
                        safe_create_task(self.db.delete_cache_metadata(cache_key), name="delete_cache_metadata_invalid")
                    return None
                # L1 に登録
                self._put_l1(cache_key, provider, content, file_path)
                return content
            except OSError as e:
                logger.warning(f"Failed to read cache bytes for {cache_key}: {e}")
                return None
        return None

    async def save(
        self,
        cache_key: str,
        audio_data: bytes,
        provider: str = "azure",
        text: Optional[str] = None,
        speaker_id: Optional[str] = None,
        speaker_name: Optional[str] = None,
        is_chunk: bool = False
    ) -> Path:
        """
        音声バイナリの破損・無音を保存前にバリデーションし、問題なければキャッシュに保存。
        メタデータ（発言内容、話者名、パーツ種別、再生時間）も自動記録。
        """
        # パイプ出力等による未確定ヘッダーサイズを実サイズで正規化
        audio_data = fix_wav_header(audio_data)

        # 保存前バリデーション (In-flight validation)
        is_valid, reason = validate_wav_bytes(audio_data)
        if not is_valid:
            logger.error(f"Refusing to save corrupted or silent audio ({provider}): {cache_key} - {reason}")
            raise ValueError(f"Corrupted or silent audio data: {reason}")

        file_path = self.get_file_path(cache_key, provider)
        
        def _write():
            if file_path.exists():
                try:
                    os.utime(file_path, None)
                except OSError:
                    pass
                return

            # 並行保存時の競合（Race Condition）を防ぐため、UUIDを付与した一意の一時ファイル名を使用
            temp_path = file_path.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")
            try:
                with open(temp_path, "wb") as f:
                    f.write(audio_data)
                temp_path.replace(file_path)
                os.utime(file_path, None)
            except FileNotFoundError:
                # 既に別タスクが先に保存を完了していた場合は正常として無視
                pass
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

        await asyncio.to_thread(_write)
        self._put_l1(cache_key, provider, audio_data, file_path)
        logger.info(f"Saved new cache ({provider}): {cache_key} ({len(audio_data)} bytes)")

        # メタデータをDBに非同期記録 (PERF-3: 音声返却をブロックせず即応)
        if self.db and text:
            duration = get_wav_duration(audio_data)
            async def _bg_record():
                try:
                    await self.db.record_cache_saved(
                        cache_key=cache_key,
                        provider=provider,
                        text=text,
                        speaker_id=speaker_id,
                        speaker_name=speaker_name,
                        is_chunk=is_chunk,
                        duration=duration,
                        size_bytes=len(audio_data)
                    )
                except Exception as e:
                    logger.warning(f"Failed to record cache metadata for {cache_key}: {e}")
            safe_create_task(_bg_record(), name=f"record_cache_meta_{cache_key[:8]}")

        # 容量チェックとLRUクリーンアップ (デバウンス制御: 最低間隔60秒 または 25回保存ごと)
        now = time.time()
        cat = "local" if (str(provider).lower() in self.LOCAL_PROVIDERS or str(provider).lower().startswith("vv_")) else "cloud"
        self._save_counter[cat] = self._save_counter.get(cat, 0) + 1
        last_t = self._last_enforce_time.get(cat, 0.0)
        if (now - last_t >= 60.0) or (self._save_counter[cat] >= 25):
            self._last_enforce_time[cat] = now
            self._save_counter[cat] = 0
            safe_create_task(self.enforce_capacity_limit(provider), name="enforce_capacity_limit")
        return file_path

    async def enforce_capacity_limit(self, provider: Optional[str] = None) -> None:
        """
        キャッシュディレクトリの総容量をチェックし、超過時は st_mtime が古い順に削除。
        """
        async with self._lock:
            p = str(provider).lower() if provider else ""
            all_deleted_keys: List[str] = []
            if p in self.LOCAL_PROVIDERS or p.startswith("vv_"):
                res = await asyncio.to_thread(self._cleanup_dir, self.voicevox_cache_dir, self.voicevox_max_size_bytes, self.voicevox_target_size_bytes, "VOICEVOX")
                if res:
                    all_deleted_keys.extend(res)
            elif p in ("azure", "google", "edge"):
                res = await asyncio.to_thread(self._cleanup_dir, self.cache_dir, self.max_size_bytes, self.target_size_bytes, "Cloud")
                if res:
                    all_deleted_keys.extend(res)
            else:
                # 全ディレクトリをクリーンアップ
                res1 = await asyncio.to_thread(self._cleanup_dir, self.cache_dir, self.max_size_bytes, self.target_size_bytes, "Cloud")
                res2 = await asyncio.to_thread(self._cleanup_dir, self.voicevox_cache_dir, self.voicevox_max_size_bytes, self.voicevox_target_size_bytes, "VOICEVOX")
                if res1:
                    all_deleted_keys.extend(res1)
                if res2:
                    all_deleted_keys.extend(res2)

            # DBメタデータ側の孤児レコードも非同期メインループから安全に一括削除 (B-4)
            if self.db and all_deleted_keys:
                safe_create_task(self.db.delete_cache_metadata_batch(all_deleted_keys), name="delete_cache_metadata_lru")

    def _cleanup_dir(self, directory: Path, max_bytes: int, target_bytes: int, label: str) -> List[str]:
        deleted_keys: List[str] = []
        try:
            if not directory.exists():
                return deleted_keys
            total_size = 0
            entries: List[Tuple[Path, float, int]] = []  # (Path, mtime, size)

            # os.scandir による高速ディレクトリスキャン (stat キャッシュ活用)
            with os.scandir(directory) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(".wav"):
                        try:
                            st = entry.stat()
                            total_size += st.st_size
                            entries.append((Path(entry.path), st.st_mtime, st.st_size))
                        except OSError:
                            continue

            eff_max_bytes, eff_target_bytes = calculate_effective_cache_limits(
                directory, max_bytes, target_bytes, current_cache_size_bytes=total_size
            )

            if total_size <= eff_max_bytes:
                return deleted_keys

            logger.warning(
                f"[{label} Cache] size ({total_size / (1024**3):.2f} GB) exceeded effective limit "
                f"({eff_max_bytes / (1024**3):.2f} GB, configured={max_bytes / (1024**3):.2f} GB). "
                f"Starting LRU cleanup to {eff_target_bytes / (1024**3):.2f} GB..."
            )

            # st_mtime が古い順（昇順）にソート
            entries.sort(key=lambda x: x[1])

            deleted_count = 0
            deleted_bytes = 0

            for file_path, mtime, size in entries:
                if total_size <= eff_target_bytes:
                    break
                try:
                    file_path.unlink(missing_ok=True)
                    self._invalidate_l1(file_path.name)
                    total_size -= size
                    deleted_bytes += size
                    deleted_count += 1
                    deleted_keys.append(file_path.name)
                except OSError as e:
                    logger.error(f"Error deleting {label} cache file {file_path}: {e}")

            logger.info(
                f"[{label} Cache] LRU cleanup completed: deleted {deleted_count} files "
                f"({deleted_bytes / (1024**2):.2f} MB). Current size: {total_size / (1024**3):.2f} GB"
            )
        except Exception as e:
            logger.exception(f"Unexpected error during {label} cache cleanup: {e}")

        return deleted_keys

    async def purge_all(self, target: str = "all") -> int:
        """
        キャッシュファイルをパージ（target: 'cloud', 'voicevox', 'all'）
        """
        async with self._lock:
            def _purge_target(dir_path: Path):
                count = 0
                if not dir_path.exists():
                    return 0
                for entry in dir_path.iterdir():
                    if entry.is_file() and (entry.suffix == ".wav" or entry.suffix == ".tmp"):
                        try:
                            entry.unlink(missing_ok=True)
                            count += 1
                        except OSError as e:
                            logger.error(f"Failed to delete {entry}: {e}")
                return count

            total_count = 0
            if target in ("cloud", "all"):
                c = await asyncio.to_thread(_purge_target, self.cache_dir)
                total_count += c
            if target in ("voicevox", "all"):
                c = await asyncio.to_thread(_purge_target, self.voicevox_cache_dir)
                total_count += c

            self._l1_cache.clear()
            self._l1_current_bytes = 0

            if self.db:
                try:
                    await self.db.purge_cache_metadata(target)
                except Exception as e:
                    logger.warning(f"Failed to purge cache metadata: {e}")

            logger.info(f"Purged {total_count} cached audio files (target={target}).")
            return total_count

    async def purge_by_word(self, word: str) -> int:
        """
        特定の単語を含むキャッシュのみを選択的にパージ。
        辞書登録・削除時に全キャッシュ（最大30GB）が消去されるのを防ぎ、影響のある音声のみを安全に再生成させる。
        """
        if not word or not word.strip():
            return 0
        w = word.strip()
        if not self.db or not hasattr(self.db, "find_cache_keys_by_word"):
            logger.warning("DB not available for selective cache purge. Skipping.")
            return 0

        async with self._lock:
            try:
                targets = await self.db.find_cache_keys_by_word(w)
                if not targets:
                    return 0

                deleted_keys = []
                for cache_key, provider in targets:
                    file_path = self.get_file_path(cache_key, provider)
                    try:
                        if await asyncio.to_thread(file_path.exists):
                            await asyncio.to_thread(file_path.unlink, True)
                    except OSError as e:
                        logger.warning(f"Failed to delete cache file {file_path}: {e}")
                    deleted_keys.append(cache_key)
                    self._invalidate_l1(cache_key)

                if deleted_keys:
                    await self.db.delete_cache_metadata_batch(deleted_keys)
                    logger.info(f"Selectively purged {len(deleted_keys)} cache file(s) matching word '{w}'.")
                return len(deleted_keys)
            except Exception as e:
                logger.error(f"Error in selective cache purge for word '{w}': {e}")
                return 0

    async def purge_by_words(self, words: List[str]) -> int:
        """複数の単語を含むキャッシュを選択的に一括パージ"""
        total = 0
        for w in set(words):
            if w and w.strip():
                total += await self.purge_by_word(w.strip())
        return total

    def get_stats(self) -> dict:
        """両キャッシュディレクトリの容量統計を取得 (ディスク空き容量に応じた実効上限も併記)"""
        def _dir_stats(d: Path, max_b: int, target_b: int):
            if not d.exists():
                eff_max, _ = calculate_effective_cache_limits(d, max_b, target_b, 0)
                return {"size_bytes": 0, "max_bytes": eff_max, "configured_max_bytes": max_b, "count": 0}
            size = 0
            cnt = 0
            for f in d.iterdir():
                if f.is_file() and f.suffix == ".wav":
                    try:
                        size += f.stat().st_size
                        cnt += 1
                    except OSError:
                        pass
            eff_max, _ = calculate_effective_cache_limits(d, max_b, target_b, size)
            return {"size_bytes": size, "max_bytes": eff_max, "configured_max_bytes": max_b, "count": cnt}

        return {
            "cloud": _dir_stats(self.cache_dir, self.max_size_bytes, self.target_size_bytes),
            "voicevox": _dir_stats(self.voicevox_cache_dir, self.voicevox_max_size_bytes, self.voicevox_target_size_bytes)
        }

    def get_explorer_file_path(self, target: str, filename: str) -> Optional[Path]:
        """
        パストラバーサルを防ぎつつ安全にキャッシュファイルのパスを取得 (CodeQL CWE-22 準拠)。
        """
        clean_name = sanitize_filename(filename)
        if not clean_name or not clean_name.endswith(".wav"):
            return None

        target_dir = self.voicevox_cache_dir if target in ("voicevox", "local") else self.cache_dir
        file_path = validate_safe_path(target_dir, clean_name)
        if file_path and file_path.exists() and file_path.is_file():
            return file_path
        return None

    def delete_file(self, target: str, filename: str) -> bool:
        """指定されたキャッシュファイルを個別削除"""
        p = self.get_explorer_file_path(target, filename)
        if not p:
            return False
        try:
            p.unlink(missing_ok=True)
            self._invalidate_l1(filename)
            if self.db:
                safe_create_task(self.db.delete_cache_metadata(filename), name="delete_cache_metadata_manual")
            logger.info(f"Deleted cache file ({target}): {filename}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete cache file {filename}: {e}")
            return False

    async def delete_files_batch(self, target: str, filenames: List[str]) -> int:
        """指定された複数のキャッシュファイルを一括削除"""
        if not filenames:
            return 0
        deleted_count = 0
        deleted_keys = []
        for fname in filenames:
            p = self.get_explorer_file_path(target, fname)
            if p and p.exists():
                try:
                    p.unlink(missing_ok=True)
                    self._invalidate_l1(fname)
                    deleted_count += 1
                    deleted_keys.append(fname)
                except Exception as e:
                    logger.error(f"Failed to delete {fname}: {e}")

        if self.db and deleted_keys:
            try:
                await self.db.delete_cache_metadata_batch(deleted_keys)
            except Exception as e:
                logger.warning(f"Failed to batch delete cache metadata: {e}")

        logger.info(f"Batch deleted {deleted_count}/{len(filenames)} cache files ({target})")
        return deleted_count

    async def list_files(
        self,
        target: str = "cloud",
        page: int = 1,
        per_page: int = 30,
        search: str = "",
        sort_by: str = "mtime",
        sort_order: str = "desc"
    ) -> Dict[str, Any]:
        """
        キャッシュファイル一覧を検索・ソート・ページネーション付きで取得。
        DBにメタデータがある場合は本文・話者名・ヒット数・再生秒数を付与して返却。
        """
        # 1. DBが利用可能な場合はメタデータ優先で取得
        if self.db:
            try:
                db_res = await self.db.list_cache_metadata(
                    target=target,
                    search=search,
                    sort_by=sort_by,
                    sort_order=sort_order,
                    page=page,
                    per_page=per_page
                )
                if db_res and "items" in db_res:
                    formatted_files = []
                    for item in db_res["items"]:
                        fname = item["cache_key"]
                        sz = item.get("size_bytes", 0)
                        if sz >= 1024 * 1024:
                            sz_str = f"{sz / (1024 * 1024):.2f} MB"
                        elif sz >= 1024:
                            sz_str = f"{sz / 1024:.1f} KB"
                        else:
                            sz_str = f"{sz} B"

                        dur = item.get("duration", 0.0)
                        dur_str = f"{dur:.1f}s" if dur > 0 else "-"

                        created_str = _format_utc_to_jst(item.get("created_at", ""))

                        formatted_files.append({
                            "filename": fname,
                            "text": item.get("text", ""),
                            "speaker_id": item.get("speaker_id", ""),
                            "speaker_name": item.get("speaker_name", ""),
                            "provider": item.get("provider", ""),
                            "is_chunk": bool(item.get("is_chunk", 0)),
                            "duration": dur,
                            "duration_str": dur_str,
                            "hit_count": item.get("hit_count", 1),
                            "size_bytes": sz,
                            "size_formatted": sz_str,
                            "mtime": 0,
                            "mtime_str": created_str,
                            "has_metadata": True
                        })

                    return {
                        "files": formatted_files,
                        "total": db_res["total"],
                        "total_pages": db_res["total_pages"],
                        "page": db_res["page"],
                        "per_page": db_res["per_page"],
                        "target": target
                    }
            except Exception as e:
                logger.warning(f"Error querying cache metadata, falling back to disk scan: {e}")

        # 2. DB未記録またはフォールバック: ディスク走査 (os.scandir で高速走査)
        target_dir = self.voicevox_cache_dir if target in ("voicevox", "local") else self.cache_dir

        def _scan():
            if not target_dir.exists():
                return {"files": [], "total": 0, "total_pages": 1, "page": 1, "target": target}

            items = []
            search_lower = search.lower().strip()

            with os.scandir(target_dir) as it:
                for entry in it:
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(".wav"):
                        fname = entry.name
                        if search_lower and search_lower not in fname.lower():
                            continue
                        try:
                            st = entry.stat()
                            items.append({
                                "filename": fname,
                                "size_bytes": st.st_size,
                                "mtime": st.st_mtime,
                            })
                        except OSError:
                            pass

            # ソート
            reverse = (sort_order.lower() == "desc")
            if sort_by == "size":
                items.sort(key=lambda x: x["size_bytes"], reverse=reverse)
            elif sort_by == "name":
                items.sort(key=lambda x: x["filename"], reverse=reverse)
            else:  # mtime
                items.sort(key=lambda x: x["mtime"], reverse=reverse)

            total_count = len(items)
            per_p = max(1, min(per_page, 100))
            total_pages = max(1, (total_count + per_p - 1) // per_p)
            current_p = max(1, min(page, total_pages))

            start_idx = (current_p - 1) * per_p
            end_idx = start_idx + per_p
            page_items = items[start_idx:end_idx]

            # 整形
            formatted_files = []
            for item in page_items:
                sz = item["size_bytes"]
                if sz >= 1024 * 1024:
                    sz_str = f"{sz / (1024 * 1024):.2f} MB"
                elif sz >= 1024:
                    sz_str = f"{sz / 1024:.1f} KB"
                else:
                    sz_str = f"{sz} B"

                mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["mtime"]))

                formatted_files.append({
                    "filename": item["filename"],
                    "text": "（メタデータ未記録）",
                    "speaker_id": "",
                    "speaker_name": "-",
                    "provider": target,
                    "is_chunk": False,
                    "duration": 0.0,
                    "duration_str": "-",
                    "hit_count": 1,
                    "size_bytes": sz,
                    "size_formatted": sz_str,
                    "mtime": item["mtime"],
                    "mtime_str": mtime_str,
                    "has_metadata": False
                })

            return {
                "files": formatted_files,
                "total": total_count,
                "total_pages": total_pages,
                "page": current_p,
                "per_page": per_p,
                "target": target
            }

        return await asyncio.to_thread(_scan)

    async def cleanup_orphan_metadata(self) -> int:
        """
        実体ファイルがディスク上に存在しない孤児キャッシュメタデータを検出し、一括クリーンアップ。
        """
        if not hasattr(self.db, "get_all_cache_keys_with_provider"):
            return 0

        all_records = await self.db.get_all_cache_keys_with_provider()
        if not all_records:
            return 0

        def _find_orphans() -> List[str]:
            orphans = []
            for c_key, prov in all_records:
                target_dir = self.voicevox_cache_dir if prov in self.LOCAL_PROVIDERS or prov.startswith("vv_") else self.cache_dir
                fname = c_key if c_key.endswith(".wav") else f"{c_key}.wav"
                fpath = target_dir / fname
                if not fpath.exists():
                    orphans.append(c_key)
            return orphans

        orphan_keys = await asyncio.to_thread(_find_orphans)
        if orphan_keys:
            chunk_size = 500
            total_deleted = 0
            for i in range(0, len(orphan_keys), chunk_size):
                chunk = orphan_keys[i:i + chunk_size]
                total_deleted += await self.db.delete_cache_metadata_batch(chunk)
            logger.info(f"[Orphan Metadata Cleanup] Removed {total_deleted} orphan records with missing audio files.")
            return total_deleted
        return 0

    async def scan_and_cleanup_corrupted(self) -> Dict[str, Any]:
        """
        両キャッシュディレクトリ内の全ファイルを一括検査。
        破損したファイルや無音ファイルを検知して自動削除・クリーンアップし、
        対応するメタデータおよび実体のない孤児メタデータを自動修復。
        """
        async with self._lock:
            deleted_keys: List[str] = []

            def _scan_directory(d: Path, label: str) -> Dict[str, int]:
                stats = {"scanned": 0, "valid": 0, "corrupted_deleted": 0, "silent_deleted": 0}
                if not d.exists():
                    return stats

                for f in d.iterdir():
                    if f.is_file() and f.suffix == ".wav":
                        stats["scanned"] += 1
                        try:
                            # 1. サイズチェック
                            sz = f.stat().st_size
                            if sz <= 44:
                                f.unlink(missing_ok=True)
                                deleted_keys.append(f.name)
                                stats["corrupted_deleted"] += 1
                                logger.info(f"[{label} Cache Scan] Deleted corrupted (empty/header only): {f.name}")
                                continue

                            # 2. WAVヘッダーおよび無音判定
                            data = f.read_bytes()
                            # パイプ出力等でヘッダーサイズが 0x7FFFFFFF になっている場合は自動修復
                            fixed_data = fix_wav_header(data)
                            if fixed_data != data:
                                f.write_bytes(fixed_data)
                                data = fixed_data
                                logger.info(f"[{label} Cache Scan] Repaired WAV header for: {f.name}")

                            is_valid, reason = validate_wav_bytes(data)
                            if not is_valid:
                                f.unlink(missing_ok=True)
                                deleted_keys.append(f.name)
                                if "silent" in reason.lower():
                                    stats["silent_deleted"] += 1
                                    logger.info(f"[{label} Cache Scan] Deleted silent audio: {f.name} ({reason})")
                                else:
                                    stats["corrupted_deleted"] += 1
                                    logger.info(f"[{label} Cache Scan] Deleted corrupted audio: {f.name} ({reason})")
                            else:
                                stats["valid"] += 1
                        except Exception as e:
                            logger.warning(f"[{label} Cache Scan] Error inspecting {f.name}: {e}")
                return stats

            cloud_res = await asyncio.to_thread(_scan_directory, self.cache_dir, "Cloud")
            vv_res = await asyncio.to_thread(_scan_directory, self.voicevox_cache_dir, "VOICEVOX")

            # 削除されたファイルのメタデータをDBからも削除
            if deleted_keys and hasattr(self.db, "delete_cache_metadata_batch"):
                await self.db.delete_cache_metadata_batch(deleted_keys)

            # 孤児メタデータの自動クリーンアップ
            orphans_removed = await self.cleanup_orphan_metadata()

            total_del = (
                cloud_res["corrupted_deleted"] + cloud_res["silent_deleted"] +
                vv_res["corrupted_deleted"] + vv_res["silent_deleted"]
            )
            total_scanned = cloud_res["scanned"] + vv_res["scanned"]

            logger.info(
                f"Cache integrity scan completed: {total_scanned} scanned, "
                f"{total_del} invalid files deleted, {orphans_removed} orphan metadata records cleaned."
            )

            corrupted_cnt = cloud_res["corrupted_deleted"] + vv_res["corrupted_deleted"]
            silent_cnt = cloud_res["silent_deleted"] + vv_res["silent_deleted"]
            healthy_cnt = total_scanned - total_del

            return {
                "cloud": cloud_res,
                "voicevox": vv_res,
                "total_scanned": total_scanned,
                "total_deleted": total_del,
                "healthy": healthy_cnt,
                "corrupted": corrupted_cnt,
                "silent": silent_cnt,
                "removed": total_del,
                "orphans_removed": orphans_removed
            }

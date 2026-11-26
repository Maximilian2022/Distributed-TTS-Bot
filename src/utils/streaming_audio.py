"""
TTSBot - パイプ型ストリーミング・オーディオソース (StreamingAudioSource)
Discord.py の vc.play() に対し、パーツ単位で生成された音声をリアルタイムに逐次供給します。
- ゼロ遅延・シームレス再生: 各パーツ間のギャップ（パケット送信リセット）を排除
- 自動フォーマット変換: 24kHz Mono PCM ➔ 48kHz Stereo PCM (Discord標準 20ms = 3840 bytes)
- バッファアンダーラン保護: 後続パーツの生成待ち時に無音パケットを安全送出し音飛び・切断を防止
"""

import io
import wave
import array
import math
import struct
import asyncio
import functools
import logging
import threading
from typing import Optional, List, Dict, Any

import discord

logger = logging.getLogger(__name__)

FRAME_SIZE = 3840  # 48kHz, 16bit, Stereo の 20ms 分のバイト数 (48000 * 2 * 2 * 0.02)


def generate_beep_pcm(duration_sec: float = 0.4, freq: float = 1000.0, sample_rate: int = 48000, volume: float = 0.25) -> bytes:
    """
    1000Hzサイン波の48kHz 16-bit Stereo PCM（自主規制ピー音）を生成。
    ポップノイズ防止のため先頭・末尾に5msのエンベロープ（フェード）を適用。
    """
    num_samples = int(sample_rate * duration_sec)
    max_amp = int(32767 * volume)
    fade_len = int(sample_rate * 0.005)  # 5ms

    pcm_bytes = bytearray(num_samples * 4)  # 2ch * 2bytes
    for i in range(num_samples):
        val = math.sin(2.0 * math.pi * freq * (i / sample_rate))
        if i < fade_len:
            gain = i / fade_len
        elif i > num_samples - fade_len:
            gain = (num_samples - i) / fade_len
        else:
            gain = 1.0
        sample_val = int(val * max_amp * gain)
        s_bytes = struct.pack("<hh", sample_val, sample_val)
        pcm_bytes[i * 4 : (i + 1) * 4] = s_bytes
    return bytes(pcm_bytes)


def calc_beep_duration(char_count: int) -> float:
    """
    NGワードの文字数に応じたピー音の長さ（秒）を算出。
    1文字あたり約120ms。最小200ms〜最大1.5秒で自然な発話長に調整。
    例:
      1文字: 0.22秒 (220ms)
      2文字: 0.34秒 (340ms)
      3文字: 0.46秒 (460ms)
      4文字: 0.58秒 (580ms)
      5文字: 0.70秒 (700ms)
      8文字: 1.06秒 (1060ms)
      12文字以上: 1.50秒 (1500ms上限)
    """
    c = max(1, char_count)
    sec = 0.10 + 0.12 * c
    return min(1.50, max(0.20, sec))


@functools.lru_cache(maxsize=64)
def get_beep_pcm(duration_sec: float) -> bytes:
    """
    指定秒数の1000Hzサイン波48kHz Stereo PCMを取得（LRUキャッシュ）。
    Discordの20msフレーム（960サンプル = 3840バイト）境界にアライン。
    """
    frame_samples = 960  # 48000 * 0.02
    target_samples = int(48000 * duration_sec)
    num_frames = max(1, round(target_samples / frame_samples))
    actual_duration = (num_frames * frame_samples) / 48000.0
    return generate_beep_pcm(duration_sec=actual_duration, freq=1000.0, sample_rate=48000, volume=0.25)


# 事前生成された自主規制ピー音 (1000Hz, 400ms, 48kHz Stereo PCM: 互換性維持用)
BEEP_PCM_48K_STEREO: bytes = get_beep_pcm(0.4)


def trim_trailing_silence(
    pcm_48k_stereo: bytes,
    margin_ms: int = 25,
    fade_ms: int = 5,
    threshold: int = 200
) -> bytes:
    """
    48kHz 16-bit Stereo PCMデータの末尾に存在するTTSエンジンの無音余韻（200ms〜800ms）を高速トリミング。
    ブツ切りノイズ（ポップノイズ）を防ぐため、音声末尾から margin_ms のマージンを残し、
    終端 fade_ms に滑らかな線形フェードアウトを適用する。
    """
    if not pcm_48k_stereo or len(pcm_48k_stereo) < FRAME_SIZE:
        return pcm_48k_stereo

    # ステレオ1フレーム(4 bytes = 2 shorts)境界に揃えて安全に読み込む
    aligned_bytes_len = len(pcm_48k_stereo) & ~3
    if aligned_bytes_len < FRAME_SIZE:
        return pcm_48k_stereo[:aligned_bytes_len]

    samples = array.array('h')
    samples.frombytes(memoryview(pcm_48k_stereo)[:aligned_bytes_len])
    n = len(samples)  # 常に偶数

    # 20msチャンク（960サンプル = 1920 shorts）単位で末尾からスキャン
    chunk_shorts = 1920
    last_voice_idx = -1

    for pos in range(n, 0, -chunk_shorts):
        start_idx = max(0, pos - chunk_shorts)
        sub = samples[start_idx:pos]
        if any(abs(s) > threshold for s in sub):
            # このブロック内に音声信号が存在する (pos, start_idx ともに偶数のため i は奇数(R), i-1 は偶数(L))
            for i in range(pos - 1, start_idx - 1, -2):
                if abs(samples[i]) > threshold or abs(samples[i - 1]) > threshold:
                    last_voice_idx = i + 1
                    break
            break

    # 全編無音の場合はそのまま返す
    if last_voice_idx < 0:
        return pcm_48k_stereo[:aligned_bytes_len]

    # マージン（余韻）を加算
    margin_shorts = int(48000 * (margin_ms / 1000.0) * 2)
    cutoff = min(n, last_voice_idx + margin_shorts)
    # 48kHz Stereo フレーム境界（4 shorts = 8 bytes）にアライン
    cutoff = min(n, (cutoff + 3) & ~3)

    # 末尾無音がほぼない（切り詰める長さが20ms未満）ならそのまま返す
    if cutoff >= n - (960 * 2):
        return pcm_48k_stereo[:aligned_bytes_len]

    # ポップノイズ防止の線形フェードアウト
    fade_shorts = max(4, int(48000 * (fade_ms / 1000.0) * 2)) & ~1
    fade_start = max(0, cutoff - fade_shorts)
    num_pairs = (cutoff - fade_start) // 2

    if num_pairs > 0:
        for idx in range(num_pairs):
            pos = fade_start + idx * 2
            gain = 1.0 - (idx / float(num_pairs))
            samples[pos] = int(samples[pos] * gain)
            samples[pos + 1] = int(samples[pos + 1] * gain)

    return samples[:cutoff].tobytes()


def _trim_mono_samples_inplace(
    samples: array.array,
    sample_rate: int,
    margin_ms: int = 25,
    fade_ms: int = 5,
    threshold: int = 200,
) -> None:
    """
    アップサンプリング前の Mono 16-bit サンプル配列をインプレースで無音トリム＆フェードアウト。
    24kHz Mono 段階で実行することで 48kHz Stereo 後と比較してスキャン・フェード演算量を 75% 削減する。
    """
    n = len(samples)
    frame_samples = max(2, int(sample_rate * 0.02))
    if n < frame_samples:
        return

    last_voice_idx = -1
    for pos in range(n, 0, -frame_samples):
        start_idx = max(0, pos - frame_samples)
        sub = samples[start_idx:pos]
        if any(abs(s) > threshold for s in sub):
            for i in range(pos - 1, start_idx - 1, -1):
                if abs(samples[i]) > threshold:
                    last_voice_idx = i + 1
                    break
            break

    if last_voice_idx < 0:
        return

    margin_samples = int(sample_rate * (margin_ms / 1000.0))
    cutoff = min(n, last_voice_idx + margin_samples)
    if cutoff >= n - frame_samples:
        return

    fade_samples = max(2, int(sample_rate * (fade_ms / 1000.0)))
    fade_start = max(0, cutoff - fade_samples)
    fade_count = cutoff - fade_start
    if fade_count > 0:
        for idx in range(fade_count):
            pos_i = fade_start + idx
            samples[pos_i] = int(samples[pos_i] * (1.0 - (idx / float(fade_count))))

    del samples[cutoff:]


def _trim_stereo_samples_inplace(
    samples: array.array,
    sample_rate: int,
    margin_ms: int = 25,
    fade_ms: int = 5,
    threshold: int = 200,
) -> None:
    """Stereo 16-bit サンプル配列をインプレースで無音トリム＆フェードアウト。"""
    n = len(samples) & ~1
    if n < len(samples):
        del samples[n:]
    frame_shorts = max(4, int(sample_rate * 0.02) * 2) & ~1
    if n < frame_shorts:
        return

    last_voice_idx = -1
    for pos in range(n, 0, -frame_shorts):
        start_idx = max(0, pos - frame_shorts)
        sub = samples[start_idx:pos]
        if any(abs(s) > threshold for s in sub):
            for i in range(pos - 1, start_idx - 1, -2):
                if abs(samples[i]) > threshold or abs(samples[i - 1]) > threshold:
                    last_voice_idx = i + 1
                    break
            break

    if last_voice_idx < 0:
        return

    margin_shorts = (int(sample_rate * (margin_ms / 1000.0)) * 2) & ~1
    cutoff = min(n, (last_voice_idx + margin_shorts + 1) & ~1)
    if cutoff >= n - frame_shorts:
        return

    fade_shorts = max(4, int(sample_rate * (fade_ms / 1000.0) * 2)) & ~1
    fade_start = max(0, cutoff - fade_shorts)
    num_pairs = (cutoff - fade_start) // 2
    if num_pairs > 0:
        for idx in range(num_pairs):
            pos_i = fade_start + idx * 2
            gain = 1.0 - (idx / float(num_pairs))
            samples[pos_i] = int(samples[pos_i] * gain)
            samples[pos_i + 1] = int(samples[pos_i + 1] * gain)

    del samples[cutoff:]


def _pcm_to_int16_samples(raw_pcm: bytes, sampwidth: int) -> array.array:
    """任意のサンプル幅 (8/16/24/32-bit PCM) を signed 16-bit array('h') に正規化する。"""
    arr = array.array('h')
    if not raw_pcm:
        return arr
    if sampwidth == 2:
        valid_len = len(raw_pcm) & ~1
        if valid_len > 0:
            arr.frombytes(memoryview(raw_pcm)[:valid_len])
        return arr
    if sampwidth == 1:
        arr.extend(((b - 128) << 8) for b in raw_pcm)
        return arr
    if sampwidth == 3:
        n_samp = len(raw_pcm) // 3
        if n_samp > 0:
            out16 = bytearray(n_samp * 2)
            out16[0::2] = raw_pcm[1 : n_samp * 3 : 3]
            out16[1::2] = raw_pcm[2 : n_samp * 3 : 3]
            arr.frombytes(out16)
        return arr
    if sampwidth == 4:
        n_samp = len(raw_pcm) // 4
        if n_samp > 0:
            out16 = bytearray(n_samp * 2)
            out16[0::2] = raw_pcm[2 : n_samp * 4 : 4]
            out16[1::2] = raw_pcm[3 : n_samp * 4 : 4]
            arr.frombytes(out16)
        return arr
    return arr


def _upsample_to_48k_stereo_array(samples: array.array, framerate: int, nchannels: int) -> array.array:
    """signed 16-bit サンプル配列を 48kHz Stereo array('h') に変換する (bytes中間コピーなし)。"""
    if not samples or framerate <= 0 or nchannels <= 0:
        return array.array('h')

    if framerate == 24000 and nchannels == 1:
        n = len(samples)
        out = array.array('h', [0]) * (n * 4)
        out[0::4] = samples
        out[1::4] = samples
        out[2::4] = samples
        out[3::4] = samples
        return out

    if framerate == 48000 and nchannels == 1:
        n = len(samples)
        out = array.array('h', [0]) * (n * 2)
        out[0::2] = samples
        out[1::2] = samples
        return out

    if framerate == 48000 and nchannels == 2:
        if len(samples) & 1:
            del samples[-1:]
        return samples

    if framerate == 24000 and nchannels == 2:
        n = len(samples) & ~1
        if n < len(samples):
            del samples[n:]
        out = array.array('h', [0]) * (n * 2)
        out[0::4] = samples[0::2]
        out[1::4] = samples[1::2]
        out[2::4] = samples[0::2]
        out[3::4] = samples[1::2]
        return out

    if framerate == 16000 and nchannels == 1:
        n = len(samples)
        out = array.array('h', [0]) * (n * 6)
        for k in range(6):
            out[k::6] = samples
        return out

    # 汎用線形補間リサンプラー (22050Hz, 32000Hz, 44100Hz 等)
    in_frames = len(samples) // nchannels
    if in_frames <= 0:
        return array.array('h')
    out_frames = max(1, int(round(in_frames * 48000.0 / float(framerate))))
    ratio = float(framerate) / 48000.0
    out = array.array('h', [0]) * (out_frames * 2)
    last_f = in_frames - 1

    if nchannels == 1:
        for j in range(out_frames):
            pos = j * ratio
            i0 = int(pos)
            if i0 >= last_f:
                s = samples[last_f]
            else:
                frac = pos - i0
                s0 = samples[i0]
                s = int(s0 + (samples[i0 + 1] - s0) * frac)
            out[j * 2] = s
            out[j * 2 + 1] = s
    else:
        for j in range(out_frames):
            pos = j * ratio
            i0 = int(pos)
            if i0 >= last_f:
                base = last_f * nchannels
                sl = samples[base]
                sr = samples[base + 1]
            else:
                frac = pos - i0
                b0 = i0 * nchannels
                b1 = (i0 + 1) * nchannels
                sl0, sr0 = samples[b0], samples[b0 + 1]
                sl = int(sl0 + (samples[b1] - sl0) * frac)
                sr = int(sr0 + (samples[b1 + 1] - sr0) * frac)
            out[j * 2] = sl
            out[j * 2 + 1] = sr
    return out


class StreamingAudioSource(discord.AudioSource):
    """
    非同期キューからPCMデータを逐次読み出すパイプ型 Discord AudioSource
    (Discord AudioPlayer 別スレッドの read() と asyncio メインスレッドの feed_*() を Lock で排他保護)
    """

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._read_pos = 0
        self._finished = False
        self._is_closed = False
        self._underrun_frames = 0
        self._total_frames = 0

    @staticmethod
    def _pcm_24k_mono_to_48k_stereo(raw_pcm_24k_mono: bytes) -> bytes:
        """
        24kHz 16-bit Mono PCM を Discord 標準の 48kHz 16-bit Stereo PCM に高速変換
        (各サンプルを2回複製し、左右L/Rに配置: サンプル数4倍)
        array.array のストライドスライス代入により、Pythonループ比で約50倍高速に処理
        """
        if not raw_pcm_24k_mono:
            return b""

        samples = array.array('h')
        samples.frombytes(raw_pcm_24k_mono)
        n = len(samples)

        # 4倍展開バッファ [L0, R0, L1, R1, ...]
        out = array.array('h', [0]) * (n * 4)
        out[0::4] = samples  # フレーム1 L
        out[1::4] = samples  # フレーム1 R
        out[2::4] = samples  # フレーム2 L
        out[3::4] = samples  # フレーム2 R

        return out.tobytes()

    @staticmethod
    def _pcm_48k_mono_to_48k_stereo(raw_pcm_48k_mono: bytes) -> bytes:
        """
        48kHz 16-bit Mono PCM を 48kHz 16-bit Stereo PCM に変換 (サンプル数2倍)
        """
        if not raw_pcm_48k_mono:
            return b""

        samples = array.array('h')
        samples.frombytes(raw_pcm_48k_mono)
        n = len(samples)

        out = array.array('h', [0]) * (n * 2)
        out[0::2] = samples  # L
        out[1::2] = samples  # R

        return out.tobytes()

    @staticmethod
    def _pcm_24k_stereo_to_48k_stereo(raw_pcm_24k_stereo: bytes) -> bytes:
        """
        24kHz 16-bit Stereo PCM を 48kHz 16-bit Stereo PCM に変換 (各フレームを2回複製: サンプル数2倍)
        """
        if not raw_pcm_24k_stereo:
            return b""

        samples = array.array('h')
        samples.frombytes(raw_pcm_24k_stereo)
        n = len(samples)  # L0, R0, L1, R1... (n個のサンプル = n//2 フレーム)

        out = array.array('h', [0]) * (n * 2)
        # 各フレーム (L, R) を2回繰り返す
        out[0::4] = samples[0::2]  # L
        out[1::4] = samples[1::2]  # R
        out[2::4] = samples[0::2]  # L (リピート)
        out[3::4] = samples[1::2]  # R (リピート)

        return out.tobytes()

    def feed_wav_bytes(
        self,
        wav_bytes: bytes,
        pause_after_ms: int = 0,
        trim_tail: bool = True,
        is_followed_by_beep: bool = False
    ) -> float:
        """
        WAVバイト列を受け取り、PCM生データを48kHz Stereoに変換してストリームに追加
        戻り値: 追加された音声の長さ (秒)
        """
        if not wav_bytes:
            return 0.0
        with self._lock:
            if self._finished or self._is_closed:
                return 0.0

        try:
            margin_ms = 12 if is_followed_by_beep else 25

            # 高速短絡パース: 標準的な 24kHz 16-bit Mono WAV (VOICEVOX/COEIROINK/SHAREVOX/Azure標準) のゼロコピー風直接抽出
            if (
                len(wav_bytes) >= 44
                and wav_bytes[:4] == b"RIFF"
                and wav_bytes[8:12] == b"WAVE"
                and wav_bytes[12:16] == b"fmt "
                and wav_bytes[20:24] == b"\x01\x00\x01\x00"  # AudioFormat=1 (PCM), NumChannels=1
                and wav_bytes[24:28] == b"\x60\x5d\x00\x00"  # SampleRate=24000
                and wav_bytes[34:36] == b"\x10\x00"          # BitsPerSample=16
                and wav_bytes[36:40] == b"data"
            ):
                data_size = struct.unpack("<I", wav_bytes[40:44])[0]
                end_idx = min(len(wav_bytes), 44 + data_size) if data_size > 0 else len(wav_bytes)
                pcm_len = (end_idx - 44) & ~1
                if pcm_len <= 0:
                    return 0.0
                samples = array.array('h')
                samples.frombytes(memoryview(wav_bytes)[44 : 44 + pcm_len])
                if trim_tail:
                    _trim_mono_samples_inplace(samples, 24000, margin_ms=margin_ms, fade_ms=5)
                out_arr = _upsample_to_48k_stereo_array(samples, 24000, 1)
            else:
                # 汎用 wave.open による解析
                with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                    framerate = wf.getframerate()
                    nchannels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    frames = wf.getnframes()
                    raw_pcm = wf.readframes(frames)

                samples = _pcm_to_int16_samples(raw_pcm, sampwidth)
                if trim_tail and samples:
                    if nchannels == 1:
                        _trim_mono_samples_inplace(samples, framerate, margin_ms=margin_ms, fade_ms=5)
                    elif nchannels == 2:
                        _trim_stereo_samples_inplace(samples, framerate, margin_ms=margin_ms, fade_ms=5)
                out_arr = _upsample_to_48k_stereo_array(samples, framerate, nchannels)

            pcm_byte_len = len(out_arr) * 2
            duration = pcm_byte_len / 192000.0  # 48000 samples/sec * 2ch * 2bytes = 192000 bytes/sec
            pause_bytes_len = int(192 * pause_after_ms) if pause_after_ms > 0 else 0
            if pause_bytes_len > 0:
                duration += (pause_after_ms / 1000.0)

            with self._lock:
                if self._finished or self._is_closed:
                    return 0.0
                if pcm_byte_len > 0:
                    self._buffer.extend(memoryview(out_arr).cast('B'))
                if pause_bytes_len > 0:
                    self._buffer.extend(b'\x00' * pause_bytes_len)
            return duration

        except Exception as e:
            logger.warning(f"Error parsing/feeding WAV bytes to stream: {e}")
            return 0.0

    def feed_raw_pcm(
        self,
        pcm_48k_stereo: bytes,
        pause_after_ms: int = 0,
        trim_tail: bool = False,
        is_followed_by_beep: bool = False
    ) -> float:
        """
        48kHz 16-bit Stereo PCM 生データを直接ストリームバッファに追加 (WAVパース不要のO(1)即時投入)
        戻り値: 追加された音声の長さ (秒)
        """
        if not pcm_48k_stereo:
            return 0.0
        with self._lock:
            if self._finished or self._is_closed:
                return 0.0

        if trim_tail:
            margin_ms = 12 if is_followed_by_beep else 25
            pcm_48k_stereo = trim_trailing_silence(pcm_48k_stereo, margin_ms=margin_ms, fade_ms=5)

        aligned_len = len(pcm_48k_stereo) & ~3
        if aligned_len <= 0:
            return 0.0

        duration = aligned_len / 192000.0  # 48000 samples/sec * 2ch * 2bytes = 192000 bytes/sec
        pause_bytes_len = int(192 * pause_after_ms) if pause_after_ms > 0 else 0
        if pause_bytes_len > 0:
            duration += (pause_after_ms / 1000.0)

        with self._lock:
            if self._finished or self._is_closed:
                return 0.0
            self._buffer.extend(memoryview(pcm_48k_stereo)[:aligned_len])
            if pause_bytes_len > 0:
                self._buffer.extend(b'\x00' * pause_bytes_len)
        return duration

    def finish(self):
        """これ以上のパーツ追加がないことを通知 (終端マーク)"""
        with self._lock:
            self._finished = True

    def read(self) -> bytes:
        """
        Discord音声送信ループから20msごとに呼び出される (3840 bytes を要求)
        """
        with self._lock:
            if self._is_closed:
                return b""

            self._total_frames += 1
            available = len(self._buffer) - self._read_pos

            # バッファに十分なデータがある場合 (O(1) スライス参照)
            if available >= FRAME_SIZE:
                start = self._read_pos
                self._read_pos += FRAME_SIZE
                chunk = bytes(self._buffer[start:self._read_pos])

                # 定期的なメモリ縮小 (Compaction):
                # 全て読み切ったか、または既読領域が64KBに達した際に先頭解放
                if self._read_pos == len(self._buffer):
                    self._buffer.clear()
                    self._read_pos = 0
                elif self._read_pos >= 65536:
                    del self._buffer[:self._read_pos]
                    self._read_pos = 0

                return chunk

            # 終端に達している場合
            if self._finished:
                if available <= 0:
                    self._buffer.clear()
                    self._read_pos = 0
                    return b""
                # 端数パディング
                chunk = bytes(self._buffer[self._read_pos:]) + (b'\x00' * (FRAME_SIZE - available))
                self._buffer.clear()
                self._read_pos = 0
                return chunk

            # まだ終了していないがバッファが空 (後続パーツの生成待ち = アンダーラン)
            # 接続を切断させないよう、無音フレームを安全送出して待機
            self._underrun_frames += 1
            return b'\x00' * FRAME_SIZE

    @property
    def underrun_frames(self) -> int:
        """アンダーラン（無音送出）が発生した20msフレーム数"""
        with self._lock:
            return self._underrun_frames

    @property
    def underrun_ms(self) -> float:
        """アンダーラン（無音送出）の総ミリ秒数"""
        with self._lock:
            return float(self._underrun_frames * 20)

    @property
    def buffer_remaining_seconds(self) -> float:
        """現在バッファに蓄積されている未再生音声の残り再生秒数 (48kHz 16-bit Stereo = 192000 bytes/sec)"""
        with self._lock:
            if self._is_closed:
                return 0.0
            avail = max(0, len(self._buffer) - self._read_pos)
            return avail / 192000.0

    def cleanup(self):
        """再生終了時のクリーンアップ"""
        with self._lock:
            self._is_closed = True
            self._buffer.clear()
            self._read_pos = 0
            total_f = self._total_frames
            underrun_f = self._underrun_frames
        if underrun_f > 0:
            logger.debug(
                f"StreamingAudioSource closed. Total frames: {total_f}, "
                f"Underrun (waited) frames: {underrun_f} ({underrun_f * 20}ms)"
            )

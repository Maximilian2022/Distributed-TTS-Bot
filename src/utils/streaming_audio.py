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
import asyncio
import logging
from typing import Optional, List, Dict, Any

import discord

logger = logging.getLogger(__name__)

FRAME_SIZE = 3840  # 48kHz, 16bit, Stereo の 20ms 分のバイト数 (48000 * 2 * 2 * 0.02)


class StreamingAudioSource(discord.AudioSource):
    """
    非同期キューからPCMデータを逐次読み出すパイプ型 Discord AudioSource
    """

    def __init__(self):
        super().__init__()
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

    def feed_wav_bytes(self, wav_bytes: bytes, pause_after_ms: int = 0) -> float:
        """
        WAVバイト列を受け取り、PCM生データを48kHz Stereoに変換してストリームに追加
        戻り値: 追加された音声の長さ (秒)
        """
        if self._finished or self._is_closed or not wav_bytes:
            return 0.0

        try:
            # 高速短絡パース: 標準的な 24kHz 16-bit Mono WAV (VOICEVOX/COEIROINK/SHAREVOX標準) の高速直接抽出 (PERF-5)
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
                import struct
                data_size = struct.unpack("<I", wav_bytes[40:44])[0]
                raw_pcm = wav_bytes[44:44 + data_size] if data_size > 0 else wav_bytes[44:]
                duration = len(raw_pcm) / 48000.0  # 24000 samples/sec * 2 bytes/sample
                pcm_48k_stereo = self._pcm_24k_mono_to_48k_stereo(raw_pcm)
            else:
                # 汎用 wave.open による解析
                with wave.open(io.BytesIO(wav_bytes), 'rb') as wf:
                    framerate = wf.getframerate()
                    nchannels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    frames = wf.getnframes()
                    raw_pcm = wf.readframes(frames)
                    duration = frames / float(framerate) if framerate > 0 else 0.0

                # 48kHz Stereo PCMへの変換
                if framerate == 24000 and nchannels == 1:
                    pcm_48k_stereo = self._pcm_24k_mono_to_48k_stereo(raw_pcm)
                elif framerate == 48000 and nchannels == 1:
                    pcm_48k_stereo = self._pcm_48k_mono_to_48k_stereo(raw_pcm)
                elif framerate == 48000 and nchannels == 2:
                    pcm_48k_stereo = raw_pcm
                elif framerate == 24000 and nchannels == 2:
                    pcm_48k_stereo = self._pcm_24k_stereo_to_48k_stereo(raw_pcm)
                else:
                    logger.warning(
                        f"Unexpected audio format: {framerate}Hz, {nchannels}ch, sampwidth={sampwidth}. "
                        f"Falling back to 24k mono upsampler."
                    )
                    pcm_48k_stereo = self._pcm_24k_mono_to_48k_stereo(raw_pcm)

            # ポーズ（無音）の追加
            if pause_after_ms > 0:
                # 48kHz 16-bit Stereo の 1ms あたり 192 バイト (48000 * 4 / 1000)
                pause_bytes = b'\x00' * int(192 * pause_after_ms)
                pcm_48k_stereo += pause_bytes
                duration += (pause_after_ms / 1000.0)

            self._buffer.extend(pcm_48k_stereo)
            return duration

        except Exception as e:
            logger.warning(f"Error parsing/feeding WAV bytes to stream: {e}")
            return 0.0

    def finish(self):
        """これ以上のパーツ追加がないことを通知 (終端マーク)"""
        self._finished = True

    def read(self) -> bytes:
        """
        Discord音声送信ループから20msごとに呼び出される (3840 bytes を要求)
        """
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
            if available == 0:
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
        return self._underrun_frames

    @property
    def underrun_ms(self) -> float:
        """アンダーラン（無音送出）の総ミリ秒数"""
        return float(self._underrun_frames * 20)

    def cleanup(self):
        """再生終了時のクリーンアップ"""
        self._is_closed = True
        self._buffer.clear()
        self._read_pos = 0
        if self._underrun_frames > 0:
            logger.debug(
                f"StreamingAudioSource closed. Total frames: {self._total_frames}, "
                f"Underrun (waited) frames: {self._underrun_frames} ({self._underrun_frames * 20}ms)"
            )

"""
ボランティア型分散音声合成ワーカー用 WebSocket 通信プロトコル仕様
- 高速バイナリフレーム転送 (ゼロコピー・Base64不要)
- 双方向ストリーミング & ヘルスチェック
- 型定義 & シリアライザ
"""

import struct
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional, List, Union

PROTOCOL_VERSION = "2.0.0"

# バイナリフレーム固定ヘッダーサイズ: 32 bytes (req_id hex) + 4 bytes (chunk_idx) + 1 byte (flags) = 37 bytes
BINARY_HEADER_FORMAT = ">32sIB"
BINARY_HEADER_SIZE = struct.calcsize(BINARY_HEADER_FORMAT)
FLAG_FINAL_CHUNK = 0x01
FLAG_ERROR_CHUNK = 0x02

@dataclass
class RegisterMessage:
    """ワーカーからサーバーへの登録要求"""
    node_id: str
    node_name: str
    auth_token: str
    supported_engines: List[str]  # 例: ["voicevox", "coeiroink", "sharevox"]
    capacity: int = 2              # 同時実行可能並列数
    client_version: str = PROTOCOL_VERSION
    specs: Dict[str, Any] = field(default_factory=dict) # CPU, GPU, OS等
    type: str = "register"

@dataclass
class RegisterAckMessage:
    """サーバーからワーカーへの登録応答"""
    node_id: str
    status: str                    # "ok" または "rejected"
    message: str = ""
    assigned_priority: int = 50
    heartbeat_interval_sec: float = 10.0
    protocol_version: str = PROTOCOL_VERSION
    type: str = "register_ack"

@dataclass
class HeartbeatMessage:
    """ワーカーからサーバーへの定期生存・負荷報告"""
    node_id: str
    active_tasks: int = 0
    cpu_percent: float = 0.0
    memory_percent: float = 0.0
    engine_status: Dict[str, bool] = field(default_factory=dict)
    uptime_sec: float = 0.0
    timestamp: float = field(default_factory=time.time)
    type: str = "heartbeat"

@dataclass
class HeartbeatAckMessage:
    """サーバーからワーカーへの生存確認応答"""
    status: str = "ok"
    server_time: float = field(default_factory=time.time)
    type: str = "heartbeat_ack"

@dataclass
class SynthesizeRequest:
    """サーバーからワーカーへの音声合成要求"""
    req_id: str                   # 32文字の一意なリクエストID
    chunk_index: int              # 文内チャンク番号 (0, 1, 2...)
    engine: str                   # "voicevox", "coeiroink", "sharevox" 等
    speaker_id: Union[str, int]   # 話者ID
    text: str                     # 合成テキスト (前処理・30文字以下に正規化済み)
    speed: float = 1.0            # 読み上げ速度
    pitch: float = 0.0            # 音高
    priority: int = 0             # 優先度 (0: 通常, 1: 優先)
    timeout_ms: int = 5000        # タイムアウトミリ秒
    type: str = "synthesize_request"

@dataclass
class SynthesizeHeader:
    """ワーカーからサーバーへの合成開始メタデータ"""
    req_id: str
    chunk_index: int
    format: str = "wav"           # "wav", "raw_pcm", "ogg"
    sample_rate: int = 24000
    channels: int = 1
    type: str = "synthesize_header"

@dataclass
class SynthesizeError:
    """ワーカーからサーバーへの合成失敗通知"""
    req_id: str
    chunk_index: int
    error_code: str
    error_message: str
    type: str = "synthesize_error"

@dataclass
class SynthesizeComplete:
    """ワーカーからサーバーへの合成完了通知 (ストリーミング終了)"""
    req_id: str
    chunk_index: int
    duration_ms: float = 0.0
    total_bytes: int = 0
    type: str = "synthesize_complete"


def pack_binary_chunk(req_id: str, chunk_index: int, payload: bytes, is_final: bool = True) -> bytes:
    """
    バイナリ音声チャンクをパッキング
    構造: [32-byte ASCII req_id][4-byte uint32 chunk_idx][1-byte flags][raw audio bytes]
    """
    req_id_bytes = req_id.encode('ascii')[:32].ljust(32, b' ')
    flags = FLAG_FINAL_CHUNK if is_final else 0x00
    header = struct.pack(BINARY_HEADER_FORMAT, req_id_bytes, chunk_index, flags)
    return header + payload

def unpack_binary_chunk(data: bytes) -> Tuple_BinaryChunk:
    """
    バイナリ音声チャンクをアンパッキング
    返却値: (req_id, chunk_index, is_final, payload)
    """
    if len(data) < BINARY_HEADER_SIZE:
        raise ValueError(f"Binary packet too short: {len(data)} bytes < {BINARY_HEADER_SIZE} bytes")
    req_id_raw, chunk_index, flags = struct.unpack_from(BINARY_HEADER_FORMAT, data, 0)
    req_id = req_id_raw.decode('ascii').strip()
    is_final = bool(flags & FLAG_FINAL_CHUNK)
    payload = data[BINARY_HEADER_SIZE:]
    return req_id, chunk_index, is_final, payload

# アンパック返却用型ヒント
from typing import Tuple
Tuple_BinaryChunk = Tuple[str, int, bool, bytes]

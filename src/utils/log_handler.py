"""
2段階（行数制限 ＋ サイズベースの zstd 圧縮）ハイブリッドローテーションハンドラ
- 第1段階: 最大 3,000 行。超過時に直近 100 行を残し、古い行を *-archive.log に追記退避。
- 第2段階: *-archive.log が 100 MB を超過時、別スレッドで高速 zstd 圧縮 (.zst)。最大 10 世代保持。
- 第3段階: logs/ フォルダ全体で 1 GB 上限超過時、古い .zst を古い順に自動削除。
"""

import os
import sys
import time
import shutil
import logging
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

# zstandard ライブラリの動的ロード (ない場合は外部 zstd コマンドへフォールバック)
try:
    import zstandard as zstd
    HAS_ZSTANDARD_PKG = True
except ImportError:
    zstd = None
    HAS_ZSTANDARD_PKG = False

_GLOBAL_COMPRESSION_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

def _get_compression_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if key not in _GLOBAL_COMPRESSION_LOCKS:
            _GLOBAL_COMPRESSION_LOCKS[key] = threading.Lock()
        return _GLOBAL_COMPRESSION_LOCKS[key]


def compress_archive_worker(
    archive_file: Path,
    max_archives: int = 10,
    dir_max_bytes: int = 1024 * 1024 * 1024
) -> bool:
    """*-archive.log を zstd 圧縮し、世代管理とディレクトリ容量上限チェックを実行"""
    lock = _get_compression_lock(str(archive_file))
    if not lock.acquire(blocking=False):
        return False

    try:
        if not archive_file.exists() or archive_file.stat().st_size == 0:
            return False

        log_dir = archive_file.parent
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tmp_archive = log_dir / f"{archive_file.name}.{timestamp}.tmp"
        target_zst = log_dir / f"{archive_file.stem}-{timestamp}.log.zst"

        # 1. アトミックリネームで現在の archive.log を切り離す
        shutil.move(str(archive_file), str(tmp_archive))
        archive_file.touch()

        # 2. zstd 圧縮を実行
        compressed_success = False
        if HAS_ZSTANDARD_PKG and zstd is not None:
            try:
                cctx = zstd.ZstdCompressor(level=3, threads=-1)
                with open(tmp_archive, "rb") as ifh, open(target_zst, "wb") as ofh:
                    cctx.copy_stream(ifh, ofh)
                compressed_success = True
            except Exception as e:
                sys.stderr.write(f"zstandard python compression failed: {e}\n")

        if not compressed_success:
            try:
                subprocess.run(
                    ["zstd", "-T0", "-3", "-q", str(tmp_archive), "-o", str(target_zst)],
                    check=True,
                    capture_output=True
                )
                compressed_success = True
            except Exception as e:
                sys.stderr.write(f"zstd command compression failed: {e}\n")

        if compressed_success and target_zst.exists():
            try:
                tmp_archive.unlink(missing_ok=True)
            except Exception:
                pass
        else:
            sys.stderr.write(f"Failed to compress {tmp_archive}, keeping tmp file.\n")
            return False

        # 3. 世代管理: 同一プレフィックスの .zst ファイルを最大 max_archives 世代に制限
        prefix = f"{archive_file.stem}-"
        zst_files = sorted(
            [p for p in log_dir.glob(f"{prefix}*.log.zst") if p.is_file()],
            key=lambda p: p.stat().st_mtime
        )
        while len(zst_files) > max_archives:
            oldest = zst_files.pop(0)
            try:
                oldest.unlink(missing_ok=True)
            except Exception:
                pass

        # 4. ディレクトリ容量上限チェック (1 GB 上限清掃)
        try:
            total_size = sum(p.stat().st_size for p in log_dir.glob("**/*") if p.is_file())
            if total_size > dir_max_bytes:
                all_zst = sorted(
                    [p for p in log_dir.glob("*.zst") if p.is_file()],
                    key=lambda p: p.stat().st_mtime
                )
                for zf in all_zst:
                    if total_size <= dir_max_bytes:
                        break
                    sz = zf.stat().st_size
                    try:
                        zf.unlink(missing_ok=True)
                        total_size -= sz
                    except Exception:
                        pass
        except Exception as e:
            sys.stderr.write(f"Error enforcing dir size limit on {log_dir}: {e}\n")

        return True
    except Exception as e:
        sys.stderr.write(f"Error in zstd archive worker for {archive_file}: {e}\n")
        return False
    finally:
        lock.release()


def rotate_file_lines(
    filepath: str,
    max_lines: int = 3000,
    keep_lines: int = 100,
    archive_max_bytes: int = 100 * 1024 * 1024,
    max_archives: int = 10,
    dir_max_bytes: int = 1024 * 1024 * 1024,
    encoding: str = "utf-8"
) -> bool:
    """
    指定ログファイルの行数が max_lines を超過した場合、
    直近 keep_lines を残して古い行を *-archive.log に追記退避する (第1段階)。
    *-archive.log が archive_max_bytes 超過時は別スレッドで zstd 圧縮を実行 (第2段階)。
    """
    path = Path(filepath)
    if not path.exists():
        return False

    archive_path = path.parent / f"{path.stem}-archive.log"

    try:
        with open(filepath, "r", encoding=encoding, errors="replace") as f:
            lines = f.readlines()

        if len(lines) < max_lines:
            return False

        older_lines = lines[:-keep_lines]
        recent_lines = lines[-keep_lines:]

        # 古い行をアーカイブファイルへ追記
        with open(archive_path, "a", encoding=encoding, errors="replace") as af:
            af.writelines(older_lines)

        # 直近 keep_lines 行をアクティブファイルに上書き
        with open(filepath, "w", encoding=encoding, errors="replace") as rf:
            rf.writelines(recent_lines)

        # 第2段階チェック: archive.log が archive_max_bytes を超えたか確認
        if archive_path.exists() and archive_path.stat().st_size >= archive_max_bytes:
            t = threading.Thread(
                target=compress_archive_worker,
                args=(archive_path, max_archives, dir_max_bytes),
                daemon=True,
                name=f"zstd-{path.stem}"
            )
            t.start()

        return True
    except Exception as e:
        sys.stderr.write(f"Error rotating file lines for {filepath}: {e}\n")
        return False


class TwoStageZstdRotatingHandler(logging.Handler):
    """
    行数ベースの第1段階ローテーションと、
    サイズベース（zstd圧縮）の第2段階ローテーションを提供するハイブリッドハンドラ
    """

    def __init__(
        self,
        filename: str,
        max_lines: int = 3000,
        keep_lines: int = 100,
        archive_max_bytes: int = 100 * 1024 * 1024,  # 100 MB
        max_archives: int = 10,
        dir_max_bytes: int = 1024 * 1024 * 1024,      # 1 GB
        encoding: str = "utf-8"
    ):
        super().__init__()
        self.baseFilename = os.path.abspath(filename)
        self.max_lines = max_lines
        self.keep_lines = keep_lines
        self.archive_max_bytes = archive_max_bytes
        self.max_archives = max_archives
        self.dir_max_bytes = dir_max_bytes
        self.encoding = encoding

        self.log_path = Path(self.baseFilename)
        self.log_dir = self.log_path.parent
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # アーカイブファイル名: 例 logs/bot-archive.log
        stem = self.log_path.stem
        self.archive_path = self.log_dir / f"{stem}-archive.log"

        self._lock = threading.RLock()
        self._compression_lock = threading.Lock()
        self.current_lines = 0

        # 既存ファイルの行数を初期カウント
        self._init_file_and_lines()

    def _init_file_and_lines(self):
        """起動時の行数確認とストリーム初期化"""
        with self._lock:
            if self.log_path.exists():
                try:
                    count = 0
                    with open(self.baseFilename, "r", encoding=self.encoding, errors="replace") as f:
                        for _ in f:
                            count += 1
                    self.current_lines = count
                except Exception:
                    self.current_lines = 0
            else:
                self.current_lines = 0

            # 起動時にすでに 3000 行を超えていれば即座に第1段階ローテーションを実施
            if self.current_lines >= self.max_lines:
                self._rollover_lines()

            self.stream = open(self.baseFilename, "a", encoding=self.encoding, errors="replace")

    def emit(self, record: logging.LogRecord):
        """ログレコードの出力と行数判定"""
        try:
            msg = self.format(record)
            with self._lock:
                if self.stream is None or self.stream.closed:
                    self.stream = open(self.baseFilename, "a", encoding=self.encoding, errors="replace")

                self.stream.write(msg + "\n")
                self.stream.flush()

                # 行数の更新（改行を含むメッセージにも対応）
                added_lines = msg.count("\n") + 1
                self.current_lines += added_lines

                # 第1段階: 3,000 行超過チェック
                if self.current_lines >= self.max_lines:
                    self._rollover_lines()
        except Exception:
            self.handleError(record)

    def _rollover_lines(self):
        """第1段階: 直近 100 行を残し、古い行を *-archive.log へ移動"""
        if not self.log_path.exists():
            return

        try:
            if hasattr(self, "stream") and self.stream and not self.stream.closed:
                self.stream.flush()
                self.stream.close()

            # 全行を読み出し
            lines = []
            with open(self.baseFilename, "r", encoding=self.encoding, errors="replace") as f:
                lines = f.readlines()

            if len(lines) > self.keep_lines:
                older_lines = lines[:-self.keep_lines]
                recent_lines = lines[-self.keep_lines:]

                # 古い行をアーカイブファイルへ追記
                with open(self.archive_path, "a", encoding=self.encoding, errors="replace") as af:
                    af.writelines(older_lines)

                # 直近 100 行をアクティブファイルに上書き
                with open(self.baseFilename, "w", encoding=self.encoding, errors="replace") as rf:
                    rf.writelines(recent_lines)

                self.current_lines = len(recent_lines)
            else:
                self.current_lines = len(lines)

            # ストリーム再オープン
            self.stream = open(self.baseFilename, "a", encoding=self.encoding, errors="replace")

            # 第2段階チェック: archive.log が 100 MB を超えたか確認
            if self.archive_path.exists() and self.archive_path.stat().st_size >= self.archive_max_bytes:
                self._trigger_compression_in_thread()

        except Exception as e:
            sys.stderr.write(f"Error during line rollover for {self.baseFilename}: {e}\n")
            if not hasattr(self, "stream") or self.stream is None or self.stream.closed:
                self.stream = open(self.baseFilename, "a", encoding=self.encoding, errors="replace")

    def _trigger_compression_in_thread(self):
        """別スレッドで第2段階 (zstd圧縮) を実行"""
        worker = threading.Thread(
            target=compress_archive_worker,
            args=(self.archive_path, self.max_archives, self.dir_max_bytes),
            daemon=True,
            name=f"zstd-compressor-{self.log_path.stem}"
        )
        worker.start()

    def close(self):
        with self._lock:
            if hasattr(self, "stream") and self.stream and not self.stream.closed:
                self.stream.flush()
                self.stream.close()
        super().close()

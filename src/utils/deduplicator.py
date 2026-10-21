import time
import logging
from collections import deque
from typing import Iterable, Tuple, Deque, Optional

logger = logging.getLogger(__name__)

class Deduplicator:
    def __init__(self, window_seconds: float = 5.0, max_length: int = 10):
        self.window_seconds = window_seconds
        self.max_length = max_length
        # (text, timestamp) の履歴 (短時間に大量の短文が来た場合のメモリ上限を200件でハード保証)
        self._history: Deque[Tuple[str, float]] = deque(maxlen=200)

    def _cleanup(self, now: float, window_seconds: Optional[float] = None) -> None:
        """指定ウィンドウ時間外の古い履歴を削除"""
        window = window_seconds if window_seconds is not None else self.window_seconds
        while self._history and (now - self._history[0][1] > window):
            self._history.popleft()

    def is_duplicate(
        self,
        text: str,
        current_queue_texts: Iterable[str],
        window_seconds: Optional[float] = None,
        max_length: Optional[int] = None
    ) -> bool:
        """
        指定文字数以下の短文について、直近指定秒以内またはキュー内にあるテキストと完全一致するか判定。
        """
        stripped = text.strip()
        limit = max_length if max_length is not None else self.max_length
        if len(stripped) > limit:
            return False

        now = time.time()
        self._cleanup(now, window_seconds=window_seconds)

        # 1. 直近ウィンドウ秒以内に再生・登録されたテキストと完全一致するか
        for history_text, _ in self._history:
            if history_text == stripped:
                logger.info(f"Duplicate suppressed (recent match): '{stripped}'")
                return True

        # 2. 現在の再生待ちキュー内に完全一致するものがあるか
        for q_text in current_queue_texts:
            if q_text.strip() == stripped:
                logger.info(f"Duplicate suppressed (queue match): '{stripped}'")
                return True

        return False

    def record_added(self, text: str, max_length: Optional[int] = None) -> None:
        """短文がキューに追加された際にタイムスタンプを記録"""
        stripped = text.strip()
        limit = max_length if max_length is not None else self.max_length
        if len(stripped) <= limit:
            now = time.time()
            self._history.append((stripped, now))
            self._cleanup(now)

    def record_played(self, text: str, max_length: Optional[int] = None) -> None:
        """再生完了時にもタイムスタンプを最新に更新"""
        stripped = text.strip()
        limit = max_length if max_length is not None else self.max_length
        if len(stripped) <= limit:
            now = time.time()
            self._history.append((stripped, now))
            self._cleanup(now)

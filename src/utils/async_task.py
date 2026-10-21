"""
非同期バックグラウンドタスク管理ヘルパー
- GC (Garbage Collection) によるタスク消失の防止
- 未キャッチ例外の自動検知と確実なログ出力
"""

import asyncio
import logging
from typing import Coroutine, Any, Optional, Set

logger = logging.getLogger("src.utils.async_task")

# GC 防止のための強参照保持セット (PEP 599 / Python公式推奨パターン)
_background_tasks: Set[asyncio.Task] = set()

def safe_create_task(
    coro: Coroutine[Any, Any, Any],
    name: Optional[str] = None
) -> asyncio.Task:
    """
    Fire-and-forget な非同期タスクを安全に起動。
    - タスクの完了まで強参照を保持して GC による中断や 'Task was destroyed but it is pending!' を防止
    - タスク終了時に例外が発生していた場合はスタックトレース付きで自動ログ出力
    """
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _done_callback(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            try:
                exc = t.exception()
                if exc:
                    t_name = t.get_name() if hasattr(t, "get_name") else "unknown"
                    logger.error(
                        f"Unhandled exception in background task '{t_name}': {exc}",
                        exc_info=exc
                    )
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                pass

    task.add_done_callback(_done_callback)
    return task

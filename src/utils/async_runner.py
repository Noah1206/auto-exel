"""Playwright 전용 백그라운드 asyncio loop 러너.

배경:
  qasync 의 QEventLoop 위에서 Playwright 의 백그라운드 IPC task 가 실행되면
  Qt 시그널 처리 중 task reentry 가 발생해
  ``RuntimeError: Cannot enter into task ... while another task ... is being executed``
  가 터진다. 이를 근본적으로 피하려면 Playwright 를 별도 스레드의 자체 asyncio
  루프에서 운영하고, Qt UI 쪽에서는 :class:`concurrent.futures.Future` 결과를
  받는 방식으로 분리해야 한다.

사용:
    runner = AsyncRunner()
    runner.start()
    fut = runner.submit(some_async_func(arg))
    fut.add_done_callback(lambda f: print(f.result()))
    ...
    runner.shutdown()
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Coroutine

from src.utils.logger import get_logger

log = get_logger()


class AsyncRunner:
    """전용 스레드에서 asyncio 이벤트 루프를 돌리는 러너.

    Qt 메인 스레드와 완전히 분리된 루프이므로 Playwright 의 백그라운드 task 가
    Qt 시그널 핸들러와 reentry 충돌하지 않는다.
    """

    def __init__(self, name: str = "AsyncRunner"):
        self._name = name
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=self._name, daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=5)
        log.info(f"{self._name} 시작")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            try:
                # 남은 task 정리
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception as exc:
                log.warning(f"{self._name} 정리 중 오류: {exc}")
            finally:
                self._loop.close()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> Future:
        """코루틴을 백그라운드 루프에 제출하고 Future 반환."""
        if self._loop is None:
            raise RuntimeError(f"{self._name} 가 시작되지 않았습니다 — start() 먼저 호출")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._loop is None or self._thread is None:
            return
        log.info(f"{self._name} 종료 중...")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        self._loop = None
        self._thread = None

    @property
    def is_running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

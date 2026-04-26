"""재시도 유틸리티 (지수 백오프)."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TypeVar

from src.utils.logger import get_logger

log = get_logger()

T = TypeVar("T")


def async_retry(
    attempts: int = 3,
    backoff_base: float = 1.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
):
    """async 함수를 지수 백오프로 재시도.

    예: @async_retry(attempts=3, backoff_base=1.0)
        1st failure → 1s wait, 2nd → 2s, 3rd → 4s
    """

    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for i in range(attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if i == attempts - 1:
                        break
                    wait = backoff_base * (2**i)
                    log.warning(
                        f"[retry] {func.__name__} 실패 ({i + 1}/{attempts}) → {wait}s 후 재시도: {exc}"
                    )
                    await asyncio.sleep(wait)
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator

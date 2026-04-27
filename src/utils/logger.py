"""loguru 기반 로깅 설정."""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_CONFIGURED = False


def setup_logger(
    log_dir: str | Path = "data/logs",
    level: str = "INFO",
    rotation: str = "00:00",
    retention_days: int = 30,
    max_file_mb: int = 10,
) -> None:
    """로거 전역 설정. 여러 번 호출해도 중복 핸들러 등록 방지."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    logger.remove()  # default stderr 제거
    # PyInstaller windowed 모드(console=False) 에선 sys.stderr 가 None 이라
    # logger.add(None) 이 TypeError 를 던진다 (Windows .exe 에서 발생).
    # stderr 가 살아있을 때만 콘솔 핸들러 등록.
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=level,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - {message}",
            enqueue=True,
        )
    logger.add(
        log_dir_path / "{time:YYYY-MM-DD}.log",
        level=level,
        rotation=rotation,
        retention=f"{retention_days} days",
        compression=None,
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} | {message}",
        enqueue=True,
    )

    _CONFIGURED = True


def get_logger():
    """설정된 로거 인스턴스 반환."""
    return logger

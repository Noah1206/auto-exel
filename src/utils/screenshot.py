"""에러 발생 시 스크린샷 저장."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.utils.logger import get_logger

log = get_logger()


async def save_error_screenshot(page, row: int, screenshot_dir: str | Path = "data/screenshots") -> str | None:
    """페이지 스크린샷 저장 후 경로 반환. 실패해도 예외 던지지 않음."""
    try:
        out = Path(screenshot_dir)
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = out / f"error_row{row:04d}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        log.info(f"스크린샷 저장: {path}")
        return str(path)
    except Exception as exc:
        log.warning(f"스크린샷 저장 실패: {exc}")
        return None

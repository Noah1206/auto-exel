"""selectors.yaml 로더 + Playwright fallback 셀렉터 헬퍼."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from playwright.async_api import Locator, Page
from playwright.async_api import TimeoutError as PwTimeout

from src.exceptions import ConfigError, ElementNotFoundError
from src.utils.logger import get_logger
from src.utils.resource_path import resource_path

log = get_logger()


class SelectorHelper:
    """YAML 기반 셀렉터 조회 + fallback 탐색."""

    def __init__(self, yaml_path: str | Path | None = None):
        if yaml_path is None:
            self.path = resource_path("config", "selectors.yaml")
        else:
            self.path = Path(yaml_path)
            # 상대경로면 번들 리소스 위치로 해석
            if not self.path.is_absolute() and not self.path.exists():
                self.path = resource_path(*self.path.parts)
        if not self.path.exists():
            raise ConfigError(f"selectors.yaml을 찾을 수 없습니다: {self.path}")
        try:
            with self.path.open(encoding="utf-8") as f:
                self._data: dict[str, Any] = yaml.safe_load(f) or {}
        except Exception as exc:
            raise ConfigError(f"selectors.yaml 파싱 실패: {exc}") from exc

    def get(self, dotted_path: str) -> list[str]:
        """'order_page.recipient_name' → ['input[name=...]', ...]"""
        node: Any = self._data
        for key in dotted_path.split("."):
            if not isinstance(node, dict) or key not in node:
                raise ConfigError(f"셀렉터 경로가 존재하지 않습니다: {dotted_path}")
            node = node[key]
        if isinstance(node, str):
            return [node]
        if isinstance(node, list):
            return [str(s) for s in node]
        raise ConfigError(f"셀렉터 타입 오류 [{dotted_path}]: {type(node)}")

    async def find(
        self,
        page: Page,
        dotted_path: str,
        timeout_ms: int = 5000,
        state: str = "visible",
    ) -> Locator:
        """Fallback 셀렉터를 순차 시도. 첫 성공을 반환."""
        selectors = self.get(dotted_path)
        per_selector = max(500, timeout_ms // max(1, len(selectors)))
        last_exc: BaseException | None = None

        for sel in selectors:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state=state, timeout=per_selector)
                return loc
            except PwTimeout as exc:
                last_exc = exc
                log.debug(f"셀렉터 실패, 다음 시도: {sel}")
                continue
            except Exception as exc:
                last_exc = exc
                log.debug(f"셀렉터 예외, 다음 시도: {sel} ({exc})")
                continue

        raise ElementNotFoundError(
            f"모든 셀렉터 실패 [{dotted_path}]: tried {selectors}"
        ) from last_exc

    async def fill(
        self,
        page: Page,
        dotted_path: str,
        value: str,
        typing_delay_ms: int = 50,
        timeout_ms: int = 5000,
    ) -> None:
        """사람같은 딜레이로 typing."""
        loc = await self.find(page, dotted_path, timeout_ms=timeout_ms)
        await loc.click()
        await loc.fill("")  # 기존 값 제거
        await loc.type(value, delay=typing_delay_ms)

    async def click(
        self, page: Page, dotted_path: str, timeout_ms: int = 5000
    ) -> None:
        loc = await self.find(page, dotted_path, timeout_ms=timeout_ms)
        await loc.click()

    async def exists(
        self, page: Page, dotted_path: str, timeout_ms: int = 2000
    ) -> bool:
        """요소 존재 여부 반환. 없으면 False (예외 던지지 않음)."""
        try:
            await self.find(page, dotted_path, timeout_ms=timeout_ms)
            return True
        except ElementNotFoundError:
            return False

    async def get_text(
        self, page: Page, dotted_path: str, timeout_ms: int = 5000
    ) -> str:
        loc = await self.find(page, dotted_path, timeout_ms=timeout_ms)
        text = await loc.inner_text()
        return text.strip()

"""샵백(Shopback) 적립 추적 모니터.

샵백은 브라우저 확장으로 동작하는 쿠키 기반 어필리에이트 트래킹이다.
11번가에서 결제 시 샵백이 추적 쿠키를 심어놨어야 적립이 되므로,
결제 직전에 샵백 네트워크 요청이 발생했는지를 확인해 "적립 가능 상태"를 검증한다.

동작:
    monitor = ShopbackMonitor(page)
    monitor.start()
    # ... 사용자가 상품 페이지 탐색, 주문 페이지로 이동 ...
    result = monitor.snapshot()
    if not result.is_tracking_active:
        print("샵백 추적 미활성 — 적립 안 될 수 있음")
    monitor.save_log("data/diagnostics/shopback_123.log")
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Request, Response

from src.utils.logger import get_logger

log = get_logger()


# 샵백 및 관련 어필리에이트 추적 도메인 패턴 (2026 기준)
_SHOPBACK_DOMAINS = (
    "shopback.co.kr",
    "shopback.com",
    "sbk.kr",            # 샵백 단축 도메인
    "shopback-analytics",
    "shopback-cdn",
)

# 11번가 URL에 어필리에이트 파라미터가 붙었는지 감지할 키워드
_AFFILIATE_PARAM_KEYS = (
    "affiliate",
    "aff_id",
    "utm_source=shopback",
    "utm_medium=affiliate",
    "refCode=shopback",
    "ref=shopback",
    "adid",
    "partner_code",
)


@dataclass
class ShopbackSnapshot:
    """특정 시점까지의 샵백 추적 상태."""

    shopback_requests: list[dict[str, Any]] = field(default_factory=list)
    affiliate_urls: list[str] = field(default_factory=list)
    first_detected_at: datetime | None = None
    last_detected_at: datetime | None = None

    @property
    def is_tracking_active(self) -> bool:
        """샵백 요청 또는 어필리에이트 URL 흔적이 하나라도 있으면 추적 활성."""
        return bool(self.shopback_requests or self.affiliate_urls)

    def summary(self) -> str:
        if not self.is_tracking_active:
            return "샵백 추적 흔적 없음 — 적립이 안 될 가능성이 높습니다."
        parts = []
        if self.shopback_requests:
            parts.append(f"샵백 네트워크 요청 {len(self.shopback_requests)}건 감지")
        if self.affiliate_urls:
            parts.append(f"어필리에이트 URL {len(self.affiliate_urls)}건 감지")
        if self.first_detected_at:
            parts.append(f"최초 감지: {self.first_detected_at:%H:%M:%S}")
        return " | ".join(parts)


class ShopbackMonitor:
    """한 Page 의 네트워크 트래픽을 구독해 샵백 관련 요청을 수집."""

    def __init__(self, page: Page):
        self.page = page
        self._snapshot = ShopbackSnapshot()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        # 네비게이션으로 인한 URL 변경도 확인 (11번가 응답 URL에 어필리에이트 파라미터)
        self.page.on("framenavigated", self._on_navigated)

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self.page.remove_listener("request", self._on_request)
            self.page.remove_listener("response", self._on_response)
            self.page.remove_listener("framenavigated", self._on_navigated)
        except Exception:
            pass
        self._started = False

    def snapshot(self) -> ShopbackSnapshot:
        return self._snapshot

    def save_log(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "captured_at": datetime.now().isoformat(),
            "is_tracking_active": self._snapshot.is_tracking_active,
            "summary": self._snapshot.summary(),
            "shopback_requests": self._snapshot.shopback_requests,
            "affiliate_urls": self._snapshot.affiliate_urls,
        }
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return out

    # -------------------------------------------------------------
    # Event handlers
    # -------------------------------------------------------------

    def _on_request(self, request: Request) -> None:
        try:
            url = request.url
            if self._is_shopback_url(url):
                self._record_shopback(url, method=request.method)
            elif self._has_affiliate_params(url):
                self._snapshot.affiliate_urls.append(url)
                self._touch()
        except Exception as exc:
            log.debug(f"샵백 모니터 request 오류: {exc}")

    def _on_response(self, response: Response) -> None:
        try:
            url = response.url
            if self._is_shopback_url(url):
                self._record_shopback(url, status=response.status)
        except Exception as exc:
            log.debug(f"샵백 모니터 response 오류: {exc}")

    def _on_navigated(self, frame) -> None:
        try:
            url = frame.url
            if self._has_affiliate_params(url):
                self._snapshot.affiliate_urls.append(url)
                self._touch()
        except Exception:
            pass

    # -------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------

    @staticmethod
    def _is_shopback_url(url: str) -> bool:
        u = url.lower()
        return any(d in u for d in _SHOPBACK_DOMAINS)

    @staticmethod
    def _has_affiliate_params(url: str) -> bool:
        u = url.lower()
        return any(k.lower() in u for k in _AFFILIATE_PARAM_KEYS)

    def _record_shopback(self, url: str, **extra) -> None:
        entry = {"url": url, "at": datetime.now().isoformat(), **extra}
        self._snapshot.shopback_requests.append(entry)
        self._touch()

    def _touch(self) -> None:
        now = datetime.now()
        if self._snapshot.first_detected_at is None:
            self._snapshot.first_detected_at = now
        self._snapshot.last_detected_at = now

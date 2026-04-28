"""11번가 상품 페이지에서 판매가 스크랩 (순차)."""
from __future__ import annotations

import asyncio
import gzip
import re
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from playwright.async_api import TimeoutError as PwTimeout

from src.core.browser_manager import BrowserManager
from src.core.selector_helper import SelectorHelper
from src.exceptions import (
    ElementNotFoundError,
    ProductUnavailableError,
)
from src.models.order import Order
from src.models.settings import PriceScraperConfig
from src.utils.logger import get_logger
from src.utils.validators import clean_price

log = get_logger()

# 진행 상황 콜백: (current, total, order) → None
ProgressCb = Callable[[int, int, Order], None]

# HTTP fast path 용 — Playwright/Chrome 안 띄우고 가격 추출. 17건 ≈ 1초.
# Chrome 시동/탭 생성 비용이 항목당 1~2초인데 이게 통째로 사라진다.
_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# HTML 텍스트에서 가격 직접 추출용 정규식 시퀀스. 우선순위 순서.
# 11번가 상품 페이지 SSR HTML 에 그대로 들어있는 패턴들.
_PRICE_HTML_PATTERNS = (
    # itemprop="price" content="50000"
    re.compile(r'itemprop=["\']price["\'][^>]*content=["\'](\d{3,9})["\']', re.IGNORECASE),
    re.compile(r'content=["\'](\d{3,9})["\'][^>]*itemprop=["\']price["\']', re.IGNORECASE),
    # data-finalprc="50000" 등
    re.compile(r'data-final[-_]?pr[ci]e?=["\'](\d{3,9})["\']', re.IGNORECASE),
    re.compile(r'data-sell[-_]?pr[ci]e?=["\'](\d{3,9})["\']', re.IGNORECASE),
    # JSON 안 finalPrc / sellPrc / lastPrc 키
    re.compile(r'"(?:finalPrc|sellPrc|lastPrc|finalPrice|sellPrice)"\s*:\s*"?(\d{3,9})"?'),
    # <strong class="...price..."> 50,000 </strong>
    re.compile(
        r'<strong[^>]*class=["\'][^"\']*(?:c_product_detail__price|SellPrice|price-value)[^"\']*["\'][^>]*>'
        r'\s*([\d,]{3,12})\s*(?:원)?\s*</strong>',
        re.IGNORECASE,
    ),
)


class PriceScraper:
    """여러 상품링크를 순차적으로 방문하여 가격 조회."""

    def __init__(
        self,
        browser: BrowserManager,
        selectors: SelectorHelper,
        config: PriceScraperConfig,
    ):
        self.browser = browser
        self.selectors = selectors
        self.config = config
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    async def scrape_all(
        self,
        orders: list[Order],
        on_progress: ProgressCb | None = None,
        only_missing: bool = True,
    ) -> list[Order]:
        """가격 스크랩 병렬 실행.

        only_missing=True 이면 total_price 가 비어있는 주문만 대상.
        결과는 order.unit_price / order.total_price 에 기록.
        config.concurrent 만큼 동시에 페이지를 열어 가격을 긁는다.
        """
        self._cancel = False
        targets = [o for o in orders if (not only_missing) or o.needs_price()]
        total = len(targets)
        concurrent = max(1, int(getattr(self.config, "concurrent", 1) or 1))
        log.info(
            f"가격 조회 시작: {total}건 (전체 {len(orders)}건 중) — 동시 {concurrent}개"
        )

        if total == 0:
            return orders

        sem = asyncio.Semaphore(concurrent)
        completed = 0
        completed_lock = asyncio.Lock()

        async def _worker(idx: int, order: Order) -> None:
            nonlocal completed
            if self._cancel:
                return
            async with sem:
                if self._cancel:
                    return
                page = None
                try:
                    # ★ Fast path: HTTP 직접 요청. Chrome/Playwright 안 띄움.
                    #    SSR HTML 에 가격이 들어있어 대부분 여기서 끝남.
                    unit: int | None = None
                    fast_unavail = False
                    try:
                        unit, fast_unavail = await self._scrape_via_http(order)
                    except Exception as exc:
                        log.debug(f"행{order.row} HTTP fast path 예외: {exc}")
                        unit, fast_unavail = None, False

                    if fast_unavail:
                        order.status = "unavailable"
                        order.error_message = "페이지 없음 (HTTP 404)"
                        log.warning(f"[{idx}/{total}] 행{order.row} HTTP 404 (fast)")
                    elif unit is not None:
                        order.unit_price = unit
                        order.compute_total()
                        log.info(
                            f"[{idx}/{total}] 행{order.row} 단가: {unit:,}원 "
                            f"× {order.quantity} = {order.total_price:,}원 (fast)"
                        )
                    else:
                        # Fallback: Playwright 경로 (셀렉터 깨졌거나 SSR 에 가격 없음)
                        page = await self.browser.new_page()
                        try:
                            unit = await self._scrape_one(page, order)
                            order.unit_price = unit
                            order.compute_total()
                            log.info(
                                f"[{idx}/{total}] 행{order.row} 단가: {unit:,}원 "
                                f"× {order.quantity} = {order.total_price:,}원"
                            )
                        except ProductUnavailableError as exc:
                            order.status = "unavailable"
                            order.error_message = f"페이지 없음: {exc.reason}"
                            log.warning(
                                f"[{idx}/{total}] 행{order.row} HTTP 404 — {exc.reason}"
                            )
                        except Exception as exc:
                            log.warning(
                                f"[{idx}/{total}] 행{order.row} 가격 조회 실패: {exc}"
                            )
                            if idx == 1 or idx % 10 == 0:
                                try:
                                    await self._save_diagnostics(page, order)
                                except Exception:
                                    pass
                finally:
                    if page is not None:
                        try:
                            await page.close()
                        except Exception:
                            pass
                    async with completed_lock:
                        completed += 1
                        cur = completed
                    if on_progress:
                        try:
                            on_progress(cur, total, order)
                        except Exception:
                            pass
                    # 동시성이 너무 높을 때 11번가 부하 완화용 짧은 딜레이
                    delay_ms = getattr(self.config, "inter_request_delay_ms", 0) or 0
                    if delay_ms > 0:
                        await asyncio.sleep(delay_ms / 1000)

        tasks = [
            asyncio.create_task(_worker(i, order))
            for i, order in enumerate(targets, start=1)
        ]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if self._cancel:
                log.info("가격 조회 취소됨")
        return orders

    def missing_price_orders(self, orders: list[Order]) -> list[Order]:
        """토탈가격이 비어있는 주문만 필터. 주문 시작 전 가드로 사용."""
        return [o for o in orders if o.needs_price()]

    async def _scrape_via_http(self, order: Order) -> tuple[int | None, bool]:
        """Chrome/Playwright 안 거치고 표준 라이브러리(urllib) 로 HTML 직접 GET.

        반환: (unit_price | None, is_unavailable)
          - 가격 추출 성공: (가격, False)
          - 404 등 판매중지: (None, True)
          - 그 외 실패(셀렉터 안 잡힘 등): (None, False)  → 호출자가 Playwright fallback

        17건 ≈ 1초. Chrome 시동/탭 생성/렌더링 비용을 통째로 절약.
        """
        timeout_s = max(2.0, self.config.per_product_timeout_ms / 1000)

        def _do_request() -> tuple[int | None, bool]:
            req = urllib.request.Request(
                order.product_url, headers=_HTTP_HEADERS, method="GET"
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    raw = resp.read()
                    enc = resp.headers.get("Content-Encoding", "").lower()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return (None, True)
                return (None, False)
            except Exception:
                return (None, False)

            # gzip/deflate 디코딩
            try:
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
            except Exception:
                pass

            # 한국어 페이지라 utf-8 우선, 실패 시 cp949
            html: str
            try:
                html = raw.decode("utf-8", errors="replace")
            except Exception:
                try:
                    html = raw.decode("cp949", errors="replace")
                except Exception:
                    return (None, False)

            # 판매중지/페이지 없음 휴리스틱 — 11번가 SSR 에 자주 노출되는 문구
            lowered = html[:20000]
            if ("존재하지 않는 상품" in lowered
                    or "삭제된 상품" in lowered
                    or "판매가 종료" in lowered
                    or "판매중지" in lowered):
                return (None, True)

            # 정규식 시퀀스로 가격 추출
            for pat in _PRICE_HTML_PATTERNS:
                m = pat.search(html)
                if not m:
                    continue
                v = clean_price(m.group(1))
                if v and v > 0:
                    return (v, False)
            return (None, False)

        return await asyncio.to_thread(_do_request)

    async def _save_diagnostics(self, page, order: Order) -> None:
        """가격 조회 실패 시 진단용 HTML/스크린샷 저장.

        사용자가 셀렉터 갱신을 요청할 때 첨부할 수 있도록 한다.
        """
        try:
            out_dir = Path("data/diagnostics")
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            html_path = out_dir / f"price_fail_row{order.row}_{ts}.html"
            png_path = out_dir / f"price_fail_row{order.row}_{ts}.png"
            html_path.write_text(await page.content(), encoding="utf-8")
            await page.screenshot(path=str(png_path), full_page=False)
            log.info(
                f"진단 파일 저장: {html_path.name}, {png_path.name} "
                f"(셀렉터 갱신 시 활용하세요)"
            )
        except Exception as exc:
            log.debug(f"진단 파일 저장 실패: {exc}")

    async def _scrape_one(self, page, order: Order) -> int:
        # route 차단은 의도적으로 사용하지 않는다.
        # 가격 fast path (HTTP) 가 대부분 처리하므로 fallback 자체가 드물고,
        # 차단 시 macOS Chromium 의 disk cache 잔재로 다른 탭 페이지가
        # 빈 박스(이미지 미로드)로 보이는 부작용이 관찰됨.
        try:
            # domcontentloaded 까지 기다림 — DOM 트리가 만들어진 시점.
            # 가격 셀렉터가 안전하게 매칭되려면 이 시점이 필요.
            response = await page.goto(
                order.product_url,
                wait_until="domcontentloaded",
                timeout=self.config.per_product_timeout_ms,
            )
        except PwTimeout as exc:
            raise RuntimeError(f"페이지 로드 타임아웃: {order.product_url}") from exc

        # 0) 판매중지/삭제 감지 — 가격 추출 전에 먼저 확인
        await self._check_unavailability(page, response)

        # 1) selectors.yaml 기반 시도 — 정상 케이스 99% 여기서 잡힘.
        #    셀렉터로 잡힌 가격은 정확하므로 시간을 충분히 주는 게 안전.
        try:
            raw = await self.selectors.get_text(
                page,
                "product_page.price",
                timeout_ms=self.config.per_product_timeout_ms,
            )
            value = clean_price(raw)
            if value is not None and value > 0:
                return value
            log.debug(f"행{order.row} 셀렉터 매칭됐으나 파싱 실패: raw={raw!r}")
        except ElementNotFoundError:
            pass  # fallback 으로 넘어감

        # 2) JavaScript fallback — 셀렉터가 페이지 개편으로 깨진 케이스만.
        #    여기 떨어졌다는 건 셀렉터 갱신이 필요하다는 신호.
        value = await self._fallback_price_from_dom(page)
        if value is None:
            raise ElementNotFoundError(
                f"가격을 찾지 못했습니다. 페이지 구조가 바뀐 것 같습니다. "
                f"URL: {order.product_url}\n"
                "→ scripts/diagnose_selectors.py 로 진단 후 selectors.yaml 을 갱신해 주세요."
            )
        log.info(f"행{order.row} 가격 fallback 추출 성공: {value:,}원")
        return value


    async def _check_unavailability(self, page, response) -> None:
        """페이지 자체가 사라졌는지만 본다 (HTTP 404).

        DOM 텍스트("품절", "판매중지" 등) 매칭은 false positive가 너무 많아 제거.
        실제로 판매 불가능한 상품이라면 결제하기 단계에서 11번가가 거부할 것이고,
        그때 일반 실패로 처리되어 다음 행으로 자연스럽게 진행된다.
        """
        if response is not None and response.status == 404:
            raise ProductUnavailableError(
                "상품 페이지를 찾을 수 없습니다 (HTTP 404)",
                reason="존재하지 않는 상품 또는 삭제됨",
            )

    async def _fallback_price_from_dom(self, page) -> int | None:
        """DOM 전체에서 "판매가"로 보이는 숫자를 추론.

        전략:
          1) 페이지 내 strong/em/span/b 중 "15,900" / "15,900원" 패턴 텍스트 수집
          2) 동일 요소가 `display:none` 이면 제외
          3) 그 중 "원래가/정가"로 보이는 취소선(del, .original) 요소는 제외
          4) 남은 값 중 페이지 상단(뷰포트 기준 y<800)에서 가장 큰 숫자를 가격으로 선택
        """
        js = r"""
() => {
  // 1) 모든 "숫자(+원)" 후보 수집
  const nodes = document.querySelectorAll('strong, em, span, b');
  const candidates = [];
  const rePrice = /^\s*[\d]{1,3}(?:,\d{3})*\s*(원)?\s*$/;
  for (const el of nodes) {
    // 숨김 요소 제외
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    // 취소선(원래가) 제외
    if (style.textDecorationLine && style.textDecorationLine.includes('line-through')) continue;
    // 조상에 "original" / "strike" / "del" 클래스 있으면 제외
    let skip = false;
    for (let p = el; p && p !== document.body; p = p.parentElement) {
      const cn = (p.className && typeof p.className === 'string') ? p.className.toLowerCase() : '';
      if (/(original|strike|del|before|was)/i.test(cn)) { skip = true; break; }
      if (p.tagName === 'DEL' || p.tagName === 'S') { skip = true; break; }
    }
    if (skip) continue;

    const t = (el.innerText || el.textContent || '').trim();
    if (!rePrice.test(t)) continue;
    const num = parseInt(t.replace(/[^\d]/g, ''), 10);
    if (!num || num < 100) continue;

    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;

    candidates.push({num: num, top: rect.top, fontSize: parseFloat(style.fontSize) || 0});
  }
  if (candidates.length === 0) return null;
  // 폰트 크기가 큰 (가격 강조) + 상단에 가까운 것을 우선
  // 스코어: fontSize * 2 - top * 0.01
  candidates.sort((a, b) => (b.fontSize * 2 - b.top * 0.01) - (a.fontSize * 2 - a.top * 0.01));
  return candidates[0].num;
}
"""
        try:
            result = await page.evaluate(js)
            if isinstance(result, (int, float)) and result > 0:
                return int(result)
        except Exception as exc:
            log.debug(f"JS fallback 가격 추출 실패: {exc}")
        return None

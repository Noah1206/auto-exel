"""주문 자동화 state machine.

설계 원칙:
  - 실패/일시정지 시 Page를 자동으로 닫지 않는다.
    사용자가 해당 탭에서 직접 입력을 수정/완료할 수 있게 유지한다.
  - 예외는 OrderAutomation 안에서 포착되어 Order.status 로 반영된다.
    프로그램의 이벤트루프/브라우저 컨텍스트는 절대 중단되지 않는다.
  - 재시도(resume)는 "어느 체크포인트부터"를 명시적으로 받아서
    이미 통과한 단계를 건너뛰고 이어서 실행한다.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import Enum

from playwright.async_api import Page, TimeoutError as PwTimeout

from src.core.browser_manager import BrowserManager
from src.core.selector_helper import SelectorHelper
from src.core.shopback_monitor import ShopbackMonitor
from src.exceptions import (
    CaptchaDetectedError,
    ElementNotFoundError,
    LoginExpiredError,
    PaymentTimeoutError,
    UserInterventionRequired,
)
from src.models.order import Order
from src.models.settings import AutomationConfig
from src.utils.logger import get_logger
from src.utils.screenshot import save_error_screenshot

log = get_logger()


class OrderState(str, Enum):
    IDLE = "idle"
    PENDING = "pending"     # 대기 (실행 안 됨 / 사용자 취소 후 복귀)
    OPEN_PRODUCT = "open_product"
    CHECK_LOGIN = "check_login"
    CLICK_BUY = "click_buy"
    FILL_FORM = "fill_form"
    WAIT_PAYMENT = "wait_payment"
    EXTRACT_ORDER_NO = "extract_order_no"
    COMPLETE = "complete"
    PAUSED = "paused"       # 사용자 개입 대기 (프로그램 유지)
    FAILED = "failed"


# 체크포인트: 자동화 재개(resume) 시 어느 단계부터 이어서 실행할지.
class Checkpoint(str, Enum):
    START = "start"
    AT_PRODUCT_PAGE = "at_product_page"
    AT_ORDER_PAGE = "at_order_page"
    FORM_FILLED = "form_filled"
    PAYMENT_DONE = "payment_done"


StateCb = Callable[[Order, OrderState, str | None], None]
ConfirmCb = Callable[[str, str], Awaitable[bool]]  # (title, message) -> yes/no


class OrderAutomation:
    """엑셀 한 행에 대한 end-to-end 주문 플로우.

    한 Order 마다 독립된 Page를 사용한다.
    execute()가 paused/failed 로 끝나면 self._pages[order.row] 에 Page가 살아있다.
    사용자가 문제를 수정한 뒤 resume(order)을 호출하면 마지막 체크포인트부터 재개.
    """

    def __init__(
        self,
        browser: BrowserManager,
        selectors: SelectorHelper,
        config: AutomationConfig,
        on_state: StateCb | None = None,
        on_confirm: ConfirmCb | None = None,
    ):
        self.browser = browser
        self.selectors = selectors
        self.config = config
        self.on_state = on_state or (lambda o, s, m: None)
        self.on_confirm = on_confirm

        # order.row → 실행 중/일시정지 중인 Page
        self._pages: dict[int, Page] = {}
        # order.row → 샵백 트래픽 모니터
        self._shopback_monitors: dict[int, ShopbackMonitor] = {}
        # order.row → 마지막 체크포인트
        self._checkpoints: dict[int, Checkpoint] = {}
        # order.row → "다음으로" 사용자 트리거 이벤트
        # (사용자가 결제 완료 후 행 메뉴에서 "다음으로" 를 누르면 set() 된다)
        self._next_events: dict[int, asyncio.Event] = {}
        # order.row → "기입" 사용자 트리거 이벤트
        # (주문서 페이지 진입 후 사용자가 '기입' 을 눌러야 자동 입력 시작)
        self._fill_events: dict[int, asyncio.Event] = {}
        # order.row → "영문기입" 사용자 트리거 이벤트
        # (받는사람/주소 입력 후 사용자가 '영문기입' 을 눌러야 통관/영문/나머지 입력)
        self._eng_fill_events: dict[int, asyncio.Event] = {}

    # -------------------------------------------------------------
    # User-triggered "next" — 사용자가 결제 완료 후 행 메뉴에서 "다음으로" 클릭 시
    # -------------------------------------------------------------

    def _signal_event(self, ev: asyncio.Event | None) -> None:
        if ev is None or ev.is_set():
            return
        try:
            ev._loop.call_soon_threadsafe(ev.set)  # type: ignore[attr-defined]
        except Exception:
            try:
                ev.set()
            except Exception:
                pass

    def signal_next(self, row: int) -> None:
        """행에 대해 '다음으로' 트리거. 대기 중인 _await_user_next() 가 깨어난다."""
        self._signal_event(self._next_events.get(row))

    def is_awaiting_next(self, row: int) -> bool:
        """해당 행이 사용자 '다음으로' 클릭을 기다리는 중인가."""
        ev = self._next_events.get(row)
        return ev is not None and not ev.is_set()

    def signal_fill(self, row: int) -> None:
        """행에 대해 '기입' 트리거. 대기 중인 _await_user_fill() 이 깨어난다."""
        self._signal_event(self._fill_events.get(row))

    def is_awaiting_fill(self, row: int) -> bool:
        """해당 행이 사용자 '기입' 클릭을 기다리는 중인가."""
        ev = self._fill_events.get(row)
        return ev is not None and not ev.is_set()

    def signal_eng_fill(self, row: int) -> None:
        """행에 대해 '영문기입' 트리거. 대기 중인 _await_user_eng_fill() 이 깨어난다."""
        self._signal_event(self._eng_fill_events.get(row))

    def is_awaiting_eng_fill(self, row: int) -> bool:
        """해당 행이 사용자 '영문기입' 클릭을 기다리는 중인가."""
        ev = self._eng_fill_events.get(row)
        return ev is not None and not ev.is_set()

    async def _await_user_next(self, row: int, timeout_sec: int = 1800) -> None:
        """행에 대해 사용자가 '다음으로' 누를 때까지 대기 (최대 30분)."""
        ev = asyncio.Event()
        self._next_events[row] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_sec)
        finally:
            self._next_events.pop(row, None)

    async def _switch_to_order_page(self, current_page: Page) -> Page:
        """현재 행의 탭이 '구매하기' 클릭으로 주문서로 navigate 됐거나
        opener=current_page 인 새 탭이 열린 경우, 그 탭을 반환.

        병렬 진행 안전: 다른 행이 점유 중인(_pages 에 등록된) 탭은 절대 반환 안 함.
        opener 정보로 '이 current_page 가 직접 연 탭' 만 후보로 삼는다.
        """
        # 1) current_page 자체가 이미 주문서로 navigate 됐으면 그대로 사용
        try:
            url = current_page.url or ""
            if (
                "/pay/" in url or "OrderInfoAction" in url
                or "orderinfo" in url.lower()
            ):
                return current_page
        except Exception:
            pass

        # 2) 다른 행이 점유 중인 탭 집합 (병렬 진행 보호)
        owned_pages = set()
        for r, p in self._pages.items():
            if p is not None and p is not current_page:
                owned_pages.add(p)

        # 3) ctx 의 페이지들 중 'current_page 가 opener' 이면서 주문서 URL 인 것
        try:
            ctx = current_page.context
        except Exception:
            return current_page
        for p in ctx.pages:
            try:
                if p is current_page or p.is_closed():
                    continue
                if p in owned_pages:
                    continue  # 다른 행 소유 → 건너뜀
                # opener 검사 — current_page 가 직접 연 popup/새 탭만 인정
                try:
                    opener = await p.opener()
                except Exception:
                    opener = None
                if opener is not current_page:
                    continue
                purl = p.url or ""
                if (
                    "/pay/" in purl or "OrderInfoAction" in purl
                    or "orderinfo" in purl.lower()
                ):
                    log.info(
                        f"_switch_to_order_page: opener 일치하는 새 주문서 탭 발견 "
                        f"({purl[:80]})"
                    )
                    return p
            except Exception:
                continue
        # 못 찾으면 current_page 그대로 (호출자가 적절히 처리)
        return current_page

    async def _await_order_page(
        self, row: int, page: Page | None = None, timeout_sec: int = 1800
    ) -> None:
        """주문서 페이지 진입까지 대기.

        다음 중 먼저 일어나는 쪽까지 대기:
          1) page 자체 또는 opener=page 인 새 탭이 주문서 URL 로 navigate
             → 정상 return (호출자가 _switch_to_order_page 로 핸들 갱신)
          2) 이 행이 점유한 페이지가 닫힘 → UserInterventionRequired

        병렬 진행 안전: 다른 행이 점유한 탭은 후보에서 제외.
        """
        if page is None:
            return

        async def _wait_order_url() -> str:
            poll = 0.4
            while True:
                try:
                    if page.is_closed():
                        return "closed"
                    # 1) 현재 page 가 주문서로 navigate 됐는가
                    url = (page.url or "").lower()
                    if (
                        "/pay/" in url or "orderinfoaction" in url
                        or "orderinfo" in url
                    ):
                        return "order"
                    # 2) opener=page 인 새 탭이 주문서로 열렸는가
                    owned_pages = {
                        p for r, p in self._pages.items()
                        if p is not None and p is not page
                    }
                    try:
                        ctx = page.context
                        candidates = list(ctx.pages)
                    except Exception:
                        candidates = []
                    for p in candidates:
                        try:
                            if p is page or p.is_closed() or p in owned_pages:
                                continue
                            try:
                                opener = await p.opener()
                            except Exception:
                                opener = None
                            if opener is not page:
                                continue
                            purl = (p.url or "").lower()
                            if (
                                "/pay/" in purl or "orderinfoaction" in purl
                                or "orderinfo" in purl
                            ):
                                return "order"
                        except Exception:
                            continue
                except Exception:
                    pass
                await asyncio.sleep(poll)

        async def _wait_pages_closed() -> str:
            poll = 0.5
            while True:
                try:
                    owned = self._pages.get(row)
                    if owned is None or owned.is_closed():
                        log.info(f"행{row}: 점유 페이지 닫힘 감지 → 행 종료")
                        return "closed"
                    if page.is_closed():
                        log.info(f"행{row}: 대기 페이지 닫힘 감지 → 행 종료")
                        return "closed"
                except Exception:
                    pass
                await asyncio.sleep(poll)

        tasks = {
            asyncio.create_task(_wait_order_url()),
            asyncio.create_task(_wait_pages_closed()),
        }
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        if not done:
            raise asyncio.TimeoutError("주문서 진입 대기 타임아웃")
        for t in done:
            try:
                result = t.result()
            except Exception:
                continue
            if result == "closed":
                raise UserInterventionRequired(
                    "사용자가 페이지를 닫아 행을 종료했습니다.",
                    checkpoint=Checkpoint.AT_PRODUCT_PAGE.value,
                    reset_to_pending=True,
                )

    async def _await_user_fill(
        self, row: int, page: Page | None = None, timeout_sec: int = 1800
    ) -> None:
        """다음 트리거 중 먼저 일어나는 쪽까지 대기:
          1) '기입' 버튼 클릭 → signal_fill(row)
          2) 점유 페이지 닫힘 → UserInterventionRequired

        호출 위치: 주문서 페이지 진입 직후. 사용자가 페이지를 검토하고
        '기입' 버튼을 누르면 자동 입력이 시작된다.
        """
        ev = asyncio.Event()
        self._fill_events[row] = ev

        async def _wait_user() -> str:
            await ev.wait()
            return "user"

        async def _wait_pages_closed() -> str:
            if page is None:
                await asyncio.Event().wait()
                return "closed"
            poll = 0.5
            while True:
                try:
                    owned = self._pages.get(row)
                    if owned is None or owned.is_closed():
                        log.info(f"행{row}: 점유 페이지 닫힘 감지 → 행 종료")
                        return "closed"
                    if page.is_closed():
                        log.info(f"행{row}: 대기 페이지 닫힘 감지 → 행 종료")
                        return "closed"
                except Exception:
                    pass
                await asyncio.sleep(poll)

        try:
            tasks = {
                asyncio.create_task(_wait_user()),
                asyncio.create_task(_wait_pages_closed()),
            }
            done, pending = await asyncio.wait(
                tasks,
                timeout=timeout_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if not done:
                raise asyncio.TimeoutError("기입 대기 타임아웃")
            for t in done:
                try:
                    result = t.result()
                except Exception:
                    continue
                if result == "closed":
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.AT_ORDER_PAGE.value,
                        reset_to_pending=True,
                    )
        finally:
            self._fill_events.pop(row, None)

    async def _await_user_eng_fill(self, row: int, timeout_sec: int = 1800) -> None:
        """받는사람/주소 입력 후 사용자가 '영문기입' 누를 때까지 대기.

        흐름: '기입' 으로 받는사람/주소까지 자동 입력 → 잠시 멈춤 →
        사용자가 결과 확인 후 '영문기입' 누르면 통관/영문/나머지 자동 입력.
        """
        ev = asyncio.Event()
        self._eng_fill_events[row] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout_sec)
        finally:
            self._eng_fill_events.pop(row, None)

    # -------------------------------------------------------------
    # Public entrypoints
    # -------------------------------------------------------------

    async def execute(self, order: Order) -> Order:
        """새 주문 실행. 탭 재사용 정책 — 이전 행의 탭을 **닫지 않고** 재활용한다.

        이유: 행마다 new_page()/close()를 반복하면 macOS 에서 Chrome 앱이
              활성화되면서 포커스가 튄다. 하나의 탭에서 goto 만 반복하면
              포커스는 원래 앱에 머무른다.
        """
        # 현재 행에 연결된 페이지가 있으면 우선 사용
        page = self._pages.get(order.row)

        # 없으면 orphan 페이지(abandon() 이 남긴 탭) 만 인수인계.
        # 주의: 다른 살아있는 행의 탭은 절대 빼앗으면 안 됨 — 병렬 진행 중인
        #       다른 결제/주문서가 닫혀버리는 사고 발생.
        if page is None or page.is_closed():
            donor_page = None
            orphan = self._pages.get(-1)
            if orphan is not None and not orphan.is_closed():
                donor_page = orphan

            if donor_page is not None:
                # orphan(주인 없는) 탭만 인수인계
                self._pages[order.row] = donor_page
                self._pages.pop(-1, None)
                self._checkpoints.pop(-1, None)
                stale_monitor = self._shopback_monitors.pop(-1, None)
                if stale_monitor:
                    try:
                        stale_monitor.stop()
                    except Exception:
                        pass
                page = donor_page
                log.debug(f"orphan 탭 → 행{order.row} 인수인계")
            else:
                # orphan 도 없으면 항상 새 탭. 병렬 진행 중인 다른 행의 탭은 건드리지 않음.
                page = await self.browser.new_page()
                self._pages[order.row] = page
                log.info(f"행{order.row}: 새 탭 생성 (병렬 진행 안전 모드)")

            self._checkpoints[order.row] = Checkpoint.START
            if getattr(self.config, "verify_shopback", True) and order.row not in self._shopback_monitors:
                monitor = ShopbackMonitor(page)
                monitor.start()
                self._shopback_monitors[order.row] = monitor

        return await self._run_from_checkpoint(order, page)

    async def resume(self, order: Order) -> Order:
        """사용자 개입 후 이어서 실행. 체크포인트부터 다시 시작.

        페이지가 닫혔으면 처음부터 새로 연다.
        """
        page = self._pages.get(order.row)
        if page is None or page.is_closed():
            log.info(f"행{order.row}: 페이지가 없어 처음부터 재시작")
            page = await self.browser.new_page()
            self._pages[order.row] = page
            self._checkpoints[order.row] = Checkpoint.START

        return await self._run_from_checkpoint(order, page)

    async def refill_form(self, order: Order) -> Order:
        """주문서 페이지에서 입력값이 초기화된 경우 폼만 다시 채우고 결제 대기로 진입.

        사용 시점:
          - 사용자가 다른 탭(예: 샵백 로그인)에 다녀온 뒤 주문서가 다시 그려져
            배송지/통관번호 등이 초기화된 경우.
        동작:
          - 페이지가 살아있고 주문서 URL 이면 체크포인트를 AT_ORDER_PAGE 로 되돌리고
            FILL_FORM 단계부터 다시 실행한다.
          - 페이지가 없거나 다른 페이지로 이동했으면 처음부터 재시작.
        """
        page = self._pages.get(order.row)
        if page is None or page.is_closed():
            log.info(f"행{order.row}: 페이지 없음 — 처음부터 재시작")
            return await self.resume(order)

        url = ""
        try:
            url = page.url or ""
        except Exception:
            url = ""

        # 11번가 주문서/결제 페이지에 머물러 있는지 확인.
        # 다른 페이지로 이동했으면 resume 으로 fallback.
        is_order_page = (
            "OrderInfoAction" in url
            or "/pay/" in url
            or "Order" in url
        )
        if not is_order_page:
            log.info(f"행{order.row}: 주문서 페이지가 아님 ({url}) — 일반 resume")
            return await self.resume(order)

        # 폼 다시 채우기 위해 체크포인트를 AT_ORDER_PAGE 로 되돌린다.
        log.info(f"행{order.row}: 주문서 폼 재입력")
        self._checkpoints[order.row] = Checkpoint.AT_ORDER_PAGE
        # paused/failed 상태였다면 pending 으로 정상화.
        if order.status in ("paused", "failed"):
            order.status = "pending"
            order.error_message = None
        return await self._run_from_checkpoint(order, page)

    async def abandon(self, order: Order, force_close: bool = False) -> None:
        """해당 주문의 상태 정리.

        기본(force_close=False): 탭을 닫지 않고 orphan 으로 남겨 다음 행이 재사용.
                                 (포커스 탈취 없음, 성공 경로에서 사용)

        force_close=True: 탭을 명시적으로 close(). 에러 경로에서 사용 — 오염된
                          페이지(잘못된 모달/팝업/alert 등) 가 다음 행에 전파되는
                          것을 막는다. 다음 행은 깨끗한 새 탭에서 시작됨.
        """
        self._checkpoints.pop(order.row, None)
        monitor = self._shopback_monitors.pop(order.row, None)
        if monitor:
            try:
                monitor.stop()
            except Exception:
                pass
        page = self._pages.pop(order.row, None)
        if page is None or page.is_closed():
            return

        if force_close:
            # 오염된 탭을 명시적으로 닫는다. orphan 으로 남기지 않는다.
            try:
                await page.close()
                log.debug(f"행{order.row}: 에러 탭 명시적 close() 완료")
            except Exception as exc:
                log.debug(f"탭 close 실패 (무시): {exc}")
        else:
            # row -1 orphan 키로 보관 — 다음 execute 가 인수인계
            self._pages[-1] = page

    def get_shopback_snapshot(self, order: Order):
        """현재까지 수집된 샵백 추적 스냅샷 (없으면 None)."""
        m = self._shopback_monitors.get(order.row)
        return m.snapshot() if m else None

    def has_active_page(self, order: Order) -> bool:
        page = self._pages.get(order.row)
        return page is not None and not page.is_closed()

    async def cleanup_orphan_tabs(self) -> None:
        """프로그램 종료/배치 완료 시점에 재사용 중이던 탭들을 모두 닫는다.

        주문 중에는 포커스 유지를 위해 탭을 닫지 않지만, 모든 주문이
        끝난 뒤엔 열려있던 탭을 정리해야 메모리/프로세스가 누적되지 않는다.
        """
        for key in list(self._pages.keys()):
            p = self._pages.pop(key, None)
            if p and not p.is_closed():
                try:
                    await p.close()
                except Exception:
                    pass
        # 샵백 모니터도 정리
        for row in list(self._shopback_monitors.keys()):
            m = self._shopback_monitors.pop(row, None)
            if m:
                try:
                    m.stop()
                except Exception:
                    pass
        self._checkpoints.clear()

    # -------------------------------------------------------------
    # Core state machine
    # -------------------------------------------------------------

    async def _run_from_checkpoint(self, order: Order, page: Page) -> Order:
        order.status = "in_progress"
        order.error_message = None
        cp = self._checkpoints.get(order.row, Checkpoint.START)

        try:
            # 1) 상품 페이지 + 로그인 체크
            if cp in (Checkpoint.START,):
                self._emit(order, OrderState.OPEN_PRODUCT, "상품 페이지 로드")
                await self._open_product(page, order)

                self._emit(order, OrderState.CHECK_LOGIN, "로그인 상태 확인")
                await self._ensure_logged_in(page)
                await self._detect_abnormal(page)

                self._checkpoints[order.row] = Checkpoint.AT_PRODUCT_PAGE
                cp = Checkpoint.AT_PRODUCT_PAGE

            # 2) 사용자가 옵션·수량 직접 설정 후 '구매하기' 클릭 대기
            #    구매하기 → 주문서 페이지 진입이 감지되면 곧바로 자동 기입.
            if cp == Checkpoint.AT_PRODUCT_PAGE:
                if page.is_closed():
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.AT_PRODUCT_PAGE.value,
                        reset_to_pending=True,
                    )
                self._emit(
                    order,
                    OrderState.CLICK_BUY,
                    "Chrome 에서 옵션·수량 설정 후 '구매하기' 를 누르면 자동으로 주문서 입력이 시작됩니다",
                )

                await self._await_order_page(order.row, page=page)
                if page.is_closed():
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.AT_PRODUCT_PAGE.value,
                        reset_to_pending=True,
                    )
                self._checkpoints[order.row] = Checkpoint.AT_ORDER_PAGE
                cp = Checkpoint.AT_ORDER_PAGE

            # 3) 주문서 진입 후 사용자가 '기입' 버튼 누를 때까지 대기 → 자동 입력
            if cp == Checkpoint.AT_ORDER_PAGE:
                # 주문서 페이지가 떠 있는 활성 탭으로 page 핸들 갱신
                page = await self._switch_to_order_page(page)
                self._pages[order.row] = page

                # '기입' 버튼 노출 — 사용자가 페이지를 검토할 시간을 줌
                self._emit(
                    order,
                    OrderState.CLICK_BUY,
                    "주문서 페이지 — '기입' 버튼을 누르면 주문자 정보 자동 입력",
                )
                await self._await_user_fill(order.row, page=page)
                if page.is_closed():
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.AT_ORDER_PAGE.value,
                        reset_to_pending=True,
                    )

                self._emit(order, OrderState.FILL_FORM, "주문자 정보 자동 입력")
                await self._fill_order_form(page, order)
                self._checkpoints[order.row] = Checkpoint.FORM_FILLED
                cp = Checkpoint.FORM_FILLED

            # 4) 사용자가 결제 완료 후 '다음으로' 누를 때까지 대기
            if cp == Checkpoint.FORM_FILLED:
                # 4-a) 결제 직전 샵백 추적 검증
                self._verify_shopback_before_payment(order)

                # 사용자가 결제하기 클릭 + 카드 인증 → 주문완료 페이지가 뜨면
                # 자동 감지해 주문번호 추출. 별도 '다음으로' 클릭 불필요.
                self._emit(
                    order,
                    OrderState.WAIT_PAYMENT,
                    "결제 진행 후 자동으로 주문번호를 가져옵니다",
                )

                self._emit(order, OrderState.EXTRACT_ORDER_NO, "결제 완료 대기 + 주문번호 추출 중")
                # 카드 인증까지 시간 충분히 주기 위해 timeout 10분.
                order_no, paid_amount = await self._wait_for_order_completion(
                    page, timeout_sec=600
                )
                order.order_number = order_no
                order.ordered_at = datetime.now()
                order.status = "completed"
                # '토탈가격' 컬럼에는 가격 조회로 채운 '개당 단가' 를 유지한다.
                # 실제 결제금액(paid_amount) 은 로그에만 남기고 컬럼에 덮어쓰지 않는다.
                if paid_amount is not None:
                    log.info(
                        f"행{order.row} 실제 결제금액(참고): {paid_amount:,}원 "
                        f"(엑셀에는 개당 단가 {order.total_price} 유지)"
                    )
                if order.total_price is None:
                    try:
                        order.compute_total()
                    except Exception:
                        pass
                self._checkpoints[order.row] = Checkpoint.PAYMENT_DONE

            self._emit(order, OrderState.COMPLETE, f"완료: {order.order_number}")
            # 완료되면 페이지/체크포인트 정리
            await self.abandon(order)
            return order

        except UserInterventionRequired as exc:
            # reset_to_pending=True 면 단순 사용자 취소 (페이지 닫음) → '대기' 로 복귀
            if getattr(exc, "reset_to_pending", False):
                order.status = "pending"
                order.error_message = None
                # 체크포인트도 START 로 리셋 (다시 더블클릭하면 처음부터)
                self._checkpoints.pop(order.row, None)
                self._emit(order, OrderState.PENDING, "사용자가 취소함 (대기 상태로 복귀)")
                log.info(f"행{order.row}: 사용자 취소 → 대기 상태로 복귀")
                await self.abandon(order, force_close=True)
                return order

            # 그 외 진짜 개입 필요 (주소 검색 실패 등) → paused
            order.status = "paused"
            order.error_message = f"사용자 개입 필요: {exc}"
            if exc.checkpoint:
                try:
                    self._checkpoints[order.row] = Checkpoint(exc.checkpoint)
                except ValueError:
                    pass
            self._emit(order, OrderState.PAUSED, str(exc))
            log.warning(f"행{order.row} 일시정지: {exc}")
            if getattr(self.config, "skip_on_pause", True):
                await self.abandon(order, force_close=True)
            return order

        except Exception as exc:
            err_msg = f"{type(exc).__name__}: {exc}"
            if self.config.screenshot_on_error:
                try:
                    order.screenshot_path = await save_error_screenshot(page, order.row)
                except Exception as shot_exc:
                    log.warning(f"스크린샷 저장 실패: {shot_exc}")
            log.error(f"주문 실패 행{order.row}: {err_msg}")
            # 실패 → '대기' 상태로 복귀.
            # 이렇게 하면 사용자가 즉시 다시 더블클릭하여 재시도 가능하고,
            # 별도의 '실패→대기' 리셋 단계가 필요 없다.
            order.status = "pending"
            order.error_message = None
            self._checkpoints.pop(order.row, None)
            self._emit(order, OrderState.PENDING, f"실패 후 대기 복귀: {err_msg}")
            # 오염된 탭을 명시적으로 close() — 다음 시도는 깨끗한 새 탭에서 시작.
            if getattr(self.config, "skip_on_error", True):
                await self.abandon(order, force_close=True)
            return order

    # -------------------------------------------------------------
    # Steps
    # -------------------------------------------------------------

    async def _open_product(self, page: Page, order: Order) -> None:
        await page.goto(order.product_url, wait_until="domcontentloaded")

    async def _ensure_logged_in(self, page: Page) -> None:
        url = page.url.lower()
        login_patterns = self.selectors.get("login_page.url_patterns")
        if any(pat in url for pat in login_patterns):
            raise LoginExpiredError(
                "11번가 로그인이 만료되었습니다. 브라우저에서 직접 로그인 후 재시도하세요."
            )
        if await self.selectors.exists(page, "product_page.login_required", timeout_ms=800):
            raise LoginExpiredError("로그인 팝업이 감지되었습니다.")

    @staticmethod
    def _escaped_order_page(url_before: str, url_after: str) -> bool:
        """클릭/이벤트로 주문서 페이지에서 이탈했는지 판정."""
        if url_before == url_after:
            return False
        bad_markers = (
            "about:blank",
            "customs.go.kr",
            "/my11st/",
            "/myPage",
            "/MyPage",
            "/OrderList",
            "login.11st.co.kr",
        )
        return any(m in url_after for m in bad_markers)

    async def _detect_abnormal(self, page: Page) -> None:
        """주문 진행 가능 여부 사전 체크.

        품절/판매중지 사전 감지는 false positive가 많아 제거.
        가격 조회 단계에서 이미 1차 필터링 됐으니, 여기서는 캡차만 본다.
        실제로 결제 불가능한 상품이면 결제하기 클릭 후 11번가가 에러를 띄울 것이고
        그때 일반 실패로 처리되어 다음 행으로 진행된다.
        """
        if await self.selectors.exists(page, "error_detection.captcha", timeout_ms=800):
            raise CaptchaDetectedError(
                "캡차가 감지되었습니다. 브라우저에서 직접 해결 후 재시도하세요."
            )

    async def _click_buy_now(self, page: Page) -> None:
        # 0) 옵션 있는 상품 대응 — "선택한 옵션 추가하기" 버튼이 있으면 먼저 클릭.
        #    옵션이 URL 로 미리 선택돼 있어도 이 단계를 강제하는 상품(아마존 해외직구관 등)이
        #    있어서, 이 버튼을 눌러야 아래쪽 "구매하기" 가 활성화된다.
        await self._maybe_click_add_option(page)

        # 1) selectors.yaml 의 후보들 순차 시도
        try:
            await self.selectors.click(page, "product_page.buy_now_button")
        except ElementNotFoundError:
            # 2) JS fallback — 페이지에서 "구매/주문" 글자가 들어간
            #    가장 그럴듯한 버튼을 찾아 클릭
            log.info("바로구매 셀렉터 모두 실패 — JS fallback 시도")
            ok = await self._click_buy_now_via_js(page)
            if not ok:
                raise ElementNotFoundError(
                    "바로구매/구매하기 버튼을 찾을 수 없습니다. "
                    "11번가 페이지 구조가 바뀐 것 같습니다."
                )
        # wait_for_load_state 는 제거 — 무거운 리소스까지 기다려서 느리다.
        # URL 변경만 확인하면 충분.
        await self._ensure_on_order_page(page)

    async def _maybe_click_add_option(self, page: Page) -> None:
        """옵션 상품의 "선택한 옵션 추가하기" 버튼이 보이면 클릭.

        없으면 아무 것도 안 하고 조용히 리턴 (옵션 없는 일반 상품).
        있으면 클릭 후 DOM 갱신 잠깐 대기.
        """
        # 1) 셀렉터 경로 — 0.8초 안에 못 찾으면 옵션 없는 상품으로 간주
        try:
            if await self.selectors.exists(
                page, "product_page.add_selected_option", timeout_ms=800
            ):
                await self.selectors.click(
                    page, "product_page.add_selected_option", timeout_ms=2000
                )
                await asyncio.sleep(0.25)
                log.info("'선택한 옵션 추가하기' 클릭 → 구매하기 활성화 대기")
                return
        except ElementNotFoundError:
            pass
        except Exception as exc:
            log.debug(f"옵션 추가 셀렉터 클릭 실패: {exc}")

        # 2) JS fallback — 텍스트로 직접 탐색
        js = r"""
() => {
  const buttons = document.querySelectorAll('button, a');
  for (const el of buttons) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!t) continue;
    if (!/선택한\s*옵션\s*추가/.test(t)) continue;
    // 장바구니/마이페이지 링크 제외
    if (/장바구니|cart|my11st|orderlist/i.test(t)) continue;
    if (el.tagName === 'A') {
      const href = (el.getAttribute('href') || '').toLowerCase();
      if (/cart|my11st|orderlist|mypage/i.test(href)) continue;
    }
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 25) continue;
    el.scrollIntoView({block: 'center'});
    el.click();
    return t;
  }
  return null;
}
"""
        try:
            result = await page.evaluate(js)
            if result:
                await asyncio.sleep(0.25)
                log.info(f"JS fallback 으로 옵션 추가 클릭: {result!r}")
        except Exception as exc:
            log.debug(f"옵션 추가 JS fallback 실패: {exc}")

    @staticmethod
    def _is_order_page(url: str) -> bool:
        """현재 URL 이 11번가 주문/결제 페이지인지."""
        u = url.lower()
        return (
            "buy.11st.co.kr/order" in u
            or "/order/orderinfo" in u
            or "/order/orderhub" in u
            or "order.11st.co.kr" in u
            or "/pay/" in u
        )

    async def _ensure_on_order_page(self, page: Page, timeout_ms: int = 2000) -> None:
        """구매하기 클릭 후 주문 페이지 URL 로 바뀌었는지 확인.

        - 이미 주문 페이지면 즉시 반환.
        - 아니면 상품페이지에 뜬 옵션/수량 모달 안의 2단계 구매 버튼을 한 번 더 클릭.
        - 그래도 안 바뀌면 ElementNotFoundError.
        """
        # 1) 현재 URL 이 이미 주문 페이지면 OK
        if self._is_order_page(page.url):
            return

        # 2) URL 변경을 잠깐 기다림 — wait_for_url(predicate) 가 일부 환경에서
        #    네비게이션 직후 즉시 해석 못 하는 경우가 있어, 직접 폴링한다.
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        while asyncio.get_event_loop().time() < deadline:
            try:
                if self._is_order_page(page.url):
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)

        # 2-b) 마이페이지/주문내역 등으로 이탈했으면 즉시 실패 (2단계 재시도 의미 없음)
        url_lower = page.url.lower()
        if any(m in url_lower for m in (
            "/my11st/", "/mypage", "orderlist", "login.11st.co.kr", "about:blank"
        )):
            raise ElementNotFoundError(
                f"구매 버튼이 잘못된 링크를 클릭 → 마이페이지/로그인으로 이탈 ({page.url}). "
                "상품 페이지 구조가 바뀌었을 수 있습니다. 이 행 건너뛰고 다음 행으로."
            )

        # 3) 모달/팝업 안에 2단계 "주문하기/구매하기" 버튼이 있는 경우 처리
        log.info(
            f"구매 클릭 후 주문 페이지로 이동 안 됨 ({page.url}). 모달 내 2단계 버튼 재시도."
        )
        js = r"""
() => {
  // 화면 안쪽에 있는 버튼 중 "주문하기/구매하기" 텍스트 가진 버튼 클릭
  const buttons = document.querySelectorAll('button, a');
  const cands = [];
  for (const el of buttons) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!t) continue;
    if (!/^(주문하기|구매하기|바로구매|결제하기)$/.test(t)) continue;
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 50 || r.height < 25) continue;
    // 모달(높은 z-index) 혹은 최근 생긴 요소 우선
    const zi = parseInt(s.zIndex || '0', 10) || 0;
    cands.push({el, t, area: r.width * r.height, zi});
  }
  if (!cands.length) return null;
  cands.sort((a, b) => (b.zi - a.zi) || (b.area - a.area));
  cands[0].el.click();
  return cands[0].t;
}
"""
        try:
            clicked = await page.evaluate(js)
            if clicked:
                log.info(f"2단계 주문 버튼 클릭: {clicked!r}")
                await asyncio.sleep(0.15)
        except Exception as exc:
            log.debug(f"2단계 버튼 클릭 실패: {exc}")

        # 4) 최종 URL 재확인 (2차 — 짧게, 폴링 방식). 실패하면 이 상품은 즉시 포기.
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                if self._is_order_page(page.url):
                    return
            except Exception:
                pass
            await asyncio.sleep(0.1)
        raise ElementNotFoundError(
            f"구매 불가: 주문 페이지로 이동하지 못함 ({page.url}). "
            "옵션 선택 필요 / 품절 / 비정상 상품 가능성 → 이 행 건너뛰고 다음 행으로."
        )

    async def _click_buy_now_via_js(self, page: Page) -> bool:
        """페이지 DOM 전체에서 구매 버튼 추론 후 클릭.

        조건:
          - button 또는 a 태그
          - 텍스트가 "구매" / "주문" 포함, "장바구니" / "찜" / "비교" 제외
          - 화면에 보이는 (display 안 숨김) + 크기 있음
          - 클래스/스타일이 primary 버튼처럼 생긴 것 우선 (배경색 진한 것)
        """
        js = r"""
() => {
  const candidates = document.querySelectorAll('button, a');
  const matches = [];
  for (const el of candidates) {
    const t = (el.innerText || el.textContent || '').trim();
    if (!t) continue;
    // "구매" 가 들어간 텍스트만 허용. "주문내역" 같은 헤더 링크 차단을 위해
    // 단순 "주문" 만 있는 건 제외.
    if (!/구매|BUY/i.test(t)) continue;
    // 부정 키워드 제외
    if (/장바구니|찜|비교|취소|확인|닫기|cart|wish|내역|조회|배송|목록/i.test(t)) continue;
    if (t.length > 20) continue;  // 너무 긴 텍스트는 버튼이 아닐 가능성

    // a 태그면 href 검증 — 마이페이지/주문내역 이동 링크는 제외
    if (el.tagName === 'A') {
      const href = (el.getAttribute('href') || '').toLowerCase();
      if (/my11st|orderlist|mypage|logout|\/login/i.test(href)) continue;
    }

    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 50 || rect.height < 25) continue;

    // 점수: 면적 + 텍스트 정확도
    let score = rect.width * rect.height;
    if (/^바로\s*구매$/.test(t)) score *= 5;
    else if (/^구매하기$/.test(t)) score *= 4;
    else if (/^지금\s*구매$/.test(t)) score *= 3;

    matches.push({el, score, text: t, rect});
  }
  if (matches.length === 0) return null;
  matches.sort((a,b) => b.score - a.score);
  const best = matches[0];
  best.el.scrollIntoView({block: 'center'});
  best.el.click();
  return {text: best.text, rect: {x: best.rect.x, y: best.rect.y, w: best.rect.width, h: best.rect.height}};
}
"""
        try:
            result = await page.evaluate(js)
            if result:
                log.info(
                    f"JS fallback 으로 바로구매 클릭 성공: text={result['text']!r}"
                )
                return True
        except Exception as exc:
            log.debug(f"JS fallback 클릭 실패: {exc}")
        return False

    async def _switch_to_direct_input(self, page: Page) -> None:
        """배송지 입력 모드를 '직접입력' 으로 즉시 전환.

        11번가 해외직구 주문 페이지는 기본배송지/최근배송지/직접입력 3가지
        라디오가 있고 기본값이 '기본배송지' 여서 받는사람/주소 input 이
        DOM 에 렌더링되지 않는다. 직접입력 라디오를 선택하면 입력 폼이 나타난다.

        속도 최적화: JS 로 즉시 전환 (셀렉터 timeout 대기 없이).
        실패 시 셀렉터 fallback (총 0.6초 안에 끝남).
        """
        # 1) JS 로 즉시 전환 — 가장 빠르고 a 링크 회피도 자체 처리
        js = r"""
() => {
  // 이미 '직접입력' 라디오가 체크돼 있는지 확인
  for (const r of document.querySelectorAll('input[type="radio"]')) {
    if (!r.checked) continue;
    const lbl = r.closest('label')?.innerText || '';
    if (/직접\s*입력|신규\s*배송지|새로운\s*배송지/.test(lbl)) return 'already-checked';
  }
  // 아니면 '직접입력' 텍스트 라벨/라디오/버튼 클릭
  const all = document.querySelectorAll('label, button, input[type="radio"]');
  for (const el of all) {
    if (el.tagName === 'A') continue;
    if (el.closest && el.closest('a[href]')) continue;
    const txt = (el.innerText || el.textContent || el.value || '').trim();
    if (!/직접\s*입력|신규\s*배송지|새로운\s*배송지/.test(txt)) continue;
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    try {
      el.click();
      if (el.tagName === 'LABEL') {
        const r = el.querySelector('input[type="radio"]');
        if (r) { r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true})); }
      } else if (el.tagName === 'INPUT') {
        el.checked = true;
        el.dispatchEvent(new Event('change', {bubbles: true}));
      }
      return 'clicked:' + txt.slice(0, 20);
    } catch(e) {}
  }
  return null;
}
"""
        try:
            result = await page.evaluate(js)
            if result:
                log.info(f"배송지 모드 → 직접입력 전환 (JS): {result}")
                return
        except Exception as exc:
            log.debug(f"JS 전환 실패: {exc}")

        # 2) JS 가 못 잡았으면 셀렉터로 빠르게 시도 (timeout 짧게)
        try:
            loc = await self.selectors.find(
                page, "order_page.direct_input_radio", timeout_ms=400
            )
            try:
                if not await loc.is_checked():
                    await loc.check()
            except Exception:
                pass
            log.info("배송지 모드 → 직접입력 전환 (라디오)")
            return
        except ElementNotFoundError:
            pass
        except Exception:
            pass

        try:
            await self.selectors.click(
                page, "order_page.direct_input_label", timeout_ms=400
            )
            log.info("배송지 모드 → 직접입력 전환 (라벨)")
            return
        except Exception:
            pass

        log.debug("직접입력 UI 없음 — 기본 흐름으로 진행")

    async def _fill_order_form(self, page: Page, order: Order) -> None:
        """주문서 자동 입력 — 한 번에 모든 필드 채움:
          - 직접입력 모드 전환
          - 받는사람 / 우편번호 / 기본주소 / 상세주소 (주소찾기 팝업 자동 처리)
          - 전화번호
          - 통관번호
          - 영문이름

        주소찾기 자동 클릭 → 검색어 입력 → 결과 자동 선택 로직은
        _ensure_address_filled() 안에서 그대로 수행한다.
        """
        delay = self.config.typing_delay_ms

        # 0) 배송지 모드 → "직접입력" 으로 전환
        await self._switch_to_direct_input(page)

        # 1) 모든 필드 일괄 주입 (이름/전화/우편/주소만 — 통관/영문은 2단계에서)
        # sweep 결과 카테고리 prefix 로 — 후속 _force_fill 호출 스킵 판단.
        # 정상 케이스 (11번가 표준 폼) 에선 sweep 한 번으로 7개 필드 다 들어가서
        # 후속 selector wait 1~2.5초가 통째로 사라진다.
        touched = await self._js_sweep_all_fields(page, order)
        sw = {t.split(":", 1)[0] for t in touched}
        addr_base = order.address_base()
        addr_detail = order.address_detail() or order.address
        log.info(
            f"행{order.row}: 주소 입력 — base={addr_base!r} detail={addr_detail!r}"
            f" (주소찾기 후 base 는 행정표준으로 자동 갱신됨)"
        )

        # 2) 받는사람 / 우편번호 / 기본주소 / 상세주소 — sweep 이 못 잡은 것만 보강
        if "name" not in sw:
            await self._force_fill(page, "order_page.recipient_name", order.name, delay)
        if "postal" not in sw:
            await self._force_fill(page, "order_page.zipcode_input", order.postal_code, delay)
        if "addr_base" not in sw:
            await self._force_fill(page, "order_page.address_base", addr_base, delay)
        if "addr_dtl" not in sw:
            await self._force_fill(page, "order_page.address_detail", addr_detail, delay)

        # 2-b) 주소찾기 팝업 자동 처리
        await self._ensure_address_filled(page, order)

        # 3) 전화번호 — sweep 이 prefix/middle/suffix 다 채웠으면 추가 작업 0
        phone_done = ("ph_middle" in sw) and ("ph_suffix" in sw) and ("prefix" in sw)
        if not phone_done:
            if await self.selectors.exists(page, "order_page.phone", timeout_ms=400):
                digits_only = order.phone.replace("-", "")
                await self._force_fill(page, "order_page.phone", digits_only, delay)
            else:
                parts = order.phone.split("-")
                if len(parts) == 3:
                    if "prefix" not in sw:
                        try:
                            prefix_loc = await self.selectors.find(
                                page, "order_page.phone_prefix", timeout_ms=600
                            )
                            await prefix_loc.select_option(parts[0])
                        except ElementNotFoundError:
                            pass
                    if "ph_middle" not in sw:
                        await self._force_fill(
                            page, "order_page.phone_middle", parts[1], delay
                        )
                    if "ph_suffix" not in sw:
                        await self._force_fill(
                            page, "order_page.phone_suffix", parts[2], delay
                        )

        # 4) 통관번호 — 실패하면 주문을 실패시킨다
        self._emit(order, OrderState.FILL_FORM, "통관번호·영문이름 자동 입력 중")
        await self._fill_customs_id_or_fail(page, order)

        # 5) 영문 이름 — 통합/분리/재시도/JS 강제 주입 4단계 fallback
        eng_name = (order.english_name or "").strip()
        if eng_name:
            await self._fill_english_name(page, eng_name, delay)

        self._emit(
            order,
            OrderState.FILL_FORM,
            "주문정보 자동 입력 완료. 결제하기 버튼을 눌러주세요",
        )

    async def _ensure_address_filled(self, page: Page, order: Order) -> None:
        """주소 입력 — 주소찾기 팝업 자동 처리 + 라벨 보강.

        흐름:
          1) 주소찾기 버튼 자동 클릭 → 팝업 오픈
          2) 팝업 검색창에 검색용 주소(시/도 제외) 자동 입력 + 검색 클릭
          3) 검색 결과 첫 번째 항목 자동 선택 (도로명 우선, 없으면 지번)
          4) 팝업 닫힌 후 상세주소 input 에 수취인 주소 전체 자동 주입
          5) 우편/기본주소도 비어있으면 JS 강제 주입 (안전망)
        """
        # 1) 주소찾기 버튼 자동 클릭 (팝업이 이미 떠있으면 스킵)
        # 즉응형 전략: popup 'page' 이벤트 + 50ms 간격 polling 을 동시에 race.
        # 둘 중 먼저 팝업을 잡는 쪽이 이김 → 팝업이 뜨는 그 즉시 다음 단계로 진입.
        # claimed_popup: 'page' 이벤트로 직접 잡은 popup page 객체. 병렬 실행 시
        # 같은 URL 의 다른 행 popup 과 섞이지 않도록 이 객체를 검색 단계에 넘긴다.
        claimed_popup: Page | None = None
        popup_open = await self._is_address_popup_open(page)
        if not popup_open:
            ctx = page.context
            popup_future: asyncio.Future = asyncio.get_event_loop().create_future()
            loop = asyncio.get_event_loop()

            # 병렬 실행 안전: 이 주문서 page 가 직접 연 popup 만 인정.
            # ctx.on("page") 는 컨텍스트 내 모든 새 탭에 fire 되므로 다른 행이
            # 연 주소찾기 popup 이 이쪽에 잡혀서는 안 된다 (가로채기 방지).
            # opener() 는 async 라 코루틴으로 검사 → run_coroutine_threadsafe 가 아닌
            # 같은 loop 에서 create_task 로 안전하게 실행.
            async def _claim_if_owned(p):
                try:
                    if p is page or p.is_closed():
                        return
                    try:
                        opener = await p.opener()
                    except Exception:
                        opener = None
                    # opener=page → 확실히 이 행의 popup → 채택
                    # opener=None (Windows 일시적) → URL 이 주소찾기 패턴이면 채택
                    # opener=다른 page → 다른 행의 popup → 무시
                    if opener is not None and opener is not page:
                        return
                    if opener is None:
                        url = (p.url or "").lower()
                        is_addr_url = (
                            "/addr/" in url
                            or "searchaddr" in url
                            or "zipcode" in url
                            or "popup" in url
                        )
                        if not is_addr_url:
                            return
                    if not popup_future.done():
                        popup_future.set_result(p)
                except Exception:
                    pass

            def _on_new_page(p):
                # 동기 콜백 → 비동기 opener 검사를 task 로 띄움
                try:
                    loop.create_task(_claim_if_owned(p))
                except Exception:
                    pass

            ctx.on("page", _on_new_page)
            try:
                clicked = await self._click_address_search_button(page)
                if clicked:
                    # 50ms 간격 polling 으로 popup_open 도 동시에 감시.
                    # 'page' 이벤트가 늦게 와도 polling 이 먼저 잡으면 즉시 진입.
                    # 최대 20초까지 기다리되, 잡히는 그 순간 break.
                    async def _poll_open() -> bool:
                        for _ in range(400):  # 50ms * 400 = 20s
                            if await self._is_address_popup_open(page):
                                return True
                            await asyncio.sleep(0.05)
                        return False

                    poll_task = asyncio.create_task(_poll_open())
                    try:
                        done, _pending = await asyncio.wait(
                            {popup_future, poll_task},
                            timeout=20.0,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if popup_future in done:
                            new_p = popup_future.result()
                            log.info(
                                f"행{order.row}: 주소찾기 popup 이벤트 즉시 감지 → "
                                f"URL={new_p.url[:80]}"
                            )
                            popup_open = True
                            claimed_popup = new_p
                            # domcontentloaded 까지만 짧게 — load 까지는 안 기다림
                            try:
                                await new_p.wait_for_load_state(
                                    "domcontentloaded", timeout=2000
                                )
                            except Exception:
                                pass
                        elif poll_task in done and poll_task.result():
                            popup_open = True
                            log.info(
                                f"행{order.row}: 주소찾기 팝업 polling 즉시 감지"
                            )
                        else:
                            log.warning(
                                f"행{order.row}: 주소찾기 팝업이 20초 내 감지되지 않음 — "
                                "그래도 자동 검색·선택 시도"
                            )
                    finally:
                        if not poll_task.done():
                            poll_task.cancel()
                            try:
                                await poll_task
                            except (asyncio.CancelledError, Exception):
                                pass
            finally:
                try:
                    ctx.remove_listener("page", _on_new_page)
                except Exception:
                    pass

        # 2~3) 팝업 감지 여부와 무관하게 자동 검색 + 결과 선택 시도.
        # popup_open=False 라도 별도 창/iframe 어딘가에는 있을 수 있어서.
        ok = False
        try:
            # 지번/도로명 판별
            is_jibun = order.is_jibun_address()
            # 첫 검색어는 지번이든 도로명이든 address_search_query() 사용 (시/도 뺀 base)
            query = order.address_search_query()
            log.info(
                f"행{order.row}: 주소찾기 검색·선택 시작 query={query!r} "
                f"postal={order.postal_code!r} is_jibun={is_jibun}"
            )
            # 11번가 해외직구는 도로명 주소 필수 → 지번으로 검색해도 도로명 결과를 선택
            # (지번 검색 시 도로명 결과에 괄호 안 동 이름이 포함되어 매칭됨)
            ok = await self._auto_search_and_pick_address(
                page, query,
                postal=order.postal_code,
                base_addr=order.address_base(),
                claimed_popup=claimed_popup,
                prefer_jibun=False,  # 항상 도로명 결과 선택
                is_jibun=is_jibun,   # 지번이면 우편번호 검색 우선
            )
            if ok:
                log.info(
                    f"행{order.row}: 주소찾기 팝업 자동 검색·선택 완료 ({query!r})"
                )
                # 보호 sleep 대신 — 주소 hidden(rcvrBaseAddr) 가 채워지길 polling.
                # popup 클릭 직후 11번가 콜백이 hidden 필드를 세팅하는 데 보통 50~150ms 소요.
                # 최대 600ms 만 기다리고 안 차면 어차피 _js_inject_address_fields 가 보강.
                for _ in range(12):
                    try:
                        filled = await page.evaluate(
                            r"""() => {
                              const el = document.querySelector(
                                'input[name="rcvrBaseAddr"], input[name="baseAddr"], input[name="addr"]'
                              );
                              return !!(el && (el.value || '').trim());
                            }"""
                        )
                        if filled:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
            else:
                log.warning(
                    f"행{order.row}: 주소찾기 자동 처리 실패 — 사용자가 직접 선택"
                )
        except Exception as exc:
            log.warning(f"행{order.row}: 주소찾기 자동 처리 예외: {exc}")

        # 4~5) 상세주소 + 비어있는 칸 라벨에 강제 주입
        # 주소찾기가 성공했으면 base(기본주소) 는 popup 이 채운 도로명 값 그대로 보존.
        # 엑셀 원본이 지번이면 우리가 덮어쓰는 순간 addrTypCd=R + base=지번 미스매치가
        # 발생해 11번가가 결제 시 "도로명 주소로 주문하셔야 합니다" alert 로 차단한다.
        await self._js_inject_address_fields(page, order, preserve_base=ok)
        # 주입 직후 React state 반영 약간 — sleep 대신 짧은 polling 으로 충분.

        # 점검 로그
        try:
            status = await page.evaluate(
                r"""() => {
                  function val(sel) {
                    const el = document.querySelector(sel);
                    return el ? (el.value || '').trim() : null;
                  }
                  return {
                    postal: val('input[name="zipcodeTxt"], input[name="rcvrZipNo"], input[name="zipCd"], input[placeholder*="우편번호"]'),
                    base:   val('input[name="rcvrBaseAddr"], input[name="baseAddr"], input[name="addr"], input[placeholder*="기본 주소"]'),
                    detail: val('input[name="rcvrDtlsAddr"], input[name="addrDtl"], input[name="dtlsAddr"], input[placeholder*="상세"]'),
                  };
                }"""
            )
            if status:
                log.info(
                    f"행{order.row}: 주소 점검 — "
                    f"postal={status.get('postal')!r} "
                    f"base={status.get('base')!r} "
                    f"detail={status.get('detail')!r}"
                )
                # 일부 비어있으면 최대 3회 재주입 (Windows 환경 detail 칸 누락 케이스 방어)
                for retry in range(3):
                    if status.get("postal") and status.get("base") and status.get("detail"):
                        break
                    log.info(
                        f"행{order.row}: 주소 일부 비어있음 → 재주입 (시도 {retry + 1}/3)"
                    )
                    await self._js_inject_address_fields(page, order, preserve_base=ok)
                    await asyncio.sleep(0.1)
                    try:
                        status = await page.evaluate(
                            r"""() => {
                              function val(sel) {
                                const el = document.querySelector(sel);
                                return el ? (el.value || '').trim() : null;
                              }
                              return {
                                postal: val('input[name="zipcodeTxt"], input[name="rcvrZipNo"], input[name="zipCd"], input[placeholder*="우편번호"]'),
                                base:   val('input[name="rcvrBaseAddr"], input[name="baseAddr"], input[name="addr"], input[placeholder*="기본 주소"]'),
                                detail: val('input[name="rcvrDtlsAddr"], input[name="addrDtl"], input[name="dtlsAddr"], input[placeholder*="상세"]'),
                              };
                            }"""
                        )
                    except Exception:
                        break
        except Exception:
            pass

    async def _is_address_popup_open(self, page: Page) -> bool:
        """주소찾기 팝업이 열려있는지 (레이어/iframe/별도 popup 창 모두 체크)."""
        # 1) 같은 페이지의 inline layer / iframe
        try:
            inline = await page.evaluate(
                r"""() => {
                  for (const f of document.querySelectorAll('iframe')) {
                    const n = (f.name || f.id || '').toLowerCase();
                    if (/zip|addr|post/i.test(n)) return true;
                  }
                  const layers = document.querySelectorAll(
                    '.layer_addr, [class*="AddressSearch" i], [class*="addr_search" i], '
                    + '[class*="addressLayer" i]'
                  );
                  for (const l of layers) {
                    const cs = window.getComputedStyle(l);
                    if (cs.display !== 'none' && cs.visibility !== 'hidden') return true;
                  }
                  const heads = document.querySelectorAll('h1, h2, h3, h4, .layer_title, .modal_title');
                  for (const h of heads) {
                    const t = (h.innerText || '').trim();
                    if (/^주소\s*찾기$/.test(t)) {
                      const cs = window.getComputedStyle(h);
                      if (cs.display !== 'none' && cs.visibility !== 'hidden') return true;
                    }
                  }
                  return false;
                }"""
            )
            if inline:
                return True
        except Exception:
            pass
        # 2) 별도 popup window — 병렬 실행 시 다른 행의 popup 가로채기 방지.
        # 다른 행이 활성 상태이면 opener 가 정확히 page 인 popup 만 인정.
        # 단독 실행이면 (다른 활성 행 없음) opener=None 도 URL 매칭으로 통과 — Windows 호환.
        try:
            ctx = page.context
            owned_pages = set()
            for r, p2 in self._pages.items():
                if p2 is not None and p2 is not page:
                    owned_pages.add(p2)
            others_active = len(owned_pages) > 0
            for p in ctx.pages:
                if p is page or p.is_closed():
                    continue
                if p in owned_pages:
                    continue
                url = p.url or ""
                if "/addr/" in url or "searchAddr" in url or "zipcode" in url.lower():
                    try:
                        opener = await p.opener()
                    except Exception:
                        opener = None
                    # 병렬: opener=page 인 경우만 인정. opener=None 은 다음 tick 까지 보류.
                    # 단독: opener=None 도 URL 매칭만으로 통과 (기존 동작 유지).
                    if opener is page:
                        return True
                    if opener is None and not others_active:
                        return True
        except Exception:
            pass
        return False

    async def _find_address_popup_pages(self, page: Page) -> list[Page]:
        """주소찾기 별도 창으로 떠 있는 page 들을 반환.

        병렬 진행 안전: opener 가 page (= 이 행의 주문서) 인 popup 만 인정.
        다른 행의 주소찾기 popup 을 가로채지 않는다.
        Windows 에서 opener() 가 일시적으로 None 반환할 수 있어
        opener 정보 없으면 '다른 행 소유 탭이 아닌 경우' 로 fallback 허용.
        """
        out: list[Page] = []
        try:
            ctx = page.context
            # 다른 행이 점유 중인 page 들 — 이건 주소찾기 popup 일 리 없으니 제외용
            owned_pages = set()
            for r, p in self._pages.items():
                if p is not None and p is not page:
                    owned_pages.add(p)
            others_active = len(owned_pages) > 0

            all_urls = []
            for p in ctx.pages:
                if p is page or p.is_closed():
                    continue
                if p in owned_pages:
                    continue  # 다른 행의 주문서/페이지
                url = (p.url or "").lower()
                all_urls.append(url[:80])
                # 11번가 주소찾기 popup URL 패턴 (소문자 비교)
                is_addr_url = (
                    "/addr/" in url
                    or "searchaddr" in url
                    or "zipcode" in url
                    or "popup" in url
                )
                if not is_addr_url:
                    continue
                # opener 검사 — 병렬 실행 시 다른 행 popup 가로채기 방지.
                try:
                    opener = await p.opener()
                except Exception:
                    opener = None
                # opener=page → 확실히 이 행의 popup
                # opener=다른 page → 다른 행의 popup → 제외
                # opener=None: 다른 행이 활성이면 어느 행 소유인지 불확정 → 제외 (안전).
                #              단독 실행이면 URL 매칭으로 통과 (Windows 호환).
                if opener is not None and opener is not page:
                    log.debug(
                        f"_find_address_popup_pages: opener 다름 → skip ({url[:60]})"
                    )
                    continue
                if opener is None and others_active:
                    log.debug(
                        f"_find_address_popup_pages: opener=None+병렬 → skip ({url[:60]})"
                    )
                    continue
                out.append(p)
            log.info(
                f"_find_address_popup_pages: 컨텍스트 page 개수={len(ctx.pages)}, "
                f"매칭={len(out)}, 다른 page URL들={all_urls}"
            )
        except Exception as exc:
            log.warning(f"_find_address_popup_pages 예외: {exc}")
        return out

    async def _click_address_search_button(self, page: Page) -> bool:
        """주소찾기 버튼 자동 클릭. 셀렉터 → JS 텍스트 매칭 fallback."""
        # 1) 셀렉터로 시도
        try:
            if await self.selectors.exists(
                page, "order_page.zipcode_search_button", timeout_ms=800
            ):
                await self.selectors.click(
                    page, "order_page.zipcode_search_button"
                )
                log.info("주소찾기 버튼 클릭 (셀렉터)")
                return True
        except Exception:
            pass
        # 2) JS 텍스트 매칭
        try:
            ok = await page.evaluate(
                r"""() => {
                  const cands = document.querySelectorAll(
                    'a, button, input[type="button"]'
                  );
                  for (const el of cands) {
                    const t = (el.innerText || el.value || '').trim();
                    if (!/^주소\s*찾기$/.test(t)) continue;
                    const cs = window.getComputedStyle(el);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    try { el.click(); return true; } catch(e) {}
                  }
                  return false;
                }"""
            )
            if ok:
                log.info("주소찾기 버튼 클릭 (JS 텍스트)")
                return True
        except Exception as exc:
            log.debug(f"주소찾기 버튼 JS 클릭 실패: {exc}")
        return False

    async def _auto_search_and_pick_address(
        self, page: Page, query: str, postal: str = "", base_addr: str = "",
        claimed_popup: Page | None = None, prefer_jibun: bool = False,
        is_jibun: bool = False,
    ) -> bool:
        """팝업에서 query 로 자동 검색 + 결과 자동 선택.

        선택 우선순위 (prefer_jibun=False, 도로명 주소):
          1) 우편번호(postal) 일치
          2) base_addr 토큰 매칭이 가장 많은 항목 (60% 이상)
          3) '도로명' 라벨 있는 첫 항목
          4) 첫 결과

        선택 우선순위 (prefer_jibun=True, 지번 주소):
          1) '지번' 라벨 + base_addr 토큰 매칭
          2) 지번 번지수 일치 항목

        팝업 형태:
          A) inline layer / 같은 페이지 iframe
          B) 별도 popup window (window.open) — 11번가 buy/addr/searchAddrV2.tmall
        세 곳 다 시도.

        claimed_popup: 호출자(_ensure_address_filled)가 'page' 이벤트로 직접 잡은
        popup page 객체. 병렬 실행 중 같은 URL 의 다른 popup 과 섞이지 않게
        이 page 객체에서만 검색·선택을 수행한다. None 이면 기존 URL 매칭으로 fallback.
        """
        if not query:
            return False

        # 검색 + 결과 선택을 한 번에 처리하는 JS (모든 컨텍스트 공통)
        search_js = r"""
([query]) => {
  function visible(el) {
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  function setVal(el, v) {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype, 'value'
    )?.set;
    if (setter) setter.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }
  // 검색창 후보 — 11번가 popup 의 #searchData 가 가장 우선
  const inputs = document.querySelectorAll(
    'input#searchData, input[name="searchData"], '
    + 'input.input_search_box, '
    + 'input[name="keyword"], input[placeholder*="도로명"], '
    + 'input[placeholder*="주소"], input[placeholder*="검색"], '
    + '.layer_addr input[type="text"], '
    + '[class*="AddressSearch" i] input[type="text"]'
  );
  let searchInput = null;
  for (const i of inputs) {
    if (visible(i)) { searchInput = i; break; }
  }
  if (!searchInput) return 'no-input';

  setVal(searchInput, query);
  searchInput.focus();
  // Enter 키 시뮬레이션
  searchInput.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', keyCode: 13, bubbles: true}));
  searchInput.dispatchEvent(new KeyboardEvent('keypress', {key: 'Enter', keyCode: 13, bubbles: true}));
  searchInput.dispatchEvent(new KeyboardEvent('keyup', {key: 'Enter', keyCode: 13, bubbles: true}));
  // 검색 버튼 클릭
  const buttons = document.querySelectorAll(
    'button.btn_search, button[onclick*="searchRoad"], '
    + 'button, a, input[type="button"]'
  );
  for (const b of buttons) {
    const t = (b.innerText || b.value || '').trim();
    const cls = (b.className || '').toLowerCase();
    const onc = (b.getAttribute('onclick') || '').toLowerCase();
    const isSearch = /^검색$/.test(t) || cls.includes('btn_search') || onc.includes('searchroad');
    if (!isSearch) continue;
    if (!visible(b)) continue;
    try { b.click(); break; } catch(e) {}
  }
  // 11번가 popup: searchRoad() 함수가 전역에 있을 수 있음
  try { if (typeof searchRoad === 'function') searchRoad(); } catch(e) {}
  return 'searched';
}
"""

        pick_js = r"""
([postal, baseAddr, preferJibun]) => {
  function visible(el) {
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  // 탭/리스트 조작 없음 — 결과 선택 시 preferJibun 에 따라 지번/도로명 구분
  // (11번가 팝업은 탭 없이 결과만 표시하는 경우가 많음)

  // 컨테이너: 한 결과 행(도로명+지번+우편번호 묶음).
  // 11번가 popup 의 실제 DOM: <a onclick="$.fn_setAddr('road',...)"> 가
  // 실제 클릭 대상이므로 모든 'fn_setAddr' onclick 을 가진 a 를 직접 모은다.
  let containers = Array.from(document.querySelectorAll(
    'a[onclick*="fn_setAddr"], a[onclick*="setAddr"], '
    + 'a[onclick*="selectAddr"], a[onclick*="chooseAddr"]'
  )).filter(visible);

  // 위 직접 매칭이 0개면 컨테이너 셀렉터로 fallback
  if (containers.length === 0) {
    containers = Array.from(document.querySelectorAll(
      '#roadList tr, #jibunList tr, '
      + 'tbody#roadList > tr, tbody#jibunList > tr, '
      + '.search_result_box tr, .search_result_box li, '
      + '#searchResultBox tr, #searchResultBox li, '
      + '.layer_addr li, .layer_addr tr, '
      + '.list_box tr, .list_box li, '
      + '.result_list_box tr, .result_list_box li, '
      + '[class*="AddressSearch" i] li, '
      + '[class*="AddressSearch" i] tr, '
      + '[class*="result" i] li, '
      + '[class*="result" i] tr, '
      + 'ul[class*="addr" i] li, '
      + '[role="listitem"]'
    )).filter(visible);
  }

  // 도로명/지번 판별 헬퍼 — 11번가의 실제 onclick 패턴 분석 결과:
  //   $.fn_setAddr('zipcode2','N','','','<URL-encoded 주소>',...)
  // 한 anchor 의 onclick 인자에 base 주소 본문이 통째로 박혀 있다.
  // 그 인자 텍스트가 도로명("…로 N", "…길 N") 인지 지번("…동/리/면/읍 N(-N)?") 인지로 판별.
  function _decodeOnclick(a) {
    if (!a || !a.getAttribute) return '';
    const onc = a.getAttribute('onclick') || '';
    // %uXXXX 형태(JS escape) 디코딩
    let dec = onc;
    try {
      dec = onc.replace(/%u([0-9a-fA-F]{4})/g,
        (_, h) => String.fromCharCode(parseInt(h, 16)));
    } catch(e) {}
    // %XX (URI-encoded) 도 처리
    try { dec = decodeURIComponent(dec); } catch(e) {}
    return dec;
  }
  // 도로명 패턴: '○○로/길 + 숫자' (○○대로, ○○로N번길, ○○길 등)
  const ROAD_RE = /[가-힣A-Za-z0-9]+(?:대로|로|길|로\d+번길|길\d+번길)\s*\d/;
  // 지번 패턴: '○○동/리/가/면/읍 + 숫자(-숫자)?' — 도로명 패턴과 상호배타
  const JIBUN_RE = /[가-힣]+(?:동|리|가|면|읍)\s+\d+(?:-\d+)?/;

  function _classify(a) {
    if (!a) return '?';
    const onc = (a.getAttribute && a.getAttribute('onclick') || '');

    // ★ 1순위: anchor 표시 텍스트의 prefix.
    //   11번가 popup 의 결과 행은 좌측에 '도로명' / '지번' 라벨 박스가 붙어있고,
    //   anchor 의 innerText 가 이 라벨로 시작한다.
    const txt = (a.innerText || '');
    const txtNoWs = txt.replace(/\s+/g, '');
    if (/^지번/.test(txtNoWs)) return 'J';
    if (/^도로명/.test(txtNoWs)) return 'R';

    // 2) onclick 첫 인자 — 'road'/'jibun' 처럼 명시적인 경우만 신뢰.
    //   (참고: 11번가는 도로명/지번 anchor 모두에 'zipcode2' 를 쓰는 사례가 있어
    //    'zipcode2' 자체로는 J 단정 불가. 진짜 신호는 위 텍스트 prefix.)
    const firstArgMatch = onc.match(/[$.]?fn_setAddr\s*\(\s*['"]([^'"]+)['"]/i)
                       || onc.match(/setAddr\s*\(\s*['"]([^'"]+)['"]/i);
    if (firstArgMatch) {
      const arg = firstArgMatch[1].toLowerCase();
      if (arg === 'road' || arg === 'roadaddr' || arg === 'r') return 'R';
      if (arg === 'jibun' || arg === 'jibun2' || arg === 'j') return 'J';
      // 'zipcode2' / 'zipcode' 는 도로명·지번 공용 함수명 → 단정 못 함
    }

    // 3) 텍스트 패턴 (한 줄 안에 도로명만/지번만)
    const hasRoad = ROAD_RE.test(txt);
    const hasJibun = JIBUN_RE.test(txt);
    if (hasRoad && !hasJibun) return 'R';
    if (hasJibun && !hasRoad) return 'J';

    // 4) ancestor 식별자
    let cur = a;
    for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
      const id = (cur.id || '').toLowerCase();
      const cls = (cur.className && cur.className.baseVal !== undefined
                   ? cur.className.baseVal : (cur.className || '')).toString().toLowerCase();
      if (/jibun/.test(id) || /jibun/.test(cls)) return 'J';
      if (/road/.test(id) || /road/.test(cls)) return 'R';
    }
    // 5) onclick 의 단일 R/J 플래그 (다른 사이트 호환)
    const oncLower = onc.toLowerCase();
    if (/['"]r['"]/.test(oncLower) || /roadaddr|roadnm|dorono/i.test(oncLower)) return 'R';
    if (/['"]j['"]/.test(oncLower) || /jibun/i.test(oncLower)) return 'J';
    return '?';
  }
  function _isJibun(a) { return _classify(a) === 'J'; }
  function _isRoad(a) { return _classify(a) === 'R'; }

  // preferJibun 에 따라 원하는 타입의 anchor 를 선택
  const _isPreferred = preferJibun ? _isJibun : _isRoad;
  const _isOpposite = preferJibun ? _isRoad : _isJibun;

  // 정규화: 컨테이너 자체가 a 인 경우, 반대 타입 a 를 같은 결과 행의 선호 타입 a 로 교체.
  // 11번가 popup 은 보통 <tr> 또는 <li> 안에 도로명 a / 지번 a 두 개를 둔다.
  containers = containers.map(el => {
    if (!el.matches || !el.matches('a, button')) return el;
    if (_isPreferred(el)) return el;  // 이미 선호 타입이면 유지
    if (!_isOpposite(el)) return el;  // 분류 불명이면 유지
    let row = el;
    for (let i = 0; row && i < 6; i++) {
      if (row.tagName === 'TR' || row.tagName === 'LI') break;
      row = row.parentElement;
    }
    const scope = row || el.parentElement || document;
    const preferredCand = Array.from(scope.querySelectorAll('a[onclick], button[onclick]'))
      .find(a => _isPreferred(a) && visible(a));
    return preferredCand || el;
  });

  // 반대 타입 anchor 만 남은 컨테이너는 매칭 후보에서 제거.
  containers = containers.filter(el => {
    if (!el.matches || !el.matches('a, button')) return true;  // tr/li 묶음은 통과
    return !_isOpposite(el);
  });


  // 진단용: 분류 통계 + 첫 도로명/지번 anchor 샘플 (디버깅용)
  const _diag = (function() {
    let nR = 0, nJ = 0, nU = 0;
    let firstR = null, firstJ = null, firstU = null;
    for (const c of containers) {
      const a = (c.matches && c.matches('a, button')) ? c
              : (c.querySelector && c.querySelector('a[onclick], button[onclick]'));
      if (!a) { nU++; continue; }
      const cls = _classify(a);
      const onc = (a.getAttribute && a.getAttribute('onclick') || '').slice(0, 100);
      const txt = (c.innerText || '').replace(/\s+/g, ' ').slice(0, 60);
      if (cls === 'R') { nR++; if (!firstR) firstR = txt + '|' + onc; }
      else if (cls === 'J') { nJ++; if (!firstJ) firstJ = txt + '|' + onc; }
      else { nU++; if (!firstU) firstU = txt + '|' + onc; }
    }
    return {
      count: containers.length,
      R: nR, J: nJ, U: nU,
      firstR: firstR, firstJ: firstJ, firstU: firstU,
      first: containers.length > 0
        ? (containers[0].innerText || '').replace(/\s+/g, ' ').slice(0, 80)
        : null,
      firstOnclick: (function() {
        if (containers.length === 0) return null;
        const c = containers[0];
        const a = (c.matches && c.matches('a, button')) ? c
                : (c.querySelector && c.querySelector('a[onclick], button[onclick]'));
        const onc = (a && a.getAttribute) ? (a.getAttribute('onclick') || '') : '';
        return onc.slice(0, 120);
      })(),
    };
  })();

  function fire(el) {
    // 11번가 popup 의 <a onclick="$.fn_setAddr(...)"> 는 onclick 속성이
    // jQuery 가 아닌 inline handler 라 el.click() 이 잘 안 먹힘.
    // 1) onclick 속성을 직접 평가 → 2) 그래도 안 되면 mousedown/mouseup/click 시뮬.
    if (!el) return false;
    try {
      const onc = el.getAttribute('onclick');
      if (onc) {
        // 'this' 컨텍스트가 필요한 경우를 위해 Function('return ...').call(el)
        try { (new Function(onc)).call(el); return true; } catch(e) {}
      }
    } catch(e) {}
    try { el.click(); return true; } catch(e) {}
    try {
      ['mousedown','mouseup','click'].forEach(t => {
        el.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true}));
      });
      return true;
    } catch(e) {}
    return false;
  }

  function pick(container, label) {
    // 컨테이너에서 진짜 클릭해야 할 요소 결정.
    // preferJibun 에 따라 지번/도로명을 우선 선택.
    // 위에서 정의한 _classify / _isRoad / _isJibun / _isPreferred / _isOpposite 를 사용.

    let target = null;
    if (container.matches && container.matches('a, button, [role="button"]')) {
      target = container;
      // 컨테이너 자체가 반대 타입이면, 형제 중 선호 타입으로 교체 시도
      if (_isOpposite(target)) {
        const parent = container.parentElement;
        if (parent) {
          const cand = Array.from(parent.querySelectorAll('a[onclick], button[onclick]'))
            .find(_isPreferred);
          if (cand) target = cand;
        }
      }
    } else {
      const anchors = Array.from(
        container.querySelectorAll('a[onclick], button[onclick], a, button, [role="button"]')
      );
      // preferJibun 에 따라 선호 타입 우선, 그 다음 반대 아닌 것, 마지막으로 아무거나
      target = anchors.find(_isPreferred)
            || anchors.find(a => !_isOpposite(a))
            || anchors[0]
            || container;
    }
    const ok = fire(target);
    if (ok) {
      const t = (container.innerText || '').replace(/\s+/g, ' ').slice(0, 80);
      const kind = _isRoad(target) ? 'R' : (_isJibun(target) ? 'J' : '?');
      const onc = (target && target.getAttribute && target.getAttribute('onclick')) || '';
      const ocSlice = onc.slice(0, 80).replace(/\s+/g, ' ');
      return label + '[' + kind + ']:' + t + ' | oc=' + ocSlice;
    }
    return null;
  }

  // 컨테이너 텍스트 — 자기 자신이 a 면 부모 tr/li 전체 텍스트도 봐야 우편번호 매칭됨
  function fullText(el) {
    let cur = el;
    for (let i = 0; cur && i < 4; i++) {
      const tag = cur.tagName;
      if (tag === 'TR' || tag === 'LI') return cur.innerText || '';
      cur = cur.parentElement;
    }
    return el.innerText || '';
  }

  // baseAddr 에서 시/도 prefix 제거 — 토큰 매칭 시 시/도가 포함되면 부정확
  function _stripSido(b) {
    if (!b) return '';
    return b.replace(
      /^(서울특별시|서울시|서울|부산광역시|부산시|부산|대구광역시|대구시|대구|인천광역시|인천시|인천|광주광역시|광주시|광주|대전광역시|대전시|대전|울산광역시|울산시|울산|세종특별자치시|세종시|세종|경기도|경기|강원특별자치도|강원도|강원|충청북도|충북|충청남도|충남|전북특별자치도|전라북도|전북|전라남도|전남|경상북도|경북|경상남도|경남|제주특별자치도|제주도|제주)\s+/,
      ''
    ).trim();
  }
  const baseAddrNorm = _stripSido(baseAddr || '');

  // baseAddr 에서 도로명 토큰 추출 — '○○로' / '○○길' / '○○대로' / '○○로N번길'
  // 같은 우편번호 안 여러 도로명 중 정답을 가르려면 이 도로명 토큰이 결과 텍스트에
  // 반드시 포함돼야 한다. (예: '어곡공단로' 가 결과에 없으면 '대동로' 결과는 다른 주소)
  function _extractRoadToken(b) {
    if (!b) return '';
    // 'N번길' 같은 합성도 포함. 우선 가장 긴 매칭부터.
    const m1 = b.match(/[가-힣A-Za-z0-9]+로\s*\d+번길/);
    if (m1) return m1[0].replace(/\s+/g, '');
    const m2 = b.match(/[가-힣A-Za-z0-9]+(?:대로|로|길)/);
    if (m2) return m2[0];
    return '';
  }
  // baseAddr 에서 번지수 추출 — 도로명 토큰 '바로 뒤'의 번지.
  // 도로명에 숫자가 들어간 경우(예: '사평대로26길', '선릉로130길')
  // 단순히 /(?:대로|로|길)\s*(\d+)/ 로는 도로명 안 숫자(26)가 잡혀서 안 됨.
  // → 토큰 전체를 먼저 매칭하고 그 다음 번지를 가져옴.
  function _extractRoadNumber(b, token) {
    if (!b || !token) return '';
    const esc = token.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(esc + '\\s*(\\d+(?:-\\d+)?)');
    const m = b.match(re);
    return m ? m[1] : '';
  }
  const baseRoadToken = _extractRoadToken(baseAddrNorm);
  const baseRoadNumber = _extractRoadNumber(baseAddrNorm, baseRoadToken);
  const baseHasRoad = !!baseRoadToken;

  // 매칭 정책 — "원래 주소와 다른 주소" 가 자동 선택되는 사고 차단:
  //   preferJibun=false (도로명 주소):
  //     case A) base 가 도로명을 포함 (예: '양산시 어곡공단로 143')
  //             → 결과 텍스트에 그 도로명 토큰('어곡공단로')이 반드시 있어야 매칭.
  //   preferJibun=true (지번 주소):
  //     case B) base 가 지번 ('고양시 일산동구 식사동 1565')
  //             → 지번 결과 중 동/리 + 번지수 일치 항목 선택.
  function _evaluateAnchor(el, postalMatched = false) {
    const a = (el.matches && el.matches('a, button')) ? el
            : (el.querySelector && el.querySelector('a[onclick], button[onclick]'));
    if (!a) return null;
    const cls = _classify(a);
    // preferJibun 에 따라 반대 타입은 제외
    if (preferJibun && cls === 'R') return null;  // 지번 원할 때 도로명 제외
    if (!preferJibun && cls === 'J') return null;  // 도로명 원할 때 지번 제외
    const aTxt = (a.innerText || '').replace(/\s+/g, ' ');

    if (baseHasRoad) {
      if (!aTxt.includes(baseRoadToken)) return null;  // 다른 도로명 결과 → 거부
      let bonus = 0;
      if (baseRoadNumber) {
        const escTok = baseRoadToken.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const numRe = new RegExp(escTok + '\\s*' + baseRoadNumber + '\\b');
        if (numRe.test(aTxt)) {
          bonus = 10;
        } else {
          // 같은 도로명 뒤에 다른 번지수가 적힌 anchor 는 거부.
          //   예) base='사평대로26길 103' vs anchor='사평대로26길 26-4'
          //   → 같은 도로의 다른 번지를 우연히 클릭하는 사고 차단.
          const anyNumRe = new RegExp(escTok + '\\s*(\\d+(?:-\\d+)?)');
          const m = aTxt.match(anyNumRe);
          if (m && m[1] && m[1] !== baseRoadNumber) return null;
        }
      }
      const tokens = baseAddrNorm.split(/\s+/).filter(t => t.length >= 1);
      let score = 0;
      for (const tok of tokens) if (aTxt.includes(tok)) score++;
      // anchor 자체 점수가 낮으면 묶음/형제 텍스트로도 추가 점수 (시·도 정보가 부모 행에만 있는 경우)
      if (score < tokens.length) {
        const groupTxt = fullText(el).replace(/\s+/g, ' ');
        let probe = aTxt + ' ' + groupTxt;
        try {
          const parent = a.parentElement;
          if (parent) {
            const sibs = Array.from(parent.querySelectorAll(
              'a[onclick*="fn_setAddr"], a[onclick*="setAddr"]'
            ));
            const idx = sibs.indexOf(a);
            if (idx >= 0) {
              if (idx - 1 >= 0) probe += ' ' + (sibs[idx - 1].innerText || '');
              if (idx + 1 < sibs.length) probe += ' ' + (sibs[idx + 1].innerText || '');
            }
          }
        } catch(e) {}
        score = 0;
        for (const tok of tokens) if (probe.includes(tok)) score++;
      }
      return { el: el, anchor: a, score: score + bonus, total: tokens.length, bonus: bonus, cls: cls };
    }

    // base 가 지번 — preferJibun=true 일 때는 지번 결과(cls='J')를 선택.
    // 지번 결과가 아니면 도로명 결과에서 동 이름이 포함된 것을 찾는 fallback.
    // base 의 지번 번지(예: '1887-4') 와 동(예: '중산동') 추출
    const jibunNumMatch = baseAddrNorm.match(/(?:동|리|면|읍|가)\s*(\d+(?:-\d+)?)/);
    const baseJibunNum = jibunNumMatch ? jibunNumMatch[1] : '';
    const dongMatch = baseAddrNorm.match(/[가-힣]+(?:동|리|면|읍|가)/);
    const baseDong = dongMatch ? dongMatch[0] : '';

    // 결과 묶음 텍스트 — 다음 계층 모두 합쳐 검사.
    //  1) anchor 자기 텍스트
    //  2) 가장 가까운 tr/li 조상 텍스트 (한 행 묶음 — 도로명+지번 모두 포함)
    //  3) 행 안의 모든 fn_setAddr anchor 텍스트 (별도 td에 있는 지번 anchor 포함)
    const groupTxt = fullText(el).replace(/\s+/g, ' ');
    let probeTxt = aTxt + ' ' + groupTxt;
    try {
      // 같은 tr/li 행 안의 모든 fn_setAddr anchor 텍스트를 합침
      // (도로명 anchor 와 지번 anchor 가 별도 td 에 있을 수 있음)
      let row = a;
      for (let i = 0; row && i < 6; i++) {
        if (row.tagName === 'TR' || row.tagName === 'LI') break;
        row = row.parentElement;
      }
      if (row && (row.tagName === 'TR' || row.tagName === 'LI')) {
        const rowAnchors = Array.from(row.querySelectorAll(
          'a[onclick*="fn_setAddr"], a[onclick*="setAddr"]'
        ));
        for (const ra of rowAnchors) {
          if (ra !== a) {
            probeTxt += ' ' + (ra.innerText || '');
          }
        }
      }
    } catch(e) {}

    // 우편번호가 정확히 일치하면 동 이름 매칭을 완화 (우편번호가 이미 주소를 특정함)
    // 우편번호 불일치 시에만 동 이름 필수 매칭
    if (!postalMatched && baseDong && !probeTxt.includes(baseDong)) return null;

    let score = 0;
    let bonus = 0;
    const tokens = baseAddrNorm.split(/\s+/).filter(t => t.length >= 1);
    for (const tok of tokens) if (probeTxt.includes(tok)) score++;
    // ★ 핵심 보너스: 같은 묶음/형제에 base 의 지번 번지(예: '1887-4') 가
    //   그대로 표기되면 거의 정답.
    if (baseJibunNum && probeTxt.includes(baseJibunNum)) bonus += 20;
    // 우편번호가 일치하면 임계값을 30%로 낮춤 (우편번호가 이미 주소를 특정)
    // 우편번호 불일치 시에도 동 이름이 매칭되면 50%로 완화 (지번 번호는 도로명 결과에 없음)
    let threshold = 0.7;
    if (postalMatched) {
      threshold = 0.3;
    } else if (baseDong && probeTxt.includes(baseDong)) {
      // 동 이름이 정확히 매칭되면 threshold를 50%로 낮춤
      // (지번 번호 "823-13" 같은 토큰은 도로명 결과에 없을 수 있음)
      threshold = 0.5;
    }
    if (score + bonus < Math.ceil(tokens.length * threshold)) return null;
    return { el: el, anchor: a, score: score + bonus, total: tokens.length, bonus: bonus, cls: cls, postalMatched: postalMatched };
  }

  // 디버깅: 평가 과정 진단 정보 수집
  let _evalDiag = { postalChecked: 0, postalMatched: 0, evalTotal: 0, evalPassed: 0,
                   firstReject: null, baseDong: '', baseJibunNum: '', baseHasRoad: baseHasRoad };

  // 1) 우편번호 매칭 컨테이너 안에서 후보 평가. 그 다음 전체에서.
  let candidates = [];
  if (postal) {
    for (const el of containers) {
      const t = fullText(el).replace(/\s+/g, ' ');
      _evalDiag.postalChecked++;
      if (!t.includes(postal)) continue;
      _evalDiag.postalMatched++;
      // 우편번호가 일치하면 postalMatched=true → 동 이름 매칭 완화
      const ev = _evaluateAnchor(el, true);
      if (ev) candidates.push(ev);
    }
  }
  if (candidates.length === 0) {
    for (const el of containers) {
      _evalDiag.evalTotal++;
      // 우편번호 불일치 → postalMatched=false → 동 이름 필수 매칭
      const ev = _evaluateAnchor(el, false);
      if (ev) {
        candidates.push(ev);
        _evalDiag.evalPassed++;
      } else if (!_evalDiag.firstReject) {
        // 첫 번째 거부 이유 기록
        const a = (el.matches && el.matches('a, button')) ? el
                : (el.querySelector && el.querySelector('a[onclick], button[onclick]'));
        const cls = a ? _classify(a) : '?';
        const aTxt = a ? (a.innerText || '').replace(/\s+/g, ' ').slice(0, 60) : '';
        const dongMatch = baseAddrNorm.match(/[가-힣]+(?:동|리|면|읍|가)/);
        const baseDong = dongMatch ? dongMatch[0] : '';
        _evalDiag.baseDong = baseDong;
        const jibunNumMatch = baseAddrNorm.match(/(?:동|리|면|읍|가)\s*(\d+(?:-\d+)?)/);
        _evalDiag.baseJibunNum = jibunNumMatch ? jibunNumMatch[1] : '';
        _evalDiag.firstReject = cls + ':' + aTxt + '|dong=' + baseDong + ',hasDong=' + aTxt.includes(baseDong);
      }
    }
  }

  if (candidates.length > 0) {
    // 정렬: 점수 내림차순, 동률이면 DOM 순서(원본 인덱스) 오름차순.
    // 11번가 popup 결과는 '관련도 높은 순'으로 정렬돼있어 첫 결과가 정답 확률 높음.
    candidates.forEach((c, i) => { c._idx = i; });
    candidates.sort((a, b) => {
      if (b.score !== a.score) return b.score - a.score;
      return a._idx - b._idx;
    });
    const top = candidates[0];

    // base 가 도로명을 포함한 케이스 — 도로명 토큰 강제 매칭이 이미 적용됐으므로
    // 후보 여러 개여도 점수 1등을 그대로 채택 (다른 도로명 anchor 는 _evaluateAnchor 에서 거부됨).
    // base 가 지번 + 후보 여러 개 + 보너스(번지 일치)도 없는 경우만 모호함 진단:
    //   1등이 압도적이면 채택, 아니면 DOM 첫 후보 채택 (사용자 개입 없이 자동화).
    if (!baseHasRoad && candidates.length > 1 && top.bonus === 0) {
      const second = candidates[1];
      const dominant = (top.score - second.score >= 2) || (top.score >= second.score * 1.5);
      if (!dominant) {
        // DOM 순서로 가장 앞선(인덱스 낮은) 후보를 정답으로 채택.
        // 11번가 popup 의 결과 정렬 순서가 관련도 기준이므로 첫 결과가 정답 확률 가장 높음.
        candidates.sort((a, b) => a._idx - b._idx);
        const firstCand = candidates[0];
        const r = pick(firstCand.el,
          'addr-match-DT-first(' + firstCand.score + '/' + firstCand.total + ')');
        if (r) return r;
      }
    }

    const label = baseHasRoad
      ? 'addr-match-RT(' + top.score + '/' + top.total + (top.bonus ? '+num' : '') + ')'
      : 'addr-match-DT(' + top.score + '/' + top.total + (top.bonus ? '+num' : '') + ')';
    const r = pick(top.el, label);
    if (r) return r;
  }
  // 매칭 후보 없음 — 호출자가 검색어 시퀀스 다음 단계로 재시도
  // 결과 0건이거나 모두 클릭 실패 → 진단 정보 반환 (onclick 패턴 + 분류 통계 포함)
  return 'no-pick(count=' + _diag.count
       + ', R=' + _diag.R + ', J=' + _diag.J + ', U=' + _diag.U
       + ', postChk=' + _evalDiag.postalChecked + ', postMatch=' + _evalDiag.postalMatched
       + ', evalT=' + _evalDiag.evalTotal + ', evalP=' + _evalDiag.evalPassed
       + ', baseRoad=' + _evalDiag.baseHasRoad
       + ', reject=' + (_evalDiag.firstReject || 'none')
       + ', first=' + (_diag.first || '?')
       + ', oc=' + (_diag.firstOnclick || '?') + ')';
}
"""

        async def try_in_context(target, query_override: str | None = None) -> bool:
            """target 은 Page 또는 Frame. 검색 + 결과 선택 시도.

            견고함을 위한 polling 전략:
              1) 검색창 등장 대기 — 최대 8초 (팝업이 천천히 로드돼도 잡음)
              2) 검색 입력·실행 — 검색창이 잡히면 1회 시도
              3) 결과 등장 + 선택 polling — 최대 15초 (느린 네트워크 대응)
              4) 결과가 안 뜨면 검색 재시도 (최대 3회) — 첫 시도가 너무 빨라
                 input 이 아직 React 상태 갱신 안 한 케이스 방어
            """
            effective_query = query_override or query
            try:
                ctx_url = ""
                try:
                    ctx_url = target.url if not callable(target.url) else target.url()
                except Exception:
                    pass
                log.info(
                    f"주소찾기 try_in_context 시작 query={effective_query!r} ({ctx_url[:80]})"
                )
            except Exception:
                pass

            # 1) 검색창 등장 대기 + 검색 실행 — 최대 3회 재시도
            # 100ms 간격 polling 으로 검색창이 잡히는 그 즉시 입력 + 검색 실행.
            search_ok = False
            for search_attempt in range(3):
                # 검색창이 나타날 때까지 polling (최대 15초 — 팝업 로드 늦어도 OK)
                input_ready = False
                for _ in range(150):
                    try:
                        r = await target.evaluate(search_js, [effective_query])
                    except Exception as exc:
                        log.debug(f"search_js 실패: {exc}")
                        await asyncio.sleep(0.1)
                        continue
                    if r == 'no-input':
                        await asyncio.sleep(0.1)
                        continue
                    log.info(
                        f"주소찾기 검색 결과 (시도 {search_attempt + 1}/3): {r}"
                    )
                    input_ready = True
                    search_ok = True
                    break
                if not input_ready:
                    log.warning(f"주소찾기 검색창이 15초 내 안 잡힘 (재시도 {search_attempt + 1}/3)")
                    return False

                # 2) 결과 등장 + 선택 polling (최대 15초)
                last_diag = None
                got_result = False
                for attempt in range(30):
                    await asyncio.sleep(0.5)
                    try:
                        r2 = await target.evaluate(pick_js, [postal, base_addr, prefer_jibun])
                    except Exception as exc:
                        log.debug(f"pick_js 실패 (attempt {attempt}): {exc}")
                        continue
                    # 디버깅: 결과가 있을 때 로그 (no-pick 진단 포함)
                    if r2 and 'count=' in r2 and 'count=0' not in r2:
                        log.info(f"pick_js 시도 {attempt}: {r2}")
                    if r2 and not r2.startswith('no-pick'):
                        log.info(f"주소찾기 결과 선택 성공: {r2}")
                        return True
                    last_diag = r2
                    # no-pick(count=N, ...) 에서 count > 0 이면 결과는 떴으니
                    # 클릭 fail 만 한 것 → 더 polling 한다고 결과 안 바뀜.
                    if r2 and 'count=' in r2 and 'count=0' not in r2:
                        got_result = True

                if got_result:
                    # 결과는 떴지만 도로명 후보가 없거나 클릭 실패한 경우.
                    # R=0 이면 현재 검색어로는 도로명 결과 부재 → 다른 검색어로 재시도.
                    is_r_zero = bool(last_diag and 'R=0' in last_diag)
                    log.warning(
                        f"주소찾기: 결과는 떴지만 클릭 실패 — {last_diag}"
                    )
                    if is_r_zero:
                        return False  # 호출자가 다른 검색어로 재시도하게 fail 반환
                    return False
                # 결과 자체가 0건 → 검색 재시도 (검색이 너무 빨라 입력 안 먹은 케이스)
                log.info(
                    f"주소찾기: 결과 0건, 검색 재시도 — last={last_diag}"
                )
                await asyncio.sleep(0.5)

            log.warning("주소찾기 결과 선택 최종 실패")
            return False

        # 1) 별도 popup window — claimed_popup 이 있으면 그것만 사용 (병렬 충돌 방지).
        # 호출자가 'page' 이벤트로 직접 붙잡은 page 객체라 같은 URL 의 다른 행 popup
        # 과 절대 섞이지 않는다. 없으면 URL 매칭으로 fallback.
        popup_pages: list[Page] = []
        if claimed_popup is not None and not claimed_popup.is_closed():
            popup_pages = [claimed_popup]
            log.info(
                f"주소찾기: claimed_popup 사용 (URL={(claimed_popup.url or '')[:80]})"
            )
        else:
            for _ in range(400):  # 50ms * 400 = 20s
                popup_pages = await self._find_address_popup_pages(page)
                if popup_pages:
                    break
                await asyncio.sleep(0.05)

        # 검색어 후보 시퀀스 — 결과 0건일 때 차례로 시도
        # 지번 주소인 경우:
        #   1순위: query (지번 주소 — "평택시 서정동 823-13" 처럼 정확한 검색)
        #   2순위: 우편번호 (fallback — 지번 검색 결과가 없을 때)
        #   3순위: 동까지만 잘라낸 base
        #   4순위: 시/구만 남긴 base — 마지막 fallback
        # 도로명 주소인 경우:
        #   1순위: query (시/도 뺀 base 주소)
        #   2순위: 우편번호
        #   (이하 동일)
        query_candidates: list[str] = []
        if is_jibun:
            # 지번 주소: 지번 쿼리 먼저 (정확한 결과), 우편번호는 fallback
            # 우편번호로 검색하면 같은 우편번호 공유하는 모든 주소가 나와서 부정확
            if query:
                query_candidates.append(query)
            if postal and postal not in query_candidates:
                query_candidates.append(postal)
        else:
            # 도로명 주소: 기존 순서 유지
            if query:
                query_candidates.append(query)
            if postal and postal not in query_candidates:
                query_candidates.append(postal)

        import re as _re

        def _shorten_base(b: str) -> list[str]:
            outs: list[str] = []
            tokens = (b or "").split()
            # 동/리/면/읍 토큰까지만 잘라낸 검색어
            for i, tok in enumerate(tokens):
                if any(tok.endswith(suf) for suf in ("동", "리", "면", "읍")):
                    outs.append(" ".join(tokens[: i + 1]))
                    break
            # 시/군/구 토큰까지만 잘라낸 검색어
            for i, tok in enumerate(tokens):
                if any(tok.endswith(suf) for suf in ("시", "군", "구")):
                    outs.append(" ".join(tokens[: i + 1]))
                    break
            return outs

        def _road_query(b: str) -> list[str]:
            """base 에 도로명이 있으면 '도로명 + 번지' 검색어로 정확 매칭 유도.
            예: '충청남도 천안시 동남구 병천면 충절로 1896' →
                ['충절로 1896', '병천면 충절로 1896'].
            """
            if not b:
                return []
            outs: list[str] = []
            m = _re.search(r"([가-힣A-Za-z0-9]+(?:대로|로|길))\s*(\d+(?:-\d+)?)", b)
            if not m:
                return outs
            road = m.group(1)
            num = m.group(2)
            outs.append(f"{road} {num}")
            # 동/면/읍/리 prefix 도 함께 — 동명도시 도로명 동음이의 방어
            tokens = b.split()
            for i, tok in enumerate(tokens):
                if any(tok.endswith(suf) for suf in ("동", "리", "면", "읍")):
                    outs.append(f"{tok} {road} {num}")
                    break
            return outs

        if base_addr:
            # base_addr 에서 시/도 제거한 버전 사용
            base_no_sido = _re.sub(
                r"^(서울특별시|서울시|서울|부산광역시|부산시|부산|대구광역시|대구시|대구|"
                r"인천광역시|인천시|인천|광주광역시|광주시|광주|대전광역시|대전시|대전|"
                r"울산광역시|울산시|울산|세종특별자치시|세종시|세종|경기도|경기|"
                r"강원특별자치도|강원도|강원|충청북도|충북|충청남도|충남|"
                r"전북특별자치도|전라북도|전북|전라남도|전남|경상북도|경북|"
                r"경상남도|경남|제주특별자치도|제주도|제주)\s+",
                "", base_addr
            ).strip()
            # ★ 도로명+번지 검색을 우편번호 다음 우선순위로 (case A 정확도 향상)
            for s in _road_query(base_no_sido):
                if s and s not in query_candidates:
                    query_candidates.append(s)
            for s in _shorten_base(base_no_sido):
                if s and s not in query_candidates:
                    query_candidates.append(s)

        log.info(f"주소찾기 검색어 후보: {query_candidates} (is_jibun={is_jibun})")

        async def _attempt(target, q: str) -> bool:
            return await try_in_context(target, query_override=q)

        for popup_page in popup_pages:
            # domcontentloaded 까지만 짧게 — load 완료는 검색 polling 이 처리.
            try:
                await popup_page.wait_for_load_state(
                    "domcontentloaded", timeout=2000
                )
            except Exception:
                pass
            for q in query_candidates:
                if await _attempt(popup_page, q):
                    return True
            # popup main_frame 이 안 됐으면 그 안의 iframe 시도
            for frame in popup_page.frames:
                if frame is popup_page.main_frame:
                    continue
                for q in query_candidates:
                    if await _attempt(frame, q):
                        return True

        # 2) 같은 page 의 main + iframe
        for q in query_candidates:
            if await _attempt(page, q):
                return True
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            for q in query_candidates:
                if await _attempt(frame, q):
                    return True
        return False

    async def _js_inject_address_fields(
        self, page: Page, order: Order, preserve_base: bool = False,
    ) -> None:
        """우편번호 / 기본주소 / 상세주소 input 에 JS 로 직접 값 주입.

        - readonly/disabled 강제 해제
        - 기존 값 무시하고 무조건 덮어쓰기 (이전 자동화 시도가 잘못된 값을 넣었을 수 있음)
        - placeholder/name/id/aria-label 어디든 매칭되면 주입
        - input/change 이벤트 dispatch (React/Vue 의 controlled input 도 동작)

        preserve_base=True 일 때:
          - 주소찾기 popup 이 도로명 base + addrTypCd=R 을 함께 세팅한 직후에 호출됨.
          - 우리가 가진 엑셀 base 가 지번이면 덮어쓰는 순간 addrTypCd=R + base=지번
            미스매치가 생겨 11번가가 결제 시 alert 로 차단한다.
          - 그래서 이 모드에선 base 칸은 비어있을 때만 fallback 으로 채우고,
            popup 이 채운 값은 절대 덮어쓰지 않는다. 우편번호/상세주소만 안전하게 채운다.
        """
        # base = 도로명+번지수까지, detail = 아파트명/동/호 등 나머지.
        # 분리 못하면 detail 에 원본 전체 (정보 손실 방지).
        addr_base = order.address_base()
        addr_detail = order.address_detail() or order.address
        try:
            touched = await page.evaluate(
                r"""([postal, baseAddr, detailAddr, preserveBase]) => {
                  function setVal(el, v) {
                    try { el.removeAttribute('readonly'); } catch(e){}
                    try { el.removeAttribute('disabled'); } catch(e){}
                    const setter = Object.getOwnPropertyDescriptor(
                      HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (setter) setter.call(el, v); else el.value = v;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                  }
                  function hayOf(el) {
                    return [el.name||'', el.id||'', el.placeholder||'',
                            el.getAttribute('aria-label')||''].join(' ').toLowerCase();
                  }
                  const touched = [];
                  for (const el of document.querySelectorAll('input')) {
                    const t = (el.type||'').toLowerCase();
                    if (['checkbox','radio','submit','button','file','image','hidden'].includes(t)) continue;
                    const hay = hayOf(el);
                    // 우편번호 — 무조건 덮어씀 (popup 이 채운 값과 동일한 게 정상)
                    if (/(zip|post|우편)/.test(hay)) {
                      setVal(el, postal);
                      touched.push('postal:' + (el.name || el.id || el.placeholder));
                      continue;
                    }
                    // 상세주소 (먼저 매칭 — 'addr' 매칭이 base 로 빠지지 않게)
                    if (/(상세.*주소|상세.*건물|addr.*dtl|dtls.*addr|addrDetail|rcvrDtls)/i.test(hay)) {
                      if (detailAddr) {
                        setVal(el, detailAddr);
                        touched.push('detail:' + (el.name || el.id || el.placeholder));
                      }
                      continue;
                    }
                    // 기본주소
                    if (/(기본.*주소|base.*addr|baseAddr|rcvrBaseAddr|^addr$|pickupBaseAddr)/i.test(hay)) {
                      // preserveBase: popup 이 도로명으로 잘 채워둔 값 보존
                      if (preserveBase && (el.value || '').trim()) {
                        touched.push('base-preserved:' + (el.name || el.id || el.placeholder));
                        continue;
                      }
                      setVal(el, baseAddr);
                      touched.push('base:' + (el.name || el.id || el.placeholder));
                      continue;
                    }
                    // 그 외 'addr' 가 들어가 있으면 일단 base 로 간주 (가장 마지막 fallback)
                    if (/addr|주소/i.test(hay) && !el.value) {
                      setVal(el, baseAddr);
                      touched.push('addr-fallback:' + (el.name || el.id || el.placeholder));
                      continue;
                    }
                  }
                  return touched;
                }""",
                [order.postal_code, addr_base, addr_detail, preserve_base],
            )
            if touched:
                log.info(f"행{order.row}: 주소 JS 강제 주입: {touched}")
            else:
                log.warning(f"행{order.row}: 주소 주입 대상 input 을 못 찾음")
        except Exception as exc:
            log.debug(f"행{order.row}: 주소 JS 강제 주입 실패: {exc}")

    async def _fill_english_name(
        self, page: Page, eng_name: str, delay: int
    ) -> None:
        """영문이름 입력 — 통합/분리/재시도/JS 강제 순으로 견고하게 시도.

        11번가는 사용자에 따라 다음 중 하나의 형태로 영문이름을 받는다:
          - 단일 input (예: ordEngNm) — 'HONG GILDONG' 풀네임
          - first/last 분리 input — first='GILDONG', last='HONG'
          - 통관번호 조회 직후에야 영문 input 이 노출되는 경우
        """
        parts = eng_name.split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) >= 2 else ""

        async def _try_once() -> bool:
            # 1) 통합 필드 우선
            if await self.selectors.exists(
                page, "order_page.english_name", timeout_ms=400
            ):
                if await self._force_fill(
                    page, "order_page.english_name", eng_name, delay
                ):
                    log.info(f"영문이름 통합 필드 입력 성공: {eng_name!r}")
                    return True
            # 2) first/last 분리 필드
            if first and last:
                ok1 = await self._force_fill(
                    page, "order_page.english_first_name", first, delay
                )
                ok2 = await self._force_fill(
                    page, "order_page.english_last_name", last, delay
                )
                if ok1 and ok2:
                    log.info(
                        f"영문이름 분리 필드 입력 성공: first={first!r} last={last!r}"
                    )
                    return True
            return False

        # 0) 가장 확실한 직접 주입 — 11번가 ordEngNm / engNm 류 input 을
        #    selector 매칭 없이 JS 로 즉시 찾아 강제 주입. 통관번호 단계가 폼을
        #    재렌더하더라도 영문이름 단계 끝에 한 번 더 채워서 안전망 확보.
        try:
            direct_ok = await page.evaluate(
                r"""
([fullName, firstName, lastName]) => {
  function setVal(el, v) {
    try { el.removeAttribute('readonly'); } catch(e){}
    try { el.removeAttribute('disabled'); } catch(e){}
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype, 'value'
    )?.set;
    if (setter) setter.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  }
  const sels = [
    'input[name="ordEngNm"]', 'input#ordEngNm',
    'input[name="engNm"]', 'input#engNm',
    'input[name="rcvrEngNm"]',
    'input[name="prsnEngNm"]', 'input#prsnEngNm',
    'input[name="psnEngNm"]', 'input#psnEngNm',
  ];
  const touched = [];
  for (const s of sels) {
    for (const el of document.querySelectorAll(s)) {
      const t = (el.type||'').toLowerCase();
      if (['hidden','checkbox','radio','submit','button'].includes(t)) continue;
      setVal(el, fullName);
      touched.push(s);
    }
  }
  // first/last 분리 input
  if (firstName) {
    for (const el of document.querySelectorAll(
      'input[name="engFirstNm"], input[name="ordEngFirstNm"]'
    )) { setVal(el, firstName); touched.push('first'); }
  }
  if (lastName) {
    for (const el of document.querySelectorAll(
      'input[name="engLastNm"], input[name="ordEngLastNm"]'
    )) { setVal(el, lastName); touched.push('last'); }
  }
  return touched;
}
""",
                [eng_name, first, last],
            )
            if direct_ok:
                log.info(f"영문이름 직접 selector 주입 성공: {direct_ok}")
                return
        except Exception as exc:
            log.debug(f"영문이름 직접 주입 시도 실패: {exc}")

        # 1차 시도 (selector 매칭 경유)
        if await _try_once():
            return

        # 통관번호 조회 결과 등 비동기 갱신 대기 후 재시도 (최대 3회) — 짧은 polling
        for attempt in range(3):
            await asyncio.sleep(0.1)
            if await _try_once():
                return
            log.debug(f"영문이름 재시도 {attempt + 1}/3")

        # 마지막 수단: JS 로 페이지 전체를 훑어 영문이름 후보를 찾아 직접 주입.
        # 통관번호 영역 안의 input / select 까지 모두 탐색.
        try:
            injected = await page.evaluate(
                r"""
([fullName, firstName, lastName]) => {
  function setVal(el, v) {
    try { el.removeAttribute('readonly'); } catch(e){}
    try { el.removeAttribute('disabled'); } catch(e){}
    const proto = el.tagName === 'SELECT'
      ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur', {bubbles: true}));
  }
  function hayOf(el) {
    return [el.name||'', el.id||'', el.placeholder||'',
            el.getAttribute('aria-label')||''].join(' ').toLowerCase();
  }
  function nearbyText(el) {
    // 가까운 라벨/조상 텍스트 — '개인통관고유부호' 영역인지 판단용
    let p = el.parentElement;
    let collected = '';
    for (let i = 0; p && i < 6; i++, p = p.parentElement) {
      const t = (p.innerText || p.textContent || '').slice(0, 200);
      if (t) collected += ' ' + t;
    }
    return collected.toLowerCase();
  }
  const touched = [];

  // 1) 일반 input — name/id/placeholder 에 'eng' 류 매칭
  for (const el of document.querySelectorAll('input')) {
    const t = (el.type||'').toLowerCase();
    if (['checkbox','radio','submit','button','file','image','hidden'].includes(t)) continue;
    const hay = hayOf(el);
    if (/(eng.*nm|eng.*name|영문이름|hong\s*gildong|영문|engNm|prsnEngNm|psnEngNm|ordEngNm|rcvrEngNm)/i.test(hay)) {
      if (firstName && /first|이름.*영문/i.test(hay)) {
        setVal(el, firstName);
        touched.push('first:' + (el.name || el.id));
        continue;
      }
      if (lastName && /last|성.*영문/i.test(hay)) {
        setVal(el, lastName);
        touched.push('last:' + (el.name || el.id));
        continue;
      }
      setVal(el, fullName);
      touched.push('full:' + (el.name || el.id));
    }
  }

  // 2) 통관번호 영역 안의 input/select — 라벨에 영문이름 hint 가 없어도
  //    근처 텍스트에 '개인통관고유부호' / 'P로 시작' / '영문' 이 있으면 해당 영역으로 간주
  for (const el of document.querySelectorAll('input, select')) {
    if (touched.find(x => x.includes(el.name) || x.includes(el.id))) continue;
    if (el.tagName === 'INPUT') {
      const t = (el.type||'').toLowerCase();
      if (['checkbox','radio','submit','button','file','image','hidden'].includes(t)) continue;
    }
    const hay = hayOf(el);
    // 통관번호 input 자체는 건너뛰기
    if (/(prsn.*cstms|cstms.*cd|psnCsc|개인통관|통관.*번호|p로\s*시작)/i.test(hay)) continue;
    // 우편/주소/전화 etc 도 건너뛰기
    if (/(zip|post|우편|addr|주소|phone|tel|mbl|mobile|휴대|받는|수취인|recipient|rcvr.*nm|recv.*nm)/i.test(hay)) continue;
    // 근처 텍스트로 통관번호 영역인지 판단
    const near = nearbyText(el);
    if (!/개인통관|통관.*고유부호|p로\s*시작/i.test(near)) continue;

    // 이미 값이 있으면 건드리지 않음 (예: 통관번호 input 옆에 잘못 매칭 방지)
    if (el.value && el.value.trim()) continue;

    if (el.tagName === 'SELECT') {
      // select 면 옵션 중 영문이름 매칭 시도
      let matched = false;
      for (const opt of el.options || []) {
        const ot = (opt.innerText || opt.textContent || '').trim().toUpperCase();
        if (ot === fullName.toUpperCase() || ot.includes(fullName.toUpperCase())) {
          setVal(el, opt.value);
          touched.push('select-opt:' + (el.name || el.id) + '=' + opt.value);
          matched = true;
          break;
        }
      }
      if (!matched && el.options && el.options.length > 0) {
        // 첫 번째 옵션 (보통 사용자 본인 이름)
        setVal(el, el.options[0].value);
        touched.push('select-first:' + (el.name || el.id));
      }
    } else {
      setVal(el, fullName);
      touched.push('customs-area-input:' + (el.name || el.id || el.placeholder));
    }
  }
  return touched;
}
""",
                [eng_name, first, last],
            )
            if injected:
                log.info(f"영문이름 JS 강제 주입 성공: {injected}")
                return
        except Exception as exc:
            log.debug(f"영문이름 JS 강제 주입 실패: {exc}")

        log.warning(
            f"영문이름 입력 실패 (필드 못 찾음): {eng_name!r} — "
            "사용자가 직접 입력해야 합니다"
        )

    async def _fill_postal_and_address(self, page: Page, order: Order) -> None:
        """우편번호/주소 자동 입력 (전 과정 무인 자동화).

        흐름:
          1) "주소찾기" 버튼을 자동 클릭하여 팝업을 연다
          2) 팝업 검색창에 우편번호를 입력하고 검색
          3) 검색 결과 중 수취인 주소와 가장 유사한 항목을 자동 선택
          4) 두번째 주소칸(상세주소) 에 수취인 주소 전체를 입력
        """
        delay = self.config.typing_delay_ms

        log.info(f"행{order.row}: 주소찾기 단계 진입 — 팝업 존재 여부 확인")

        # 1) 주소찾기 버튼 자동 클릭 (팝업이 이미 떠 있으면 스킵)
        popup_open = await self.selectors.exists(
            page, "order_page.zipcode_popup_container", timeout_ms=600
        )
        if not popup_open:
            log.info(f"행{order.row}: 팝업 미감지 → 주소찾기 버튼 탐색 시작")
            btn_found = await self.selectors.exists(
                page, "order_page.zipcode_search_button", timeout_ms=2000
            )
            log.info(
                f"행{order.row}: 주소찾기 버튼 탐색 결과 = "
                f"{'발견' if btn_found else '미발견'}"
            )
            if btn_found:
                try:
                    await self.selectors.click(
                        page, "order_page.zipcode_search_button"
                    )
                    log.info(f"행{order.row}: 주소찾기 버튼 자동 클릭 완료")
                except Exception as exc:
                    log.warning(f"행{order.row}: 주소찾기 버튼 클릭 실패: {exc}")
            # 팝업이 뜰 때까지 잠깐 대기
            for _ in range(20):
                if await self.selectors.exists(
                    page, "order_page.zipcode_popup_container", timeout_ms=300
                ):
                    popup_open = True
                    break
                await asyncio.sleep(0.2)
            if not popup_open and self.on_confirm is not None:
                # 자동 클릭 실패 → 사용자에게 모달로 묻고 한 번 더 시도
                log.info(
                    f"행{order.row}: 주소찾기 자동 클릭 실패 → 사용자 확인 모달 표시"
                )
                try:
                    user_did = await self.on_confirm(
                        "주소찾기 팝업 확인",
                        f"행{order.row} 배송지 입력 단계입니다.\n\n"
                        "주문서의 \u300C주소찾기\u300D 버튼을 눌러 팝업을 열어주세요.\n"
                        f"우편번호 {order.postal_code} 검색·선택은 자동으로 진행됩니다.\n\n"
                        "팝업을 여신 뒤 \u300C확인\u300D 을 눌러주세요. "
                        "(\u300C취소\u300D 를 누르면 이 행은 건너뛰고 다음 행으로 이동합니다.)",
                    )
                except Exception as exc:
                    log.warning(f"행{order.row}: on_confirm 호출 실패: {exc}")
                    user_did = False
                log.info(
                    f"행{order.row}: 사용자 모달 응답 = {'확인' if user_did else '취소'}"
                )
                if user_did:
                    for _ in range(20):
                        if await self.selectors.exists(
                            page, "order_page.zipcode_popup_container", timeout_ms=300
                        ):
                            popup_open = True
                            break
                        await asyncio.sleep(0.2)
                    if popup_open:
                        log.info(f"행{order.row}: 사용자 클릭 후 팝업 감지됨")
                    else:
                        log.warning(
                            f"행{order.row}: 사용자가 확인했으나 팝업이 여전히 미감지"
                        )
            if not popup_open:
                raise UserInterventionRequired(
                    "주소찾기 팝업이 열리지 않았습니다. 다음 행으로 건너뜁니다.",
                    checkpoint=Checkpoint.AT_ORDER_PAGE.value,
                    detail=f"postal_code={order.postal_code} address={order.address}",
                )

        # 2) 팝업 검색창에 우편번호 입력 + 검색
        if await self.selectors.exists(
            page, "order_page.zipcode_popup_search_input", timeout_ms=2000
        ):
            await self.selectors.fill(
                page,
                "order_page.zipcode_popup_search_input",
                order.postal_code,
                typing_delay_ms=delay,
            )
            if await self.selectors.exists(
                page, "order_page.zipcode_popup_search_button", timeout_ms=500
            ):
                await self.selectors.click(
                    page, "order_page.zipcode_popup_search_button"
                )
            else:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
            await asyncio.sleep(0.8)

        # 3) 검색 결과 중 수취인 주소와 가장 유사한 항목 선택
        picked = await self._pick_best_zipcode_result(page, order.address)
        if not picked:
            raise UserInterventionRequired(
                f"우편번호 {order.postal_code} 검색 결과에서 적합한 항목을 찾지 못했습니다. "
                "수동으로 주소를 선택해 주세요.",
                checkpoint=Checkpoint.AT_ORDER_PAGE.value,
                detail=f"postal_code={order.postal_code} address={order.address}",
            )
        await asyncio.sleep(0.5)

        # 4) 상세주소칸 — 아파트명/동/호 등만 입력 (도로명+번지는 base 칸에 이미 채워짐)
        addr_detail = order.address_detail() or order.address
        if addr_detail and await self.selectors.exists(
            page, "order_page.address_detail", timeout_ms=1500
        ):
            await self._force_fill(
                page, "order_page.address_detail", addr_detail, delay
            )
            log.info(f"행{order.row}: 상세주소 입력 완료 — {addr_detail!r}")

    async def _pick_best_zipcode_result(
        self, page: Page, target_address: str
    ) -> bool:
        """검색 결과 목록에서 target_address 와 가장 유사한 항목을 클릭한다.

        팝업이 iframe 일 수도 있고 inline 레이어일 수도 있으므로 둘 다 시도한다.
        유사도는 SequenceMatcher 기반 ratio 로 계산.
        반환: 클릭에 성공하면 True.
        """
        from difflib import SequenceMatcher

        norm_target = re.sub(r"\s+", "", target_address or "")

        async def _pick_in(scope) -> bool:
            # scope: Page | Frame
            try:
                elements = await scope.query_selector_all(
                    ".layer_addr li, [class*='AddressSearch' i] li, "
                    "[class*='result' i] li, ul[class*='addr' i] li, "
                    ".search_result_box li, [role='listitem']"
                )
            except Exception:
                elements = []
            if not elements:
                return False
            best = None
            best_score = -1.0
            for el in elements:
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    continue
                if not text:
                    continue
                norm = re.sub(r"\s+", "", text)
                score = SequenceMatcher(None, norm_target, norm).ratio()
                if score > best_score:
                    best_score = score
                    best = el
            if best is None:
                return False
            try:
                await best.click()
                log.info(
                    f"주소 결과 선택: 유사도={best_score:.2f}"
                )
                return True
            except Exception as exc:
                log.debug(f"결과 클릭 실패: {exc}")
                return False

        # 1) 메인 페이지 내 inline 레이어
        if await _pick_in(page):
            return True
        # 2) iframe 안의 결과 목록
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            try:
                if await _pick_in(frame):
                    return True
            except Exception:
                continue
        return False

    def _extract_address_detail(self, full_address: str) -> str:
        """전체 주소에서 상세 부분(동/호수/건물명 등)만 추출.

        팝업으로 선택된 도로명 주소가 base 에 자동으로 채워지므로,
        엑셀의 전체 주소에서 도로명 부분 뒤의 꼬리만 detail input 에 넣어야 한다.

        규칙:
            - 도로명 패턴(`...로NN`, `...길NN`, `...로NN-NN` 등) 뒤의 텍스트를 추출
            - 매칭 실패 시 빈 문자열 반환 (사용자가 직접 입력하도록 둠)
        """
        if not full_address:
            return ""
        text = full_address.strip()
        # "...로/길 NN(-NN)?" 패턴 뒤를 잘라낸다.
        m = re.search(r"(?:로|길)\s*\d+(?:-\d+)?", text)
        if not m:
            return ""
        tail = text[m.end():].strip()
        # 선두에 붙은 콤마/하이픈 등 정리
        tail = re.sub(r"^[,\-\s]+", "", tail)
        return tail

    async def _force_fill(
        self,
        page: Page,
        selector_path: str,
        value: str,
        typing_delay_ms: int,
    ) -> bool:
        """fill 을 시도하고, readonly/disabled 라서 실패하면 JS로 value 강제 세팅.

        반환: 실제로 값이 들어갔으면 True, 요소를 못 찾으면 False.
        """
        try:
            await self.selectors.fill(
                page, selector_path, value, typing_delay_ms=typing_delay_ms
            )
            return True
        except ElementNotFoundError:
            return False
        except Exception as exc:
            log.debug(f"{selector_path} fill 실패 → JS 강제 세팅 시도: {exc}")

        # JS fallback — value 직접 세팅 + input/change 이벤트
        try:
            sels = self.selectors.get(selector_path)
        except Exception:
            return False
        js = r"""
([selectors, value]) => {
  for (const sel of selectors) {
    try {
      const el = document.querySelector(sel);
      if (!el) continue;
      // readonly 해제
      try { el.removeAttribute('readonly'); } catch(e){}
      try { el.removeAttribute('disabled'); } catch(e){}
      const setter = Object.getOwnPropertyDescriptor(
        el.__proto__, 'value'
      )?.set || Object.getOwnPropertyDescriptor(
        HTMLInputElement.prototype, 'value'
      )?.set;
      if (setter) setter.call(el, value); else el.value = value;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      return true;
    } catch(e) {}
  }
  return false;
}
"""
        try:
            ok = await page.evaluate(js, [sels, value])
            return bool(ok)
        except Exception as exc:
            log.debug(f"JS 강제 세팅도 실패 {selector_path}: {exc}")
            return False

    async def _js_fill_all_address_fields(
        self, page: Page, order: Order
    ) -> list[str]:
        """페이지 전체에서 '주소' 성격을 가진 input 을 찾아 전부 엑셀 주소로 채움.

        라벨/placeholder/name/id 에 '주소/addr/address' 가 들어간 모든 input 을 대상.
        우편번호 성격 필드는 제외하고, 주소/상세주소만 채운다.
        """
        js = r"""
([fullAddr, postal]) => {
  const inputs = document.querySelectorAll('input[type="text"], input:not([type])');
  const touched = [];
  for (const el of inputs) {
    try {
      const s = window.getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') continue;
      const hay = [
        el.name || '', el.id || '', el.placeholder || '',
        (el.getAttribute('aria-label') || '')
      ].join(' ').toLowerCase();

      // 우편번호 성격 필드는 fullAddr 말고 postal 로
      if (/(zip|post|우편)/i.test(hay)) {
        if (!el.value) {
          try { el.removeAttribute('readonly'); } catch(e){}
          const setter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype, 'value'
          )?.set;
          if (setter) setter.call(el, postal); else el.value = postal;
          el.dispatchEvent(new Event('input', {bubbles: true}));
          el.dispatchEvent(new Event('change', {bubbles: true}));
          touched.push('postal:' + (el.name || el.id));
        }
        continue;
      }

      // 주소 성격 필드 — base/detail/addr/address 전부 같은 전체 주소로
      if (/(addr|address|주소)/i.test(hay)) {
        try { el.removeAttribute('readonly'); } catch(e){}
        const setter = Object.getOwnPropertyDescriptor(
          HTMLInputElement.prototype, 'value'
        )?.set;
        if (setter) setter.call(el, fullAddr); else el.value = fullAddr;
        el.dispatchEvent(new Event('input', {bubbles: true}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
        touched.push('addr:' + (el.name || el.id));
      }
    } catch(e) {}
  }
  return touched;
}
"""
        try:
            touched = await page.evaluate(js, [order.address, order.postal_code])
            if touched:
                log.info(f"JS fallback 으로 주소 필드 {len(touched)}개 채움: {touched}")
                return list(touched)
        except Exception as exc:
            log.debug(f"JS 주소 주입 실패: {exc}")
        return []

    async def _fill_customs_id_or_fail(self, page: Page, order: Order) -> None:
        """통관번호를 어떻게든 주입하고, 실제로 값이 들어갔는지 검증한다.

        단계:
          1) 셀렉터로 입력란 찾아서 fill
          2) 라디오 "직접입력" 클릭 후 재시도
          3) 숨겨진 input 까지 포함해 JS 로 강제 주입
          4) 최종 검증 — 어느 input 에도 값이 없으면 ElementNotFoundError
        """
        delay = self.config.typing_delay_ms
        cid = order.customs_id
        if not cid:
            raise ElementNotFoundError(
                f"행{order.row}: 엑셀에 통관번호가 비어 있습니다."
            )

        # 0) 통관번호 섹션이 lazy-load 되는 경우를 대비해 페이지 하단까지 스크롤.
        # sleep 대신 — 다음 단계(셀렉터 exists) 자체가 polling 이므로 즉시 진행.
        try:
            await page.evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); "
                "window.scrollTo(0, 0); }"
            )
        except Exception:
            pass

        # 0-b) 통관번호 영역의 select/dropdown/radio/탭 중 '직접입력' 옵션을 선택.
        #      11번가는 회원이 등록한 통관번호가 있으면 기본이 'KIM MINA' 같은
        #      회원 정보가 선택돼있어 입력칸이 readonly 로 고정된다.
        #      '직접입력' 으로 바꿔야 새 영문이름·통관번호가 입력 가능.
        try:
            switched = await page.evaluate(
                r"""() => {
                  // '개인통관고유부호' 섹션 안의 요소만 대상
                  function inCustomsSection(el) {
                    let p = el;
                    for (let i = 0; p && i < 15; i++, p = p.parentElement) {
                      const t = (p.innerText || p.textContent || '').slice(0, 300);
                      if (/개인통관|통관.*고유부호|통관번호/.test(t)) return true;
                    }
                    return false;
                  }
                  function fire(el) {
                    try { el.click(); return true; } catch(e) {}
                    try {
                      el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                      return true;
                    } catch(e) {}
                    return false;
                  }
                  const out = [];

                  // 1) <select> 드롭다운 — 통관 섹션 안의 select 에서 '직접입력' option 선택
                  for (const sel of document.querySelectorAll('select')) {
                    if (!inCustomsSection(sel)) continue;
                    let target = null;
                    for (const opt of sel.options) {
                      const ot = (opt.innerText || opt.textContent || '').trim();
                      if (/^직접\s*입력$|^직접입력$|^입력$/.test(ot)) {
                        target = opt;
                        break;
                      }
                    }
                    if (target && sel.value !== target.value) {
                      const setter = Object.getOwnPropertyDescriptor(
                        HTMLSelectElement.prototype, 'value'
                      )?.set;
                      if (setter) setter.call(sel, target.value); else sel.value = target.value;
                      sel.dispatchEvent(new Event('input', {bubbles: true}));
                      sel.dispatchEvent(new Event('change', {bubbles: true}));
                      out.push('select-direct:' + (sel.name || sel.id) + '=' + target.value);
                    }
                  }

                  // 2) 11번가 커스텀 드롭다운 — <button>/<div role="combobox"> + 옵션 리스트
                  //    버튼 텍스트가 'KIM MINA' 같은 회원 정보면 클릭해서 펼친 후
                  //    '직접입력' 항목 클릭.
                  for (const trig of document.querySelectorAll(
                    'button, [role="combobox"], [role="button"], [class*="select" i], [class*="dropdown" i]'
                  )) {
                    if (!inCustomsSection(trig)) continue;
                    const txt = (trig.innerText || '').trim();
                    // '직접입력' 이미 표시중이면 skip
                    if (/^직접\s*입력$|^직접입력$/.test(txt)) continue;
                    // 너무 긴 텍스트(섹션 전체) 는 trigger 가 아님
                    if (txt.length > 30) continue;
                    // 후보: 영문 이름 패턴(알파벳 공백) 또는 'P'+숫자 가 trigger 텍스트
                    if (!/^[A-Z][A-Z\s]+$|P\d{8,}/.test(txt)) continue;
                    if (!fire(trig)) continue;
                    out.push('trigger-clicked:' + txt.slice(0, 30));
                  }
                  // 펼쳐진 후 옵션에서 '직접입력' 클릭 시도
                  for (const opt of document.querySelectorAll(
                    '[role="option"], li, .option, [class*="option" i]'
                  )) {
                    const otx = (opt.innerText || '').trim();
                    if (!/^직접\s*입력$|^직접입력$/.test(otx)) continue;
                    if (!inCustomsSection(opt)) continue;
                    // 화면에 보이는지 체크
                    const cs = window.getComputedStyle(opt);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    if (fire(opt)) {
                      out.push('option-direct-clicked');
                      break;
                    }
                  }

                  // 3) radio 형태 — '직접입력' 라벨/value
                  for (const r of document.querySelectorAll('input[type="radio"]')) {
                    if (!inCustomsSection(r)) continue;
                    const lbl = r.closest('label');
                    const lblTxt = lbl ? (lbl.innerText || '').trim() : '';
                    const v = (r.value || '').toLowerCase();
                    if (/^직접\s*입력$|^직접입력$|^입력$/.test(lblTxt) ||
                        /direct|input|new/i.test(v)) {
                      if (!r.checked) {
                        r.checked = true;
                        r.dispatchEvent(new Event('change', {bubbles: true}));
                        r.dispatchEvent(new Event('click', {bubbles: true}));
                        out.push('radio-direct:' + (r.name || r.id));
                      }
                    }
                  }
                  return out;
                }"""
            )
            if switched:
                log.info(f"행{order.row}: 통관번호 '직접입력' 전환: {switched}")
                # input 활성화 polling — 최대 400ms, 잡히면 즉시 진행.
                for _ in range(8):
                    try:
                        ready = await page.evaluate(
                            r"""() => {
                              const el = document.querySelector(
                                'input[name="psnCscUniqNo"], input#psnCscUniqNo'
                              );
                              if (!el) return false;
                              return !el.disabled && !el.readOnly;
                            }"""
                        )
                        if ready:
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
        except Exception as exc:
            log.debug(f"통관 '직접입력' 전환 실패: {exc}")

        # 1) 셀렉터 경로 — timeout 단축 (이미 위 polling 으로 input 준비된 상태)
        filled = False
        if await self.selectors.exists(page, "order_page.customs_id", timeout_ms=400):
            filled = await self._force_fill(
                page, "order_page.customs_id", cid, delay
            )

        # 2) 직접입력 라디오 → 다시 시도
        if not filled:
            if await self.selectors.exists(
                page, "order_page.customs_direct_input", timeout_ms=400
            ):
                url_before = page.url
                try:
                    await self.selectors.click(page, "order_page.customs_direct_input")
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
                if self._escaped_order_page(url_before, page.url):
                    log.warning(
                        f"직접입력 라디오가 외부 이동 유발 → 뒤로가기"
                    )
                    try:
                        await page.go_back(wait_until="domcontentloaded", timeout=2000)
                    except Exception:
                        pass
            if await self.selectors.exists(page, "order_page.customs_id", timeout_ms=500):
                filled = await self._force_fill(
                    page, "order_page.customs_id", cid, delay
                )

        # 3) JS 강제 주입 (숨겨진 input 포함)
        injected = await self._force_inject_customs_id(page, cid)
        if injected:
            log.info(f"행{order.row}: 통관번호 JS 주입 {injected}개 필드")
            filled = True

        # 4) 최종 검증 — 페이지에 통관번호 값이 실제로 들어간 input 이 있는지
        has_value = await self._verify_customs_id_present(page, cid)
        if not has_value:
            raise ElementNotFoundError(
                f"행{order.row}: 통관번호({cid}) 를 주문서에 주입하지 못했습니다."
            )

    async def _verify_customs_id_present(self, page: Page, cid: str) -> bool:
        """주문서 페이지의 input 중 하나라도 cid 값을 갖고 있는지."""
        try:
            js = r"""
(cid) => {
  const inputs = document.querySelectorAll('input');
  for (const el of inputs) {
    if ((el.value || '').trim() === cid) return true;
  }
  return false;
}
"""
            return bool(await page.evaluate(js, cid))
        except Exception:
            return False

    async def _force_inject_customs_id(self, page: Page, customs_id: str) -> int:
        """통관번호 관련 모든 input 에 값을 강제 주입.

        숨겨진(display:none, type=hidden) input 도 포함한다.
        11번가가 "회원에 저장된 통관번호 사용" 모드일 때 실제 input 은 숨어있고
        텍스트로만 표시되는 경우가 있어, 이를 덮어쓰기 위함.

        Returns: 값이 주입된 input 개수.
        """
        if not customs_id:
            return 0
        # label / 조상 텍스트 / 섹션 heading 까지 훑어서 매칭
        js = r"""
(cid) => {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype, 'value'
  )?.set;

  function labelText(el) {
    let texts = [];
    if (el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l) texts.push(l.innerText || l.textContent || '');
    }
    // 조상 5단계까지 훑어 '통관/cstms' 텍스트가 있으면 포함
    let p = el.parentElement;
    for (let i = 0; p && i < 5; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL') texts.push(p.innerText || p.textContent || '');
    }
    return texts.join(' ');
  }
  function nearbyText(el) {
    // 같은 섹션(조상 10단계)의 heading/legend/label 텍스트 수집
    let p = el.parentElement;
    for (let i = 0; p && i < 10; i++, p = p.parentElement) {
      const head = p.querySelector('legend, h1, h2, h3, h4, h5');
      if (head) return (head.innerText || head.textContent || '').slice(0, 120);
    }
    return '';
  }

  const debug = [];
  const matched = [];
  const inputs = document.querySelectorAll('input');
  for (const el of inputs) {
    const type = (el.type || '').toLowerCase();
    if (['checkbox','radio','submit','button','file','image'].includes(type)) continue;
    // 수집: 매칭 대상 후보 정보
    const info = {
      name: el.name || '', id: el.id || '',
      placeholder: el.placeholder || '',
      aria: el.getAttribute('aria-label') || '',
      label: labelText(el),
      near: nearbyText(el),
      value: el.value || '',
      type: type,
    };
    // 매칭은 필드 자체 메타(name/id/placeholder/aria/label) 로만 — 'near'(섹션 heading)
    // 까지 보면 같은 '개인통관고유부호' 섹션 안의 영문이름/주민번호 칸까지
    // 통관번호로 덮어버리는 사고 발생. near 는 디버그용으로만 표시.
    const fieldHay = [info.name, info.id, info.placeholder, info.aria, info.label]
      .join(' ').toLowerCase();
    // 명시적 제외: 이름/주민/사업자 필드는 절대 통관번호로 덮지 않음
    const exclude = /ordengnm|ordnm|ordrsdnt|bizno|english|name|주민|사업자|영문/i;
    const isMatch = !exclude.test(fieldHay) && /prsn.*cstms|cstms.*cd|prsncstms|customs|통관|개인통관|통관고유|psncscuniqno/i.test(fieldHay);
    debug.push({...info, matched: isMatch});
    if (!isMatch) continue;

    try { el.removeAttribute('readonly'); } catch(e) {}
    try { el.removeAttribute('disabled'); } catch(e) {}
    try {
      if (setter) setter.call(el, cid); else el.value = cid;
      el.dispatchEvent(new Event('input', {bubbles: true}));
      el.dispatchEvent(new Event('change', {bubbles: true}));
      matched.push(info);
    } catch(e) {}
  }
  return {count: matched.length, matched, debug};
}
"""
        try:
            result = await page.evaluate(js, customs_id)
            count = int(result.get("count", 0))
            if count == 0:
                # 매칭 실패했을 때 진단 정보 로그
                debug = result.get("debug", [])
                # "통관/cstms/customs" 후보만 추려서 출력
                cands = [
                    d for d in debug
                    if any(k in (d.get("name","") + d.get("id","") + d.get("placeholder","") + d.get("label","") + d.get("near","")).lower()
                           for k in ("통관", "cstms", "customs", "prsn", "13자리"))
                ]
                if cands:
                    log.warning(f"통관번호 후보 input 발견했으나 매칭 실패: {cands}")
                else:
                    log.warning(
                        f"통관번호 input 자체를 페이지에서 찾지 못함. 전체 input {len(debug)}개 중 관련 키워드 0개"
                    )
            else:
                log.info(f"통관번호 주입 성공 {count}개 → {result.get('matched')}")
            return count
        except Exception as exc:
            log.debug(f"통관번호 강제 주입 실패: {exc}")
            return 0

    async def _js_sweep_all_fields(
        self, page: Page, order: Order, only_postal: bool = False
    ) -> list[str]:
        """페이지의 모든 input/select 를 훑어 placeholder/label/name/id/aria-label
        매칭으로 엑셀 값을 강제 주입. 셀렉터가 놓친 필드의 안전망.

        only_postal=True 면 우편번호 필드만 채운다 (배송지 라벨 우선 주입용).

        매칭 규칙:
          - "받는 사람" / "수취인" → order.name
          - "휴대폰 앞자리" → phone_middle
          - "휴대폰 뒷자리" → phone_suffix
          - 010/011/... select → phone prefix
          - 우편번호 → order.postal_code
          - 기본 주소 / 주소 (상세 제외) → order.address
          - 상세 주소 / 상세 건물 → order.address (동일)
          - 영문이름 / HONG GILDONG → order.english_name
          - 개인통관 / P로 시작 → order.customs_id
        """
        # 전화번호 분리
        parts = order.phone.split("-")
        prefix = parts[0] if len(parts) == 3 else "010"
        middle = parts[1] if len(parts) == 3 else ""
        suffix = parts[2] if len(parts) == 3 else ""

        js = r"""
([order]) => {
  const touched = [];

  function setVal(el, val) {
    try { el.removeAttribute('readonly'); } catch(e){}
    try { el.removeAttribute('disabled'); } catch(e){}
    const tag = el.tagName;
    const proto = tag === 'SELECT' ? HTMLSelectElement.prototype
                 : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, val); else el.value = val;
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  }

  function labelFor(el) {
    // id 로 연결된 label 텍스트 찾기
    if (el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l) return (l.innerText || '').trim();
    }
    // 조상 label
    let p = el.parentElement;
    for (let i = 0; p && i < 4; i++, p = p.parentElement) {
      if (p.tagName === 'LABEL') return (p.innerText || '').trim();
    }
    return '';
  }

  function hayOf(el) {
    return [
      el.name || '', el.id || '', el.placeholder || '',
      el.getAttribute('aria-label') || '',
      labelFor(el),
    ].join(' ').toLowerCase();
  }

  // only_postal: 우편번호 + 받는사람 이름만 채우고 종료
  // (주소찾기 모달 전에 라벨 우선 주입용)
  if (order.only_postal) {
    for (const el of document.querySelectorAll('input')) {
      const type = (el.type || '').toLowerCase();
      if (['checkbox','radio','submit','button','file','image','hidden'].includes(type)) continue;
      const s = window.getComputedStyle(el);
      if (s.display === 'none' || s.visibility === 'hidden') continue;
      const hay = hayOf(el);
      if (/(zip|post|우편)/.test(hay)) {
        setVal(el, order.postal_code);
        touched.push('postal:' + (el.name || el.id || el.placeholder));
        continue;
      }
      // 받는사람 (수취인 이름)
      if (/(받는|수취인|recipient|rcvr.*nm|recv.*nm)/i.test(hay)
          && !/주소|addr|우편|phone|tel|mbl|mobile|통관|영문|eng/i.test(hay)) {
        setVal(el, order.name);
        touched.push('name:' + (el.name || el.id || el.placeholder));
      }
    }
    return touched;
  }

  // 1) select (전화 prefix 등)
  for (const sel of document.querySelectorAll('select')) {
    const s = window.getComputedStyle(sel);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    const hay = hayOf(sel);
    // 010/휴대폰 prefix
    if (/(mobile|mbl|휴대|mob).*1|.*1.*(mobile|mbl|휴대|mob)/i.test(hay)
        || /prefix|앞자리/i.test(hay)) {
      const opts = Array.from(sel.options).map(o => o.value);
      if (opts.includes(order.prefix)) {
        sel.value = order.prefix;
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        touched.push('prefix:' + (sel.name || sel.id));
      }
    }
  }

  // 2) input 전체 — 숨겨진 input 도 통관번호/이름 류면 값 주입 (hidden radio/button 제외)
  for (const el of document.querySelectorAll('input')) {
    const type = (el.type || '').toLowerCase();
    if (['checkbox','radio','submit','button','file','image'].includes(type)) continue;
    const s = window.getComputedStyle(el);
    const isHidden = (s.display === 'none' || s.visibility === 'hidden' || type === 'hidden');
    // 숨겨져도 통관/영문이름/주소 관련이면 시도
    const hayPeek = [el.name||'', el.id||'', el.placeholder||''].join(' ').toLowerCase();
    const criticalHidden = /prsn|cstms|통관|eng|영문|addr|주소|rcvr/i.test(hayPeek);
    if (isHidden && !criticalHidden) continue;
    const hay = hayOf(el);

    // 우편번호
    if (/(zip|post|우편)/.test(hay) && !el.value) {
      setVal(el, order.postal_code);
      touched.push('postal:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 휴대폰 앞자리
    if (/(앞자리|middle|mbl.*2|mobile.*2|mbl2|mobile2)/i.test(hay)) {
      if (!el.value) setVal(el, order.middle);
      touched.push('ph_middle:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 휴대폰 뒷자리
    if (/(뒷자리|suffix|mbl.*3|mobile.*3|mbl3|mobile3)/i.test(hay)) {
      if (!el.value) setVal(el, order.suffix);
      touched.push('ph_suffix:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 통관번호
    if (/(prsn.*cstms|cstms.*cd|p로\s*시작|개인통관|통관)/i.test(hay)) {
      setVal(el, order.customs_id);
      touched.push('customs:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 영문이름
    if (/(eng.*nm|eng.*name|영문이름|HONG\s*GILDONG|영문)/i.test(hay)) {
      setVal(el, order.english_name);
      touched.push('eng:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 상세주소 — 원본 주소 전체 (정보 손실 0)
    if (/(상세.*주소|상세.*건물|addr.*dtl|dtls.*addr|addrDetail)/i.test(hay)) {
      setVal(el, order.address);
      touched.push('addr_dtl:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 기본주소 — 일단 원본 주소 (주소찾기 결과가 자동 갱신함)
    if (/(기본.*주소|base.*addr|baseAddr|rcvrBaseAddr)/i.test(hay)) {
      setVal(el, order.address);
      touched.push('addr_base:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 받는사람 (수취인 이름)
    if (/(받는|수취인|recipient|rcvr.*nm|recv.*nm)/i.test(hay)
        && !/주소|addr|우편|phone|tel|mbl|mobile|통관|영문|eng/i.test(hay)) {
      setVal(el, order.name);
      touched.push('name:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 휴대폰 통합 (앞/뒷자리 아닌 단일 필드)
    if (/(phone|tel|mobile|mbl|휴대)/i.test(hay)
        && !/앞자리|middle|뒷자리|suffix|^.*[12].*$|^.*[23].*$/.test(hay)) {
      if (!el.value) {
        setVal(el, order.phone.replace(/-/g, ''));
        touched.push('phone:' + (el.name || el.id || el.placeholder));
      }
      continue;
    }
  }

  return touched;
}
"""
        payload = {
            "name": order.name,
            "phone": order.phone,
            "prefix": prefix,
            "middle": middle,
            "suffix": suffix,
            "postal_code": order.postal_code,
            "address": order.address,
            "address_base": order.address_base(),
            "address_detail": order.address_detail(),
            "customs_id": order.customs_id,
            "english_name": order.english_name,
            "only_postal": only_postal,
        }
        try:
            touched = await page.evaluate(js, [payload])
            if touched:
                tag = "postal-only" if only_postal else "full"
                log.info(
                    f"JS sweep({tag}) 으로 필드 {len(touched)}개 주입: {touched}"
                )
            return list(touched) if touched else []
        except Exception as exc:
            log.debug(f"JS sweep 실패: {exc}")
            return []

    async def _click_final_payment(self, page: Page) -> None:
        """약관 전체동의 + 결제수단(카드) 선택 + 결제하기 클릭."""
        # 1) 약관 전체 동의
        try:
            await self.selectors.click(
                page, "order_page.agree_all_checkbox", timeout_ms=2000
            )
            await asyncio.sleep(0.1)
        except ElementNotFoundError:
            log.debug("약관 동의 체크박스 못 찾음 (없거나 이미 체크됨)")

        # 2) 결제 수단 선택 — 11번가에 저장된 기본 결제수단이 이미 선택되어 있으면 그대로,
        #    아니면 카드를 선택한다.
        try:
            if not await self.selectors.exists(
                page, "order_page.payment_default", timeout_ms=1000
            ):
                await self.selectors.click(
                    page, "order_page.payment_card", timeout_ms=2000
                )
                await asyncio.sleep(0.1)
        except ElementNotFoundError:
            log.debug("결제수단 선택 UI 못 찾음 (기본값으로 진행)")

        # 3) 결제하기 버튼 클릭
        await self.selectors.click(page, "order_page.final_pay_button", timeout_ms=8000)

    def _verify_shopback_before_payment(self, order: Order) -> None:
        """결제 직전 샵백 추적 활성 여부를 로그 + 진단 파일로 기록.

        모니터가 없거나(verify_shopback=False) 비활성이면 그냥 경고 로그만.
        abort_if_no_shopback=True 일 때는 UserInterventionRequired 발생.
        """
        monitor = self._shopback_monitors.get(order.row)
        if monitor is None:
            return

        snap = monitor.snapshot()
        log.info(f"행{order.row} 샵백 추적: {snap.summary()}")

        # 진단 파일 저장 (결제 시점 스냅샷)
        try:
            from datetime import datetime
            from pathlib import Path
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = Path("data/diagnostics") / f"shopback_row{order.row}_{ts}.json"
            monitor.save_log(out)
            log.debug(f"샵백 진단 저장: {out}")
        except Exception as exc:
            log.debug(f"샵백 진단 저장 실패: {exc}")

        if not snap.is_tracking_active:
            warning = (
                f"샵백 추적이 감지되지 않았습니다 (행{order.row}). "
                "이 주문은 적립이 안 될 가능성이 높습니다. "
                "브라우저에서 샵백 확장 아이콘이 활성화되어 있는지 확인하세요."
            )
            log.warning(warning)
            order.error_message = (order.error_message or "") + f" [샵백 미감지]"
            if getattr(self.config, "abort_if_no_shopback", False):
                raise UserInterventionRequired(
                    "샵백 추적이 감지되지 않습니다. "
                    "샵백 확장 아이콘을 클릭해 활성화한 뒤 '이어서 진행'을 눌러주세요.",
                    checkpoint=Checkpoint.FORM_FILLED.value,
                    detail=warning,
                )

    async def _wait_for_order_completion(
        self, page: Page, timeout_sec: int = 600
    ) -> tuple[str, int | None]:
        """결제하기 클릭 후 카드 인증(ARS/ISP/앱카드/PIN 등)을 사용자가 직접
        처리할 시간을 충분히 주고, 주문 완료 페이지가 뜨면 주문번호 + 실제 결제금액을 추출.

        기본 timeout 10분 — 카드사 인증 절차에 시간이 걸려도 충분히 대기.

        Returns: (주문번호, 실제결제금액_원) — 결제금액을 못 찾으면 None.
        """
        # 주의: document.body.innerText 전체를 보면 주문서의 "주문 완료하기" 버튼
        #       텍스트가 즉시 매칭되어 false positive 가 발생한다. URL 변경 또는
        #       페이지 제목/헤딩만 검사해야 한다.
        try:
            await page.wait_for_function(
                """() => {
                    // 1) URL 이 주문완료 경로로 바뀌었는가
                    if (/order\\/(complete|result|orderEnd|OrderComplete|orderComplete)/i.test(window.location.href)) {
                        return true;
                    }
                    // 2) 페이지 제목 또는 h1/h2 에 "주문/결제 완료" 문구
                    if (/주문.*완료|결제.*완료|주문이\\s*완료/.test(document.title)) {
                        return true;
                    }
                    const heads = document.querySelectorAll('h1, h2, h3, .title, .tit, .page_title');
                    for (const el of heads) {
                        const t = (el.innerText || el.textContent || '').trim();
                        if (/주문.*완료|결제.*완료|주문이\\s*완료/.test(t)) return true;
                    }
                    return false;
                }""",
                timeout=timeout_sec * 1000,
            )
        except PwTimeout as exc:
            raise PaymentTimeoutError(
                f"{timeout_sec // 60}분 내에 결제가 완료되지 않았습니다. "
                "카드 인증을 못 하셨거나 결제가 취소되었습니다."
            ) from exc

        # 1) 주문번호 추출
        #    11번가 완료 페이지의 "주문번호" 는 URL 쿼리스트링 ordNo=... 에 가장
        #    신뢰성 있게 들어있다 (예: getOrderDone&ordNo=20260429062671039).
        #    페이지 본문 휴리스틱은 전화번호(010-XXXX-XXXX) 같은 11자리 패턴을
        #    오인식하는 사고가 있어 URL 추출을 1순위로 둔다.
        #
        # 우선순위:
        #   a) URL 쿼리 ordNo (정답)
        #   b) selector confirmation.order_number — 단, 본문 매칭 시 전화번호 제외
        #   c) JS fallback — '주문번호' 라벨 근처 / 본문에서 가장 긴 순수 숫자
        #      (전화번호 형식은 명시적으로 거부)
        import re as _re
        # 순수 숫자 8자리 이상 (전화번호 010- 시작 패턴은 별도 거부 로직으로 차단)
        ORDER_NO_RE = _re.compile(r"\d{8,}")
        # 한국 휴대폰: 010/011/016/017/018/019 로 시작하는 하이픈 포함 패턴
        PHONE_RE = _re.compile(r"\b01[0-9]-?\d{3,4}-?\d{4}\b")
        order_no: str | None = None

        def _accept(candidate: str | None) -> str | None:
            """후보 문자열에서 주문번호로 인정 가능한 부분만 추출.
            전화번호(010-XXXX-XXXX) 가 끼어 있으면 그건 제거하고 다시 매칭.
            """
            if not candidate:
                return None
            cleaned = PHONE_RE.sub("", candidate)
            m = ORDER_NO_RE.search(cleaned)
            if not m:
                return None
            num = m.group(0)
            # 너무 짧거나(8자리 미만) 명백한 전화번호 끝자리(8자리 이하) 거부
            if len(num) < 8:
                return None
            return num

        # a) URL ordNo — 가장 신뢰도 높음
        try:
            current_url = page.url or ""
            url_match = _re.search(r"[?&]ordNo=([0-9\-]+)", current_url)
            if url_match:
                candidate = _accept(url_match.group(1))
                if candidate:
                    order_no = candidate
                    log.info(f"주문번호 URL ordNo 에서 추출: {order_no}")
        except Exception as exc:
            log.debug(f"URL ordNo 추출 실패: {exc}")

        # b) selector
        if not order_no:
            try:
                text = await self.selectors.get_text(
                    page, "confirmation.order_number", timeout_ms=5000
                )
                candidate = _accept(text)
                if candidate:
                    order_no = candidate
                else:
                    log.warning(
                        f"주문번호 셀렉터가 잡았으나 숫자 패턴 없음: text={text!r} "
                        "→ JS fallback 으로 재시도"
                    )
            except ElementNotFoundError:
                log.warning(
                    "주문번호 셀렉터 매칭 실패 → 페이지 텍스트에서 패턴 검색"
                )

        # c) JS fallback — 전화번호는 명시적으로 거부
        if not order_no:
            try:
                js_result = await page.evaluate(
                    r"""() => {
                      const body = document.body ? (document.body.innerText || '') : '';
                      // 한국 휴대폰 형식 (010-1234-5678 등) 은 매칭에서 제외
                      const stripped = body.replace(/\b01[0-9]-?\d{3,4}-?\d{4}\b/g, ' ');
                      // 1) "주문번호" 라벨 뒤에 오는 8자리 이상 순수 숫자
                      let m = stripped.match(/주문\s*번호[^\d]{0,20}(\d{8,})/);
                      if (m) return m[1];
                      // 2) 본문에서 가장 긴 순수 숫자 시퀀스 (10자리 이상 우선)
                      const all = stripped.match(/\d{10,}/g) || [];
                      if (all.length === 0) return null;
                      all.sort((a, b) => b.length - a.length);
                      return all[0];
                    }"""
                )
                candidate = _accept(js_result)
                if candidate:
                    order_no = candidate
            except Exception as exc:
                log.warning(f"JS 주문번호 추출 실패: {exc}")

        # 최종 안전장치
        if order_no:
            # 휴대폰 패턴이면 거부 (이중 안전장치)
            if PHONE_RE.fullmatch(order_no.replace(" ", "")):
                log.warning(
                    f"주문번호로 전화번호가 잡혀 거부: {order_no!r}"
                )
                order_no = None
            else:
                final_match = ORDER_NO_RE.search(order_no)
                if not final_match:
                    log.warning(
                        f"주문번호 검증 실패 ('숫자 8자리+' 아님): {order_no!r} → 거부"
                    )
                    order_no = None
                else:
                    order_no = final_match.group(0)

        if not order_no:
            raise ElementNotFoundError(
                "주문번호 추출 실패: 완료 페이지에서 주문번호를 찾을 수 없습니다"
            )

        # 2) 실제 결제금액 추출 (배송비/할인/관세 등 모두 반영된 최종 금액)
        paid_amount = await self._extract_paid_amount(page)
        if paid_amount is not None:
            log.info(f"실제 결제금액: {paid_amount:,}원")
        else:
            log.warning("결제금액을 완료 페이지에서 찾지 못함 — 단가×수량으로 fallback")

        return order_no, paid_amount

    async def _extract_paid_amount(self, page: Page) -> int | None:
        """주문 완료 페이지에서 '최종 결제금액'을 추출.

        11번가 완료 페이지에는 "결제금액 / 총 결제금액 / 총 결제 금액 / 합계"
        라벨 옆에 원화 금액이 표시된다. 가장 큰/마지막 값을 최종으로 간주.
        """
        js = r"""
() => {
  // 라벨-값 쌍을 찾는다. 라벨 텍스트에 "결제금액" 또는 "총 결제" 가 있으면 근처의 숫자.
  const LABEL_RE = /(총\s*결제.*금액|최종.*결제.*금액|결제\s*금액|결제금액|합계)/;
  const PRICE_RE = /(-?\s*[\d,]+)\s*원/;

  // 페이지 전체의 텍스트 노드 중 라벨 포함 요소 찾기
  const walker = document.createTreeWalker(
    document.body, NodeFilter.SHOW_ELEMENT,
    {
      acceptNode: (el) => {
        const t = (el.innerText || el.textContent || '').trim();
        if (t.length > 200) return NodeFilter.FILTER_SKIP;
        return LABEL_RE.test(t) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      }
    }
  );

  const candidates = [];
  let node;
  while ((node = walker.nextNode())) {
    // 해당 요소 또는 형제/부모에서 금액 텍스트 추출
    const self_text = (node.innerText || node.textContent || '').trim();
    // 근접 텍스트에서 숫자 찾기
    let pool = self_text;
    // 부모 3단계까지 합쳐서 검색
    let p = node.parentElement;
    for (let i = 0; p && i < 3; i++, p = p.parentElement) {
      pool += ' ' + (p.innerText || '').slice(0, 400);
    }
    const matches = [...pool.matchAll(/(-?\s*[\d,]+)\s*원/g)];
    for (const m of matches) {
      const n = parseInt((m[1] || '').replace(/[,\s]/g, ''), 10);
      if (Number.isFinite(n) && n > 0) {
        candidates.push({amount: n, label: self_text.slice(0, 60)});
      }
    }
    if (candidates.length >= 20) break;  // safety
  }

  if (!candidates.length) return null;
  // 동일 금액이 여러 번 나오는 경우가 흔함 (상품금액=합계). 중복 제거 후 최대값.
  const uniq = [...new Set(candidates.map(c => c.amount))].sort((a, b) => b - a);
  return {amount: uniq[0], all: uniq, samples: candidates.slice(0, 5)};
}
"""
        try:
            result = await page.evaluate(js)
            if result and result.get("amount"):
                return int(result["amount"])
        except Exception as exc:
            log.debug(f"결제금액 JS 추출 실패: {exc}")
        return None

    # -------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------

    def _emit(self, order: Order, state: OrderState, msg: str | None = None) -> None:
        # 사용자가 로그 패널에서 진행 상황을 실시간으로 볼 수 있도록 정보 로그도 함께
        if msg:
            log.info(f"행{order.row} · {state.value} · {msg}")
        else:
            log.info(f"행{order.row} · {state.value}")
        try:
            self.on_state(order, state, msg)
        except Exception as exc:
            log.warning(f"state callback 오류: {exc}")

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
    OPEN_PRODUCT = "open_product"
    CHECK_LOGIN = "check_login"
    SELECT_QUANTITY = "select_quantity"
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
        # (수량 변경 후 사용자가 직접 '구매하기' 를 누르고 '기입' 버튼을 누를 때까지 대기)
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
        """현재 컨텍스트의 모든 탭에서 주문서 URL(/pay/...) 인 탭을 찾아 반환.

        사용자가 '구매하기' 누르면 새 탭이 열릴 수도, 기존 탭이 navigate 될 수도 있다.
        주문서 페이지가 안 보이면 current_page 를 그대로 반환.
        """
        try:
            ctx = current_page.context
        except Exception:
            return current_page
        for p in ctx.pages:
            try:
                if p.is_closed():
                    continue
                url = p.url or ""
                # 11번가 주문서 URL 패턴
                if "/pay/" in url or "OrderInfoAction" in url or "orderInfo" in url.lower():
                    return p
            except Exception:
                continue
        return current_page

    async def _await_user_fill(
        self, row: int, page: Page | None = None, timeout_sec: int = 1800
    ) -> None:
        """다음 트리거 중 먼저 일어나는 쪽까지 대기:
          1) '기입' 버튼 클릭 → signal_fill(row)
          2) 자동 감지: 주문서 페이지(/pay/...) 가 뜸 → 자동 진행
          3) 페이지(또는 모든 컨텍스트 페이지) 닫힘 → UserInterventionRequired 예외

        반환: 1/2 일 때 정상 return.
        예외: 3 일 때 UserInterventionRequired (호출자가 행 종료 처리).
        """
        ev = asyncio.Event()
        self._fill_events[row] = ev

        async def _wait_user() -> str:
            await ev.wait()
            return "user"

        async def _wait_order_page() -> str:
            if page is None:
                await asyncio.Event().wait()
                return "order"
            try:
                ctx = page.context
            except Exception:
                await asyncio.Event().wait()
                return "order"
            poll = 0.7
            while True:
                try:
                    for p in ctx.pages:
                        if p.is_closed():
                            continue
                        url = p.url or ""
                        if (
                            "/pay/" in url
                            or "OrderInfoAction" in url
                            or "orderInfo" in url.lower()
                        ):
                            log.info(
                                f"행{row}: 주문서 페이지 자동 감지 → '기입' 자동 트리거 ({url[:80]})"
                            )
                            return "order"
                except Exception:
                    pass
                await asyncio.sleep(poll)

        async def _wait_pages_closed() -> str:
            """모든 페이지가 닫힐 때까지 폴링. 닫히면 'closed' 반환."""
            if page is None:
                await asyncio.Event().wait()
                return "closed"
            try:
                ctx = page.context
            except Exception:
                await asyncio.Event().wait()
                return "closed"
            poll = 0.5
            while True:
                try:
                    alive = any(not p.is_closed() for p in ctx.pages)
                    if not alive:
                        log.info(
                            f"행{row}: 모든 페이지 닫힘 감지 → 행 종료"
                        )
                        return "closed"
                except Exception:
                    pass
                await asyncio.sleep(poll)

        try:
            tasks = {
                asyncio.create_task(_wait_user()),
                asyncio.create_task(_wait_order_page()),
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
            # 어떤 trigger 였는지 확인
            for t in done:
                try:
                    result = t.result()
                except Exception:
                    continue
                if result == "closed":
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.AT_PRODUCT_PAGE.value,
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

        # 없으면 orphan 페이지(이전 행이 두고 간 탭) → 다른 행의 탭 순으로 인수인계
        if page is None or page.is_closed():
            donor_row = None
            donor_page = None
            # 1순위: abandon() 이 남긴 orphan 페이지
            orphan = self._pages.get(-1)
            if orphan is not None and not orphan.is_closed():
                donor_row = -1
                donor_page = orphan
            else:
                # 2순위: 다른 row 에 남아있는 살아있는 탭
                for r, p in list(self._pages.items()):
                    if r != order.row and p and not p.is_closed():
                        donor_row = r
                        donor_page = p
                        break

            if donor_page is not None:
                # 이전 행의 페이지를 현재 행으로 인수인계
                self._pages[order.row] = donor_page
                self._pages.pop(donor_row, None)
                self._checkpoints.pop(donor_row, None)
                # 샵백 모니터도 이전 행의 것을 종료하고 새로 붙임
                stale_monitor = self._shopback_monitors.pop(donor_row, None)
                if stale_monitor:
                    try:
                        stale_monitor.stop()
                    except Exception:
                        pass
                page = donor_page
                log.debug(f"행{donor_row} → 행{order.row} 탭 인수인계 (포커스 유지)")
            else:
                # 정말 탭이 없을 때만 새로 만든다 (첫 행이거나 브라우저 재시작 후)
                page = await self.browser.new_page()
                self._pages[order.row] = page

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

                # 사용자가 옵션을 선택할 때까지 대기 → 옵션이 사이드바에 추가되면
                # 그 옵션의 수량을 엑셀의 수량으로 자동 조정
                self._emit(
                    order,
                    OrderState.SELECT_QUANTITY,
                    f"옵션 선택 대기 중... 옵션을 고르면 수량 {order.quantity} 로 자동 조정",
                )
                ok = await self._wait_option_then_set_quantity(page, order)
                if not ok or page.is_closed():
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.START.value,
                    )

                self._checkpoints[order.row] = Checkpoint.AT_PRODUCT_PAGE
                cp = Checkpoint.AT_PRODUCT_PAGE

            # 2) 사용자가 직접 '구매하기' 누르고 '기입' 버튼 클릭 대기
            if cp == Checkpoint.AT_PRODUCT_PAGE:
                if page.is_closed():
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.AT_PRODUCT_PAGE.value,
                    )
                self._emit(
                    order,
                    OrderState.CLICK_BUY,
                    "Chrome 에서 '구매하기' 를 누르면 자동으로 주문서 입력이 시작됩니다",
                )
                await self._await_user_fill(order.row, page=page)
                if page.is_closed():
                    raise UserInterventionRequired(
                        "사용자가 페이지를 닫아 행을 종료했습니다.",
                        checkpoint=Checkpoint.AT_PRODUCT_PAGE.value,
                    )
                self._checkpoints[order.row] = Checkpoint.AT_ORDER_PAGE
                cp = Checkpoint.AT_ORDER_PAGE

            # 3) 주문 정보 입력 (사용자가 '기입' 누른 후 실행됨)
            if cp == Checkpoint.AT_ORDER_PAGE:
                # 주문서 페이지가 떠 있는 활성 탭으로 page 핸들 갱신
                page = await self._switch_to_order_page(page)
                self._pages[order.row] = page

                self._emit(order, OrderState.FILL_FORM, "주문자 정보 자동 입력")
                await self._fill_order_form(page, order)
                self._checkpoints[order.row] = Checkpoint.FORM_FILLED
                cp = Checkpoint.FORM_FILLED

            # 4) 사용자가 결제 완료 후 '다음으로' 누를 때까지 대기
            if cp == Checkpoint.FORM_FILLED:
                # 4-a) 결제 직전 샵백 추적 검증
                self._verify_shopback_before_payment(order)

                # 결제·카드 인증 후 사용자가 직접 "다음으로" 를 눌러야 진행한다.
                self._emit(
                    order,
                    OrderState.WAIT_PAYMENT,
                    "결제 완료 후 상태칸의 '▶ 다음으로' 버튼을 눌러주세요",
                )
                await self._await_user_next(order.row)

                self._emit(order, OrderState.EXTRACT_ORDER_NO, "주문번호 추출 중")
                order_no, paid_amount = await self._wait_for_order_completion(
                    page, timeout_sec=15
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
            order.status = "paused"
            order.error_message = f"사용자 개입 필요: {exc}"
            if exc.checkpoint:
                try:
                    self._checkpoints[order.row] = Checkpoint(exc.checkpoint)
                except ValueError:
                    pass
            self._emit(order, OrderState.PAUSED, str(exc))
            log.warning(f"행{order.row} 일시정지: {exc}")
            # 일시정지도 오염 가능성이 있으므로 탭을 명시적으로 닫는다.
            # (사용자가 원하면 우클릭 → '이어서 진행' 으로 새 탭에서 다시 시작)
            if getattr(self.config, "skip_on_pause", True):
                await self.abandon(order, force_close=True)
            return order

        except Exception as exc:
            order.status = "failed"
            order.error_message = f"{type(exc).__name__}: {exc}"
            if self.config.screenshot_on_error:
                try:
                    order.screenshot_path = await save_error_screenshot(page, order.row)
                except Exception as shot_exc:
                    log.warning(f"스크린샷 저장 실패: {shot_exc}")
            self._emit(order, OrderState.FAILED, order.error_message)
            log.error(f"주문 실패 행{order.row}: {order.error_message}")
            # 에러 경로: 탭을 명시적으로 close() — 오염된 페이지가 다음 행에
            # 전파되지 않도록. 다음 행은 깨끗한 새 탭에서 시작.
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

    async def _wait_option_then_set_quantity(
        self, page: Page, order: Order, timeout_sec: int = 1800
    ) -> bool:
        """사용자가 옵션을 선택해서 사이드바에 추가될 때까지 대기.

        반환:
          True  — 옵션 감지 후 수량 조정 성공
          False — 페이지 닫힘 / 타임아웃 → 호출자가 빠르게 종료해야 함
        """
        qty = order.quantity
        if qty <= 0:
            return True

        elapsed = 0.0
        poll = 0.5
        sidebar_seen = False

        while elapsed < timeout_sec:
            if page.is_closed():
                log.info(
                    f"행{order.row}: 페이지 닫힘 → 옵션 대기 종료 (행 종료)"
                )
                return False
            try:
                cur = await self._read_current_quantity(page)
            except Exception:
                cur = None
            if cur is not None:
                sidebar_seen = True
                log.info(
                    f"행{order.row}: 옵션 감지됨 (현재 수량={cur}) → "
                    f"{qty} 로 조정 시도"
                )
                await self._select_quantity(page, order)
                return True
            await asyncio.sleep(poll)
            elapsed += poll

        if not sidebar_seen:
            log.warning(
                f"행{order.row}: 옵션 대기 타임아웃 ({timeout_sec}s)"
            )
        return False

    async def _select_quantity(self, page: Page, order: Order) -> None:
        """수량을 order.quantity 만큼 맞춘다. 4가지 UI 패턴 지원:
        1) <select> 드롭다운 — selectOption
        2) <input> — fill (현재 값 읽고 다르면 덮어쓰기)
        3) + / - 버튼 — 현재 수량 읽어서 차이만큼 클릭
        4) JS fallback — 페이지에서 수량 컨테이너를 찾아 직접 조작
        """
        qty = order.quantity
        if qty <= 0:
            return

        # 1) select 우선
        try:
            sel = await self.selectors.find(
                page, "product_page.quantity_select", timeout_ms=800
            )
            await sel.select_option(str(qty))
            log.info(f"행{order.row}: 수량 select 로 {qty} 선택")
            return
        except ElementNotFoundError:
            pass

        # 2) input 직접 입력
        try:
            inp = await self.selectors.find(
                page, "product_page.quantity_input", timeout_ms=800
            )
            # 현재 값 확인
            try:
                cur_val = await inp.input_value()
            except Exception:
                cur_val = ""
            await inp.click()
            try:
                await inp.fill("")
            except Exception:
                pass
            await inp.fill(str(qty))
            try:
                await inp.press("Tab")
            except Exception:
                pass
            # change 이벤트가 안 발화되는 경우 대비 — JS 로 강제 발화
            try:
                await inp.evaluate(
                    "el => { el.dispatchEvent(new Event('input', {bubbles: true}));"
                    " el.dispatchEvent(new Event('change', {bubbles: true})); }"
                )
            except Exception:
                pass
            log.info(
                f"행{order.row}: 수량 input 으로 {qty} 입력 (이전 값={cur_val!r})"
            )
            await asyncio.sleep(0.2)
            return
        except ElementNotFoundError:
            pass

        # 3) + / - 버튼 — 현재 수량 읽어서 차이만큼 클릭
        try:
            current = await self._read_current_quantity(page)
        except Exception:
            current = 1
        if current is None:
            current = 1
        diff = qty - current
        if diff != 0:
            try:
                if diff > 0:
                    btn = await self.selectors.find(
                        page,
                        "product_page.quantity_plus_button",
                        timeout_ms=800,
                    )
                    for _ in range(diff):
                        await btn.click()
                        await asyncio.sleep(0.1)
                else:
                    btn = await self.selectors.find(
                        page,
                        "product_page.quantity_minus_button",
                        timeout_ms=800,
                    )
                    for _ in range(-diff):
                        await btn.click()
                        await asyncio.sleep(0.1)
                log.info(
                    f"행{order.row}: 수량 +/- 버튼으로 {current} → {qty} 조정"
                )
                return
            except ElementNotFoundError:
                pass

        # 4) JS fallback — 페이지의 수량 영역을 추론해서 직접 조작
        if await self._set_quantity_via_js(page, qty):
            log.info(f"행{order.row}: 수량 JS fallback 으로 {qty} 설정")
            return

        if qty > 1:
            log.warning(
                f"행{order.row}: 수량 선택 UI를 찾지 못함 (수량={qty}). "
                "사용자가 직접 조정해 주세요"
            )

    async def _read_current_quantity(self, page: Page) -> int | None:
        """현재 페이지에 표시된 수량 값을 읽는다."""
        # 1) input value
        try:
            inp = await self.selectors.find(
                page, "product_page.quantity_input", timeout_ms=400
            )
            v = await inp.input_value()
            if v and v.strip().isdigit():
                return int(v.strip())
        except Exception:
            pass
        # 2) JS — 11번가 아마존관 신 UI + 일반 수량 컨테이너
        try:
            v = await page.evaluate(
                """() => {
                    // 11번가 아마존관 신 UI 우선
                    const newUi = document.querySelector(
                      '.c_product_input input[aria-live="assertive"], '
                      + '.c-card-item__cart input[type="text"], '
                      + 'input[aria-label="주문 수량"]'
                    );
                    if (newUi && newUi.value) {
                      const n = parseInt(newUi.value.replace(/[^\\d]/g, ''), 10);
                      if (!isNaN(n)) return n;
                    }
                    // 일반 input
                    const inp = document.querySelector(
                      'input[name*="qty" i], input[name*="Qty" i], '
                      + 'input[role="spinbutton"], input[aria-label*="수량"]'
                    );
                    if (inp && inp.value) {
                      const n = parseInt(inp.value.replace(/[^\\d]/g, ''), 10);
                      if (!isNaN(n)) return n;
                    }
                    const boxes = document.querySelectorAll(
                      '[class*="quantity" i], [class*="Quantity" i]'
                    );
                    for (const b of boxes) {
                      const t = (b.innerText || '').trim();
                      const m = t.match(/^\\s*(\\d+)\\s*$/m);
                      if (m) return parseInt(m[1], 10);
                    }
                    return null;
                }"""
            )
            if isinstance(v, (int, float)):
                return int(v)
        except Exception:
            pass
        return None

    async def _set_quantity_via_js(self, page: Page, qty: int) -> bool:
        """JS 로 수량 input/스피너 값을 직접 세팅 + change 이벤트 발화."""
        try:
            ok = await page.evaluate(
                r"""(qty) => {
                    function setVal(el, v) {
                      try { el.removeAttribute('readonly'); } catch(e){}
                      try { el.removeAttribute('disabled'); } catch(e){}
                      const setter = Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                      )?.set;
                      if (setter) setter.call(el, String(v));
                      else el.value = String(v);
                      el.dispatchEvent(new Event('input', {bubbles: true}));
                      el.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    const sel = document.querySelector(
                      'input[name*="qty" i], input[name*="Qty" i], '
                      + 'input[role="spinbutton"], input[aria-label*="수량"], '
                      + 'div[class*="quantity" i] input, '
                      + 'div[class*="Quantity" i] input'
                    );
                    if (!sel) return false;
                    setVal(sel, qty);
                    return true;
                }""",
                qty,
            )
            return bool(ok)
        except Exception as exc:
            log.debug(f"수량 JS fallback 실패: {exc}")
            return False

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
        """주문서 자동 입력 — 두 단계로 분리:

        1단계 (이 함수에서 수행):
          - 직접입력 모드 전환
          - 받는사람 / 우편번호 / 기본주소 / 상세주소 (주소찾기 팝업까지 자동)
          - 전화번호
          - 그 후 사용자 '영문기입' 트리거 대기

        2단계 (사용자가 '영문기입' 누르면):
          - 통관번호
          - 영문이름
        """
        delay = self.config.typing_delay_ms

        # 0) 배송지 모드 → "직접입력" 으로 전환
        await self._switch_to_direct_input(page)

        # 1) 모든 필드 일괄 주입 (이름/전화/우편/주소만 — 통관/영문은 2단계에서)
        await self._js_sweep_all_fields(page, order)

        # 2) 받는사람 / 우편번호 / 기본주소 / 상세주소 셀렉터 보강
        await self._force_fill(
            page, "order_page.recipient_name", order.name, delay
        )
        await self._force_fill(
            page, "order_page.zipcode_input", order.postal_code, delay
        )
        await self._force_fill(
            page, "order_page.address_base", order.address, delay
        )
        await self._force_fill(
            page, "order_page.address_detail", order.address, delay
        )

        # 2-b) 주소찾기 팝업 자동 처리
        await self._ensure_address_filled(page, order)

        # 3) 전화번호 - 통합 or 분리 필드
        if await self.selectors.exists(page, "order_page.phone", timeout_ms=800):
            digits_only = order.phone.replace("-", "")
            await self._force_fill(
                page, "order_page.phone", digits_only, delay
            )
        else:
            parts = order.phone.split("-")
            if len(parts) == 3:
                try:
                    prefix_loc = await self.selectors.find(
                        page, "order_page.phone_prefix", timeout_ms=2000
                    )
                    await prefix_loc.select_option(parts[0])
                except ElementNotFoundError:
                    pass
                await self._force_fill(
                    page, "order_page.phone_middle", parts[1], delay
                )
                await self._force_fill(
                    page, "order_page.phone_suffix", parts[2], delay
                )

        # 4) 사용자가 '영문기입' 누를 때까지 대기 → 그 후 통관/영문 자동 입력
        self._emit(
            order,
            OrderState.FILL_FORM,
            "받는사람·주소·전화 입력 완료. 상태칸의 '영문기입' 버튼을 눌러주세요",
        )
        await self._await_user_eng_fill(order.row)

        # 5) 통관번호 — 실패하면 주문을 실패시킨다
        self._emit(order, OrderState.FILL_FORM, "통관번호·영문이름 자동 입력 중")
        await self._fill_customs_id_or_fail(page, order)

        # 6) 영문 이름 — 통합/분리/재시도/JS 강제 주입 4단계 fallback
        eng_name = (order.english_name or "").strip()
        if eng_name:
            await self._fill_english_name(page, eng_name, delay)

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
        popup_open = await self._is_address_popup_open(page)
        if not popup_open:
            clicked = await self._click_address_search_button(page)
            if clicked:
                # 팝업이 뜰 때까지 잠깐 대기
                for _ in range(15):
                    await asyncio.sleep(0.2)
                    if await self._is_address_popup_open(page):
                        popup_open = True
                        break

        # 2~3) 팝업이 열렸으면 자동 검색 + 첫 결과 선택
        if popup_open:
            try:
                query = order.address_search_query() or order.postal_code
                ok = await self._auto_search_and_pick_address(page, query)
                if ok:
                    log.info(
                        f"행{order.row}: 주소찾기 팝업 자동 검색·선택 완료 ({query!r})"
                    )
                    await asyncio.sleep(0.4)
                else:
                    log.warning(
                        f"행{order.row}: 주소찾기 팝업 자동 처리 실패 — 사용자가 직접 선택"
                    )
            except Exception as exc:
                log.debug(f"행{order.row}: 주소찾기 팝업 자동 처리 예외: {exc}")

        # 4~5) 상세주소 + 비어있는 칸 라벨에 강제 주입
        await self._js_inject_address_fields(page, order)
        await asyncio.sleep(0.2)

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
                if not (status.get("postal") and status.get("base") and status.get("detail")):
                    log.info(f"행{order.row}: 주소 일부 비어있음 → 재주입")
                    await self._js_inject_address_fields(page, order)
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
        # 2) 별도 popup window — context 의 다른 page 중 주소찾기 URL
        try:
            ctx = page.context
            for p in ctx.pages:
                if p is page or p.is_closed():
                    continue
                url = p.url or ""
                if "/addr/" in url or "searchAddr" in url or "zipcode" in url.lower():
                    return True
        except Exception:
            pass
        return False

    async def _find_address_popup_pages(self, page: Page) -> list[Page]:
        """주소찾기 별도 창으로 떠 있는 page 들을 모두 반환."""
        out: list[Page] = []
        try:
            ctx = page.context
            for p in ctx.pages:
                if p is page or p.is_closed():
                    continue
                url = p.url or ""
                if "/addr/" in url or "searchAddr" in url or "zipcode" in url.lower():
                    out.append(p)
        except Exception:
            pass
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
        self, page: Page, query: str
    ) -> bool:
        """팝업에서 query 로 자동 검색 + 첫 결과 자동 클릭.

        팝업 형태:
          A) inline layer / 같은 페이지 iframe
          B) 별도 popup window (window.open) — 11번가 buy/addr/searchAddrV2.tmall
        세 곳 다 시도.
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
() => {
  function visible(el) {
    const cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  // 결과 목록
  const cands = document.querySelectorAll(
    '.search_result_box a, .search_result_box li, '
    + '#searchResultBox a, #searchResultBox li, '
    + '.layer_addr a, .layer_addr li, '
    + '[class*="AddressSearch" i] a, [class*="AddressSearch" i] li, '
    + '[class*="result" i] a, [class*="result" i] li, '
    + 'ul[class*="addr" i] li, '
    + '[role="listitem"]'
  );
  // 도로명 우선
  for (const el of cands) {
    if (!visible(el)) continue;
    const t = (el.innerText || '').trim();
    if (!t) continue;
    if (/도로명/.test(t)) {
      try { el.click(); return 'road:' + t.slice(0, 40); } catch(e) {}
    }
  }
  for (const el of cands) {
    if (!visible(el)) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length < 6) continue;
    try { el.click(); return 'pick:' + t.slice(0, 40); } catch(e) {}
  }
  return null;
}
"""

        async def try_in_context(target) -> bool:
            """target 은 Page 또는 Frame. 검색 + 결과 선택 시도."""
            try:
                r = await target.evaluate(search_js, [query])
                log.debug(f"주소찾기 검색 시도 ({getattr(target, 'url', lambda: '')()}): {r}")
                if r == 'no-input':
                    return False
            except Exception as exc:
                log.debug(f"주소찾기 검색 JS 실패: {exc}")
                return False
            # 결과 로딩 대기
            await asyncio.sleep(1.0)
            try:
                r2 = await target.evaluate(pick_js)
                if r2:
                    log.info(f"주소찾기 결과 선택: {r2}")
                    return True
            except Exception:
                return False
            return False

        # 1) 별도 popup window 우선 (가장 흔한 케이스)
        for popup_page in await self._find_address_popup_pages(page):
            try:
                # popup 이 완전 로드되기까지 대기
                await popup_page.wait_for_load_state(
                    "domcontentloaded", timeout=3000
                )
            except Exception:
                pass
            if await try_in_context(popup_page):
                return True
            # popup main_frame 이 안 됐으면 그 안의 iframe 시도
            for frame in popup_page.frames:
                if frame is popup_page.main_frame:
                    continue
                if await try_in_context(frame):
                    return True

        # 2) 같은 page 의 main + iframe
        if await try_in_context(page):
            return True
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            if await try_in_context(frame):
                return True
        return False

    async def _js_inject_address_fields(self, page: Page, order: Order) -> None:
        """우편번호 / 기본주소 / 상세주소 input 에 JS 로 직접 값 주입.

        - readonly/disabled 강제 해제
        - 기존 값 무시하고 무조건 덮어쓰기 (이전 자동화 시도가 잘못된 값을 넣었을 수 있음)
        - placeholder/name/id/aria-label 어디든 매칭되면 주입
        - input/change 이벤트 dispatch (React/Vue 의 controlled input 도 동작)
        """
        try:
            touched = await page.evaluate(
                r"""([postal, address]) => {
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
                    // 우편번호 — 무조건 덮어씀
                    if (/(zip|post|우편)/.test(hay)) {
                      setVal(el, postal);
                      touched.push('postal:' + (el.name || el.id || el.placeholder));
                      continue;
                    }
                    // 상세주소 (먼저 매칭 — 'addr' 매칭이 base 로 빠지지 않게)
                    if (/(상세.*주소|상세.*건물|addr.*dtl|dtls.*addr|addrDetail|rcvrDtls)/i.test(hay)) {
                      setVal(el, address);
                      touched.push('detail:' + (el.name || el.id || el.placeholder));
                      continue;
                    }
                    // 기본주소
                    if (/(기본.*주소|base.*addr|baseAddr|rcvrBaseAddr|^addr$|pickupBaseAddr)/i.test(hay)) {
                      setVal(el, address);
                      touched.push('base:' + (el.name || el.id || el.placeholder));
                      continue;
                    }
                    // 그 외 'addr' 가 들어가 있으면 일단 base 로 간주 (가장 마지막 fallback)
                    if (/addr|주소/i.test(hay) && !el.value) {
                      setVal(el, address);
                      touched.push('addr-fallback:' + (el.name || el.id || el.placeholder));
                      continue;
                    }
                  }
                  return touched;
                }""",
                [order.postal_code, order.address],
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

        # 1차 시도
        if await _try_once():
            return

        # 통관번호 조회 결과 등 비동기 갱신 대기 후 재시도 (최대 3회)
        for attempt in range(3):
            await asyncio.sleep(0.3)
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

        # 4) 두번째 주소칸(상세주소)에 수취인 주소 전체 입력
        if order.address and await self.selectors.exists(
            page, "order_page.address_detail", timeout_ms=1500
        ):
            await self._force_fill(
                page, "order_page.address_detail", order.address, delay
            )
            log.info(f"행{order.row}: 상세주소칸에 수취인 주소 전체 입력 완료")

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

        # 0) 통관번호 섹션이 lazy-load 되는 경우를 대비해 페이지 하단까지 스크롤
        try:
            await page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"
            )
            await asyncio.sleep(0.2)
            # 스크롤 후 다시 위로 (UI 원복)
            await page.evaluate("() => window.scrollTo(0, 0)")
        except Exception:
            pass

        # 1) 셀렉터 경로
        filled = False
        if await self.selectors.exists(page, "order_page.customs_id", timeout_ms=1500):
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
    const hay = Object.values(info).join(' ').toLowerCase();
    const isMatch = /prsn.*cstms|cstms.*cd|prsncstms|customs|통관|개인통관|통관고유|p로\s*시작|13자리/i.test(hay);
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
    ) -> None:
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
    // 상세주소
    if (/(상세.*주소|상세.*건물|addr.*dtl|dtls.*addr|addrDetail)/i.test(hay)) {
      setVal(el, order.address);
      touched.push('addr_dtl:' + (el.name || el.id || el.placeholder));
      continue;
    }
    // 기본주소
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
        except Exception as exc:
            log.debug(f"JS sweep 실패: {exc}")

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
        try:
            text = await self.selectors.get_text(
                page, "confirmation.order_number", timeout_ms=5000
            )
            import re
            match = re.search(r"[\d\-]{8,}", text)
            order_no = match.group(0) if match else text
        except ElementNotFoundError as exc:
            raise ElementNotFoundError(
                "주문번호 추출 실패: 완료 페이지의 주문번호 요소를 찾을 수 없습니다"
            ) from exc

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

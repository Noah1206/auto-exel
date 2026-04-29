"""Playwright persistent context 관리."""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from src.exceptions import BrowserError
from src.models.settings import BrowserConfig
from src.utils.logger import get_logger

log = get_logger()


class BrowserManager:
    """하나의 persistent Chrome 컨텍스트를 앱 수명 동안 재사용."""

    def __init__(self, config: BrowserConfig, stealth_enabled: bool = True):
        self.config = config
        self.stealth_enabled = stealth_enabled
        self._playwright = None
        self._context: BrowserContext | None = None
        self._stealth = None  # Stealth() instance (lazy init)

    # -------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------

    async def start(self) -> BrowserContext:
        if self._context is not None:
            if self._is_context_alive(self._context):
                return self._context
            # 사용자가 Chrome 창을 닫았거나 크래시 → 재시작해야 후속 주문이 살아남는다.
            log.warning("이전 브라우저 컨텍스트가 닫혀있음 — 재시작합니다")
            self._context = None
            await self._safe_stop_playwright()
            self._stealth = None

        profile = Path(self.config.profile_dir).absolute()
        profile.mkdir(parents=True, exist_ok=True)

        log.info(f"크롬 프로필: {profile}")

        # 이전 Chrome 이 비정상 종료되면 SingletonLock(macOS/Linux) / lockfile(Windows)
        # 가 남아 다음 launch 가 "profile is already in use" 로 실패한다.
        # 자동 재시작 흐름에서 이게 항상 발목을 잡으니 stale lock 은 제거한다.
        # 실행 중인 Chrome 이 있으면 잠금 파일이 즉시 다시 생성되므로 안전.
        for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            try:
                lock_path = profile / lock_name
                if lock_path.exists() or lock_path.is_symlink():
                    lock_path.unlink()
                    log.info(f"이전 프로필 잠금 파일 제거: {lock_name}")
            except Exception as exc:
                log.debug(f"잠금 파일 제거 실패 ({lock_name}): {exc}")

        self._playwright = await async_playwright().start()

        # 창 위치/크기 정책:
        # 과거에는 hide_window=True 일 때 --window-position=-3000,0 으로 화면 밖으로
        # 밀고, 필요할 때 osascript/ctypes 로 끌어왔다. 그러나 macOS osascript 는
        # 'process "Google Chrome" / window 1' 만 매칭해 사용자가 평소 쓰는 Chrome
        # 창을 건드렸고, Windows ctypes 도 chrome.exe PID 단위 매칭이라 사용자
        # 평소 Chrome 창까지 같이 리사이즈하는 사이드 이펙트가 있었다.
        #
        # 우리 Playwright Chrome 만 정확히 식별할 안정적 방법이 없어 창 이동을
        # 중단하기로 결정. Playwright 가 띄운 창은 OS 기본 위치/사이즈로 그대로
        # 떠 있고, 사용자가 직접 옮기거나 사이즈 조정한다.
        # 샵백 확장프로그램은 headful 유지가 필수라 headless 는 사용하지 않는다.
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]

        # Windows: 시스템 Chrome 채널 fallback — chrome → chrome-beta → msedge.
        # macOS/Linux 는 첫 시도만.
        channels_to_try = [self.config.channel]
        if sys.platform == "win32" and self.config.channel == "chrome":
            channels_to_try += ["chrome-beta", "msedge"]

        last_exc = None
        for ch in channels_to_try:
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile),
                    channel=ch,
                    headless=self.config.headless,
                    # viewport=None: 페이지/창 크기를 Playwright 가 강제하지 않음.
                    # 과거에 viewport 를 명시하면 Chrome 이 시스템에 떠있는 다른 Chrome
                    # 창까지 같은 사이즈로 리사이즈하는 사이드이펙트가 있었음.
                    # no_viewport=True 이면 페이지 viewport 는 창 크기에 따른다.
                    no_viewport=True,
                    locale=self.config.locale,
                    timezone_id=self.config.timezone,
                    args=launch_args,
                    # --enable-automation: stealth 위해 제거
                    # --disable-extensions: 사용자가 설치한 확장프로그램(샵백 등) 사용 위해 제거
                    ignore_default_args=["--enable-automation", "--disable-extensions"],
                )
                if ch != self.config.channel:
                    log.info(f"브라우저 채널 fallback 성공: {ch}")
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                log.warning(f"채널 {ch} 실행 실패: {exc} — 다음 채널 시도")

        if last_exc is not None:
            await self._safe_stop_playwright()
            raise BrowserError(
                f"Chrome 실행 실패 (시도 채널: {channels_to_try}): {last_exc}. "
                f"시스템에 Chrome 또는 Edge 가 설치되어 있는지 확인하세요."
            ) from last_exc

        self._context.set_default_timeout(self.config.default_timeout_ms)
        self._context.set_default_navigation_timeout(self.config.navigation_timeout_ms)

        # 컨텍스트가 닫히면(사용자 창 닫기/크래시) 즉시 핸들 비워 다음 start() 가 재시작하게 한다.
        try:
            self._context.on("close", self._on_context_closed)
        except Exception as exc:
            log.debug(f"context close 핸들러 등록 실패 (무시): {exc}")

        # JS confirm()/alert()/prompt() 다이얼로그를 일반 Chrome 처럼 사용자에게
        # 보여줘야 한다 (예: 11번가 개인통관부호 삭제 confirm).
        # 모든 페이지에 빈 dialog 리스너를 부착해 Playwright 의 자동 dismiss 를 막는다.
        try:
            self._context.on("page", self._disable_auto_dialog)
            for page in self._context.pages:
                self._disable_auto_dialog(page)
        except Exception as exc:
            log.debug(f"dialog 자동 dismiss 차단 등록 실패 (무시): {exc}")

        # stealth 초기화 (선택)
        if self.stealth_enabled:
            try:
                from playwright_stealth import Stealth

                self._stealth = Stealth()
                # 기존 페이지들에도 적용
                for page in self._context.pages:
                    await self._apply_stealth(page)
                log.info("playwright-stealth 활성화")
            except Exception as exc:
                log.warning(f"stealth 초기화 실패 (계속 진행): {exc}")
                self._stealth = None

        log.info(f"브라우저 시작 완료: pages={len(self._context.pages)}")
        return self._context

    @staticmethod
    def _is_context_alive(ctx: BrowserContext) -> bool:
        """컨텍스트(브라우저)가 아직 살아있는지 비파괴적으로 점검.

        Playwright 의 닫힘 신호는 한 곳에서 깔끔하게 안 나와서 다중 체크:
          1) 내부 _impl_obj._closed_or_closing (있으면) — 가장 빠름
          2) browser.is_connected() — 브라우저 프로세스 단위
          3) ctx.pages 길이 — 닫힌 persistent context 는 0 이거나 stale page
             (단 이 판단만으로는 부족)
          4) ctx.cookies(...) await 호출 — 가장 확실하지만 async 라 여기선 못 씀.

        가짜 alive 보다는 가짜 dead 가 안전(재시작하면 그만). 한 신호라도 죽었다고
        말하면 죽은 것으로 간주.
        """
        try:
            # 1) Playwright 내부 플래그 — 가장 빠르고 정확
            impl = getattr(ctx, "_impl_obj", None)
            for attr in ("_closed_or_closing", "_closed", "_was_closed"):
                if impl is not None and getattr(impl, attr, False):
                    return False
            # 2) 브라우저 연결 상태
            browser = ctx.browser
            if browser is not None:
                try:
                    if not browser.is_connected():
                        return False
                except Exception:
                    return False
            # 3) pages 접근 — 닫힌 ctx 에서 예외 또는 stale 반환 모두 처리
            _ = ctx.pages
            return True
        except Exception:
            return False

    def _on_context_closed(self, *_args) -> None:
        """Playwright 가 컨텍스트 close 이벤트를 발사하면 핸들을 비워준다."""
        log.warning("브라우저 컨텍스트 close 이벤트 — 다음 주문에서 재시작 예정")
        self._context = None
        self._stealth = None

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception as exc:
                log.warning(f"컨텍스트 종료 오류: {exc}")
            self._context = None
        await self._safe_stop_playwright()

    async def _safe_stop_playwright(self) -> None:
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:
                log.warning(f"playwright stop 오류: {exc}")
            self._playwright = None

    # -------------------------------------------------------------
    # Page factory
    # -------------------------------------------------------------

    async def new_page(self) -> Page:
        ctx = await self.start()
        page = await ctx.new_page()
        await self._apply_stealth(page)
        # 과거: macOS 에서 새 탭 생성 시 사용자 OS 포커스를 빼앗는 것을 막기 위해
        # 즉시 창을 화면 밖으로 밀어냈다. 그러나 그 이동 로직이 사용자가 평소 쓰는
        # Chrome 창까지 같이 건드리는 사이드 이펙트가 있어 이동을 비활성화함.
        return page

    async def _apply_stealth(self, page: Page) -> None:
        if self._stealth is None:
            return
        try:
            await self._stealth.apply_stealth_async(page)
        except AttributeError:
            # playwright-stealth 2.0.x: apply via context manager or direct call
            try:
                from playwright_stealth import stealth_async  # legacy API fallback

                await stealth_async(page)
            except Exception as exc:
                log.debug(f"stealth 적용 실패: {exc}")
        except Exception as exc:
            log.debug(f"stealth 적용 실패: {exc}")

    # -------------------------------------------------------------
    # Convenience
    # -------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._context is not None

    async def _move_chrome_window(self, x: int, y: int) -> None:
        """Chrome 창 이동 — 비활성화 (no-op).

        과거: macOS osascript / Windows ctypes(EnumWindows+MoveWindow) 로
        Chrome 창을 화면 밖/안으로 이동시켜 포커스를 보호하려 했음.

        문제: macOS osascript 의 'process "Google Chrome" / window 1' 매칭과
              Windows EnumWindows 의 chrome.exe PID 매칭이 모두 사용자가
              평소 사용 중인 다른 Chrome 창까지 함께 이동·리사이즈하는
              사이드 이펙트가 있다 (사용자 보고 + 스크린샷 확인).

              우리 Playwright Chrome 만 정확히 식별할 안정적 방법이 없으므로
              창 이동 자체를 중단. Playwright 이 띄운 창은 OS 기본 위치에
              그대로 떠 있고, 사용자가 직접 옮길 수 있다.

        호출자 호환성을 위해 메서드는 유지하되 아무 동작도 하지 않는다.
        """
        return


    async def show_window(self, page: Page | None = None) -> None:
        """크롬 창을 화면 안으로 이동만 시킨다 (포커스 절대 건드리지 않음)."""
        await self._move_chrome_window(100, 50)

    # 하위 호환 alias (UI 에서 기존 호출명 유지)
    bring_to_front = show_window

    async def hide_window(self) -> None:
        """창을 화면 밖으로 이동만 시킨다 (포커스 절대 건드리지 않음)."""
        await self._move_chrome_window(-3000, 0)

    async def get_or_create_page(self) -> Page:
        """컨텍스트의 첫 페이지 재사용(있으면) 또는 새로 생성."""
        ctx = await self.start()
        if ctx.pages:
            return ctx.pages[0]
        return await self.new_page()

    # 11번가가 로그인 시 세팅하는 쿠키 이름들
    _LOGIN_COOKIE_NAMES = (
        "PCID",        # 비로그인도 받지만 만료 처리 다름
        "ASESSIONID",
        "SESSION_TICKET",
        "LASTLOGIN",
        "11ST_PCID",
        "AUTH_KEY",
        "MEM_NO",
    )

    async def is_logged_in(self, selectors=None, timeout_sec: float = 15.0) -> bool:
        """11번가 로그인 상태 확인 — 쿠키 우선 + 마이페이지 체크.

        전체 timeout_sec 안에 결과가 안 나오면 False (안전한 쪽).
        """
        import asyncio

        try:
            return await asyncio.wait_for(
                self._is_logged_in_impl(), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            log.warning(f"로그인 체크 {timeout_sec}초 timeout — 미로그인 간주")
            return False
        except Exception as exc:
            log.warning(f"로그인 체크 중 오류: {exc} — 미로그인 간주")
            return False

    async def _is_logged_in_impl(self) -> bool:
        ctx = await self.start()

        # 1) 쿠키 빠른 체크 (네트워크 무관)
        try:
            cookies = await ctx.cookies(["https://www.11st.co.kr/"])
            cookie_names = {c.get("name", "").upper() for c in cookies}
            login_hits = [n for n in self._LOGIN_COOKIE_NAMES if n.upper() in cookie_names]
            if any(n in ("MEM_NO", "AUTH_KEY", "SESSION_TICKET", "LASTLOGIN") for n in login_hits):
                log.debug(f"로그인 쿠키 감지: {login_hits}")
                return True
        except Exception as exc:
            log.debug(f"쿠키 체크 실패: {exc}")

        # 2) 마이페이지 한 번만 시도 (게스트면 login 페이지로 튕김)
        page = await self.new_page()
        try:
            try:
                await page.goto(
                    "https://www.11st.co.kr/myPage/initMyPage.tmall",
                    wait_until="domcontentloaded",
                    timeout=8000,
                )
            except Exception as exc:
                log.debug(f"마이페이지 로드 실패: {exc}")
                return False

            current_url = page.url.lower()
            if "login.11st.co.kr" in current_url or "/login" in current_url:
                return False

            # 로그인 상태 표시 요소
            indicators = [
                'a:has-text("로그아웃")',
                'button:has-text("로그아웃")',
                'a[href*="logout" i]',
                'a[href*="myPage" i]',
                'a[href*="MyPage" i]',
            ]
            for sel in indicators:
                try:
                    if await page.locator(sel).first.count() > 0:
                        return True
                except Exception:
                    continue
            # 마이페이지가 정상 로드되고 로그인 페이지로 안 튕겼으면 로그인된 것으로 간주
            return "mypage" in current_url or "initmypage" in current_url.replace("-", "")
        finally:
            try:
                await page.close()
            except Exception:
                pass

    @staticmethod
    def _disable_auto_dialog(page: Page) -> None:
        """JS 다이얼로그를 일반 Chrome 처럼 화면에 표시되게 한다.

        Playwright 기본 동작 (_browser_context.py:_on_dialog):
          - dialog 이벤트 listener 가 없으면 자동 dismiss
          - listener 가 있으면 listener 안에서 accept/dismiss 를 호출해야 함

        우리는 사용자에게 '삭제하시겠습니까?' 같은 confirm 을 직접 보여주고
        싶으므로, 비어있는 listener 만 등록한다. listener 가 존재하므로
        Playwright 의 자동 dismiss 가 동작하지 않고, 핸들러 안에서 아무것도
        안 부르면 Chrome 이 다이얼로그를 화면에 표시한 채로 유지한다.
        사용자가 직접 다이얼로그의 '확인' / '취소' 를 누르면 페이지가 진행된다.

        주의: 이 핸들러는 Chrome 의 자동화 비활성화 (launch 시
        ignore_default_args=['--enable-automation']) 와 결합돼야 정상 동작.
        """
        def _noop(_dialog) -> None:
            try:
                dtype = getattr(_dialog, "type", "")
                msg = getattr(_dialog, "message", "")
                log.info(
                    f"JS dialog 표시 (사용자 결정 대기): type={dtype} msg={msg!r}"
                )
            except Exception:
                pass
            # accept/dismiss 호출 안 함 — 사용자가 직접 클릭하게 둠.

        try:
            page.on("dialog", _noop)
        except Exception as exc:
            log.debug(f"dialog 핸들러 부착 실패: {exc}")

    async def open_login_page(self) -> None:
        """앱의 Chrome 프로필에서 11번가 로그인 페이지를 열어준다.

        사용자가 평소 쓰는 다른 Chrome 창에서 로그인하지 않도록,
        반드시 앱이 띄운 창에서 로그인하게 유도.
        """
        ctx = await self.start()
        page = await ctx.new_page()
        try:
            await page.goto(
                "https://login.11st.co.kr/auth/front/login.tmall",
                wait_until="domcontentloaded",
                timeout=15000,
            )
        except Exception as exc:
            log.warning(f"로그인 페이지 열기 실패: {exc}")
        # page는 닫지 않음 — 사용자가 그 페이지에서 로그인해야 함

    # 로그인 상태를 갖는 도메인들 — clear_login_state 시 이 도메인 쿠키를 지움.
    # 11번가 + 카카오 / SKT(T 인증) / 네이버 / 페이스북 / 구글 소셜 로그인 등 포괄.
    _LOGIN_DOMAINS_TO_CLEAR = (
        "11st.co.kr", "011st.com",
        "kakao.com", "kakaocorp.com", "daum.net",
        "sktelecom.com", "tworld.co.kr", "skt-id.com",
        "naver.com", "nid.naver.com",
        "facebook.com", "fb.com",
        "google.com", "googleapis.com", "accounts.google.com",
        "payco.com", "nhnpayco.com",
    )

    async def clear_login_state(self) -> None:
        """11번가/소셜 로그인 쿠키를 모두 지워 '비로그인' 상태로 초기화.

        Chrome 프로필 자체는 유지(설치된 확장프로그램, 즐겨찾기 등 보존)하되,
        로그인 토큰만 제거해서 매번 깨끗한 로그인 페이지로 시작하게 한다.
        프로그램 시작 시 호출하면 사용자가 직접 카카오/T월드 로그인 가능.
        """
        ctx = await self.start()
        try:
            cookies = await ctx.cookies()
        except Exception as exc:
            log.warning(f"쿠키 목록 조회 실패: {exc}")
            return

        keep: list[dict] = []
        cleared = 0
        for c in cookies:
            domain = (c.get("domain") or "").lstrip(".").lower()
            if any(d in domain for d in self._LOGIN_DOMAINS_TO_CLEAR):
                cleared += 1
                continue
            keep.append(c)

        try:
            await ctx.clear_cookies()
            if keep:
                await ctx.add_cookies(keep)
            log.info(
                f"로그인 쿠키 초기화: {cleared}개 제거, {len(keep)}개 유지"
            )
        except Exception as exc:
            log.warning(f"로그인 쿠키 초기화 실패: {exc}")

    # 샵백(Shopback) Chrome Web Store 페이지 — 한국 샵백 캐시백 보상 확장
    SHOPBACK_WEBSTORE_URL = (
        "https://chromewebstore.google.com/detail/"
        "%EC%83%B5%EB%B0%B1-%EC%BA%90%EC%8B%9C%EB%B0%B1-%EB%B3%B4%EC%83%81/"
        "djjjmdgomejlopjnccoejdhgjmiappap?hl=ko"
    )

    async def open_extensions_page(self) -> None:
        """샵백 확장프로그램 설치 페이지를 앱 Chrome 에서 새 탭으로 연다.

        chrome:// 내부 URL 은 Playwright 가 직접 열지 못하므로
        Chrome Web Store 의 샵백 페이지로 보낸다.
        """
        ctx = await self.start()
        page = await ctx.new_page()
        try:
            await page.goto(
                self.SHOPBACK_WEBSTORE_URL,
                wait_until="domcontentloaded",
                timeout=15000,
            )
        except Exception as exc:
            log.warning(f"확장프로그램 페이지 열기 실패: {exc}")
        # page 는 닫지 않음 — 사용자가 'Chrome에 추가' 클릭해야 함

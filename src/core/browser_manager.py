"""Playwright persistent context 관리."""
from __future__ import annotations

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
            return self._context

        profile = Path(self.config.profile_dir).absolute()
        profile.mkdir(parents=True, exist_ok=True)

        log.info(f"크롬 프로필: {profile}")

        self._playwright = await async_playwright().start()

        # 창을 화면 밖(왼쪽 -3000px)으로 밀어두어 사용자가 평소에는 못 보게 한다.
        # 샵백 확장프로그램은 헤드리스 모드에선 로드가 안 되므로 headful 유지가 필수.
        # 포커스는 어떤 경우에도 Chrome 으로 자동 이동하지 않는다.
        # 사용자가 UI 버튼으로 '크롬 창 보기'를 누르면 창 위치만 이동(포커스 X).
        hidden = getattr(self.config, "hide_window", True)
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if hidden:
            launch_args += [
                "--window-position=-3000,0",
                f"--window-size={self.config.viewport.width},{self.config.viewport.height}",
            ]
        else:
            launch_args.append("--start-maximized")

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                channel=self.config.channel,
                headless=self.config.headless,
                viewport={
                    "width": self.config.viewport.width,
                    "height": self.config.viewport.height,
                },
                locale=self.config.locale,
                timezone_id=self.config.timezone,
                args=launch_args,
                # --enable-automation: stealth 위해 제거
                # --disable-extensions: 사용자가 설치한 확장프로그램(샵백 등) 사용 위해 제거
                ignore_default_args=["--enable-automation", "--disable-extensions"],
            )
        except Exception as exc:
            await self._safe_stop_playwright()
            raise BrowserError(
                f"Chrome 실행 실패 (channel={self.config.channel}): {exc}. "
                f"시스템에 Chrome이 설치되어 있는지 확인하세요."
            ) from exc

        self._context.set_default_timeout(self.config.default_timeout_ms)
        self._context.set_default_navigation_timeout(self.config.navigation_timeout_ms)

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
        # macOS: 새 탭이 생기면 Chrome 앱이 frontmost 가 되며 사용자 OS 포커스를
        # 빼앗는다. hide_window 모드라면 즉시 창을 화면 밖으로 다시 밀어내
        # 사용자가 작업 중이던 앱의 포커스를 보존한다.
        if getattr(self.config, "hide_window", True):
            await self.hide_window()
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

    async def show_window(self, page: Page | None = None) -> None:
        """크롬 창을 화면 안으로 이동만 시킨다 (포커스 절대 건드리지 않음).

        - position 만 이동, activate/frontmost 는 절대 호출하지 않음.
        - Playwright Page.bring_to_front() 도 호출하지 않음 (이것도 포커스 이동).
        - 사용자가 필요하면 직접 Cmd+Tab 으로 크롬을 선택.
        """
        try:
            import asyncio as _asyncio
            # 중요: activate 없이, frontmost 설정 없이, 창 위치만 변경
            script = (
                'tell application "System Events"\n'
                '  tell process "Google Chrome"\n'
                '    try\n'
                '      set position of window 1 to {100, 50}\n'
                '    end try\n'
                '  end tell\n'
                'end tell'
            )
            proc = await _asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=_asyncio.subprocess.DEVNULL,
                stderr=_asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as exc:
            log.debug(f"창 위치 이동 실패: {exc}")

    # 하위 호환 alias (UI 에서 기존 호출명 유지)
    bring_to_front = show_window

    async def hide_window(self) -> None:
        """창을 화면 밖으로 이동만 시킨다 (포커스 절대 건드리지 않음)."""
        try:
            import asyncio as _asyncio
            script = (
                'tell application "System Events"\n'
                '  tell process "Google Chrome"\n'
                '    try\n'
                '      set position of window 1 to {-3000, 0}\n'
                '    end try\n'
                '  end tell\n'
                'end tell'
            )
            proc = await _asyncio.create_subprocess_exec(
                "osascript", "-e", script,
                stdout=_asyncio.subprocess.DEVNULL,
                stderr=_asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except Exception as exc:
            log.debug(f"창 숨기기 실패: {exc}")

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

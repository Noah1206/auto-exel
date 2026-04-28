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

        # 창을 화면 밖(왼쪽 -3000px)으로 밀어두어 사용자가 평소에는 못 보게 한다.
        # 샵백 확장프로그램은 헤드리스 모드에선 로드가 안 되므로 headful 유지가 필수.
        # 포커스는 어떤 경우에도 Chrome 으로 자동 이동하지 않는다.
        # 사용자가 UI 버튼으로 '크롬 창 보기'를 누르면 창 위치만 이동(포커스 X).
        #
        # Windows 한정: --window-position=-3000,0 으로 launch 한 뒤 PowerShell
        # MoveWindow 로 다시 안으로 못 끌어오는 사례 (멀티모니터/PowerShell 차단/
        # MainWindowHandle 미할당 등) 가 잦아서, Windows 에서는 hide_window 를
        # 무시하고 처음부터 화면 안 좌표(0,0) 에 띄운다. 사용자 포커스 보호는
        # macOS 보다 덜 민감하고, 보이는 게 안 보이는 것보다 낫다.
        hidden = getattr(self.config, "hide_window", True)
        if sys.platform == "win32":
            hidden = False
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ]
        if hidden:
            launch_args += [
                "--window-position=-3000,0",
                f"--window-size={self.config.viewport.width},{self.config.viewport.height}",
            ]
        elif sys.platform == "win32":
            # Windows: maximized 만으론 멀티모니터/이전 좌표 기억 등으로
            # 화면 밖에 뜰 수 있어 (0,0) 좌표 + 사이즈 명시 + maximized 동시 적용.
            launch_args += [
                "--window-position=0,0",
                f"--window-size={self.config.viewport.width},{self.config.viewport.height}",
                "--start-maximized",
            ]
        else:
            launch_args.append("--start-maximized")

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
        # macOS: 새 탭이 생기면 Chrome 앱이 frontmost 가 되며 사용자 OS 포커스를
        # 빼앗는다. hide_window 모드라면 즉시 창을 화면 밖으로 다시 밀어내
        # 사용자가 작업 중이던 앱의 포커스를 보존한다.
        # Windows 는 hide_window 무시 (창이 화면 안에 그대로 보이는 게 더 중요).
        if sys.platform != "win32" and getattr(self.config, "hide_window", True):
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

    async def _move_chrome_window(self, x: int, y: int) -> None:
        """Chrome 창을 (x, y) 로 이동. macOS 는 osascript, Windows 는 ctypes/PowerShell.

        포커스를 빼앗지 않도록 activate/frontmost 는 절대 호출하지 않는다.
        실패해도 silently 무시 — 창 이동은 best-effort.

        Windows 는 ctypes(user32.dll) 직접 호출이 1순위. PowerShell 차단 환경
        (ExecutionPolicy / ConstrainedLanguage / pwsh 만 설치됨) 모두 우회.
        ctypes 도 실패하면 PowerShell fallback.
        """
        import asyncio as _asyncio
        try:
            if sys.platform == "darwin":
                script = (
                    'tell application "System Events"\n'
                    '  tell process "Google Chrome"\n'
                    '    try\n'
                    f'      set position of window 1 to {{{x}, {y}}}\n'
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
            elif sys.platform == "win32":
                if await self._move_chrome_window_ctypes(x, y):
                    return
                log.debug("ctypes 창 이동 실패 → PowerShell fallback")
                await self._move_chrome_window_powershell(x, y)
            else:
                # Linux: 창 매니저별로 다르므로 no-op
                return
        except Exception as exc:
            log.debug(f"창 위치 이동 실패: {exc}")

    async def _move_chrome_window_ctypes(self, x: int, y: int) -> bool:
        """ctypes 로 user32.dll 직접 호출 — PowerShell 차단 환경 우회.

        MainWindowHandle 이 아직 만들어지지 않은 타이밍 보호를 위해 0.1s 간격 3회 retry.
        Chrome 프로세스 PID 매칭으로 다른 사용자 Chrome 창은 건드리지 않는다.
        """
        import asyncio as _asyncio

        def _do_move() -> bool:
            try:
                import ctypes
                from ctypes import wintypes
            except Exception:
                return False
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
            except Exception:
                return False

            # 시그니처 정의
            EnumWindows = user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
            )
            EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
            EnumWindows.restype = wintypes.BOOL

            GetWindowThreadProcessId = user32.GetWindowThreadProcessId
            GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
            ]
            GetWindowThreadProcessId.restype = wintypes.DWORD

            IsWindowVisible = user32.IsWindowVisible
            IsWindowVisible.argtypes = [wintypes.HWND]
            IsWindowVisible.restype = wintypes.BOOL

            GetWindowTextLengthW = user32.GetWindowTextLengthW
            GetWindowTextLengthW.argtypes = [wintypes.HWND]
            GetWindowTextLengthW.restype = ctypes.c_int

            ShowWindow = user32.ShowWindow
            ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            ShowWindow.restype = wintypes.BOOL

            MoveWindow = user32.MoveWindow
            MoveWindow.argtypes = [
                wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.BOOL,
            ]
            MoveWindow.restype = wintypes.BOOL

            SW_SHOWNOACTIVATE = 4
            target_pids: set[int] = set()

            # Chrome 프로세스 PID 수집 — Playwright 로 띄운 우리 Chrome 만 노린다.
            # ctx.browser._impl_obj 에서 직접 얻기 어려우니, image name 로
            # tasklist 대신 ctypes 로 모든 chrome.exe PID 를 잡는다.
            try:
                # toolhelp32 로 PID 수집
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                TH32CS_SNAPPROCESS = 0x00000002
                INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

                class PROCESSENTRY32W(ctypes.Structure):
                    _fields_ = [
                        ("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260),
                    ]

                snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
                if snap and snap != INVALID_HANDLE_VALUE:
                    try:
                        entry = PROCESSENTRY32W()
                        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                            while True:
                                exe = (entry.szExeFile or "").lower()
                                if exe in ("chrome.exe", "msedge.exe"):
                                    target_pids.add(int(entry.th32ProcessID))
                                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                                    break
                    finally:
                        kernel32.CloseHandle(snap)
            except Exception:
                target_pids = set()

            if not target_pids:
                # PID 못 잡으면 ctypes 경로 포기 (MoveWindow 무차별 호출은 위험)
                return False

            moved_any = [False]
            w = self.config.viewport.width
            h = self.config.viewport.height

            def _enum_proc(hwnd, _lparam):
                try:
                    if not IsWindowVisible(hwnd):
                        return True
                    if GetWindowTextLengthW(hwnd) == 0:
                        return True
                    pid = wintypes.DWORD(0)
                    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if int(pid.value) not in target_pids:
                        return True
                    # 우리 Chrome 창 — 포커스 안 빼앗고 이동
                    ShowWindow(hwnd, SW_SHOWNOACTIVATE)
                    MoveWindow(hwnd, int(x), int(y), int(w), int(h), True)
                    moved_any[0] = True
                except Exception:
                    pass
                return True

            try:
                EnumWindows(EnumWindowsProc(_enum_proc), 0)
            except Exception:
                return False
            return moved_any[0]

        # 핸들 늦게 만들어지는 경우 대비 0.1s × 3회 retry
        for _ in range(3):
            try:
                ok = await _asyncio.get_event_loop().run_in_executor(None, _do_move)
            except Exception as exc:
                log.debug(f"ctypes 창 이동 실패: {exc}")
                return False
            if ok:
                return True
            await _asyncio.sleep(0.1)
        return False

    async def _move_chrome_window_powershell(self, x: int, y: int) -> None:
        """레거시 PowerShell 경로 — ctypes 가 실패한 환경에서만 호출."""
        import asyncio as _asyncio
        ps_script = (
            "$sig=@\"\n"
            "  [DllImport(\"user32.dll\")] public static extern bool MoveWindow(IntPtr hWnd,int X,int Y,int W,int H,bool R);\n"
            "  [DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr hWnd,int n);\n"
            "\"@\n"
            "Add-Type -MemberDefinition $sig -Name W -Namespace U -UsingNamespace System.Runtime.InteropServices\n"
            "Get-Process chrome,msedge -ErrorAction SilentlyContinue | "
            "Where-Object {$_.MainWindowHandle -ne 0} | "
            "ForEach-Object {\n"
            f"  [U.W]::ShowWindow($_.MainWindowHandle, 4)\n"
            f"  [U.W]::MoveWindow($_.MainWindowHandle, {x}, {y}, "
            f"{self.config.viewport.width}, {self.config.viewport.height}, $true)\n"
            "}"
        )
        # powershell.exe 없으면 pwsh 로 fallback
        for exe in ("powershell", "pwsh"):
            try:
                proc = await _asyncio.create_subprocess_exec(
                    exe, "-NoProfile", "-WindowStyle", "Hidden",
                    "-ExecutionPolicy", "Bypass",
                    "-Command", ps_script,
                    stdout=_asyncio.subprocess.DEVNULL,
                    stderr=_asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                return
            except FileNotFoundError:
                continue
            except Exception as exc:
                log.debug(f"{exe} 창 이동 실패: {exc}")
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

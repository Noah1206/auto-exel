# 기술 아키텍처 문서 (Architecture)

**대상**: 개발자 / 기술 검토자
**작성일**: 2026-04-21
**관련 문서**: `PLAN.md` (비즈니스), `selectors.yaml` (셀렉터)

---

## 1. 시스템 아키텍처 다이어그램

```
┌──────────────────────────────────────────────────────────┐
│                     사용자 (클라이언트)                   │
└────────────────────────┬─────────────────────────────────┘
                         │
         ┌───────────────┴────────────────┐
         │   PySide6 Main Window (UI)     │
         │   ┌────────────────────────┐   │
         │   │ QTableView (orders)    │   │
         │   │ QPlainTextEdit (logs)  │   │
         │   │ QStatusBar (status)    │   │
         │   └────────────────────────┘   │
         └────┬──────────────────┬────────┘
              │ signals/slots    │
              ▼                  ▼
    ┌─────────────────┐  ┌──────────────────┐
    │ AsyncWorker     │  │ ExcelManager     │
    │ (QThread+qasync)│  │ (openpyxl)       │
    └────────┬────────┘  └────────┬─────────┘
             │                    │
             ▼                    ▼
    ┌──────────────────────────────────┐
    │   BrowserManager (Playwright)    │
    │   - persistent_context           │
    │   - page pool                    │
    │   - event handlers               │
    └──────────┬───────────────────────┘
               │
       ┌───────┴───────┐
       ▼               ▼
┌─────────────┐  ┌────────────────┐
│System Chrome│  │ StateManager   │
│ + Shopback  │  │ (state.json)   │
└─────────────┘  └────────────────┘
```

---

## 2. 모듈 구조

```
kmong_11st_order/
├── main.py                       # 앱 엔트리포인트
├── config/
│   ├── settings.yaml             # 사용자 설정 (편집 가능)
│   ├── selectors.yaml            # 11번가 셀렉터 (유지보수 포인트)
│   └── default_settings.yaml     # 기본값
├── src/
│   ├── core/
│   │   ├── browser_manager.py       # Playwright 컨텍스트 생명주기
│   │   ├── order_automation.py      # 주문 자동입력 (state machine)
│   │   ├── price_scraper.py         # 가격 조회
│   │   ├── excel_manager.py         # Excel I/O (openpyxl)
│   │   ├── state_manager.py         # 크래시 복구
│   │   └── selector_loader.py       # YAML → 셀렉터 객체
│   ├── ui/
│   │   ├── main_window.py           # QMainWindow
│   │   ├── order_table_model.py     # QAbstractTableModel (pandas)
│   │   ├── log_panel.py             # QPlainTextEdit 로그뷰
│   │   ├── settings_dialog.py       # QDialog (설정)
│   │   ├── first_run_wizard.py      # 첫 실행 마법사
│   │   └── widgets/
│   │       ├── status_bar.py
│   │       ├── progress_delegate.py # 진행률 셀 렌더러
│   │       └── color_role.py        # 상태별 색상
│   ├── models/
│   │   ├── order.py                 # @dataclass Order
│   │   ├── settings.py              # Pydantic Settings
│   │   └── state.py                 # Pydantic State
│   ├── utils/
│   │   ├── logger.py                # loguru 설정
│   │   ├── retry.py                 # @retry 데코레이터
│   │   ├── screenshot.py            # 에러 시 자동 캡처
│   │   ├── async_worker.py          # QThread + qasync
│   │   └── validators.py            # 전화번호/통관번호 검증
│   └── exceptions.py                # 커스텀 예외 클래스
├── data/
│   ├── chrome_profile/              # Chrome 프로필 (사용자별)
│   ├── logs/                        # 일별 로그
│   ├── screenshots/                 # 에러 스크린샷
│   └── backups/                     # 엑셀 백업
├── tests/
│   ├── unit/
│   │   ├── test_excel_manager.py
│   │   ├── test_validators.py
│   │   └── test_selector_loader.py
│   ├── integration/
│   │   └── test_order_flow.py       # 실제 11번가 (mock)
│   └── fixtures/
│       ├── sample_orders.xlsx
│       └── mock_pages/              # 캡처된 HTML
├── build/
│   ├── build.spec                   # PyInstaller 설정
│   ├── version.txt                  # 버전 정보
│   └── icon.ico                     # 앱 아이콘
├── requirements.txt
├── pyproject.toml                   # ruff/black/pytest 설정
├── README.md                        # 사용자용
└── ARCHITECTURE.md                  # 이 문서
```

---

## 3. 핵심 데이터 모델

### 3.1 Order (pydantic)
```python
from pydantic import BaseModel, Field, HttpUrl, field_validator
from datetime import datetime
from typing import Optional, Literal
import re

OrderStatus = Literal["pending", "in_progress", "completed", "failed"]

class Order(BaseModel):
    row: int                          # 엑셀 행 번호
    product_url: HttpUrl              # 상품링크
    name: str = Field(min_length=1, max_length=20)
    phone: str                        # 자동 정규화
    customs_id: str                   # P123456789012
    address: str
    english_name: str

    # 자동 채워지는 필드
    price: Optional[int] = None
    order_number: Optional[str] = None
    ordered_at: Optional[datetime] = None
    status: OrderStatus = "pending"
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if not digits.startswith(("010", "011")):
            raise ValueError("010 또는 011로 시작해야 합니다")
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"

    @field_validator("customs_id")
    @classmethod
    def validate_customs(cls, v: str) -> str:
        if not re.match(r"^P\d{12}$", v):
            raise ValueError("통관번호 형식: P + 12자리 숫자")
        return v

    @field_validator("english_name")
    @classmethod
    def validate_english(cls, v: str) -> str:
        if not re.match(r"^[A-Z\s]+$", v):
            raise ValueError("영문 대문자만 허용")
        return v.strip()

    @field_validator("product_url")
    @classmethod
    def validate_11st_url(cls, v):
        if "11st.co.kr" not in str(v):
            raise ValueError("11번가 URL이 아닙니다")
        return v
```

### 3.2 AppState (크래시 복구)
```python
class AppState(BaseModel):
    session_id: str
    excel_path: str
    last_processed_row: int = 0
    completed_rows: list[int] = []
    failed_rows: list[int] = []
    updated_at: datetime

    def save(self, path: str = "data/state.json"):
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: str = "data/state.json") -> Optional["AppState"]:
        if not Path(path).exists():
            return None
        return cls.model_validate_json(Path(path).read_text())
```

---

## 4. 브라우저 관리 구현

```python
# src/core/browser_manager.py
from playwright.async_api import async_playwright, BrowserContext
from pathlib import Path

class BrowserManager:
    def __init__(self, profile_dir: str = "data/chrome_profile"):
        self.profile_dir = Path(profile_dir).absolute()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self._context: Optional[BrowserContext] = None

    async def start(self) -> BrowserContext:
        """Chrome persistent context 시작"""
        if self._context:
            return self._context

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="chrome",             # system Chrome
            headless=False,               # extensions require headed
            viewport={"width": 1400, "height": 900},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
        )

        # 기본 타임아웃
        self._context.set_default_timeout(15000)
        self._context.set_default_navigation_timeout(30000)

        # stealth 적용 (최소한)
        from playwright_stealth import stealth_async
        for page in self._context.pages:
            await stealth_async(page)

        return self._context

    async def new_page(self):
        ctx = await self.start()
        page = await ctx.new_page()
        from playwright_stealth import stealth_async
        await stealth_async(page)
        return page

    async def close(self):
        if self._context:
            await self._context.close()
        if self._playwright:
            await self._playwright.stop()
        self._context = None
        self._playwright = None
```

---

## 5. 셀렉터 전략 (Fallback)

```python
# src/core/selector_loader.py
from playwright.async_api import Page, Locator
import yaml
from typing import Union

class SelectorHelper:
    def __init__(self, yaml_path: str = "config/selectors.yaml"):
        with open(yaml_path, encoding="utf-8") as f:
            self.selectors = yaml.safe_load(f)

    def get(self, path: str) -> list[str]:
        """예: 'order_page.recipient_name' → ['input[name=...]', ...]"""
        node = self.selectors
        for key in path.split("."):
            node = node[key]
        return node if isinstance(node, list) else [node]

    async def find(self, page: Page, path: str, timeout: int = 5000) -> Locator:
        """Fallback 셀렉터 순회"""
        selectors = self.get(path)
        last_error = None
        per_selector_timeout = timeout // len(selectors)

        for selector in selectors:
            try:
                locator = page.locator(selector).first
                await locator.wait_for(state="visible", timeout=per_selector_timeout)
                return locator
            except Exception as e:
                last_error = e
                continue

        raise ElementNotFoundError(
            f"모든 셀렉터 실패 [{path}]: tried {selectors}"
        ) from last_error

    async def fill(self, page: Page, path: str, value: str, delay: int = 50):
        """사람같은 딜레이로 타이핑"""
        locator = await self.find(page, path)
        await locator.click()
        await locator.fill("")  # 기존 값 클리어
        await locator.type(value, delay=delay)
```

---

## 6. 주문 자동화 상태머신

```python
# src/core/order_automation.py
from enum import Enum
from typing import Callable

class OrderState(Enum):
    IDLE = "idle"
    OPEN_PRODUCT = "open_product"
    CHECK_LOGIN = "check_login"
    CLICK_BUY = "click_buy"
    FILL_FORM = "fill_form"
    VERIFY = "verify"
    WAIT_PAYMENT = "wait_payment"
    EXTRACT_ORDER_NO = "extract_order_no"
    SAVE_EXCEL = "save_excel"
    COMPLETE = "complete"
    FAILED = "failed"

class OrderAutomation:
    def __init__(self, browser_mgr, selector_helper, excel_mgr, on_state_change: Callable):
        self.browser = browser_mgr
        self.selectors = selector_helper
        self.excel = excel_mgr
        self.on_state_change = on_state_change

    async def execute(self, order: Order) -> Order:
        """한 주문의 전체 플로우"""
        page = await self.browser.new_page()
        try:
            await self._transition(order, OrderState.OPEN_PRODUCT)
            await page.goto(str(order.product_url), wait_until="domcontentloaded")

            await self._transition(order, OrderState.CHECK_LOGIN)
            if await self._is_login_required(page):
                raise LoginExpiredError("로그인 필요")

            await self._transition(order, OrderState.CLICK_BUY)
            await self.selectors.find(page, "product_page.buy_now_button")
            await page.click(self.selectors.get("product_page.buy_now_button")[0])

            await self._transition(order, OrderState.FILL_FORM)
            await self._fill_order_form(page, order)

            await self._transition(order, OrderState.WAIT_PAYMENT)
            # 사용자 결제 대기 (최대 5분)
            order_no = await self._wait_for_order_completion(page, timeout=300_000)

            order.order_number = order_no
            order.ordered_at = datetime.now()
            order.status = "completed"

            await self._transition(order, OrderState.SAVE_EXCEL)
            self.excel.update_order(order)

            await self._transition(order, OrderState.COMPLETE)
            return order

        except Exception as e:
            order.status = "failed"
            order.error_message = str(e)
            order.screenshot_path = await save_screenshot(page, order.row)
            await self._transition(order, OrderState.FAILED)
            raise

        finally:
            await page.close()

    async def _fill_order_form(self, page, order: Order):
        await self.selectors.fill(page, "order_page.recipient_name", order.name)
        await self.selectors.fill(page, "order_page.phone", order.phone)
        # ... 나머지 필드
```

---

## 7. Qt ↔ asyncio 통합 (qasync)

```python
# main.py
import sys
import asyncio
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop
from src.ui.main_window import MainWindow

def main():
    app = QApplication(sys.argv)
    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
```

```python
# src/ui/main_window.py
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QMainWindow
import asyncio

class MainWindow(QMainWindow):
    orderCompleted = Signal(int, str)  # row, order_no

    def __init__(self):
        super().__init__()
        self._browser_mgr = BrowserManager()
        self._setup_ui()
        self.orderCompleted.connect(self._on_order_completed)

    def start_order(self, row: int):
        """더블클릭 시 호출 - Qt 측에서 asyncio 태스크 생성"""
        asyncio.ensure_future(self._run_order_async(row))

    async def _run_order_async(self, row: int):
        order = self.model.get_order(row)
        automation = OrderAutomation(self._browser_mgr, ...)
        try:
            completed = await automation.execute(order)
            self.orderCompleted.emit(row, completed.order_number)
        except Exception as e:
            self.log_panel.append(f"❌ 주문 #{row} 실패: {e}")
```

---

## 8. PyInstaller 빌드 (build.spec)

```python
# build/build.spec
# -*- mode: python ; coding: utf-8 -*-
import os

block_cipher = None

a = Analysis(
    ['../main.py'],
    pathex=['..'],
    binaries=[],
    datas=[
        ('../config', 'config'),
        ('../build/icon.ico', '.'),
    ],
    hiddenimports=[
        'playwright',
        'playwright.async_api',
        'PySide6.QtCore',
        'PySide6.QtWidgets',
        'PySide6.QtGui',
        'qasync',
        'loguru',
        'openpyxl',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PyQt5'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas,
    [],
    name='11st_auto_order',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                     # GUI 앱
    icon='icon.ico',
    version='version.txt',
)
```

**빌드 명령:**
```bash
# 1. Playwright browsers는 번들하지 않음 (system Chrome 사용)
# 2. 단일 파일로 빌드
pyinstaller build/build.spec --clean --noconfirm

# 결과: dist/11st_auto_order.exe (~50MB)
```

---

## 9. 테스트 전략

### 9.1 단위 테스트 (Unit)
```python
# tests/unit/test_excel_manager.py
def test_load_valid_excel():
    mgr = ExcelManager("tests/fixtures/sample_orders.xlsx")
    orders = mgr.load_orders()
    assert len(orders) == 5
    assert orders[0].phone == "010-1234-5678"

def test_invalid_phone_raises():
    with pytest.raises(ValidationError):
        Order(phone="invalid", ...)
```

### 9.2 통합 테스트 (Integration)
- Playwright 테스트 서버로 11번가 mock 페이지 호스팅
- 캡처된 HTML로 셀렉터 검증
- 실제 11번가는 수동 테스트 (주문 발생 방지)

### 9.3 수동 테스트 체크리스트 (Week 3, Day 15)
- [ ] 첫 실행 마법사 동작
- [ ] 11번가 로그인 유지 (프로그램 재시작 후)
- [ ] 샵백 확장 정상 동작
- [ ] 엑셀 5건 로드 → 화면 표시
- [ ] 일괄 가격 조회 (5건)
- [ ] 개별 주문 1건 end-to-end (실제 결제 X, 직전까지만)
- [ ] 강제 크래시 → 재시작 → 이어하기
- [ ] 에러 발생 → 스크린샷 자동 저장
- [ ] 로그 파일 일별 로테이션

---

## 10. CI/CD (선택)

GitHub Actions로 자동화:
```yaml
# .github/workflows/build.yml
name: Build Windows EXE
on: [push]
jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -r requirements.txt
      - run: pyinstaller build/build.spec
      - uses: actions/upload-artifact@v4
        with:
          name: 11st_auto_order
          path: dist/11st_auto_order.exe
```

---

## 11. 유지보수 가이드

**11번가 DOM 변경 시 대응 절차:**
1. 에러 스크린샷 확인 → 어느 단계에서 실패했는지 파악
2. 개발자 도구로 새 셀렉터 확인
3. `config/selectors.yaml`의 해당 경로에 새 셀렉터 **맨 위**에 추가
4. 기존 셀렉터는 fallback으로 유지
5. 프로그램 재시작 (재컴파일 불필요)

**로그 분석:**
- `data/logs/2026-04-21.log` → timestamp, level, module, message
- 에러 발생 시: `data/screenshots/error_ROW_TIMESTAMP.png` 참조

---

## 부록 A: 의존성 목록 (requirements.txt)

```
playwright==1.40.0
playwright-stealth==1.0.6
PySide6==6.6.0
qasync==0.27.1
openpyxl==3.1.2
pandas==2.1.4
pydantic==2.5.3
pyyaml==6.0.1
loguru==0.7.2
pyinstaller==6.3.0    # 빌드 전용
pytest==7.4.4         # 테스트 전용
pytest-asyncio==0.23.3
```

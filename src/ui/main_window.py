"""메인 윈도우.

자동화(Playwright) 코드는 모두 ``AsyncRunner`` 의 백그라운드 스레드 + 자체 asyncio
루프에서 실행된다. Qt UI 스레드와 분리되어 있어 Playwright 의 백그라운드 IPC task
가 Qt 시그널 핸들러와 reentry 충돌하는 ``RuntimeError: Cannot enter into task ...``
가 발생하지 않는다.
"""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Coroutine

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.core.browser_manager import BrowserManager
from src.core.excel_manager import ExcelManager
from src.core.order_automation import OrderAutomation, OrderState
from src.core.price_scraper import PriceScraper
from src.core.selector_helper import SelectorHelper
from src.core.state_manager import StateManager
from src.exceptions import AppError
from src.models.order import Order
from src.models.settings import AppSettings
from src.ui.empty_state import EmptyStateBanner
from src.ui.log_panel import LogPanel, QtLogBridge
from src.ui.onboarding_wizard import OnboardingWizard
from src.ui.order_table_model import STATUS_KEY_ROLE, OrderTableModel
from src.ui.settings_dialog import SettingsDialog
from src.ui.widgets.animated_button import install_press_animation
from src.ui.widgets.cell_editor_delegate import CellEditorDelegate
from src.ui.widgets.excel_table import ExcelTableView
from src.ui.widgets.section_card import CompositeCard, SectionRow
from src.ui.widgets.status_delegate import StatusDelegate
from src.utils.async_runner import AsyncRunner
from src.utils.logger import get_logger

log = get_logger()


class MainWindow(QMainWindow):
    """앱 메인 윈도우 - UI + 자동화 오케스트레이션."""

    orderUpdated = Signal(object)  # Order

    def __init__(self, settings: AppSettings):
        super().__init__()
        self.settings = settings

        # 코어 컴포넌트
        self.browser = BrowserManager(
            settings.browser, stealth_enabled=settings.automation.stealth_enabled
        )
        self.selectors = SelectorHelper()
        self.state_mgr = StateManager()
        self.excel_mgr: ExcelManager | None = None

        # 주문 자동화: 프로그램 생명주기 동안 유지되는 단일 인스턴스.
        # 각 행의 Page가 PAUSED/FAILED 상태로 보존되어 사용자 개입 후 이어서 진행 가능.
        self.automation: OrderAutomation | None = None

        # UI 구성
        self.setWindowTitle("11번가 자동 주문 프로그램 v1.0")
        self.resize(1400, 800)
        self._setup_ui()
        self._setup_toolbar()
        self._setup_menu()

        # 로그 브릿지
        self.log_bridge = QtLogBridge()
        self.log_bridge.message.connect(self.log_panel.append_line)
        # 샵백 미감지 같은 중요 경고는 팝업으로도 띄운다.
        self.log_bridge.message.connect(self._maybe_popup_from_log)
        self.log_bridge.attach()
        # 같은 이벤트로 팝업이 연달아 뜨지 않도록 중복 억제용 키 집합
        self._shown_popup_keys: set[str] = set()

        # 시그널 연결
        self.orderUpdated.connect(self._on_order_updated)

        # 백그라운드 자동화 러너 (자체 asyncio loop in worker thread)
        self._runner = AsyncRunner(name="PlaywrightRunner")
        self._runner.start()
        self._scraper: PriceScraper | None = None
        # 동시 실행 방지용 단순 플래그 (UI 스레드에서만 set/clear)
        self._busy = False
        # 주문 중단 플래그 — 중단 버튼/단축키로 set, 루프에서 체크하여 조기 종료
        self._abort_requested = False
        # 중단 요청 후 3초 경과 시 '강제 중단' 모드로 진입 (Future cancel)
        self._force_abort_armed = False
        # 현재 실행 중인 코루틴 Future (강제 중단 시 cancel 대상)
        self._current_future: Future | None = None

        log.info("프로그램 시작")

        if self.settings.ui.first_run:
            QTimer.singleShot(200, self._show_onboarding_first_time)

    # -------------------------------------------------------------
    # UI setup
    # -------------------------------------------------------------

    def _setup_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        # 엑셀 로드 후 보이는 정보 카드 (업로드 파일 + 주문 실행 요약)
        self.info_card = self._build_info_card()
        self.info_card.setVisible(False)
        root.addWidget(self.info_card)

        splitter = QSplitter(Qt.Vertical)
        root.addWidget(splitter, stretch=1)

        self.content_stack = QStackedWidget()

        self.empty_banner = EmptyStateBanner()
        self.empty_banner.loadExcelRequested.connect(self._on_load_excel)
        self.empty_banner.openBrowserRequested.connect(self._on_open_browser)
        self.empty_banner.showWizardRequested.connect(self._show_onboarding)
        self.empty_banner.installShopbackRequested.connect(self._on_install_shopback)
        self.empty_banner.recentFileSelected.connect(self._on_recent_file_selected)
        self.empty_banner.recentFileRemoved.connect(self._on_recent_file_removed)
        # 시작 시 최근 파일 목록 동기화
        self._refresh_recent_files()
        self.content_stack.addWidget(self.empty_banner)

        self.model = OrderTableModel()
        self.table = ExcelTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        # 주문 실행 트리거:
        #   - "상태" 컬럼 더블클릭 → 단건 주문 (데이터 셀은 편집 모드 진입)
        #   - 좌측 행 번호 헤더 더블클릭 → 단건 주문 (어디서든 빠르게)
        self.table.doubleClicked.connect(self._on_cell_double_clicked)
        self.table.verticalHeader().sectionDoubleClicked.connect(
            self._on_row_header_double_clicked
        )
        # 엑셀 느낌: 격자 선 + 적당한 행 높이 + 고정폭 폰트 느낌의 숫자
        self.table.setShowGrid(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setDefaultSectionSize(
            max(32, self.settings.ui.table_row_height)
        )
        self.table.verticalHeader().setVisible(True)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignCenter)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignCenter)
        self._apply_column_widths()

        # 모든 셀의 편집기를 셀 영역 안에 고정 (엑셀처럼 튀어나오지 않게).
        self._cell_editor_delegate = CellEditorDelegate(self.table)
        self.table.setItemDelegate(self._cell_editor_delegate)

        # 상태 컬럼에 pill + 스피너 delegate 설치 (기본 delegate 를 덮어씀)
        def _is_awaiting_next(idx) -> bool:
            order = self._order_for_index(idx)
            if order is None or self.automation is None:
                return False
            try:
                return self.automation.is_awaiting_next(order.row)
            except Exception:
                return False

        def _on_next(idx) -> None:
            order = self._order_for_index(idx)
            if order is None or self.automation is None:
                return
            self.automation.signal_next(order.row)
            self.statusBar().showMessage(
                f"행 {order.row}: '다음으로' 신호 전송 — 주문번호 추출 중...", 4000
            )

        def _is_awaiting_fill(idx) -> bool:
            order = self._order_for_index(idx)
            if order is None or self.automation is None:
                return False
            try:
                return self.automation.is_awaiting_fill(order.row)
            except Exception:
                return False

        def _on_fill(idx) -> None:
            order = self._order_for_index(idx)
            if order is None or self.automation is None:
                return
            self.automation.signal_fill(order.row)
            self.statusBar().showMessage(
                f"행 {order.row}: '기입' 신호 전송 — 주문서 자동 입력 중...", 4000
            )

        self._status_delegate = StatusDelegate(
            self.table,
            status_getter=lambda idx: idx.data(STATUS_KEY_ROLE),
            is_awaiting_next=_is_awaiting_next,
            on_next_clicked=_on_next,
            is_awaiting_fill=_is_awaiting_fill,
            on_fill_clicked=_on_fill,
        )
        status_col = self._find_status_column()
        if status_col >= 0:
            self.table.setItemDelegateForColumn(status_col, self._status_delegate)
        self._status_delegate.start()

        self.content_stack.addWidget(self.table)
        self.content_stack.setCurrentWidget(self.empty_banner)

        splitter.addWidget(self.content_stack)

        self.log_panel = LogPanel(max_lines=self.settings.ui.log_max_lines)
        splitter.addWidget(self.log_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        # 시작 화면(엑셀 로드 전)에서는 로그 패널 숨김 — 엑셀 로드 후에 표시.
        self.log_panel.setVisible(False)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("준비")

    def _find_status_column(self) -> int:
        for c in range(self.model.columnCount()):
            if self.model.headerData(c, Qt.Horizontal, Qt.DisplayRole) == "상태":
                return c
        return -1

    # -------------------------------------------------------------
    # Info card (엑셀 로드 후 표시)
    # -------------------------------------------------------------

    def _build_info_card(self) -> CompositeCard:
        card = CompositeCard()

        # ── 섹션 1: 파일 정보 + 변경 버튼
        self._upload_section = SectionRow("엑셀 파일")
        self._upload_filename_lbl = QLabel("파일 미선택")
        self._upload_filename_lbl.setStyleSheet(
            "QLabel { color: #111827; font-weight: 600; background: transparent;"
            " border: none; padding: 0; }"
        )
        self._upload_meta_lbl = QLabel("")
        self._upload_meta_lbl.setStyleSheet(
            "QLabel { color: #6B7280; font-size: 12px; background: transparent;"
            " border: none; padding: 0; }"
        )
        # 파일명/크기를 subtitle 영역에 바로 보여주기 위해 set_subtitle 대신 title 옆 배치.
        # 섹션 자체에 부제로는 용도를 짧게.
        self._upload_section.set_subtitle("로드된 엑셀")
        # 본문에 파일명 + 메타
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        col.addWidget(self._upload_filename_lbl)
        col.addWidget(self._upload_meta_lbl)
        lay.addLayout(col, stretch=1)
        self._upload_section.set_body(body)
        # 우측 액션: 변경
        change_btn = QToolButton()
        change_btn.setText("변경")
        change_btn.setCursor(Qt.PointingHandCursor)
        change_btn.setStyleSheet(
            "QToolButton { color: #374151; background: #FFFFFF;"
            " border: 1px solid #E5E7EB; border-radius: 6px;"
            " padding: 4px 10px; font-weight: 500; }"
            "QToolButton:hover { background: #F3F4F6; }"
        )
        change_btn.clicked.connect(self._on_load_excel)
        self._upload_section.set_action(change_btn)
        card.add_section(self._upload_section)

        # ── 섹션 2: 주문 실행 요약 + 중단 버튼
        self._run_section = SectionRow(
            "주문 실행", "엑셀을 먼저 불러오세요."
        )
        stop_btn = QToolButton()
        stop_btn.setText("중단")
        stop_btn.setCursor(Qt.PointingHandCursor)
        stop_btn.setStyleSheet(
            "QToolButton { color: #374151; background: #FFFFFF;"
            " border: 1px solid #E5E7EB; border-radius: 6px;"
            " padding: 4px 12px; font-weight: 500; }"
            "QToolButton:hover { background: #F3F4F6; }"
            "QToolButton:disabled { color: #9CA3AF; background: #F9FAFB; }"
        )
        stop_btn.setEnabled(False)
        stop_btn.clicked.connect(self._on_toggle_start_stop)
        self._run_section.set_action(stop_btn)
        self._card_stop_btn = stop_btn
        card.add_section(self._run_section)

        # ── 섹션 3: 주문 정보 다시 입력 (샵백 로그인 등으로 폼 초기화 시 사용)
        self._refill_section = SectionRow(
            "주문 정보 다시 입력",
            "샵백 로그인 등으로 주문서가 초기화되면 선택한 행을 다시 자동 입력합니다.",
        )
        refill_btn = QToolButton()
        refill_btn.setText("선택한 행 재입력")
        refill_btn.setCursor(Qt.PointingHandCursor)
        refill_btn.setStyleSheet(
            "QToolButton {"
            "  color: #FFFFFF; background: #2563EB;"
            "  border: 1px solid #1D4ED8; border-radius: 6px;"
            "  padding: 6px 14px; font-weight: 700;"
            "}"
            "QToolButton:hover { background: #1D4ED8; }"
            "QToolButton:pressed { background: #1E40AF; }"
            "QToolButton:disabled { color: #9CA3AF; background: #E5E7EB;"
            " border-color: #E5E7EB; }"
        )
        refill_btn.setToolTip(
            "테이블에서 한 행을 선택한 뒤 누르면, 활성 주문서 페이지에 "
            "배송지·통관번호·영문이름 등을 다시 자동 입력합니다."
        )
        refill_btn.clicked.connect(self._on_refill_selected_clicked)
        self._refill_section.set_action(refill_btn)
        self._card_refill_btn = refill_btn
        card.add_section(self._refill_section)

        return card

    def _refresh_info_card(self) -> None:
        """엑셀 로드 상태와 주문 모델을 카드 UI 에 반영."""
        if self.excel_mgr is None:
            self._upload_filename_lbl.setText("파일 미선택")
            self._upload_meta_lbl.setText("")
            self._run_section.set_subtitle("엑셀을 먼저 불러오세요.")
        else:
            p = Path(self.excel_mgr.path)
            self._upload_filename_lbl.setText(p.name)
            try:
                size_kb = max(1, p.stat().st_size // 1024)
                self._upload_meta_lbl.setText(f"{size_kb:,} KB")
            except Exception:
                self._upload_meta_lbl.setText("")
            total = len(self.model.valid_orders())
            if total == 0:
                self._run_section.set_subtitle(
                    "유효한 주문이 없습니다. 빨간 셀을 수정해 주세요."
                )
            else:
                self._run_section.set_subtitle(
                    f"총 {total}건의 주문이 준비되었습니다."
                )
        # 중단 버튼 enable 상태는 _set_orders_button_running() 한 곳에서만 통제.
        # 여기서 덮어쓰면 주문 진행 중 _refresh_summary() 가 불릴 때마다
        # 잠깐씩 비활성화되는 문제가 생긴다.

    def _set_toolbar_visible(self, visible: bool) -> None:
        """엑셀 로드 여부에 따라 상단 툴바 표시 토글."""
        tb = getattr(self, "_toolbar", None)
        if tb is not None:
            tb.setVisible(visible)

    def _setup_toolbar(self) -> None:
        tb = QToolBar("메인")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)
        self._toolbar = tb

        # ── 액션 정의 (툴바 노출 여부와 별개로 모두 정의 → 메뉴/단축키에서도 사용)
        # 시작 화면(EmptyStateBanner) 으로 돌아가기 — 툴바 맨 왼쪽
        self.action_back_home = QAction("← 시작 화면", self)
        self.action_back_home.setToolTip("최근 파일 목록이 있는 시작 화면으로 돌아갑니다")
        self.action_back_home.triggered.connect(self._on_back_to_home)

        self.action_open = QAction("엑셀 불러오기", self)
        self.action_open.setShortcut(QKeySequence.Open)
        self.action_open.triggered.connect(self._on_load_excel)

        self.action_save = QAction("결과 저장", self)
        self.action_save.setShortcut(QKeySequence.Save)
        self.action_save.triggered.connect(self._on_save_excel)

        self.action_save_original = QAction("원본에 저장", self)
        self.action_save_original.triggered.connect(self._on_save_to_original)

        self.action_scrape = QAction("가격 조회", self)
        self.action_scrape.triggered.connect(self._on_scrape_prices)

        # 주문하기 / 중단하기 토글 — 한 버튼이 상태에 따라 역할 전환
        self.action_start_orders = QAction("주문하기", self)
        self.action_start_orders.setShortcut(QKeySequence("Ctrl+R"))
        self.action_start_orders.triggered.connect(self._on_toggle_start_stop)

        # 내부 상태: 루프 실행 중이면 True → 버튼이 중단 역할
        self._orders_running = False

        # 크롬 창 표시/숨김 토글 (hide_window 모드에서 유용)
        self.action_toggle_chrome = QAction("크롬 창 보기", self)
        self.action_toggle_chrome.setShortcut(QKeySequence("Ctrl+B"))
        self.action_toggle_chrome.triggered.connect(self._on_toggle_chrome_window)

        # 메뉴 전용 (툴바엔 노출 안 함)
        self.action_browser = QAction("브라우저 열기", self)
        self.action_browser.triggered.connect(self._on_open_browser)

        self.action_install_shopback = QAction("샵백 확장프로그램 설치", self)
        self.action_install_shopback.setToolTip(
            "Chrome Web Store 의 샵백 페이지를 앱 브라우저에서 열어줍니다. "
            "'Chrome에 추가' 를 누르면 다음 실행부터 자동으로 로드됩니다."
        )
        self.action_install_shopback.triggered.connect(self._on_install_shopback)

        self.action_settings = QAction("설정", self)
        self.action_settings.triggered.connect(self._on_open_settings)

        # ── 툴바 구성: 엑셀 로드 이후에만 보이는 핵심 기능
        # '엑셀 불러오기' 는 시작 화면에서만 노출 — 툴바에서는 제외.
        # 전체 '주문하기' 버튼은 제거 — 행을 더블클릭해 한 건씩만 진행.
        tb.addAction(self.action_back_home)      # ← 시작 화면 (맨 왼쪽)
        tb.addSeparator()
        tb.addAction(self.action_save_original)  # 원본에 저장
        tb.addAction(self.action_scrape)         # 가격 조회

        # 초기: 엑셀 미로드 상태이므로 툴바 숨김
        tb.setVisible(False)

        # 시작 화면 버튼 — 보조 톤(회색)으로 구분
        back_btn = tb.widgetForAction(self.action_back_home)
        if isinstance(back_btn, QToolButton):
            back_btn.setStyleSheet(
                "QToolButton {"
                "  color: #4B5563;"
                "  background: transparent;"
                "  border: 1px solid #D1D5DB;"
                "  border-radius: 6px;"
                "  padding: 6px 10px;"
                "}"
                "QToolButton:hover { background: #F3F4F6; color: #111827; }"
            )

        # 주문하기 버튼만 primary 스타일 (검정 강조)
        order_btn = tb.widgetForAction(self.action_start_orders)
        if isinstance(order_btn, QToolButton):
            order_btn.setObjectName("primaryAction")
            order_btn.setStyleSheet(
                "QToolButton#primaryAction {"
                "  background: #111827;"
                "  color: #FFFFFF;"
                "  font-weight: 700;"
                "  border: 1px solid #111827;"
                "  border-radius: 6px;"
                "  padding: 6px 14px;"
                "}"
                "QToolButton#primaryAction:hover { background: #1F2937; }"
                "QToolButton#primaryAction:pressed { background: #000000; }"
            )

        self._toolbar_anim = install_press_animation(tb)

    def _setup_menu(self) -> None:
        mb = self.menuBar()
        file_menu = mb.addMenu("파일")
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_save)
        file_menu.addAction(self.action_save_original)
        file_menu.addSeparator()
        quit_action = QAction("종료", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        tools_menu = mb.addMenu("도구")
        tools_menu.addAction(self.action_browser)
        tools_menu.addAction(self.action_install_shopback)
        tools_menu.addAction(self.action_scrape)
        tools_menu.addAction(self.action_start_orders)
        tools_menu.addSeparator()
        diag_action = QAction("셀렉터 진단 (선택한 행)", self)
        diag_action.triggered.connect(self._on_diagnose_selectors)
        tools_menu.addAction(diag_action)
        open_diag_dir = QAction("진단 폴더 열기", self)
        open_diag_dir.triggered.connect(self._on_open_diagnostics_dir)
        tools_menu.addAction(open_diag_dir)

        settings_menu = mb.addMenu("설정")
        settings_menu.addAction(self.action_settings)

        help_menu = mb.addMenu("도움말")
        wizard_action = QAction("사용 안내 처음부터 다시 보기", self)
        wizard_action.setShortcut(QKeySequence("F1"))
        wizard_action.triggered.connect(self._show_onboarding)
        help_menu.addAction(wizard_action)
        help_menu.addSeparator()
        about = QAction("프로그램 정보", self)
        about.triggered.connect(self._on_about)
        help_menu.addAction(about)

    # -------------------------------------------------------------
    # Slots: Excel
    # -------------------------------------------------------------

    def _on_load_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "엑셀 파일 선택", "", "Excel Files (*.xlsx *.xls)"
        )
        if not path:
            return
        self._load_excel_from_path(path)

    def _load_excel_from_path(self, path: str) -> None:
        try:
            self.excel_mgr = ExcelManager(path)
            rows = self.excel_mgr.load(backup=self.settings.excel.backup_on_load)
            self.model.set_rows(rows)
            self.model.set_promote_fn(self.excel_mgr.try_promote)
            self.state_mgr.start_session(path)
            self._refresh_summary()
            self.content_stack.setCurrentWidget(self.table)
            self._set_toolbar_visible(True)
            self.info_card.setVisible(True)
            self.log_panel.setVisible(True)
            self._refresh_info_card()
            invalid = len(self.model.invalid_rows())
            log.info(f"엑셀 로드: {len(rows)}건 (수정 필요 {invalid}건)")
            if invalid:
                self.statusBar().showMessage(
                    f"로드 완료: {Path(path).name} — {invalid}건은 값 확인이 필요합니다"
                    " (빨간 셀을 클릭해 수정)"
                )
            else:
                self.statusBar().showMessage(f"로드 완료: {Path(path).name}")
            # 최근 파일 목록 갱신 (성공한 파일만)
            self._add_recent_file(path)
            # 첫 엑셀 로드 시 샵백 확장프로그램 설치 안내 (1회만)
            self._maybe_prompt_shopback_install()
        except AppError as exc:
            # 파일 자체를 못 연 경우만 여기로 온다. 나머지는 UI에서 수정 가능.
            QMessageBox.critical(self, "엑셀 로드 실패", str(exc))
            log.error(f"엑셀 로드 실패: {exc}")

    # -------------------------------------------------------------
    # 최근 파일 관리
    # -------------------------------------------------------------

    def _refresh_recent_files(self) -> None:
        """settings 의 최근 파일 목록을 EmptyStateBanner 에 반영."""
        recents = list(getattr(self.settings.ui, "recent_excel_files", []) or [])
        self.empty_banner.set_recent_files(recents)

    def _add_recent_file(self, path: str) -> None:
        """경로를 최근 목록 맨 앞으로 올린다 (중복 제거, 최대 개수 유지)."""
        try:
            absolute = str(Path(path).resolve())
        except Exception:
            absolute = path
        recents = list(getattr(self.settings.ui, "recent_excel_files", []) or [])
        # 동일 경로 제거 (대소문자/심볼릭 차이는 resolve 로 흡수)
        recents = [
            p for p in recents
            if (lambda x: x != absolute)(self._safe_resolve(p))
        ]
        recents.insert(0, absolute)
        max_n = int(getattr(self.settings.ui, "recent_excel_max", 10) or 10)
        recents = recents[:max_n]
        self.settings.ui.recent_excel_files = recents
        try:
            self.settings.save()
        except Exception as exc:
            log.debug(f"설정 저장 실패(무시): {exc}")
        self._refresh_recent_files()

    @staticmethod
    def _safe_resolve(p: str) -> str:
        try:
            return str(Path(p).resolve())
        except Exception:
            return p

    def _on_recent_file_selected(self, path: str) -> None:
        if not Path(path).exists():
            QMessageBox.warning(
                self,
                "파일 없음",
                f"파일을 찾을 수 없습니다:\n{path}\n\n최근 목록에서 제거합니다.",
            )
            self._on_recent_file_removed(path)
            return
        self._load_excel_from_path(path)

    def _on_back_to_home(self) -> None:
        """현재 엑셀 세션을 닫고 시작 화면(EmptyStateBanner)으로 돌아간다."""
        # 주문 실행 중이면 차단
        if getattr(self, "_orders_running", False):
            QMessageBox.warning(
                self,
                "진행 중",
                "주문이 실행 중입니다. 먼저 중단한 후 시작 화면으로 돌아가세요.",
            )
            return
        # 모델/엑셀 매니저 정리
        self.excel_mgr = None
        try:
            self.model.set_rows([])
            self.model.set_promote_fn(None)
        except Exception as exc:
            log.debug(f"모델 초기화 중 오류(무시): {exc}")
        # state_manager 는 그대로 둔다 — 같은 파일을 다시 열었을 때
        # 이전 진행 상태(완료된 행 등)를 유지하기 위함.
        # UI 전환
        self.content_stack.setCurrentWidget(self.empty_banner)
        self._set_toolbar_visible(False)
        if hasattr(self, "info_card"):
            self.info_card.setVisible(False)
        if hasattr(self, "log_panel"):
            self.log_panel.setVisible(False)
        self._refresh_recent_files()
        self.statusBar().showMessage("시작 화면으로 돌아왔습니다", 3000)

    def _on_recent_file_removed(self, path: str) -> None:
        target = self._safe_resolve(path)
        recents = [
            p for p in (self.settings.ui.recent_excel_files or [])
            if self._safe_resolve(p) != target
        ]
        self.settings.ui.recent_excel_files = recents
        try:
            self.settings.save()
        except Exception as exc:
            log.debug(f"설정 저장 실패(무시): {exc}")
        self._refresh_recent_files()

    def _maybe_prompt_shopback_install(self) -> None:
        """샵백 설치 안내를 1회 띄운다. '설치하기' 선택 시 자동으로 페이지 오픈."""
        if getattr(self.settings.ui, "shopback_install_prompted", False):
            return
        # 노출 플래그를 먼저 켜고 저장 — 다음 실행부터는 안 뜬다.
        self.settings.ui.shopback_install_prompted = True
        try:
            self.settings.save()
        except Exception as exc:
            log.debug(f"설정 저장 실패(무시): {exc}")

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("샵백 확장프로그램 설치")
        box.setText("주문 시 캐시백 적립을 받으려면 샵백 확장프로그램이 필요합니다.")
        box.setInformativeText(
            "지금 앱 브라우저에서 Chrome Web Store 의 샵백 페이지를 열까요?\n\n"
            "• '설치하기' → 페이지가 열립니다. 'Chrome에 추가' 를 누르세요.\n"
            "• '나중에' → 도구 메뉴에서 언제든 다시 설치할 수 있습니다."
        )
        install_btn = box.addButton("설치하기", QMessageBox.AcceptRole)
        box.addButton("나중에", QMessageBox.RejectRole)
        box.setDefaultButton(install_btn)
        box.exec()
        if box.clickedButton() is install_btn:
            self._on_install_shopback()

    def _on_save_excel(self) -> None:
        """결과 파일(_완료_TIMESTAMP) 로 저장. 원본은 건드리지 않는다."""
        if self.excel_mgr is None:
            QMessageBox.warning(self, "알림", "먼저 엑셀을 불러와주세요.")
            return
        try:
            out = self.excel_mgr.save(self.model.all_rows())
            self.statusBar().showMessage(f"저장 완료: {out.name}")
            log.info(f"저장: {out}")
            QMessageBox.information(
                self,
                "저장 완료",
                f"결과 파일로 저장되었습니다.\n\n{out}\n\n"
                "토탈가격/주문번호는 현재 채워진 값 그대로 저장됩니다.\n"
                "원본 엑셀에 덮어쓰려면 '원본에 저장' 버튼을 사용하세요.",
            )
        except AppError as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))

    def _on_save_to_original(self) -> None:
        """원본 엑셀 파일 자체에 덮어쓰기 저장 (UI 수정사항 반영)."""
        if self.excel_mgr is None:
            QMessageBox.warning(self, "알림", "먼저 엑셀을 불러와주세요.")
            return
        reply = QMessageBox.question(
            self,
            "원본에 저장",
            f"원본 파일에 현재 수정사항을 덮어쓰시겠습니까?\n\n"
            f"{self.excel_mgr.path}\n\n"
            "(저장 직전 자동으로 data/backups/ 폴더에 백업됩니다.)",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            out = self.excel_mgr.save_to_original(self.model.all_rows())
            self.statusBar().showMessage(f"원본 저장: {out.name}")
            log.info(f"원본 저장: {out}")
            QMessageBox.information(self, "저장 완료", f"원본에 저장되었습니다.\n\n{out}")
        except AppError as exc:
            QMessageBox.critical(self, "저장 실패", str(exc))

    # -------------------------------------------------------------
    # Slots: Browser / Price
    # -------------------------------------------------------------

    def _on_open_browser(self) -> None:
        if not self._try_acquire():
            return
        self._run_async(self._open_browser_async())

    async def _open_browser_async(self) -> None:
        try:
            await self.browser.start()
            self._ui(lambda: self.statusBar().showMessage(
                "브라우저 준비됨 (11번가 로그인/샵백 설치 후 재사용됩니다)"
            ))
            log.info("브라우저 연결 완료 - 11번가에 로그인하세요")
        except AppError as exc:
            log.error(f"브라우저 시작 실패: {exc}")
            await self._show_info_async("브라우저 시작 실패", str(exc), icon="critical")

    def _on_install_shopback(self) -> None:
        """샵백 확장프로그램 설치 페이지를 앱 브라우저에서 연다."""
        if not self._try_acquire():
            return
        self._run_async(self._install_shopback_async())

    async def _install_shopback_async(self) -> None:
        try:
            await self.browser.start()
            await self.browser.show_window()
            await self.browser.open_extensions_page()
            self._ui(lambda: self.statusBar().showMessage(
                "샵백 페이지를 열었습니다 — 'Chrome에 추가' 를 눌러 설치하세요", 8000
            ))
            log.info("샵백 확장프로그램 설치 페이지 열림")
        except AppError as exc:
            log.error(f"샵백 설치 페이지 열기 실패: {exc}")
            await self._show_info_async(
                "샵백 설치 페이지 열기 실패", str(exc), icon="critical"
            )

    def _on_toggle_start_stop(self) -> None:
        """주문하기 버튼이 클릭됨.
        상태 머신:
          - 실행 중 → 중단 요청 ('중단 중...' 으로 전환, 일정 시간 후 '강제 중단' 활성)
          - 중단 중 + 강제 중단 가능 → Future cancel + UI 강제 복구
          - 정지 상태 → 시작
        """
        if self._orders_running:
            if getattr(self, "_force_abort_armed", False):
                # 두 번째 클릭 — 강제 중단
                self._force_abort_now()
                return
            # 첫 번째 클릭 — 정상 중단 요청
            self._abort_requested = True
            self.action_start_orders.setText("중단 중...")
            self.action_start_orders.setEnabled(False)
            self.statusBar().showMessage(
                "중단 요청됨 — 현재 행 처리 후 종료합니다 (3초 후 강제 중단 활성화)", 5000
            )
            log.info("사용자 요청으로 주문 중단됨 — 현재 행 완료 후 종료")
            # 3초 후에도 여전히 실행중이면 '강제 중단' 버튼으로 전환
            QTimer.singleShot(3000, self._arm_force_abort)
        else:
            # 시작
            self._on_start_all_orders()

    def _arm_force_abort(self) -> None:
        """중단 요청 후 3초가 지나도 루프가 안 끝나면 강제 중단 버튼 활성."""
        if not self._orders_running:
            return
        self._force_abort_armed = True
        self.action_start_orders.setText("강제 중단")
        self.action_start_orders.setToolTip(
            "현재 실행 중인 작업을 즉시 취소합니다 (진행 상태가 유실될 수 있음)"
        )
        self.action_start_orders.setEnabled(True)
        self.statusBar().showMessage(
            "현재 단계가 응답하지 않습니다 — '강제 중단' 을 누르면 즉시 취소됩니다", 8000
        )

    def _force_abort_now(self) -> None:
        """실행 중인 Future 를 강제 취소하고 UI 를 정지 상태로 되돌린다."""
        log.warning("사용자 요청으로 강제 중단 — 실행 중이던 작업을 취소합니다")
        fut = getattr(self, "_current_future", None)
        if fut is not None:
            try:
                fut.cancel()
            except Exception as exc:
                log.warning(f"Future cancel 실패: {exc}")
        # UI 즉시 복구 — _bridge 가 호출되더라도 중복 복구는 안전함
        self._busy = False
        self._abort_requested = False
        self._force_abort_armed = False
        self._set_orders_button_running(False)
        self.statusBar().showMessage("강제 중단됨", 5000)

    def _set_orders_button_running(self, running: bool) -> None:
        """주문하기 버튼을 실행/중단 모드로 전환."""
        self._orders_running = running
        if running:
            self.action_start_orders.setText("중단하기")
            self.action_start_orders.setToolTip(
                "진행 중인 주문을 중단합니다 (현재 행은 끝까지 시도)"
            )
        else:
            self.action_start_orders.setText("주문하기")
            self.action_start_orders.setToolTip("")
        self.action_start_orders.setEnabled(True)
        btn = getattr(self, "_card_stop_btn", None)
        if btn is not None:
            btn.setEnabled(running)

    # 크롬 창 보기/숨기기 토글 — hide_window 모드에서 창이 안 보일 때 사용.
    def _on_toggle_chrome_window(self) -> None:
        if not self.browser.is_running:
            QMessageBox.information(
                self,
                "알림",
                "크롬이 아직 실행되지 않았습니다.\n엑셀 불러오기 또는 주문하기를 먼저 실행하세요.",
            )
            return
        self._chrome_visible = not getattr(self, "_chrome_visible", False)
        if self._chrome_visible:
            self.action_toggle_chrome.setText("크롬 창 숨기기")
            self._run_async(self.browser.bring_to_front())
            self.statusBar().showMessage("크롬 창 표시 중 — 다시 누르면 숨김", 3000)
        else:
            self.action_toggle_chrome.setText("크롬 창 보기")
            self._run_async(self.browser.hide_window())
            self.statusBar().showMessage("크롬 창 숨김 (백그라운드 실행 중)", 3000)

    def _on_scrape_prices(self) -> None:
        if self.excel_mgr is None or not self.model.all_rows():
            QMessageBox.warning(self, "알림", "먼저 엑셀을 불러와주세요.")
            return
        if not self.model.valid_orders():
            QMessageBox.warning(
                self,
                "알림",
                "검증된 주문이 없습니다. 빨간색 행의 값을 먼저 수정해 주세요.",
            )
            return
        # 선택된 행이 있으면 범위 묻기
        selected = self._selected_orders()
        targets: list[Order] | None = None
        if len(selected) >= 1:
            from PySide6.QtWidgets import QMessageBox as _QM
            box = _QM(self)
            box.setIcon(_QM.Question)
            box.setWindowTitle("가격 조회 범위 선택")
            box.setText(
                f"선택된 행이 {len(selected)}건 있습니다.\n"
                "어느 범위로 가격을 조회하시겠어요?"
            )
            sel_btn = box.addButton(
                f"선택한 {len(selected)}건만", _QM.AcceptRole
            )
            all_btn = box.addButton("전체 조회", _QM.AcceptRole)
            cancel_btn = box.addButton("취소", _QM.RejectRole)
            box.setDefaultButton(sel_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel_btn or clicked is None:
                return
            if clicked is sel_btn:
                targets = selected

        if not self._try_acquire():
            return
        self._run_async(
            self._scrape_prices_async(only_missing=False, targets=targets)
        )


    async def _scrape_prices_async(
        self,
        only_missing: bool,
        targets: list[Order] | None = None,
    ) -> bool:
        """가격 조회. 성공하면 True. 취소/오류 시 False.

        targets=None 이면 전체 valid 행. 리스트 주면 그 행들만.
        """
        try:
            await self.browser.start()
            self._scraper = PriceScraper(
                self.browser, self.selectors, self.settings.price_scraper
            )

            def progress(current: int, total: int, order: Order) -> None:
                # 백그라운드 스레드에서 호출됨 — UI 변경은 메인 스레드로 마샬링
                cur, tot = current, total
                ord_ = order
                self._ui(lambda: (
                    self.model.update_order(ord_),
                    self.statusBar().showMessage(f"가격 조회 중: {cur}/{tot}"),
                ))

            scrape_list = targets if targets is not None else self.model.valid_orders()
            await self._scraper.scrape_all(
                scrape_list, on_progress=progress, only_missing=only_missing
            )
            # 스크랩 결과는 진행 콜백으로 이미 모델에 반영됨.
            if self.excel_mgr:
                self.excel_mgr.save(self.model.all_rows())
            self._ui(lambda: (
                self.statusBar().showMessage("가격 조회 완료 — 저장 가능합니다"),
                self._refresh_summary(),
            ))
            log.info("일괄 가격 조회 완료")
            return True
        except AppError as exc:
            log.error(f"가격 조회 실패: {exc}")
            await self._show_info_async("가격 조회 실패", str(exc), icon="critical")
            return False

    # -------------------------------------------------------------
    # Slots: Orders
    # -------------------------------------------------------------

    def _on_cell_double_clicked(self, index) -> None:
        """더블클릭 동작을 셀 편집 기본으로 두되, '상태' 컬럼 더블클릭만
        단건 주문 실행 진입점으로 사용한다. (데이터 컬럼은 편집이 시작됨)
        """
        # 상태 컬럼인지 판정 — 다른 컬럼은 ExcelTableView 가 편집을 시작한다.
        col_name = self.model.headerData(
            index.column(), Qt.Horizontal, Qt.DisplayRole
        )
        if col_name != "상태":
            return
        self._start_single_order_for_row(index.row())

    def _on_row_header_double_clicked(self, row: int) -> None:
        """좌측 행번호 헤더 더블클릭 → 그 행 단건 주문."""
        self._start_single_order_for_row(row)

    def _start_single_order_for_row(self, row_index: int) -> None:
        """모델 row index 로 단건 주문 시작. 검증/완료여부/lock 까지 처리."""
        item = self.model.get_row(row_index)
        if item is None:
            return
        if not isinstance(item, Order):
            QMessageBox.information(
                self,
                "값 확인 필요",
                f"행 {getattr(item, 'row', '?')}의 값이 유효하지 않아 주문을 시작할 수 없습니다.\n\n"
                f"오류: {getattr(item, 'error', '')}\n\n"
                "해당 행의 빨간 셀을 클릭해 값을 수정한 뒤 다시 시도해 주세요.",
            )
            return
        order = item
        if order.is_done():
            reply = QMessageBox.question(
                self,
                "이미 완료된 주문",
                f"행 {order.row}은 이미 완료되었습니다. 다시 실행할까요?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        if not self._try_acquire():
            return
        # 단건 주문도 가격 가드 적용
        self._run_async(self._run_single_order_with_guard(order))

    async def _run_single_order_with_guard(self, order: Order) -> None:
        # 토탈가격이 비어있으면 묻지 말고 바로 조회 진행
        # (어차피 가격 없이는 주문 못 함, 모달이 화면에 안 뜨면 무한 대기됨)
        if order.needs_price():
            log.info(
                f"행 {order.row}: 토탈가격 누락 → 가격 자동 조회 후 주문 진행"
            )
            self._ui(lambda: self.statusBar().showMessage(
                f"행 {order.row} 가격 조회 중... (잠시 후 자동으로 주문 진행)"
            ))
            ok = await self._scrape_prices_async(only_missing=True, targets=[order])
            if not ok:
                self._ui(lambda: self.statusBar().showMessage(
                    f"행 {order.row} 가격 조회 실패 — 주문 취소됨", 5000
                ))
                return
            # 스크랩 후 동일 행의 최신 Order 다시 찾기
            for r in self.model.all_rows():
                if isinstance(r, Order) and r.row == order.row:
                    order = r
                    break
        await self._execute_order_async(order)

    def _on_start_all_orders(self) -> None:
        """주문 시작. 선택된 행이 있으면 '선택만 vs 전체' 묻고, 없으면 전체."""
        if not self.model.all_rows():
            QMessageBox.warning(self, "알림", "먼저 엑셀을 불러와주세요.")
            return

        # 선택된 행이 있으면 사용자에게 범위 묻기
        selected = self._selected_orders()
        scope_selected: list[Order] | None = None
        if len(selected) >= 1:
            from PySide6.QtWidgets import QMessageBox as _QM
            box = _QM(self)
            box.setIcon(_QM.Question)
            box.setWindowTitle("주문 범위 선택")
            box.setText(
                f"선택된 행이 {len(selected)}건 있습니다.\n"
                "어느 범위로 주문하시겠어요?"
            )
            sel_btn = box.addButton(
                f"선택한 {len(selected)}건만", _QM.AcceptRole
            )
            all_btn = box.addButton("전체 주문", _QM.AcceptRole)
            cancel_btn = box.addButton("취소", _QM.RejectRole)
            box.setDefaultButton(sel_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel_btn or clicked is None:
                return
            if clicked is sel_btn:
                scope_selected = selected

        invalid = self.model.invalid_rows()
        if invalid and scope_selected is None:
            QMessageBox.warning(
                self,
                "값 확인 필요",
                f"{len(invalid)}건의 행이 값 검증을 통과하지 못했습니다.\n"
                "빨간 셀을 클릭해 값을 수정한 뒤 다시 시도해 주세요.\n\n"
                "(유효한 행만 자동으로 처리됩니다.)",
            )
            reply = QMessageBox.question(
                self,
                "계속 진행",
                "유효한 행만 주문을 진행할까요?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        if not self._try_acquire():
            return
        # 루프 진입 전에도 로그인 체크/가격 조회 등 오래 걸리는 단계가 있으므로
        # 즉시 중단 버튼을 활성화해 사용자가 언제든 중단할 수 있게 한다.
        self._abort_requested = False
        self._set_orders_button_running(True)
        self._show_address_search_notice_once()
        self._run_async(self._start_all_orders_async(selected_only=scope_selected))

    def _show_address_search_notice_once(self) -> None:
        """주문서 진입 시 사용자가 직접 '주소찾기' 팝업을 열어야 한다는 안내.

        11번가 해외직구는 도로명 주소 정책상 주소찾기 팝업으로 선택된 주소만
        인정하므로, 자동화는 팝업이 열린 뒤부터 우편번호 검색·선택을 처리한다.
        세션당 1회만 띄운다.
        """
        if getattr(self, "_address_notice_shown", False):
            return
        self._address_notice_shown = True
        text = (
            "\u26A0 배송지 입력 단계에서 자동화가 멈추면 "
            "주문서의 \u300C주소찾기\u300D 버튼을 직접 눌러 팝업을 열고, "
            "우편번호를 직접 입력해 주소를 선택해 주세요."
        )
        QTimer.singleShot(0, lambda: self._show_inline_notice(text))

    async def _start_all_orders_async(
        self, selected_only: list[Order] | None = None
    ) -> None:
        # 동시 실행 방지는 호출 측 _try_acquire() 가 담당 — 여기는 그대로 inner 실행
        await self._start_all_orders_inner(selected_only=selected_only)

    async def _start_all_orders_inner(
        self, selected_only: list[Order] | None = None
    ) -> None:
        # 1) 로그인 상태 점검 — 실패해도 주문을 강제 차단하지 않는다.
        try:
            await self.browser.start()
            self._ui(lambda: self.statusBar().showMessage("11번가 로그인 상태 확인 중..."))
            logged_in = await self.browser.is_logged_in(timeout_sec=15.0)
        except Exception as exc:
            log.warning(f"로그인 체크 실패: {exc}")
            logged_in = False

        if not logged_in:
            # 로그인 페이지 자동 오픈
            try:
                await self.browser.open_login_page()
            except Exception as exc:
                log.warning(f"로그인 페이지 자동 오픈 실패: {exc}")

            # 사용자가 Chrome 에서 로그인 완료할 때까지 무한 폴링.
            # 사용자가 '중단' 누르면 self._abort_requested 가 True 가 되어 종료.
            log.info("11번가 로그인 대기 시작 — Chrome 에서 로그인해 주세요")
            self._ui(lambda: self.statusBar().showMessage(
                "Chrome 에서 11번가 로그인을 완료해 주세요 (자동으로 감지합니다)"
            ))
            import asyncio as _aio
            attempt = 0
            while True:
                if getattr(self, "_abort_requested", False):
                    log.info("로그인 대기 중 사용자 중단 요청 → 주문 취소")
                    self._ui(lambda: self.statusBar().showMessage(
                        "주문 취소됨 (로그인 대기 중)"
                    ))
                    return
                attempt += 1
                try:
                    logged_in = await self.browser.is_logged_in(timeout_sec=5.0)
                except Exception:
                    logged_in = False
                if logged_in:
                    log.info(f"로그인 감지됨 (시도 {attempt}회) — 주문 진행")
                    break
                # 5초마다 확인 — 너무 자주 체크해 Chrome 에 부담주지 않게
                self._ui(lambda a=attempt: self.statusBar().showMessage(
                    f"Chrome 에서 11번가 로그인을 완료해 주세요 "
                    f"(자동 감지 중 · {a}회 체크)"
                ))
                await _aio.sleep(5)

        log.info("11번가 로그인 확인됨 — 주문 시작")
        # 대상 결정: 선택된 행이 있으면 그것만, 없으면 전체 valid 행
        scope_label = "전체" if selected_only is None else f"선택 {len(selected_only)}건"
        scope_orders = selected_only if selected_only is not None else self.model.valid_orders()

        # 루프 진입 전 중단 체크 (로그인 단계 사이에 요청된 경우)
        if self._abort_requested:
            log.info("중단 요청으로 주문 시작 취소됨 (로그인 단계 후)")
            return

        # 가격 누락 보충 (대상 안에서만)
        missing = [o for o in scope_orders if o.needs_price() and o.status != "unavailable"]
        if missing:
            log.info(f"토탈가격 누락 {len(missing)}건 → 자동 가격 재조회 ({scope_label})")
            n = len(missing)
            self._ui(lambda: self.statusBar().showMessage(
                f"주문 시작 전 누락 가격 {n}건 자동 조회 중..."
            ))
            ok = await self._scrape_prices_async(only_missing=True, targets=missing)
            if not ok:
                return
            # 가격 조회 직후 중단 체크
            if self._abort_requested:
                log.info("중단 요청으로 주문 시작 취소됨 (가격 조회 후)")
                return
            still_missing = [
                o for o in scope_orders if o.needs_price() and o.status != "unavailable"
            ]
            if still_missing:
                await self._show_info_async(
                    "가격 조회 실패",
                    f"{len(still_missing)}건의 가격을 가져오지 못했습니다.\n"
                    "해당 행을 확인 후 개별 실행해 주세요.",
                    icon="warning",
                )
                return

        # 판매 불가 알림 (대상 안에서만)
        unavailable = [o for o in scope_orders if o.status == "unavailable"]
        if unavailable:
            rows = ", ".join(str(o.row) for o in unavailable[:10])
            more = f" 외 {len(unavailable) - 10}건" if len(unavailable) > 10 else ""
            await self._show_info_async(
                "판매 불가 상품 자동 제외",
                f"판매중지/품절/삭제된 상품 {len(unavailable)}건은 자동 주문에서 "
                f"제외됩니다.\n\n해당 행: {rows}{more}",
            )

        targets = [o for o in scope_orders if o.is_retryable()]
        if not targets:
            await self._show_info_async("알림", "실행할 주문이 없습니다.")
            return

        skip_on_pause = getattr(self.settings.automation, "skip_on_pause", True)
        skip_on_error = getattr(self.settings.automation, "skip_on_error", True)
        # 성공한 행은 즉시 저장 (아래 루프에서). 루프 끝에도 최종 저장 보강.
        ok_count = 0
        skipped_count = 0
        total_targets = len(targets)
        # 루프 시작 — 버튼/플래그는 _on_start_all_orders 에서 이미 세팅됨.
        # 단, 백그라운드 스레드에서 UI 갱신이 누락되지 않도록 한 번 더 동기화.
        self._ui(lambda: self._set_orders_button_running(True))
        aborted = False
        for i, order in enumerate(targets, start=1):
            # 각 행 시작 전에 중단 플래그 체크
            if self._abort_requested:
                log.info(f"중단 요청으로 루프 종료 (행{order.row} 전)")
                aborted = True
                break
            row = order.row
            idx = i
            self._ui(lambda r=row, j=idx, t=total_targets: self.statusBar().showMessage(
                f"전체 진행 중 [{j}/{t}] 행{r} ..."
            ))
            await self._execute_order_async(order)

            if order.status == "completed":
                ok_count += 1
                # 주문 성공 직후 즉시 엑셀 저장 — 주문번호/토탈가격 유실 방지
                if self.excel_mgr:
                    try:
                        self.excel_mgr.save(self.model.all_rows())
                        log.info(
                            f"행{order.row} 엑셀 저장: 주문번호={order.order_number} "
                            f"토탈가격={order.total_price}"
                        )
                    except Exception as exc:
                        log.warning(f"행{order.row} 저장 실패 (루프 끝에 재시도): {exc}")
            elif order.status == "paused":
                if skip_on_pause:
                    log.info(
                        f"행{order.row} 일시정지 → 무인 모드라 다음 행으로 건너뜁니다 "
                        "(나중에 우클릭 → 이어서 진행)"
                    )
                    skipped_count += 1
                else:
                    log.info(
                        f"행{order.row} 일시정지 — 사용자 수정 후 '이어서 진행' 필요. 전체 진행 중단."
                    )
                    break
            elif order.status in ("failed", "unavailable"):
                if skip_on_error:
                    log.info(f"행{order.row} {order.status} → 다음 행으로 건너뜁니다")
                    skipped_count += 1
                else:
                    log.info(f"행{order.row} {order.status} — 전체 진행 중단")
                    break

            # 행간 딜레이 (서버 부담 회피)
            if i < len(targets):
                import asyncio as _aio
                await _aio.sleep(self.settings.automation.inter_order_delay_ms / 1000)

        # 루프 종료 후 하이라이트 해제 + 마지막 저장
        self._ui(lambda: self._focus_active_row(None))
        if self.excel_mgr:
            try:
                self.excel_mgr.save(self.model.all_rows())
            except Exception as exc:
                log.warning(f"최종 저장 실패: {exc}")

        # 결과 알림
        if aborted:
            title = "주문 중단됨"
        else:
            title = "주문 완료" if selected_only is None else "선택 주문 완료"
        shopback_note = ""
        if getattr(self.settings.automation, "verify_shopback", True):
            shopback_note = (
                "\n\n[샵백 적립 검증]\n"
                "각 주문의 샵백 추적 결과는 data/diagnostics/shopback_*.json 에 저장됩니다.\n"
                "실제 적립 여부는 1~3일 후 샵백 사이트에서 확인하세요."
            )
        done_count = ok_count + skipped_count
        header = (
            f"{scope_label} — 사용자 요청으로 중단됨 ({done_count}/{len(targets)}건 처리).\n\n"
            if aborted else
            f"{scope_label} 중 {len(targets)}건 처리 완료.\n\n"
        )
        await self._show_info_async(
            title,
            header +
            f"성공: {ok_count}건\n"
            f"건너뜀/미처리: {len(targets) - ok_count}건\n\n"
            "건너뛴 행은 테이블에서 상태(실패/수정 필요/판매 불가)로 확인 후 "
            "우클릭 메뉴로 개별 처리할 수 있습니다."
            f"{shopback_note}",
        )

    async def _execute_order_async(self, order: Order) -> None:
        """단일 주문 실행. 예외가 발생해도 프로그램은 절대 종료되지 않는다."""
        try:
            await self.browser.start()

            def on_state(o: Order, state: OrderState, msg: str | None) -> None:
                # 백그라운드 스레드 → UI 호출은 모두 메인 스레드로 마샬링
                self.orderUpdated.emit(o)
                stage = f"{state.value}"
                if msg:
                    stage = f"{state.value} · {msg}"
                    text = f"행 {o.row}: {msg}"
                    self._ui(lambda: self.statusBar().showMessage(text))
                # 현재 진행 중 행 하이라이트 + 테이블 스크롤
                row_num = o.row
                st = stage
                self._ui(lambda: self._focus_active_row(row_num, st))
                if state == OrderState.WAIT_PAYMENT and msg and "인증" in msg:
                    self._ui(lambda: self._notify_user_attention(o, msg or ""))

            if self.automation is None:
                self.automation = OrderAutomation(
                    self.browser,
                    self.selectors,
                    self.settings.automation,
                    on_state=on_state,
                    on_confirm=self._ask_yesno_async,
                )
            else:
                self.automation.on_state = on_state
                self.automation.on_confirm = self._ask_yesno_async

            await self.automation.execute(order)
            # 이 주문 완료 → 하이라이트 해제 (다음 행에서 다시 켜짐)
            self._ui(lambda: self._focus_active_row(None))

            if self.excel_mgr:
                self.excel_mgr.update_order(order)

            if order.status == "completed":
                self.state_mgr.mark_completed(order)
                log.info(f"주문 완료: 행{order.row} 주문번호={order.order_number}")
            elif order.status == "failed":
                self.state_mgr.mark_failed(order)

        except Exception as exc:
            log.exception(f"예상치 못한 오류 (프로그램은 계속됩니다): {exc}")
            order.status = "failed"
            order.error_message = str(exc)
            if self.excel_mgr:
                self.excel_mgr.update_order(order)
        finally:
            self.orderUpdated.emit(order)
            self._ui(self._refresh_summary)

    def _on_refill_selected_clicked(self) -> None:
        """카드 버튼: 선택한 행의 주문서 폼을 다시 자동 입력."""
        selected = self._selected_orders()
        if not selected:
            QMessageBox.information(
                self,
                "행 선택 필요",
                "테이블에서 다시 입력할 주문 행을 먼저 선택해 주세요.\n"
                "(여러 행 선택 시 첫 번째 행이 사용됩니다)",
            )
            return
        order = selected[0]
        if not (self.automation and self.automation.has_active_page(order)):
            QMessageBox.information(
                self,
                "활성 주문 없음",
                f"행 {order.row} 의 주문서 페이지가 열려 있지 않습니다.\n"
                "먼저 '주문하기'로 해당 행을 시작하세요.",
            )
            return
        self._run_async(self._refill_order_async(order))

    async def _refill_order_async(self, order: Order) -> None:
        """현재 활성 주문서 페이지에 배송지/통관번호 등을 다시 자동 입력.

        샵백 로그인 등으로 다른 탭을 다녀온 뒤 주문서가 초기화된 경우 사용.
        """
        try:
            await self.browser.start()

            def on_state(o: Order, state: OrderState, msg: str | None) -> None:
                self.orderUpdated.emit(o)
                if msg:
                    text = f"행 {o.row}: {msg}"
                    self._ui(lambda: self.statusBar().showMessage(text))

            if self.automation is None:
                QMessageBox.information(
                    self,
                    "알림",
                    "재입력할 활성 주문이 없습니다. 먼저 '주문하기'로 시작하세요.",
                )
                return
            self.automation.on_state = on_state
            await self.automation.refill_form(order)
            if self.excel_mgr:
                self.excel_mgr.update_order(order)
            if order.status == "completed":
                self.state_mgr.mark_completed(order)
        except Exception as exc:
            log.exception(f"폼 재입력 중 오류: {exc}")
            order.status = "failed"
            order.error_message = str(exc)
        finally:
            self.orderUpdated.emit(order)
            self._ui(self._refresh_summary)

    async def _resume_order_async(self, order: Order) -> None:
        """paused/failed 주문을 현재 페이지 상태에서 이어서 진행."""
        try:
            await self.browser.start()

            def on_state(o: Order, state: OrderState, msg: str | None) -> None:
                self.orderUpdated.emit(o)
                if msg:
                    text = f"행 {o.row}: {msg}"
                    self._ui(lambda: self.statusBar().showMessage(text))

            if self.automation is None:
                self.automation = OrderAutomation(
                    self.browser, self.selectors, self.settings.automation,
                    on_state=on_state,
                    on_confirm=self._ask_yesno_async,
                )
            else:
                self.automation.on_state = on_state
                self.automation.on_confirm = self._ask_yesno_async
            await self.automation.resume(order)
            if self.excel_mgr:
                self.excel_mgr.update_order(order)
            if order.status == "completed":
                self.state_mgr.mark_completed(order)
        except Exception as exc:
            log.exception(f"재개 중 예상치 못한 오류: {exc}")
            order.status = "failed"
            order.error_message = str(exc)
        finally:
            self.orderUpdated.emit(order)
            self._ui(self._refresh_summary)

    def _on_order_updated(self, order: Order) -> None:
        self.model.update_order(order)

    def _selected_orders(self) -> list[Order]:
        """현재 선택된 행 중 유효한 Order만 반환."""
        sel_model = self.table.selectionModel()
        if sel_model is None:
            return []
        seen = set()
        out: list[Order] = []
        for ix in sel_model.selectedRows():
            r = ix.row()
            if r in seen:
                continue
            seen.add(r)
            it = self.model.get_row(r)
            if isinstance(it, Order):
                out.append(it)
        return out

    def _order_for_index(self, idx) -> Order | None:
        """QModelIndex → Order. Order 가 아니거나 invalid 행이면 None."""
        if idx is None or not idx.isValid():
            return None
        item = self.model.get_row(idx.row())
        if isinstance(item, Order):
            return item
        return None

    def _show_context_menu(self, pos) -> None:
        idx = self.table.indexAt(pos)
        if not idx.isValid():
            return
        item = self.model.get_row(idx.row())
        if item is None:
            return

        # 다중 선택 액션 (선택 2개 이상 + 모두 Order)
        selected = self._selected_orders()
        if len(selected) >= 2:
            menu = QMenu(self)
            scrape_sel = menu.addAction(
                f"선택한 {len(selected)}건 가격 조회"
            )
            order_sel = menu.addAction(
                f"선택한 {len(selected)}건 주문하기"
            )
            menu.addSeparator()
            reset_sel = menu.addAction(
                f"선택한 {len(selected)}건 대기 상태로 되돌리기"
            )
            act = menu.exec(self.table.viewport().mapToGlobal(pos))
            if act is scrape_sel:
                self._run_selected_scrape(selected)
            elif act is order_sel:
                self._run_selected_orders(selected)
            elif act is reset_sel:
                self._reset_orders_to_pending(selected)
            return

        if not isinstance(item, Order):
            # 검증 실패 행은 편집만 가능, 주문 관련 메뉴는 제공하지 않는다.
            menu = QMenu(self)
            delete_action = menu.addAction("이 행 삭제")
            act = menu.exec(self.table.viewport().mapToGlobal(pos))
            if act is delete_action:
                rows = self.model.all_rows()
                rows.pop(idx.row())
                self.model.set_rows(rows)
                self._refresh_summary()
            return
        order = item
        menu = QMenu(self)

        # "다음으로" — 결제 후 사용자가 트리거하는 액션.
        # automation 에서 해당 행이 사용자 트리거를 기다리는 중일 때만 노출.
        next_action = None
        if (
            self.automation
            and getattr(self.automation, "is_awaiting_next", None)
            and self.automation.is_awaiting_next(order.row)
        ):
            next_action = menu.addAction(
                "\u25B6 다음으로 (결제 완료 → 주문번호 저장)"
            )
            menu.addSeparator()

        resume_action = None
        if order.status in ("paused", "failed") and self.automation and self.automation.has_active_page(order):
            resume_action = menu.addAction("\u25B7 이어서 진행 (브라우저에서 수정 완료 후)")

        # 주문 정보 다시 입력 — 활성 페이지가 있으면 항상 노출
        refill_action = None
        if self.automation and self.automation.has_active_page(order):
            refill_action = menu.addAction(
                "주문 정보 다시 입력 (배송지/통관번호 등 자동 재입력)"
            )
            menu.addSeparator()

        # 판매 불가 상품 전용 액션
        unavailable_actions = {}
        if order.status == "unavailable":
            unavailable_actions["recheck"] = menu.addAction(
                "다시 확인 (재고 복구되었는지)"
            )
            unavailable_actions["delete"] = menu.addAction(
                "이 행 삭제 (엑셀에서 제외)"
            )
            unavailable_actions["mark_pending"] = menu.addAction(
                "대기 상태로 되돌리기 (URL 수정 후)"
            )
            menu.addSeparator()

        retry = menu.addAction("처음부터 재시도")
        skip = menu.addAction("건너뛰기 (완료로 표시)")
        menu.addSeparator()
        abandon = menu.addAction("이 주문의 브라우저 탭 닫기")
        abandon.setEnabled(bool(self.automation and self.automation.has_active_page(order)))
        view_screenshot = menu.addAction("에러 스크린샷 열기")
        view_screenshot.setEnabled(bool(order.screenshot_path))

        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if next_action and action is next_action:
            if self.automation:
                self.automation.signal_next(order.row)
                self.statusBar().showMessage(
                    f"행 {order.row}: '다음으로' 신호 전송 — 주문번호 추출 중...", 4000
                )
        elif resume_action and action is resume_action:
            self._run_async(self._resume_order_async(order))
        elif refill_action and action is refill_action:
            self._run_async(self._refill_order_async(order))
        elif action is retry:
            # 처음부터 다시 — 기존 페이지 버리기
            if self.automation:
                self._run_async(self.automation.abandon(order))
            order.status = "pending"
            order.error_message = None
            self.model.update_order(order)
            self._run_async(self._run_single_order_with_guard(order))
        elif action is skip:
            order.status = "completed"
            self.model.update_order(order)
            if self.excel_mgr:
                self.excel_mgr.update_order(order)
        elif action is abandon:
            if self.automation:
                self._run_async(self.automation.abandon(order))
        elif action is view_screenshot and order.screenshot_path:
            self._open_path(order.screenshot_path)
        elif action is unavailable_actions.get("recheck"):
            # 판매 재개 여부 다시 체크
            order.status = "pending"
            order.error_message = None
            self.model.update_order(order)
            self._run_async(self._scrape_prices_async(only_missing=True))
        elif action is unavailable_actions.get("delete"):
            reply = QMessageBox.question(
                self,
                "행 삭제",
                f"행 {order.row}을 엑셀에서 삭제하시겠습니까? "
                "(메모리상 삭제 — 원본 파일에 반영하려면 '원본에 저장'을 누르세요)",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                rows = self.model.all_rows()
                rows.pop(idx.row())
                self.model.set_rows(rows)
                if self.excel_mgr:
                    self.excel_mgr.replace_rows(rows)
                self._refresh_summary()
        elif action is unavailable_actions.get("mark_pending"):
            order.status = "pending"
            order.error_message = None
            self.model.update_order(order)
            QMessageBox.information(
                self,
                "안내",
                "대기 상태로 되돌렸습니다.\n"
                "구매처(URL) 셀을 더블클릭해 새 상품 URL로 수정한 뒤 다시 시도하세요.",
            )

    # -------------------------------------------------------------
    # Selected-rows actions
    # -------------------------------------------------------------

    def _run_selected_scrape(self, orders: list[Order]) -> None:
        if not orders:
            return
        if not self._try_acquire():
            return
        self._run_async(self._scrape_prices_async(only_missing=False, targets=orders))

    def _run_selected_orders(self, orders: list[Order]) -> None:
        if not orders:
            return
        if not self._try_acquire():
            return
        self._run_async(self._start_all_orders_async(selected_only=orders))

    def _reset_orders_to_pending(self, orders: list[Order]) -> None:
        """선택한 주문들을 pending 상태로 되돌리기 (가격/주문번호는 유지)."""
        n = 0
        for o in orders:
            if o.status != "completed":
                o.status = "pending"
                o.error_message = None
                self.model.update_order(o)
                n += 1
        self._refresh_summary()
        QMessageBox.information(
            self, "되돌리기 완료",
            f"{n}건을 대기 상태로 되돌렸습니다."
        )

    def _on_open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            log.info("설정 저장됨 (일부 변경은 프로그램 재시작 필요)")

    def _on_diagnose_selectors(self) -> None:
        """선택한 행의 URL로 진단 — 페이지 HTML/스크린샷/매칭 셀렉터 후보 저장."""
        selected = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not selected:
            QMessageBox.warning(
                self, "알림", "진단할 행을 먼저 선택해 주세요 (행을 클릭)."
            )
            return
        idx = selected[0].row()
        item = self.model.get_row(idx)
        url = None
        if isinstance(item, Order):
            url = item.product_url
        elif item is not None:
            url = item.get("구매처")
        if not url:
            QMessageBox.warning(self, "알림", "선택한 행에 URL이 없습니다.")
            return
        if not self._try_acquire():
            return
        self._run_async(self._diagnose_url_async(url, item))

    async def _diagnose_url_async(self, url: str, order_item) -> None:
        from src.core.price_scraper import PriceScraper
        try:
            await self.browser.start()
            page = await self.browser.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # 페이지 로드 완료 대기
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

                # 1) 진단 파일 저장
                scraper = PriceScraper(self.browser, self.selectors, self.settings.price_scraper)
                # row 정보를 위해 더미 객체 처리
                row_no = getattr(order_item, "row", 0) if order_item else 0

                from datetime import datetime
                from pathlib import Path
                out_dir = Path("data/diagnostics")
                out_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                html_path = out_dir / f"diag_row{row_no}_{ts}.html"
                png_path = out_dir / f"diag_row{row_no}_{ts}.png"
                html_path.write_text(await page.content(), encoding="utf-8")
                await page.screenshot(path=str(png_path), full_page=False)

                # 2) JS fallback 으로 가격 후보 추출
                fallback_price = await scraper._fallback_price_from_dom(page)

                # 3) 셀렉터 후보 매칭 결과
                tried = self.selectors.get("product_page.price")
                matched = []
                for sel in tried:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0:
                            try:
                                txt = (await loc.inner_text()).strip()
                            except Exception:
                                txt = "(텍스트 없음)"
                            matched.append((sel, txt[:60]))
                    except Exception:
                        pass

                msg = [f"진단 완료\n\nURL: {url}\n"]
                msg.append(f"HTML: {html_path}")
                msg.append(f"스크린샷: {png_path}\n")
                if fallback_price:
                    msg.append(f"✓ 자동 추출된 가격(fallback): {fallback_price:,}원")
                else:
                    msg.append("✗ 자동 추출 실패 — DOM 구조 변경 의심")
                msg.append(f"\n시도한 셀렉터({len(tried)}개) 중 매칭된 것:")
                if matched:
                    for sel, txt in matched:
                        msg.append(f"  ✓ {sel}\n     → {txt!r}")
                else:
                    msg.append("  (없음)")
                msg.append(
                    "\n→ 매칭이 없으면 HTML/스크린샷을 확인 후 "
                    "config/selectors.yaml 의 product_page.price 에 셀렉터를 추가하세요."
                )
                await self._show_info_async("셀렉터 진단 결과", "\n".join(msg))
                log.info(f"진단 완료: {html_path.name}")
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
        except Exception as exc:
            log.exception(f"진단 실패: {exc}")
            await self._show_info_async("진단 실패", str(exc), icon="critical")

    def _on_open_diagnostics_dir(self) -> None:
        from pathlib import Path
        path = Path("data/diagnostics").resolve()
        path.mkdir(parents=True, exist_ok=True)
        self._open_path(str(path))

    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "프로그램 정보",
            "<b>11번가 자동 주문 프로그램 v1.0</b><br><br>"
            "Python + Playwright + PySide6 기반<br>"
            "개인 사용자 편의를 위한 도구입니다.",
        )

    # -------------------------------------------------------------
    # Onboarding
    # -------------------------------------------------------------

    def _show_onboarding_first_time(self) -> None:
        wizard = OnboardingWizard(self)
        wizard.exec()
        # 어떤 방식으로 닫았든(X, ESC, 완료 버튼) 한 번 본 것이므로 다음부터는 표시 안 함.
        # 사용자가 다시 보고 싶으면 도움말 메뉴의 '온보딩 마법사'를 직접 클릭.
        self.settings.ui.first_run = False
        try:
            self.settings.save()
            log.info("온보딩 완료 → 다음부터 표시 안 함")
        except Exception as exc:
            log.warning(f"설정 저장 실패: {exc}")

    def _show_onboarding(self) -> None:
        wizard = OnboardingWizard(self)
        wizard.exec()

    # -------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------

    def _apply_column_widths(self) -> None:
        """엑셀 느낌으로 컬럼별 합리적 기본폭 지정. 사용자는 자유롭게 드래그 가능."""
        from src.ui.order_table_model import COLUMNS as _COLS

        # 컬럼 이름 → 픽셀 폭
        widths = {
            "상태": 110,
            "구매처": 280,
            "수취인": 80,
            "수취인번호": 130,
            "통관번호": 130,
            "우편번호": 75,
            "수취인 주소": 240,
            "수량": 55,
            "영문이름": 140,
            "토탈가격": 100,
            "주문번호": 130,
            "비고": 200,
        }
        for i, (name, _, _) in enumerate(_COLS):
            w = widths.get(name)
            if w is not None:
                self.table.setColumnWidth(i, w)

    def _refresh_summary(self) -> None:
        """요약 정보는 하단 상태바 + 상단 정보 카드에 반영."""
        s = self.model.summary()
        try:
            self._refresh_info_card()
        except Exception:
            pass
        if s["total"] == 0:
            self.statusBar().showMessage("준비")
            return
        self.statusBar().showMessage(
            f"총 {s['total']}건 | 완료 {s.get('completed', 0)} | "
            f"진행중 {s.get('in_progress', 0)} | 수정필요 {s.get('paused', 0)} | "
            f"대기 {s.get('pending', 0)} | 실패 {s.get('failed', 0)} | "
            f"값확인 {s.get('invalid', 0)} | 판매불가 {s.get('unavailable', 0)}"
        )

    def _run_async(
        self,
        coro: Coroutine[Any, Any, Any],
        on_done: Callable[[Future], None] | None = None,
    ) -> Future:
        """코루틴을 백그라운드 러너에 제출하고 Future 반환.

        on_done 콜백은 항상 Qt 메인 스레드에서 호출된다 (QTimer.singleShot 으로 마샬링).
        """
        # 어떤 코루틴이 돌고 있는지 로그/디버깅용으로 이름 보존
        try:
            coro_name = getattr(coro, "__qualname__", None) or getattr(
                coro, "__name__", "<coro>"
            )
        except Exception:
            coro_name = "<coro>"
        log.debug(f"_run_async 시작: {coro_name}")
        fut = self._runner.submit(coro)
        # 강제 중단 시 cancel 대상으로 쓰기 위해 현재 실행 중인 Future 를 저장.
        self._current_future = fut
        self._current_future_name = coro_name

        def _bridge(f: Future) -> None:
            # add_done_callback 은 백그라운드 스레드에서 실행되므로,
            # UI 변경이 있는 콜백은 메인 스레드로 옮겨준다.
            def _emit():
                try:
                    if on_done is not None:
                        on_done(f)
                except Exception as exc:
                    log.exception(f"async 콜백 오류: {exc}")
                finally:
                    # 어떤 작업이든 끝나면 busy 해제 (UI 스레드)
                    self._busy = False
                    # 주문하기 버튼을 "주문하기" 로 복귀 + 플래그 리셋
                    try:
                        self._set_orders_button_running(False)
                    except Exception:
                        pass
                    self._abort_requested = False
                    self._force_abort_armed = False
                    if self._current_future is f:
                        self._current_future = None
            QTimer.singleShot(0, _emit)

        fut.add_done_callback(_bridge)
        return fut

    def _try_acquire(self) -> bool:
        """동시 실행 방지. True 면 이번 작업 시작 가능, False 면 거절.

        실제로 실행 중인 future 가 없는데도 _busy=True 인 경우
        (콜백이 어떤 이유로 호출 못 된 경우) 자동으로 해제한다.
        모달은 띄우지 않고 상태바 메시지로만 안내.
        """
        if self._busy:
            # 자동 복구: 실제 실행 중인 future 가 없으면 stale lock 으로 간주
            cur = getattr(self, "_current_future", None)
            stale = cur is None or cur.done()
            if stale:
                log.warning(
                    "_busy 가 True 인데 실행 중인 future 가 없음 → 자동 해제 (stale lock 복구)"
                )
                self._busy = False
                self._current_future = None
                # 버튼 상태도 함께 복구
                try:
                    self._set_orders_button_running(False)
                except Exception:
                    pass
            else:
                # 진짜로 실행 중 — 모달 대신 상태바 안내만
                running_name = getattr(self, "_current_future_name", "<unknown>")
                self.statusBar().showMessage(
                    f"'{running_name}' 작업이 끝나면 다시 시도해 주세요", 3500
                )
                log.info(
                    f"_try_acquire 거절: 다른 작업 진행 중 — {running_name}"
                )
                return False
        self._busy = True
        return True

    def _ui(self, fn: Callable[[], None]) -> None:
        """백그라운드 콜백에서 UI 변경할 때 사용 — 메인 스레드로 마샬링."""
        QTimer.singleShot(0, fn)

    # -------------------------------------------------------------
    # 로그 → 팝업 디스패처
    # -------------------------------------------------------------

    def _maybe_popup_from_log(self, message: str, level: str) -> None:
        """특정 중요한 경고 로그가 올라오면 사용자에게 팝업으로도 알린다.

        로그는 로그 패널에 계속 쌓이지만, 놓치면 적립이 안 되는 이벤트
        (예: 샵백 미감지)는 모달 팝업으로 한번 더 주의를 끈다.
        같은 행/이벤트로 팝업이 연타되지 않도록 키 기반 중복 억제를 한다.
        """
        if level != "WARNING":
            return

        # 샵백 추적 미감지 경고
        if "샵백 추적이 감지되지 않았습니다" in message:
            import re
            m = re.search(r"행(\d+)", message)
            row_label = m.group(1) if m else "?"
            key = f"shopback_miss:{row_label}:{message[:80]}"
            if key in self._shown_popup_keys:
                return
            self._shown_popup_keys.add(key)

            text = (
                f"\u26A0 행{row_label} 주문에서 샵백 추적이 감지되지 않았습니다. "
                "이 주문은 적립이 안 될 가능성이 높습니다. "
                "브라우저의 샵백 확장 아이콘을 클릭해 활성화해 주세요."
            )
            QTimer.singleShot(0, lambda: self._show_inline_notice(text))
            return

    def _show_inline_notice(self, text: str) -> None:
        """메인 윈도우 내부에 임베드된 알림 배너 표시. 별도 창이 뜨지 않음.

        - 우측 상단 고정, 사용자가 [확인] 을 누르거나 X 를 눌러야 닫힘.
        - 부모는 main window 의 central widget 이므로 프로그램 창 밖으로 나가지 않음.
        """
        parent = self.centralWidget() or self
        banner = QFrame(parent)
        banner.setObjectName("inlineNotice")
        banner.setStyleSheet(
            "QFrame#inlineNotice {"
            "  background: #FEF3C7;"
            "  border: 1px solid #F59E0B;"
            "  border-radius: 8px;"
            "}"
            "QLabel { background: transparent; color: #78350F; font-size: 13px; }"
            "QPushButton {"
            "  background: #F59E0B; color: white; border: none;"
            "  border-radius: 6px; padding: 6px 14px; font-weight: 600;"
            "}"
            "QPushButton:hover { background: #D97706; }"
        )
        lay = QHBoxLayout(banner)
        lay.setContentsMargins(14, 10, 10, 10)
        lay.setSpacing(10)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(520)
        ok_btn = QPushButton("확인")
        ok_btn.setCursor(Qt.PointingHandCursor)
        lay.addWidget(lbl, 1)
        lay.addWidget(ok_btn, 0, Qt.AlignVCenter)

        # 이전에 뜬 배너가 있으면 위로 쌓이지 않게 스택 정리
        if not hasattr(self, "_inline_notices"):
            self._inline_notices: list[QFrame] = []

        def _close():
            try:
                self._inline_notices.remove(banner)
            except ValueError:
                pass
            banner.hide()
            banner.deleteLater()
            self._reflow_inline_notices()

        ok_btn.clicked.connect(_close)

        self._inline_notices.append(banner)
        banner.adjustSize()
        banner.show()
        banner.raise_()
        self._reflow_inline_notices()

    def _reflow_inline_notices(self) -> None:
        """배너들을 우측 상단에 세로로 쌓는다."""
        notices = getattr(self, "_inline_notices", [])
        parent = self.centralWidget() or self
        margin = 16
        y = margin
        for b in notices:
            b.adjustSize()
            w = min(580, max(360, b.sizeHint().width()))
            h = b.sizeHint().height()
            x = parent.width() - w - margin
            b.setGeometry(x, y, w, h)
            y += h + 8

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_inline_notices") and self._inline_notices:
            self._reflow_inline_notices()

    # -------------------------------------------------------------
    # Main-thread dialog bridge (백그라운드 코루틴 → 메인 스레드 다이얼로그)
    #
    # 규칙: AsyncRunner 의 백그라운드 스레드에서 실행되는 코루틴은
    #       절대 Qt UI 객체(QMessageBox 등)를 직접 만들면 안 된다.
    #       반드시 아래 헬퍼들을 await 하여 메인 스레드에서 띄우게 한다.
    # -------------------------------------------------------------

    def _ask_main_thread(self, fn: Callable[[], Any]) -> Any:
        """fn() 을 메인 스레드에서 실행하고 결과를 동기적으로 반환.

        백그라운드 스레드에서 호출 — 메인 스레드로 작업을 던지고 result 까지 대기.
        """
        result_fut: Future = Future()

        def _runner():
            try:
                result_fut.set_result(fn())
            except BaseException as exc:
                result_fut.set_exception(exc)

        QTimer.singleShot(0, _runner)
        return result_fut.result()  # 메인 스레드에서 끝날 때까지 블로킹

    async def _show_info_async(
        self, title: str, text: str, icon: str = "info"
    ) -> None:
        """info / warning / critical 다이얼로그 (확인 버튼만)."""
        import asyncio

        def _show():
            box = QMessageBox(self)
            if icon == "warning":
                box.setIcon(QMessageBox.Warning)
            elif icon == "critical":
                box.setIcon(QMessageBox.Critical)
            else:
                box.setIcon(QMessageBox.Information)
            box.setWindowTitle(title)
            box.setText(text)
            box.exec()

        await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._ask_main_thread(_show)
        )

    async def _ask_yesno_async(
        self, title: str, text: str, default_yes: bool = True
    ) -> bool:
        """예/아니오 모달 다이얼로그. 메인 윈도우 전체를 어두운 오버레이로 가린다.

        백그라운드 코루틴에서 await 가능. 사용자가 '확인' 을 누르면 True,
        '취소' 또는 닫기를 누르면 False 반환.
        """
        import asyncio

        def _ask() -> bool:
            return self._show_blocking_modal(title, text, default_yes)

        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._ask_main_thread(_ask)
        )

    def _show_blocking_modal(
        self, title: str, text: str, default_yes: bool = True
    ) -> bool:
        """메인 윈도우 전체를 어두운 오버레이로 가린 뒤 중앙에 모달을 띄운다.

        반환: 사용자가 '확인' 을 누르면 True, 그 외(취소/닫기)는 False.
        """
        from src.utils.logger import get_logger as _gl
        _log = _gl()
        _log.info(f"[MODAL] _show_blocking_modal 진입: title={title!r}")
        # 메인 윈도우가 숨겨져 있거나 최소화되어 있으면 다이얼로그가 보이지 않을 수
        # 있으므로 강제로 표시/활성화한다.
        try:
            if self.isMinimized():
                self.showNormal()
            self.show()
            self.raise_()
            self.activateWindow()
        except Exception as exc:
            _log.warning(f"[MODAL] 메인 윈도우 활성화 실패: {exc}")
        # 1) 어두운 반투명 오버레이 — 메인 윈도우 콘텐츠를 덮는다.
        parent_for_overlay = self.centralWidget() or self
        overlay = QFrame(parent_for_overlay)
        overlay.setObjectName("modalOverlay")
        overlay.setStyleSheet(
            "QFrame#modalOverlay { background: rgba(0, 0, 0, 170); }"
        )
        overlay.setGeometry(
            0, 0, parent_for_overlay.width(), parent_for_overlay.height()
        )
        overlay.show()
        overlay.raise_()

        # 2) 중앙 모달 다이얼로그 — 부모는 메인 윈도우(self), 앱 전체 모달.
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setStyleSheet(
            "QDialog { background: white; border-radius: 12px; }"
            "QLabel#modalTitle { font-size: 16px; font-weight: 700; color: #111827; }"
            "QLabel#modalBody { font-size: 13px; color: #374151; }"
            "QPushButton {"
            "  padding: 8px 18px; border-radius: 6px; font-weight: 600;"
            "  min-width: 90px;"
            "}"
            "QPushButton#okBtn { background: #2563EB; color: white; border: none; }"
            "QPushButton#okBtn:hover { background: #1D4ED8; }"
            "QPushButton#cancelBtn {"
            "  background: white; color: #374151; border: 1px solid #D1D5DB;"
            "}"
            "QPushButton#cancelBtn:hover { background: #F3F4F6; }"
        )
        v = QVBoxLayout(dlg)
        v.setContentsMargins(24, 22, 24, 20)
        v.setSpacing(14)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("modalTitle")
        body_lbl = QLabel(text)
        body_lbl.setObjectName("modalBody")
        body_lbl.setWordWrap(True)
        body_lbl.setMinimumWidth(420)
        v.addWidget(title_lbl)
        v.addWidget(body_lbl)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("취소")
        cancel_btn.setObjectName("cancelBtn")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        ok_btn = QPushButton("확인")
        ok_btn.setObjectName("okBtn")
        ok_btn.setCursor(Qt.PointingHandCursor)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(ok_btn)
        v.addLayout(btn_row)

        ok_btn.clicked.connect(lambda: dlg.done(1))
        cancel_btn.clicked.connect(lambda: dlg.done(0))
        if default_yes:
            ok_btn.setDefault(True)
            ok_btn.setAutoDefault(True)
        else:
            cancel_btn.setDefault(True)
            cancel_btn.setAutoDefault(True)

        # 메인 윈도우 중앙으로 위치시키고 가장 앞으로 띄운다.
        dlg.adjustSize()
        try:
            geo = self.geometry()
            dlg.move(
                geo.center().x() - dlg.width() // 2,
                geo.center().y() - dlg.height() // 2,
            )
        except Exception:
            pass
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        _log.info("[MODAL] dlg.exec() 호출 직전")
        try:
            result = dlg.exec()
        finally:
            overlay.hide()
            overlay.deleteLater()
        _log.info(f"[MODAL] dlg.exec() 반환: result={result}")
        return result == 1

    async def _ask_choice_async(
        self,
        title: str,
        text: str,
        choices: list[str],
        default_index: int = 0,
    ) -> int | None:
        """여러 선택지 다이얼로그. 선택 인덱스 또는 None(취소) 반환."""
        import asyncio

        def _ask() -> int | None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Question)
            box.setWindowTitle(title)
            box.setText(text)
            buttons: list = []
            for i, label in enumerate(choices):
                btn = box.addButton(label, QMessageBox.AcceptRole)
                buttons.append(btn)
                if i == default_index:
                    box.setDefaultButton(btn)
            cancel = box.addButton("취소", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is cancel or clicked is None:
                return None
            for i, b in enumerate(buttons):
                if clicked is b:
                    return i
            return None

        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self._ask_main_thread(_ask)
        )

    def _focus_active_row(self, excel_row: int | None, stage: str = "") -> None:
        """테이블에서 현재 처리 중인 행 하이라이트 + 스크롤."""
        self.model.set_active_row(excel_row, stage)
        if excel_row is None:
            return
        # 해당 엑셀 행을 테이블에서 찾아 스크롤
        for i, r in enumerate(self.model.all_rows()):
            if getattr(r, "row", None) == excel_row:
                idx = self.model.index(i, 0)
                self.table.scrollTo(idx, self.table.PositionAtCenter)
                break

    def _notify_user_attention(self, order: Order, msg: str) -> None:
        """카드 인증 등 사용자 직접 처리가 필요할 때 주의 환기.

        - 작업표시줄 아이콘 깜빡임 (OS 지원 시)
        - 윈도우 raise (포커스 강탈은 안 함 — 사용자가 인증 작업 중일 수 있음)
        """
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app:
                app.alert(self, 0)  # 작업표시줄 깜빡임 (macOS dock bounce)
        except Exception:
            pass
        log.warning(f"\u26A0 사용자 작업 필요 — 행 {order.row}: {msg}")

    def _open_path(self, path: str) -> None:
        import subprocess
        import sys

        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform == "win32":
                import os

                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            log.warning(f"파일 열기 실패: {exc}")

    # -------------------------------------------------------------
    # Close
    # -------------------------------------------------------------

    def closeEvent(self, event) -> None:
        log.info("프로그램 종료 중...")
        # 백그라운드 러너의 자체 루프에서 브라우저 정리
        try:
            fut = self._runner.submit(self.browser.close())
            fut.result(timeout=5)
        except Exception as exc:
            log.warning(f"브라우저 종료 오류: {exc}")
        try:
            self._runner.shutdown(timeout=3)
        except Exception as exc:
            log.warning(f"러너 종료 오류: {exc}")

        self.log_bridge.detach()
        super().closeEvent(event)

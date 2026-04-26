"""앱 전체 라이트(화이트) 테마 팔레트 + 글로벌 스타일시트.

OS가 다크 모드라도 프로그램은 항상 화이트 모드로 고정.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


# 팔레트 색상 (중앙 관리)
COLOR_WINDOW = "#FFFFFF"
COLOR_BASE = "#FFFFFF"
COLOR_ALT_BASE = "#F3F4F6"          # 더 진하게 — zebra stripe 가독성 ↑
COLOR_TEXT = "#111827"
COLOR_MUTED = "#6B7280"
COLOR_BORDER = "#E5E7EB"
COLOR_GRID = "#D1D5DB"              # 테이블 gridline 전용 (테두리보다 진함)
COLOR_HEADER_BG = "#E5E7EB"         # 헤더 배경 (기존 #F3F4F6 보다 진함)
COLOR_HIGHLIGHT = "#111827"
COLOR_HIGHLIGHT_TEXT = "#FFFFFF"
COLOR_DISABLED_TEXT = "#9CA3AF"
COLOR_BUTTON = "#F3F4F6"
COLOR_TOOLTIP_BASE = "#111827"
COLOR_TOOLTIP_TEXT = "#FFFFFF"


def build_light_palette() -> QPalette:
    """화이트 모드 QPalette 생성 (OS 다크 모드 무시)."""
    p = QPalette()

    # Active/Inactive 공통
    p.setColor(QPalette.Window, QColor(COLOR_WINDOW))
    p.setColor(QPalette.WindowText, QColor(COLOR_TEXT))
    p.setColor(QPalette.Base, QColor(COLOR_BASE))
    p.setColor(QPalette.AlternateBase, QColor(COLOR_ALT_BASE))
    p.setColor(QPalette.Text, QColor(COLOR_TEXT))
    p.setColor(QPalette.Button, QColor(COLOR_BUTTON))
    p.setColor(QPalette.ButtonText, QColor(COLOR_TEXT))
    p.setColor(QPalette.BrightText, QColor("#111827"))
    p.setColor(QPalette.Highlight, QColor(COLOR_HIGHLIGHT))
    p.setColor(QPalette.HighlightedText, QColor(COLOR_HIGHLIGHT_TEXT))
    p.setColor(QPalette.ToolTipBase, QColor(COLOR_TOOLTIP_BASE))
    p.setColor(QPalette.ToolTipText, QColor(COLOR_TOOLTIP_TEXT))
    p.setColor(QPalette.Link, QColor(COLOR_TEXT))
    p.setColor(QPalette.LinkVisited, QColor(COLOR_MUTED))
    p.setColor(QPalette.PlaceholderText, QColor(COLOR_MUTED))

    # Disabled
    p.setColor(QPalette.Disabled, QPalette.WindowText, QColor(COLOR_DISABLED_TEXT))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(COLOR_DISABLED_TEXT))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(COLOR_DISABLED_TEXT))
    p.setColor(QPalette.Disabled, QPalette.Highlight, QColor("#D1D5DB"))

    return p


GLOBAL_STYLESHEET = f"""
/* ===== 전역 기본값 ===== */
* {{
    color: {COLOR_TEXT};
}}

QWidget {{
    background-color: {COLOR_WINDOW};
    color: {COLOR_TEXT};
    font-size: 13px;
}}

QMainWindow, QDialog {{
    background-color: {COLOR_WINDOW};
}}

/* ===== 메뉴 & 툴바 ===== */
QMenuBar {{
    background-color: {COLOR_WINDOW};
    color: {COLOR_TEXT};
    border-bottom: 1px solid {COLOR_BORDER};
}}
QMenuBar::item {{
    background: transparent;
    padding: 6px 12px;
}}
QMenuBar::item:selected {{
    background: #F3F4F6;
    color: {COLOR_TEXT};
}}
QMenu {{
    background-color: {COLOR_WINDOW};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
}}
QMenu::item {{
    padding: 6px 24px 6px 16px;
}}
QMenu::item:selected {{
    background: #F3F4F6;
    color: {COLOR_TEXT};
}}
QMenu::separator {{
    height: 1px;
    background: {COLOR_BORDER};
    margin: 4px 8px;
}}

QToolBar {{
    background-color: {COLOR_WINDOW};
    border-bottom: 1px solid {COLOR_BORDER};
    spacing: 4px;
    padding: 4px;
}}
QToolBar::separator {{
    background: {COLOR_BORDER};
    width: 1px;
    margin: 4px 6px;
}}
QToolButton {{
    background: transparent;
    color: {COLOR_TEXT};
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 6px 10px;
}}
QToolButton:hover {{
    background: #F3F4F6;
    border-color: #E5E7EB;
}}
QToolButton:pressed {{
    background: #E5E7EB;
}}

/* ===== 상태 바 ===== */
QStatusBar {{
    background-color: #F9FAFB;
    color: {COLOR_MUTED};
    border-top: 1px solid {COLOR_BORDER};
}}

/* ===== 테이블 (엑셀처럼 뚜렷한 구분선) ===== */
QTableView {{
    background-color: {COLOR_WINDOW};
    alternate-background-color: {COLOR_ALT_BASE};
    color: {COLOR_TEXT};
    gridline-color: {COLOR_GRID};
    selection-background-color: #D1D5DB;
    selection-color: {COLOR_TEXT};
    border: 1px solid {COLOR_GRID};
    /* 셀마다 세로/가로선을 또렷하게 */
    show-decoration-selected: 1;
}}
QTableView::item {{
    padding: 6px 10px;
    border-right: 1px solid {COLOR_GRID};
    border-bottom: 1px solid {COLOR_GRID};
}}
QTableView::item:selected {{
    background: #D1D5DB;
    color: {COLOR_TEXT};
}}
QTableView::item:focus {{
    outline: 2px solid {COLOR_HIGHLIGHT};
    outline-offset: -2px;
}}
/* 테이블 셀 편집기: 전역 QLineEdit 스타일이 들어오면 삐져나오므로
   셀 안에 꽉 차게 플랫 스타일로 오버라이드한다 (엑셀과 동일한 느낌).
   테두리는 1px 로 얇게 해서 셀 바깥으로 튀어나오는 것을 방지. */
QTableView QLineEdit,
QTableView QPlainTextEdit,
QTableView QTextEdit,
QTableView QSpinBox,
QTableView QDoubleSpinBox,
QTableView QComboBox {{
    border: 1px solid {COLOR_HIGHLIGHT};
    border-radius: 0;
    padding: 0 2px;
    margin: 0;
    background: {COLOR_WINDOW};
    color: {COLOR_TEXT};
    selection-background-color: #D1D5DB;
    selection-color: {COLOR_TEXT};
}}

/* 헤더: 엑셀의 A,B,C / 1,2,3 같은 느낌 — 진한 배경 + 볼드 + 또렷한 테두리 */
QHeaderView {{
    background-color: {COLOR_HEADER_BG};
}}
QHeaderView::section {{
    background-color: {COLOR_HEADER_BG};
    color: {COLOR_TEXT};
    padding: 8px 10px;
    border: none;
    border-right: 1px solid {COLOR_GRID};
    border-bottom: 2px solid #9CA3AF;
    font-weight: 700;
}}
QHeaderView::section:horizontal {{
    border-top: 1px solid {COLOR_GRID};
}}
QHeaderView::section:vertical {{
    background-color: {COLOR_HEADER_BG};
    padding: 4px 8px;
    border-right: 2px solid #9CA3AF;
    border-bottom: 1px solid {COLOR_GRID};
    font-weight: 600;
    color: {COLOR_MUTED};
}}
QHeaderView::section:last {{
    border-right: none;
}}
QHeaderView::section:hover {{
    background-color: #D1D5DB;
}}
QTableCornerButton::section {{
    background: {COLOR_HEADER_BG};
    border: none;
    border-right: 2px solid #9CA3AF;
    border-bottom: 2px solid #9CA3AF;
}}

/* ===== 입력 위젯 ===== */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {COLOR_WINDOW};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #E5E7EB;
    selection-color: {COLOR_TEXT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {COLOR_HIGHLIGHT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: {COLOR_WINDOW};
    color: {COLOR_TEXT};
    selection-background-color: #E5E7EB;
    selection-color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
}}

/* ===== 버튼 ===== */
QPushButton {{
    background-color: #F3F4F6;
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 6px;
    padding: 6px 14px;
}}
QPushButton:hover {{
    background-color: #E5E7EB;
}}
QPushButton:pressed {{
    background-color: #D1D5DB;
}}
QPushButton:disabled {{
    background-color: #F9FAFB;
    color: {COLOR_DISABLED_TEXT};
    border-color: #E5E7EB;
}}
QPushButton:default {{
    background-color: {COLOR_HIGHLIGHT};
    color: {COLOR_HIGHLIGHT_TEXT};
    border-color: {COLOR_HIGHLIGHT};
}}
QPushButton:default:hover {{
    background-color: #1F2937;
}}

/* ===== 체크박스/라디오 ===== */
QCheckBox, QRadioButton {{
    color: {COLOR_TEXT};
    spacing: 6px;
    background: transparent;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
}}

/* ===== 스크롤바 ===== */
QScrollBar:vertical {{
    background: #F9FAFB;
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #D1D5DB;
    border-radius: 6px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: #9CA3AF;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: #F9FAFB;
    height: 12px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: #D1D5DB;
    border-radius: 6px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background: #9CA3AF;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* ===== 탭 ===== */
QTabWidget::pane {{
    border: 1px solid {COLOR_BORDER};
    background: {COLOR_WINDOW};
}}
QTabBar::tab {{
    background: #F3F4F6;
    color: {COLOR_TEXT};
    padding: 8px 14px;
    border: 1px solid {COLOR_BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{
    background: {COLOR_WINDOW};
    font-weight: 600;
}}

/* ===== Splitter ===== */
QSplitter::handle {{
    background: {COLOR_BORDER};
}}
QSplitter::handle:horizontal {{
    width: 2px;
}}
QSplitter::handle:vertical {{
    height: 2px;
}}

/* ===== QGroupBox / QFrame ===== */
QGroupBox {{
    background-color: {COLOR_WINDOW};
    color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER};
    border-radius: 8px;
    margin-top: 10px;
    padding-top: 14px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: {COLOR_TEXT};
}}

/* ===== Wizard ===== */
QWizard {{
    background-color: {COLOR_WINDOW};
}}
QWizard > QWidget {{
    background-color: {COLOR_WINDOW};
}}

/* ===== ToolTip ===== */
QToolTip {{
    background-color: {COLOR_TOOLTIP_BASE};
    color: {COLOR_TOOLTIP_TEXT};
    border: 1px solid {COLOR_TOOLTIP_BASE};
    padding: 4px 8px;
    border-radius: 4px;
}}

/* ===== MessageBox ===== */
QMessageBox {{
    background-color: {COLOR_WINDOW};
}}
QMessageBox QLabel {{
    color: {COLOR_TEXT};
    background: transparent;
}}
"""


def apply_light_theme(app: QApplication) -> None:
    """QApplication에 화이트 테마 적용. main.py에서 한 번만 호출."""
    app.setStyle("Fusion")
    app.setPalette(build_light_palette())
    app.setStyleSheet(GLOBAL_STYLESHEET)

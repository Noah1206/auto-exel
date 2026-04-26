"""실시간 로그 패널."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from src.utils.logger import get_logger


class LogPanel(QWidget):
    """터미널 스타일 헤더 + 스크롤 로그 영역."""

    def __init__(self, max_lines: int = 500, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setStyleSheet(
            "QWidget {"
            "  background: #111827;"
            "  border-top-left-radius: 6px;"
            "  border-top-right-radius: 6px;"
            "}"
        )
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 6, 10, 6)
        header_layout.setSpacing(8)

        # 선 아이콘 (모니터 모양 글리프) — 색이 들어간 이모지 대신.
        icon_lbl = QLabel("\u25A2")  # ▢
        icon_lbl.setStyleSheet("color: #10B981; font-size: 13px; background: transparent;")
        title_lbl = QLabel("실시간 로그")
        title_lbl.setStyleSheet(
            "color: #F9FAFB; font-size: 12px; font-weight: 600; background: transparent;"
        )
        header_layout.addWidget(icon_lbl, 0, Qt.AlignVCenter)
        header_layout.addWidget(title_lbl, 0, Qt.AlignVCenter)
        header_layout.addStretch(1)

        self._view = _LogTextEdit(max_lines=max_lines)

        layout.addWidget(header)
        layout.addWidget(self._view, 1)

    def append_line(self, message: str, level: str = "INFO") -> None:
        self._view.append_line(message, level)


class _LogTextEdit(QPlainTextEdit):
    """로그 메시지를 스크롤 영역에 표시."""

    def __init__(self, max_lines: int = 500, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(max_lines)
        self.setStyleSheet(
            "QPlainTextEdit {"
            "  font-family: Consolas, Menlo, monospace;"
            "  font-size: 12px;"
            "  background: #FFFFFF;"
            "  color: #111827;"
            "  border: 1px solid #E5E7EB;"
            "  border-top: none;"
            "  border-bottom-left-radius: 6px;"
            "  border-bottom-right-radius: 6px;"
            "  padding: 6px;"
            "}"
        )

    def append_line(self, message: str, level: str = "INFO") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        # 모두 선 글리프로 통일 (색깔 이모지 제외)
        icon = {
            "INFO": "\u00B7",     # ·
            "WARNING": "\u26A0",  # ⚠ (경고는 가독성 위해 유지)
            "ERROR": "\u2715",    # ✕
            "SUCCESS": "\u2713",  # ✓
        }.get(level, "·")
        self.appendPlainText(f"[{ts}] {icon} {message}")
        self.moveCursor(QTextCursor.End)


class QtLogBridge(QObject):
    """loguru → Qt 신호 브릿지. 별도 스레드에서도 안전하게 UI로 전달."""

    message = Signal(str, str)  # (message, level)

    def __init__(self):
        super().__init__()
        self._sink_id: int | None = None

    def attach(self) -> None:
        if self._sink_id is not None:
            return
        logger = get_logger()
        self._sink_id = logger.add(self._sink, level="INFO", enqueue=False)

    def detach(self) -> None:
        if self._sink_id is not None:
            get_logger().remove(self._sink_id)
            self._sink_id = None

    def _sink(self, msg) -> None:
        """loguru 핸들러 - 스레드 안전하게 Qt 신호 emit."""
        record = msg.record
        level = record["level"].name
        text = record["message"]
        self.message.emit(text, level)

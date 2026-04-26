"""컴팩트 섹션 카드 — 제목 + 우측 액션 + 본문 (선택).

카드 내부 여백을 최소화한 버전. 여러 섹션을 CompositeCard 로 묶어
하나의 테두리 안에 세로로 쌓을 수 있다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class SectionRow(QWidget):
    """한 섹션: 제목/부제 + 우측 액션 + (옵션) 본문 위젯."""

    def __init__(
        self,
        title: str,
        subtitle: str | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title_col.setContentsMargins(0, 0, 0, 0)

        self._title_lbl = QLabel(title)
        self._title_lbl.setStyleSheet(
            "QLabel { font-size: 14px; font-weight: 700; color: #111827;"
            " background: transparent; border: none; padding: 0; }"
        )
        title_col.addWidget(self._title_lbl)

        self._subtitle_lbl = QLabel(subtitle or "")
        self._subtitle_lbl.setStyleSheet(
            "QLabel { color: #6B7280; font-size: 12px;"
            " background: transparent; border: none; padding: 0; }"
        )
        self._subtitle_lbl.setVisible(bool(subtitle))
        title_col.addWidget(self._subtitle_lbl)

        header.addLayout(title_col, stretch=1)

        self._action_host = QWidget()
        self._action_host.setStyleSheet("background: transparent;")
        self._action_layout = QHBoxLayout(self._action_host)
        self._action_layout.setContentsMargins(0, 0, 0, 0)
        self._action_layout.setSpacing(6)
        header.addWidget(self._action_host, alignment=Qt.AlignRight | Qt.AlignVCenter)

        outer.addLayout(header)

        self._body_host = QWidget()
        self._body_host.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._body_host)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(4)
        self._body_host.setVisible(False)
        outer.addWidget(self._body_host)

    # ---- API ----

    def set_subtitle(self, text: str) -> None:
        self._subtitle_lbl.setText(text)
        self._subtitle_lbl.setVisible(bool(text))

    def set_action(self, widget: QWidget | None) -> None:
        while self._action_layout.count():
            item = self._action_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if widget is not None:
            self._action_layout.addWidget(widget)

    def set_body(self, widget: QWidget | None) -> None:
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if widget is not None:
            self._body_layout.addWidget(widget)
            self._body_host.setVisible(True)
        else:
            self._body_host.setVisible(False)


class CompositeCard(QFrame):
    """여러 SectionRow 를 하나의 테두리 안에 세로로 쌓는 카드.

    각 섹션 사이에는 얇은 구분선을 자동 삽입.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("compositeCard")
        self.setStyleSheet(
            "QFrame#compositeCard {"
            "  background: #FFFFFF;"
            "  border: 1px solid #E5E7EB;"
            "  border-radius: 10px;"
            "}"
        )
        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(12, 10, 12, 10)
        self._vbox.setSpacing(8)

    def add_section(self, section: SectionRow) -> None:
        if self._vbox.count() > 0:
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet(
                "QFrame { background: #F3F4F6; border: none; max-height: 1px; min-height: 1px; }"
            )
            self._vbox.addWidget(sep)
        self._vbox.addWidget(section)

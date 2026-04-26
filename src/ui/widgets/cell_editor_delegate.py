"""엑셀 느낌의 셀 편집 Delegate.

QTableView 기본 편집기는 QLineEdit 의 minimumSizeHint 때문에
긴 텍스트를 편집할 때 셀보다 넓게 튀어나오는 경우가 있다.
이 delegate 는 에디터 geometry 를 항상 option.rect (셀 영역)에
고정시켜 엑셀처럼 셀 안에서만 편집되도록 한다.
"""
from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QLineEdit, QStyledItemDelegate


class CellEditorDelegate(QStyledItemDelegate):
    """셀 영역을 벗어나지 않는 인-셀 편집기."""

    def createEditor(self, parent, option, index):  # noqa: N802
        editor = super().createEditor(parent, option, index)
        # QLineEdit 이라면 minimumSizeHint 를 셀 크기로 눌러버린다.
        if isinstance(editor, QLineEdit):
            editor.setFrame(False)  # 셀 경계와 충돌하는 내부 프레임 제거
            editor.setTextMargins(0, 0, 0, 0)
            editor.setMinimumSize(0, 0)
            editor.setMaximumSize(16777215, 16777215)
            # 배경을 흰색으로 채워서 옆 셀로 글자가 비치는 현상 방지
            editor.setAutoFillBackground(True)
            editor.setStyleSheet(
                "QLineEdit { background: white; border: 0; padding: 0; margin: 0; }"
            )
        return editor

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802
        # 셀 rect 에 강제 고정 — 에디터가 넓어지지 않도록.
        # 격자선(1px)을 침범하지 않도록 안쪽으로 1px 줄여서 그린다.
        rect = option.rect.adjusted(0, 0, -1, -1)
        editor.setGeometry(rect)
        editor.setFixedSize(rect.size())

    def sizeHint(self, option, index):  # noqa: N802
        # 텍스트 길이에 따라 셀이 커지지 않도록 기본 힌트 유지.
        base = super().sizeHint(option, index)
        return QSize(base.width(), base.height())

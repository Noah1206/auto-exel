"""주소 컬럼 전용 delegate — 검색용 부분만 굵게 표시.

ADDRESS_SEARCH_QUERY_ROLE 로 받은 부분 문자열이 셀 텍스트 안에 있으면
그 부분만 Bold 로, 나머지는 일반 weight 로 그린다.
"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from src.ui.order_table_model import ADDRESS_SEARCH_QUERY_ROLE


class AddressDelegate(QStyledItemDelegate):
    """주소 셀에서 검색용 부분만 굵게 그리는 delegate."""

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        full_text = index.data(Qt.DisplayRole) or ""
        bold_part = index.data(ADDRESS_SEARCH_QUERY_ROLE) or ""

        painter.save()
        try:
            # 배경
            if option.state & QStyle.State_Selected:
                painter.fillRect(option.rect, option.palette.highlight())
                fg = option.palette.highlightedText().color()
            else:
                bg = index.data(Qt.BackgroundRole)
                if isinstance(bg, QColor):
                    painter.fillRect(option.rect, bg)
                fg = QColor("#111827")

            if not full_text:
                return

            # bold_part 가 full_text 안에 있으면 그 구간만 굵게.
            # 못 찾으면 전체를 일반 폰트로.
            doc = QTextDocument()
            doc.setDefaultFont(option.font)
            if bold_part and bold_part in full_text:
                start = full_text.index(bold_part)
                end = start + len(bold_part)
                # rich-text 로 구성
                pre = full_text[:start].replace("&", "&amp;").replace("<", "&lt;")
                mid = full_text[start:end].replace("&", "&amp;").replace("<", "&lt;")
                post = full_text[end:].replace("&", "&amp;").replace("<", "&lt;")
                color_hex = fg.name()
                html = (
                    f'<span style="color:{color_hex};">'
                    f'{pre}<b>{mid}</b>{post}'
                    f'</span>'
                )
                doc.setHtml(html)
            else:
                doc.setPlainText(full_text)

            # 셀 좌측 패딩 4px, 세로 중앙
            padding = 6
            doc.setTextWidth(option.rect.width() - padding * 2)
            painter.translate(
                option.rect.left() + padding,
                option.rect.center().y() - doc.size().height() / 2,
            )
            doc.drawContents(painter)
        finally:
            painter.restore()

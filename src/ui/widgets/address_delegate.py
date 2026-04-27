"""주소 컬럼 전용 delegate — 검색용 부분만 굵게 표시 (한 줄 고정).

ADDRESS_SEARCH_QUERY_ROLE 로 받은 부분 문자열이 셀 텍스트 안에 있으면
그 부분만 Bold 로, 나머지는 일반 weight 로 그린다. 셀 폭을 넘으면
자동 줄바꿈하지 않고 한 줄로 잘리며, 전체 텍스트는 툴팁으로 보여준다.
"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate


# 모듈 import 의 순환 의존을 피하려고 ADDRESS_SEARCH_QUERY_ROLE 은 지연 import
def _addr_role():
    from src.ui.order_table_model import ADDRESS_SEARCH_QUERY_ROLE
    return ADDRESS_SEARCH_QUERY_ROLE


class AddressDelegate(QStyledItemDelegate):
    """주소 셀에서 검색용 부분만 굵게, 한 줄로 그리는 delegate."""

    HORIZONTAL_PADDING = 6

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        full_text = index.data(Qt.DisplayRole) or ""
        bold_part = index.data(_addr_role()) or ""

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

            # 그리기 영역: 가로 패딩, 세로 중앙
            x = option.rect.left() + self.HORIZONTAL_PADDING
            y_center = option.rect.center().y()
            avail_w = option.rect.width() - self.HORIZONTAL_PADDING * 2
            if avail_w <= 0:
                return

            # bold_part 가 full_text 에 있으면 [pre][bold][post] 3구간으로 분리
            if bold_part and bold_part in full_text:
                idx = full_text.index(bold_part)
                pre = full_text[:idx]
                mid = bold_part
                post = full_text[idx + len(bold_part):]
            else:
                pre, mid, post = full_text, "", ""

            # 폰트 두 개 — 일반 / Bold
            normal_font = QFont(option.font)
            bold_font = QFont(option.font)
            bold_font.setBold(True)

            # 폭이 부족하면 elide(...) 처리: 전체를 일반 폰트 기준으로 fits 체크 후
            # 맨 끝에 '…' 붙이는 방식. Bold 일부가 깨질 수 있어도 읽기 우선.
            painter.setPen(fg)

            # 단순 한 줄 그리기 — clip 해서 셀 밖으로 안 나가게
            painter.setClipRect(option.rect)
            cursor_x = x

            def _draw_segment(text: str, font: QFont) -> bool:
                """남은 폭 안에서 그릴 수 있는 만큼만 그리고 True 반환.
                폭을 넘기면 잘라서 '…' 붙여 그리고 False (더 그릴 거 없음) 반환.
                """
                nonlocal cursor_x
                if not text:
                    return True
                painter.setFont(font)
                fm = painter.fontMetrics()
                remaining = option.rect.right() - cursor_x - self.HORIZONTAL_PADDING
                if remaining <= 0:
                    return False
                seg_w = fm.horizontalAdvance(text)
                # 모두 그릴 수 있는 경우
                if seg_w <= remaining:
                    rect = QRect(
                        cursor_x,
                        int(y_center - fm.height() / 2),
                        seg_w,
                        fm.height(),
                    )
                    painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, text)
                    cursor_x += seg_w
                    return True
                # 잘라서 '…' 붙이기
                elided = fm.elidedText(text, Qt.ElideRight, remaining)
                rect = QRect(
                    cursor_x,
                    int(y_center - fm.height() / 2),
                    remaining,
                    fm.height(),
                )
                painter.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter, elided)
                cursor_x = option.rect.right() - self.HORIZONTAL_PADDING
                return False

            if _draw_segment(pre, normal_font):
                if _draw_segment(mid, bold_font):
                    _draw_segment(post, normal_font)
        finally:
            painter.restore()

    def helpEvent(self, event, view, option, index):
        """전체 텍스트를 툴팁으로 보여줘 잘린 부분도 확인 가능."""
        from PySide6.QtCore import QEvent
        from PySide6.QtWidgets import QToolTip
        if event.type() == QEvent.ToolTip:
            text = index.data(Qt.DisplayRole) or ""
            if text:
                QToolTip.showText(event.globalPos(), text, view)
            return True
        return super().helpEvent(event, view, option, index)

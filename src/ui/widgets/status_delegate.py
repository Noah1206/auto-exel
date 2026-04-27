"""상태 컬럼 전용 delegate — pill 배지 + 진행중 회전 스피너.

스크린샷 디자인처럼 상태를 둥근 알약 배지로 그린다.
진행중(in_progress)일 때는 배지 왼쪽의 동그라미가 계속 회전.
"""
from __future__ import annotations

from PySide6.QtCore import (
    QAbstractItemModel,
    QModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    QTimer,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QAbstractItemView, QStyledItemDelegate, QStyle

# status_key → (bg, fg, label, icon_char)
# icon_char: 진행중은 스피너로 그려서 None, 나머지는 글리프.
# 흑/회/백 무채색 팔레트만 사용.
_STATUS_STYLE: dict[str, tuple[str, str, str, str | None]] = {
    "completed":   ("#111827", "#FFFFFF", "성공",      "\u2713"),   # ✓  검정 배경 + 흰 글자
    "in_progress": ("#F3F4F6", "#111827", "진행중",    None),
    "paused":      ("#FFFFFF", "#111827", "수정 필요", "\u23F8"),  # ⏸  흰 배경 + 검정 테두리
    "failed":      ("#FFFFFF", "#111827", "실패",      "\u2715"),  # ✕
    "pending":     ("#F3F4F6", "#6B7280", "대기",      "\u25CB"),  # ○
    "invalid":     ("#FFFFFF", "#111827", "값 확인",   "!"),
    "unavailable": ("#E5E7EB", "#4B5563", "판매 불가", "\u00D7"),  # ×
}

# 테두리를 그려야 하는 상태 (배경이 흰색일 때 시각적으로 명확)
_STATUS_OUTLINE: set[str] = {"paused", "failed", "invalid"}


class StatusDelegate(QStyledItemDelegate):
    """상태 컬럼용 delegate — pill 스타일 + 진행중 회전 애니메이션.

    진행중 행이 있으면 내부 타이머가 약 60fps 로 돌면서 해당 행만 repaint.
    """

    SPINNER_INTERVAL_MS = 33   # ~30fps
    SPIN_DEGREES_PER_TICK = 12  # 360deg / 30 ≈ 12deg

    def __init__(
        self,
        view: QAbstractItemView,
        status_getter,
        parent=None,
        is_awaiting_next=None,
        on_next_clicked=None,
        is_awaiting_fill=None,
        on_fill_clicked=None,
        is_awaiting_eng_fill=None,
        on_eng_fill_clicked=None,
    ):
        """
        view: QTableView — repaint 를 위해 참조 필요
        status_getter: callable(index) -> str | None — 상태 key 반환
        is_awaiting_next: callable(index) -> bool — '다음으로' 버튼 노출 여부
        on_next_clicked: callable(index) -> None — '다음으로' 클릭 시 호출
        is_awaiting_fill: callable(index) -> bool — '기입' 버튼 노출 여부
        on_fill_clicked: callable(index) -> None — '기입' 클릭 시 호출
        """
        super().__init__(parent or view)
        self._view = view
        self._status_getter = status_getter
        self._is_awaiting_next = is_awaiting_next
        self._on_next_clicked = on_next_clicked
        self._is_awaiting_fill = is_awaiting_fill
        self._on_fill_clicked = on_fill_clicked
        self._is_awaiting_eng_fill = is_awaiting_eng_fill
        self._on_eng_fill_clicked = on_eng_fill_clicked
        self._spin_angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.SPINNER_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        # 행별 인라인 버튼의 화면 영역 캐시 (클릭 hit-test 용)
        self._next_btn_rects: dict[int, QRect] = {}
        self._fill_btn_rects: dict[int, QRect] = {}
        self._eng_fill_btn_rects: dict[int, QRect] = {}

    # ---- 타이머 제어 ----

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _on_tick(self) -> None:
        self._spin_angle = (self._spin_angle + self.SPIN_DEGREES_PER_TICK) % 360
        # in_progress 상태인 행만 업데이트 (viewport 전체 repaint 는 과함)
        model = self._view.model()
        if model is None:
            return
        col = self._status_column(model)
        if col < 0:
            return
        for row in range(model.rowCount()):
            idx = model.index(row, col)
            status = self._status_getter(idx)
            awaiting_next = bool(
                self._is_awaiting_next and self._is_awaiting_next(idx)
            )
            awaiting_fill = bool(
                self._is_awaiting_fill and self._is_awaiting_fill(idx)
            )
            awaiting_eng_fill = bool(
                self._is_awaiting_eng_fill
                and self._is_awaiting_eng_fill(idx)
            )
            if status == "in_progress" or awaiting_next or awaiting_fill or awaiting_eng_fill:
                rect = self._view.visualRect(idx)
                if rect.isValid():
                    self._view.viewport().update(rect)

    def _status_column(self, model: QAbstractItemModel) -> int:
        # 헤더에서 '상태' 찾기
        for c in range(model.columnCount()):
            header = model.headerData(c, Qt.Horizontal, Qt.DisplayRole)
            if header == "상태":
                return c
        return -1

    # ---- paint ----

    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        status = self._status_getter(index) or "pending"
        style = _STATUS_STYLE.get(status, _STATUS_STYLE["pending"])
        bg_hex, fg_hex, label, icon_char = style

        # 사용자 트리거 대기 중이면 알약 대신 액션 버튼을 셀 중앙에 단독 표시.
        row_key = index.row()
        awaiting_fill = bool(
            self._is_awaiting_fill and self._is_awaiting_fill(index)
        )
        awaiting_eng_fill = (
            not awaiting_fill
            and self._is_awaiting_eng_fill is not None
            and self._is_awaiting_eng_fill(index)
        )
        awaiting_next = (
            not awaiting_fill
            and not awaiting_eng_fill
            and self._is_awaiting_next is not None
            and self._is_awaiting_next(index)
        )

        painter.save()
        try:
            # 선택 하이라이트 / 행 배경 먼저 그림
            if option.state & QStyle.State_Selected:
                painter.fillRect(option.rect, option.palette.highlight())
            else:
                bg = index.data(Qt.BackgroundRole)
                if isinstance(bg, QColor):
                    painter.fillRect(option.rect, bg)

            painter.setRenderHint(QPainter.Antialiasing, True)

            # ── awaiting 상태: 액션 버튼만 셀 중앙에 단독 표시
            if awaiting_fill or awaiting_eng_fill or awaiting_next:
                if awaiting_fill:
                    btn_label = "기입"
                    btn_color = QColor("#10B981")  # 초록
                    target_rect_dict = self._fill_btn_rects
                    self._next_btn_rects.pop(row_key, None)
                    self._eng_fill_btn_rects.pop(row_key, None)
                elif awaiting_eng_fill:
                    btn_label = "영문기입"
                    btn_color = QColor("#F59E0B")  # 주황
                    target_rect_dict = self._eng_fill_btn_rects
                    self._next_btn_rects.pop(row_key, None)
                    self._fill_btn_rects.pop(row_key, None)
                else:
                    btn_label = "다음으로"
                    btn_color = QColor("#2563EB")  # 파랑
                    target_rect_dict = self._next_btn_rects
                    self._fill_btn_rects.pop(row_key, None)
                    self._eng_fill_btn_rects.pop(row_key, None)

                btn_font = QFont(option.font)
                btn_font.setWeight(QFont.DemiBold)
                painter.setFont(btn_font)
                bfm = painter.fontMetrics()
                btn_text_w = bfm.horizontalAdvance(btn_label)
                btn_h = max(22, bfm.height() + 6)
                btn_w = btn_text_w + 22
                cx = option.rect.center().x()
                cy = option.rect.center().y()
                btn_rect = QRectF(
                    cx - btn_w / 2, cy - btn_h / 2, btn_w, btn_h
                )
                # 셀 우측 넘으면 안쪽으로 당기기
                if btn_rect.right() > option.rect.right() - 4:
                    btn_rect.moveRight(option.rect.right() - 4)
                if btn_rect.left() < option.rect.left() + 4:
                    btn_rect.moveLeft(option.rect.left() + 4)
                btn_path = QPainterPath()
                radius = btn_h / 2
                btn_path.addRoundedRect(btn_rect, radius, radius)
                painter.fillPath(btn_path, btn_color)
                painter.setPen(QColor("#FFFFFF"))
                painter.drawText(btn_rect, Qt.AlignCenter, btn_label)
                target_rect_dict[row_key] = btn_rect.toRect()
                return

            # ── 일반 상태: 알약 배지 표시 (기존 디자인)
            self._next_btn_rects.pop(row_key, None)
            self._fill_btn_rects.pop(row_key, None)
            self._eng_fill_btn_rects.pop(row_key, None)

            font = QFont(option.font)
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            fm = painter.fontMetrics()

            text_w = fm.horizontalAdvance(label)
            pill_h = max(22, fm.height() + 6)
            icon_slot = 16
            inner_pad_h = 10
            inner_gap = 6
            pill_w = inner_pad_h + icon_slot + inner_gap + text_w + inner_pad_h

            cx = option.rect.center().x()
            cy = option.rect.center().y()
            pill_rect = QRectF(
                cx - pill_w / 2, cy - pill_h / 2, pill_w, pill_h
            )

            path = QPainterPath()
            path.addRoundedRect(pill_rect, pill_h / 2, pill_h / 2)
            painter.fillPath(path, QColor(bg_hex))
            if status in _STATUS_OUTLINE:
                border_pen = QPen(QColor("#111827"), 1.0)
                painter.setPen(border_pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawPath(path)

            fg = QColor(fg_hex)

            icon_rect = QRectF(
                pill_rect.left() + inner_pad_h,
                pill_rect.top() + (pill_h - icon_slot) / 2,
                icon_slot,
                icon_slot,
            )

            if status == "in_progress":
                self._paint_spinner(painter, icon_rect, fg)
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(fg)
                painter.setBrush(Qt.NoBrush)
                pen = QPen(fg, 1.5)
                painter.setPen(pen)
                painter.drawEllipse(icon_rect.adjusted(1.5, 1.5, -1.5, -1.5))
                if icon_char:
                    glyph_font = QFont(option.font)
                    base_pt = font.pointSizeF()
                    if base_pt > 0:
                        glyph_font.setPointSizeF(max(8.0, base_pt - 1))
                    glyph_font.setWeight(QFont.Bold)
                    painter.setFont(glyph_font)
                    painter.setPen(fg)
                    painter.drawText(icon_rect, Qt.AlignCenter, icon_char)
                    painter.setFont(font)

            painter.setPen(fg)
            text_rect = QRectF(
                icon_rect.right() + inner_gap,
                pill_rect.top(),
                pill_rect.right() - (icon_rect.right() + inner_gap) - inner_pad_h,
                pill_rect.height(),
            )
            painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, label)
        finally:
            painter.restore()

    def _paint_spinner(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        """회전하는 반원 호(arc). 트위터식 로딩 스피너 느낌."""
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            # 배경 링(연하게)
            bg_pen = QPen(QColor(color.red(), color.green(), color.blue(), 60), 2.0)
            bg_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(bg_pen)
            arc_rect = rect.adjusted(2, 2, -2, -2)
            painter.drawArc(arc_rect, 0, 360 * 16)

            # 회전 호(강하게)
            fg_pen = QPen(color, 2.0)
            fg_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(fg_pen)
            start_angle = int((90 - self._spin_angle) * 16)
            span_angle = -int(270 * 16 / 4)  # ≈ -67.5°... 짧은 호
            # 더 부드러운 느낌을 위해 120° 호로
            span_angle = -int(120 * 16)
            painter.drawArc(arc_rect, start_angle, span_angle)
        finally:
            painter.restore()

    def editorEvent(self, event, model, option, index) -> bool:
        # 인라인 버튼 hit-test (기입 / 영문기입 / 다음으로)
        if event.type() == QEvent.MouseButtonRelease:
            # 기입 버튼
            if (
                self._on_fill_clicked is not None
                and self._is_awaiting_fill is not None
                and self._is_awaiting_fill(index)
            ):
                rect = self._fill_btn_rects.get(index.row())
                if rect is not None and rect.contains(event.pos()):
                    try:
                        self._on_fill_clicked(index)
                    except Exception:
                        pass
                    return True
            # 영문기입 버튼
            if (
                self._on_eng_fill_clicked is not None
                and self._is_awaiting_eng_fill is not None
                and self._is_awaiting_eng_fill(index)
            ):
                rect = self._eng_fill_btn_rects.get(index.row())
                if rect is not None and rect.contains(event.pos()):
                    try:
                        self._on_eng_fill_clicked(index)
                    except Exception:
                        pass
                    return True
            # 다음으로 버튼
            if (
                self._on_next_clicked is not None
                and self._is_awaiting_next is not None
                and self._is_awaiting_next(index)
            ):
                rect = self._next_btn_rects.get(index.row())
                if rect is not None and rect.contains(event.pos()):
                    try:
                        self._on_next_clicked(index)
                    except Exception:
                        pass
                    return True
        return super().editorEvent(event, model, option, index)

    def sizeHint(self, option, index):
        base = super().sizeHint(option, index)
        # "다음으로" 버튼이 들어가는 행은 폭을 더 넓게 잡는다.
        return QSize(max(220, base.width()), max(30, base.height()))

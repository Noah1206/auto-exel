"""부드러운 press/hover 애니메이션이 있는 버튼 위젯."""
from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRect,
    Qt,
)
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGraphicsOpacityEffect, QPushButton, QToolButton


class AnimatedButton(QPushButton):
    """클릭 시 살짝 눌리는 느낌을 주는 QPushButton.

    - press: 1-2px 축소 + 살짝 투명도 감소
    - release: 원래 크기/투명도로 부드럽게 복귀
    - 지속시간 120ms, OutCubic easing
    """

    DURATION_MS = 120
    SHRINK_PX = 2
    PRESSED_OPACITY = 0.85

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(Qt.PointingHandCursor)

        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(1.0)
        self.setGraphicsEffect(self._opacity_effect)

        self._geom_anim = QPropertyAnimation(self, b"geometry", self)
        self._geom_anim.setDuration(self.DURATION_MS)
        self._geom_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._opacity_anim = QPropertyAnimation(self._opacity_effect, b"opacity", self)
        self._opacity_anim.setDuration(self.DURATION_MS)
        self._opacity_anim.setEasingCurve(QEasingCurve.OutCubic)

        self._base_geometry: QRect | None = None

    # -------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------

    def _capture_base_geometry(self) -> None:
        if self._base_geometry is None:
            self._base_geometry = QRect(self.geometry())

    def _shrunk_geometry(self) -> QRect:
        assert self._base_geometry is not None
        g = self._base_geometry
        return QRect(
            g.x() + self.SHRINK_PX,
            g.y() + self.SHRINK_PX,
            g.width() - self.SHRINK_PX * 2,
            g.height() - self.SHRINK_PX * 2,
        )

    # -------------------------------------------------------------
    # Events
    # -------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._capture_base_geometry()
            self._geom_anim.stop()
            self._geom_anim.setStartValue(self.geometry())
            self._geom_anim.setEndValue(self._shrunk_geometry())
            self._geom_anim.start()

            self._opacity_anim.stop()
            self._opacity_anim.setStartValue(self._opacity_effect.opacity())
            self._opacity_anim.setEndValue(self.PRESSED_OPACITY)
            self._opacity_anim.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and self._base_geometry is not None:
            self._geom_anim.stop()
            self._geom_anim.setStartValue(self.geometry())
            self._geom_anim.setEndValue(self._base_geometry)
            self._geom_anim.start()

            self._opacity_anim.stop()
            self._opacity_anim.setStartValue(self._opacity_effect.opacity())
            self._opacity_anim.setEndValue(1.0)
            self._opacity_anim.start()

    def resizeEvent(self, event) -> None:
        # 크기 변경 시 기준 지오메트리 무효화 (레이아웃 재조정 후 재포착)
        if self._geom_anim.state() != QPropertyAnimation.Running:
            self._base_geometry = None
        super().resizeEvent(event)

    def moveEvent(self, event) -> None:
        if self._geom_anim.state() != QPropertyAnimation.Running:
            self._base_geometry = None
        super().moveEvent(event)


# ---------------------------------------------------------------------------
# ToolBar / QToolButton 용 이벤트 필터
# ---------------------------------------------------------------------------


class PressAnimationFilter(QObject):
    """QToolButton 등에 설치되어 press 시 잠깐 투명도 감소 애니메이션을 부여.

    QToolBar 액션에는 QPushButton이 아닌 QToolButton이 사용되므로,
    subclass 대신 이벤트 필터로 처리.
    """

    DURATION_MS = 120
    PRESSED_OPACITY = 0.75

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._effects: dict[int, QGraphicsOpacityEffect] = {}
        self._anims: dict[int, QPropertyAnimation] = {}

    def _get_effect(self, widget) -> QGraphicsOpacityEffect:
        key = id(widget)
        if key not in self._effects:
            effect = QGraphicsOpacityEffect(widget)
            effect.setOpacity(1.0)
            widget.setGraphicsEffect(effect)
            self._effects[key] = effect

            anim = QPropertyAnimation(effect, b"opacity", widget)
            anim.setDuration(self.DURATION_MS)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            self._anims[key] = anim
        return self._effects[key]

    def eventFilter(self, watched, event) -> bool:
        etype = event.type()
        if etype in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.Leave):
            # 위젯이 좀비 상태일 수 있음 - try/except 보호
            try:
                effect = self._get_effect(watched)
                anim = self._anims[id(watched)]
                anim.stop()
                anim.setStartValue(effect.opacity())
                if etype == QEvent.MouseButtonPress:
                    anim.setEndValue(self.PRESSED_OPACITY)
                else:
                    anim.setEndValue(1.0)
                anim.start()
            except Exception:
                pass
        return False  # 이벤트는 원래 처리 로직에 전달


def install_press_animation(toolbar) -> PressAnimationFilter:
    """QToolBar의 모든 QToolButton에 press 애니메이션 설치."""
    filt = PressAnimationFilter(toolbar)
    for child in toolbar.findChildren(QToolButton):
        child.installEventFilter(filt)
    return filt

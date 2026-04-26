"""테이블이 비어있을 때 표시되는 드롭존.

3단계 안내 카드는 제거. 사용 방법은 첫 실행 시 OnboardingWizard 에서 한 번만 안내.
여기서는 사용자가 바로 엑셀을 올릴 수 있도록 간결한 드롭존만 표시한다.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.widgets.animated_button import AnimatedButton

_BG = "#F9FAFB"
_DROP_BG = "#FFFFFF"
_DROP_BORDER = "#D1D5DB"
_DROP_BORDER_HOVER = "#111827"
_TEXT = "#111827"
_MUTED = "#6B7280"


class EmptyStateBanner(QWidget):
    """엑셀 로드 전 화면 — 중앙에 단일 드롭존/버튼만 표시."""

    loadExcelRequested = Signal()
    installShopbackRequested = Signal()
    # 최근 파일 클릭 시 path(str) 전달
    recentFileSelected = Signal(str)
    # 최근 파일 항목 우측 X 클릭 시 — 목록에서 제거 요청
    recentFileRemoved = Signal(str)
    # 아래 두 시그널은 외부에서 더 이상 연결 안 해도 되지만,
    # 기존 코드와의 호환을 위해 정의는 남겨둔다.
    openBrowserRequested = Signal()
    showWizardRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("emptyState")
        self.setStyleSheet(f"QWidget#emptyState {{ background: {_BG}; }}")

        root = QVBoxLayout(self)
        root.setContentsMargins(60, 60, 60, 60)
        root.setSpacing(16)
        root.setAlignment(Qt.AlignCenter)

        drop = QFrame()
        drop.setObjectName("dropZone")
        drop.setStyleSheet(
            f"QFrame#dropZone {{"
            f"  background: {_DROP_BG};"
            f"  border: 1.5px dashed {_DROP_BORDER};"
            f"  border-radius: 12px;"
            f"}}"
            f"QFrame#dropZone:hover {{"
            f"  border-color: {_DROP_BORDER_HOVER};"
            f"}}"
        )
        drop.setCursor(Qt.PointingHandCursor)
        drop.setMinimumSize(520, 200)
        drop.mouseReleaseEvent = lambda _e: self.loadExcelRequested.emit()  # type: ignore[assignment]

        drop_lay = QVBoxLayout(drop)
        drop_lay.setContentsMargins(32, 32, 32, 32)
        drop_lay.setSpacing(10)
        drop_lay.setAlignment(Qt.AlignCenter)

        title = QLabel("엑셀 파일을 선택하세요")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"QLabel {{ font-size: 18px; font-weight: 700; color: {_TEXT};"
            f" background: transparent; border: none; }}"
        )
        drop_lay.addWidget(title)

        sub = QLabel("클릭해서 .xlsx 파일을 업로드")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(
            f"QLabel {{ color: {_MUTED}; font-size: 13px;"
            f" background: transparent; border: none; }}"
        )
        drop_lay.addWidget(sub)

        btn = AnimatedButton("엑셀 불러오기")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(38)
        btn.setMinimumWidth(180)
        btn.setStyleSheet(
            "QPushButton {"
            f"  background: {_TEXT}; color: #FFFFFF;"
            "  border: none; border-radius: 8px;"
            "  padding: 8px 20px; font-weight: 600; font-size: 14px;"
            "}"
            "QPushButton:hover { background: #1F2937; }"
        )
        btn.clicked.connect(self.loadExcelRequested.emit)

        btn_wrap = QWidget()
        btn_wrap.setStyleSheet("background: transparent;")

        bl = QHBoxLayout(btn_wrap)
        bl.setContentsMargins(0, 8, 0, 0)
        bl.addStretch()
        bl.addWidget(btn)
        bl.addStretch()
        drop_lay.addWidget(btn_wrap)

        root.addWidget(drop, alignment=Qt.AlignCenter)

        # 최근 파일 섹션 — 드롭존 아래
        self._recent_section = QFrame()
        self._recent_section.setObjectName("recentSection")
        self._recent_section.setStyleSheet(
            "QFrame#recentSection {"
            f"  background: {_DROP_BG};"
            f"  border: 1px solid #E5E7EB;"
            "  border-radius: 10px;"
            "}"
        )
        self._recent_section.setMinimumWidth(520)
        self._recent_section.setMaximumWidth(640)
        rs_lay = QVBoxLayout(self._recent_section)
        rs_lay.setContentsMargins(16, 12, 16, 12)
        rs_lay.setSpacing(6)

        rs_title = QLabel("최근 열었던 엑셀")
        rs_title.setStyleSheet(
            f"QLabel {{ font-size: 13px; font-weight: 700; color: {_TEXT};"
            f" background: transparent; border: none; }}"
        )
        rs_lay.addWidget(rs_title)

        # 항목들이 들어가는 컨테이너
        self._recent_list_wrap = QWidget()
        self._recent_list_wrap.setStyleSheet("background: transparent;")
        self._recent_list_lay = QVBoxLayout(self._recent_list_wrap)
        self._recent_list_lay.setContentsMargins(0, 4, 0, 0)
        self._recent_list_lay.setSpacing(2)
        rs_lay.addWidget(self._recent_list_wrap)

        # 비어있을 때 안내 라벨
        self._recent_empty_lbl = QLabel(
            "아직 열어본 파일이 없습니다 — 위에서 엑셀을 선택해 주세요"
        )
        self._recent_empty_lbl.setStyleSheet(
            f"QLabel {{ color: {_MUTED}; font-size: 12px;"
            f" background: transparent; border: none; padding: 6px 0; }}"
        )
        rs_lay.addWidget(self._recent_empty_lbl)

        root.addWidget(self._recent_section, alignment=Qt.AlignCenter)
        # 시작은 빈 상태로
        self.set_recent_files([])

        # 보조 액션: 샵백 확장프로그램 설치 — 드롭존 바깥, 눈에 띄게
        shopback_btn = AnimatedButton("샵백 확장프로그램 설치하기")
        shopback_btn.setCursor(Qt.PointingHandCursor)
        shopback_btn.setFixedHeight(40)
        shopback_btn.setMinimumWidth(280)
        shopback_btn.setStyleSheet(
            "QPushButton {"
            "  background: #FFF7ED; color: #9A3412;"
            "  border: 1.5px solid #FB923C; border-radius: 10px;"
            "  padding: 8px 22px; font-weight: 700; font-size: 14px;"
            "}"
            "QPushButton:hover { background: #FFEDD5; border-color: #EA580C; }"
        )
        shopback_btn.setToolTip(
            "Chrome Web Store 의 샵백 페이지를 앱 브라우저에서 열어줍니다. "
            "'Chrome에 추가' 를 누르면 다음 실행부터 자동으로 로드됩니다."
        )
        shopback_btn.clicked.connect(self.installShopbackRequested.emit)

        sub_hint = QLabel(
            "샵백 캐시백 적립이 필요하다면 먼저 확장프로그램을 설치하세요"
        )
        sub_hint.setAlignment(Qt.AlignCenter)
        sub_hint.setStyleSheet(
            f"QLabel {{ color: {_MUTED}; font-size: 12px;"
            f" background: transparent; border: none; }}"
        )

        sb_wrap = QWidget()
        sb_wrap.setStyleSheet("background: transparent;")
        sb_lay = QVBoxLayout(sb_wrap)
        sb_lay.setContentsMargins(0, 8, 0, 0)
        sb_lay.setSpacing(6)
        sb_lay.setAlignment(Qt.AlignCenter)
        sb_lay.addWidget(shopback_btn, alignment=Qt.AlignCenter)
        sb_lay.addWidget(sub_hint, alignment=Qt.AlignCenter)
        root.addWidget(sb_wrap, alignment=Qt.AlignCenter)

    # -------------------------------------------------------------
    # 최근 파일 목록 API
    # -------------------------------------------------------------

    def set_recent_files(self, paths: list[str]) -> None:
        """최근 파일 목록을 (가장 최근이 맨 위) 순서로 다시 그린다."""
        # 기존 항목 모두 제거
        while self._recent_list_lay.count():
            item = self._recent_list_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

        valid_rows: list[tuple[str, Path, datetime | None]] = []
        for p in paths:
            try:
                path_obj = Path(p)
                mtime = (
                    datetime.fromtimestamp(path_obj.stat().st_mtime)
                    if path_obj.exists() else None
                )
            except Exception:
                mtime = None
                path_obj = Path(p)
            valid_rows.append((p, path_obj, mtime))

        if not valid_rows:
            self._recent_empty_lbl.setVisible(True)
            return
        self._recent_empty_lbl.setVisible(False)

        for raw_path, path_obj, mtime in valid_rows:
            row = self._make_recent_row(raw_path, path_obj, mtime)
            self._recent_list_lay.addWidget(row)

    def _make_recent_row(
        self, raw_path: str, path_obj: Path, mtime: datetime | None
    ) -> QWidget:
        exists = path_obj.exists()

        row = QFrame()
        row.setObjectName("recentRow")
        row.setStyleSheet(
            "QFrame#recentRow {"
            "  background: transparent; border: none; border-radius: 6px;"
            "}"
            "QFrame#recentRow:hover { background: #F3F4F6; }"
        )
        row.setCursor(Qt.PointingHandCursor if exists else Qt.ForbiddenCursor)
        row.setToolTip(raw_path if exists else f"파일을 찾을 수 없습니다:\n{raw_path}")

        h = QHBoxLayout(row)
        h.setContentsMargins(10, 6, 6, 6)
        h.setSpacing(8)

        # 가운데: 파일명 + 경로
        center = QWidget()
        center.setStyleSheet("background: transparent;")
        center.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        name_color = _TEXT if exists else "#9CA3AF"
        name = QLabel(path_obj.name)
        name.setStyleSheet(
            f"QLabel {{ color: {name_color}; font-size: 13px; font-weight: 600;"
            f" background: transparent; border: none; }}"
        )
        cl.addWidget(name)

        # 부제: 디렉토리 (홈 ~ 축약) + 수정일
        try:
            home = Path.home()
            disp_dir = str(path_obj.parent)
            if disp_dir.startswith(str(home)):
                disp_dir = "~" + disp_dir[len(str(home)):]
        except Exception:
            disp_dir = str(path_obj.parent)
        if mtime is not None:
            sub_text = f"{disp_dir}  ·  {mtime.strftime('%Y-%m-%d %H:%M')}"
        else:
            sub_text = f"{disp_dir}  ·  파일 없음"
        sub = QLabel(sub_text)
        sub.setStyleSheet(
            f"QLabel {{ color: {_MUTED}; font-size: 11px;"
            f" background: transparent; border: none; }}"
        )
        cl.addWidget(sub)
        h.addWidget(center, 1)

        # 우측: 제거 버튼
        remove_btn = QToolButton()
        remove_btn.setText("✕")
        remove_btn.setCursor(Qt.PointingHandCursor)
        remove_btn.setToolTip("최근 목록에서 제거")
        remove_btn.setStyleSheet(
            "QToolButton {"
            f"  color: {_MUTED}; background: transparent; border: none;"
            "  font-size: 14px; padding: 2px 6px;"
            "}"
            "QToolButton:hover { color: #DC2626; }"
        )
        remove_btn.clicked.connect(lambda _=False, p=raw_path: self.recentFileRemoved.emit(p))
        h.addWidget(remove_btn)

        # 행 클릭 → 파일 열기 (존재할 때만)
        if exists:
            row.mouseReleaseEvent = (  # type: ignore[assignment]
                lambda _e, p=raw_path: self.recentFileSelected.emit(p)
            )
        return row

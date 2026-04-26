"""엑셀 느낌의 QTableView 서브클래스.

기능:
- 셀 단위 선택 (여러 셀 드래그 선택 가능)
- 더블클릭 / F2 / 문자키 입력 시 편집 시작
- Enter → 편집 확정 + 아래 셀로 이동
- Tab / Shift+Tab → 편집 확정 + 오른쪽/왼쪽 셀로 이동
- Escape → 편집 취소
- Delete / Backspace → 선택된 셀 비우기
- Ctrl+C / Ctrl+V → 선택 영역 복사 / 붙여넣기 (TSV, 여러 셀 지원)
- Ctrl+A → 전체 셀 선택
"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QTableView,
)


class ExcelTableView(QTableView):
    """엑셀 UX 에 가까운 테이블 뷰."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 셀 단위 선택, 다중 영역 허용
        self.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        # 편집 트리거: 엑셀과 동일하게 더블클릭 또는 F2 일 때만 편집 시작.
        # (단순 클릭/문자키로는 편집모드 진입 안 함 — 셀 선택만)
        self.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
        )
        # Tab 포커스가 셀 이동이 되도록
        self.setTabKeyNavigation(True)
        # Enter 로 편집 종료 시 아래로 이동 (Qt 기본 설정)

        # 스크롤바 숨김 — 휠/키보드 스크롤은 그대로 동작
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    # -------------------------------------------------------------
    # 키 이벤트
    # -------------------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # 편집 중에는 기본 동작 유지
        if self.state() == QAbstractItemView.EditingState:
            super().keyPressEvent(event)
            return

        key = event.key()
        mods = event.modifiers()

        # Ctrl+C — 복사
        if event.matches(QKeySequence.Copy):
            self._copy_selection_to_clipboard()
            event.accept()
            return

        # Ctrl+V — 붙여넣기
        if event.matches(QKeySequence.Paste):
            self._paste_from_clipboard()
            event.accept()
            return

        # Ctrl+A — 전체 선택
        if event.matches(QKeySequence.SelectAll):
            self.selectAll()
            event.accept()
            return

        # Delete / Backspace — 선택된 셀 내용 비우기
        if key in (Qt.Key_Delete, Qt.Key_Backspace):
            if self._clear_selected_cells():
                event.accept()
                return

        # Enter — 편집 진입 (편집 중이 아닐 때)
        if key in (Qt.Key_Return, Qt.Key_Enter) and mods == Qt.NoModifier:
            idx = self.currentIndex()
            if idx.isValid() and (self.model().flags(idx) & Qt.ItemIsEditable):
                self.edit(idx)
                event.accept()
                return

        super().keyPressEvent(event)

    # -------------------------------------------------------------
    # 복사 / 붙여넣기 / 비우기
    # -------------------------------------------------------------

    def _copy_selection_to_clipboard(self) -> None:
        """선택된 셀 영역을 TSV(탭 구분) 로 클립보드에 복사.

        선택이 연속 영역이 아니어도 bounding rectangle 로 묶어서 복사.
        """
        sel = self.selectionModel()
        if sel is None:
            return
        indexes = sel.selectedIndexes()
        if not indexes:
            return
        rows = sorted({i.row() for i in indexes})
        cols = sorted({i.column() for i in indexes})
        selected_pairs = {(i.row(), i.column()) for i in indexes}

        model = self.model()
        lines: list[str] = []
        for r in rows:
            cells: list[str] = []
            for c in cols:
                if (r, c) in selected_pairs:
                    val = model.data(model.index(r, c), Qt.EditRole)
                    cells.append("" if val is None else str(val))
                else:
                    cells.append("")
            lines.append("\t".join(cells))
        text = "\n".join(lines)
        QApplication.clipboard().setText(text)

    def _paste_from_clipboard(self) -> None:
        """클립보드 TSV 를 현재 셀을 좌상단으로 해서 붙여넣기.

        - 탭 → 다음 열, 개행 → 다음 행
        - 편집 불가 셀은 건너뜀
        - 모델 범위를 벗어나는 셀은 무시
        """
        text = QApplication.clipboard().text()
        if not text:
            return
        start = self.currentIndex()
        if not start.isValid():
            return
        model = self.model()
        if model is None:
            return

        # 마지막 개행은 무시 (엑셀이 줄 끝에 붙이는 경우가 있음)
        lines = text.rstrip("\n").split("\n")
        base_row = start.row()
        base_col = start.column()

        for dr, line in enumerate(lines):
            r = base_row + dr
            if r >= model.rowCount():
                break
            cells = line.split("\t")
            for dc, val in enumerate(cells):
                c = base_col + dc
                if c >= model.columnCount():
                    break
                idx = model.index(r, c)
                if not (model.flags(idx) & Qt.ItemIsEditable):
                    continue
                model.setData(idx, val, Qt.EditRole)

    def _clear_selected_cells(self) -> bool:
        """선택된 편집 가능 셀을 빈 문자열로 설정. 하나라도 바뀌면 True."""
        sel = self.selectionModel()
        if sel is None:
            return False
        indexes = sel.selectedIndexes()
        if not indexes:
            return False
        model = self.model()
        changed = False
        for idx in indexes:
            if not (model.flags(idx) & Qt.ItemIsEditable):
                continue
            model.setData(idx, "", Qt.EditRole)
            changed = True
        return changed

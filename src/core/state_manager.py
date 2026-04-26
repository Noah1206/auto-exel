"""크래시 복구용 상태 저장소."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.models.order import Order
from src.models.state import AppState, CompletedEntry, FailedEntry
from src.utils.logger import get_logger

log = get_logger()


class StateManager:
    """state.json에 진행 상황을 즉시 기록."""

    def __init__(self, path: str | Path = "data/state.json"):
        self.path = Path(path)
        self._state: AppState | None = None

    # -------------------------------------------------------------
    # Session lifecycle
    # -------------------------------------------------------------

    def start_session(self, excel_path: str | Path) -> AppState:
        self._state = AppState(
            session_id=datetime.now().strftime("%Y%m%d_%H%M%S"),
            excel_path=str(excel_path),
        )
        self._state.save(self.path)
        log.info(f"세션 시작: {self._state.session_id}")
        return self._state

    def load_previous(self) -> AppState | None:
        prev = AppState.load(self.path)
        if prev is None:
            return None
        self._state = prev
        return prev

    def clear(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
            self._state = None
        except Exception as exc:
            log.warning(f"state.json 삭제 실패: {exc}")

    # -------------------------------------------------------------
    # Updates
    # -------------------------------------------------------------

    def mark_completed(self, order: Order) -> None:
        self._ensure_state()
        assert self._state is not None
        # 중복 제거
        self._state.completed = [c for c in self._state.completed if c.row != order.row]
        self._state.completed.append(
            CompletedEntry(
                row=order.row,
                order_number=order.order_number,
                completed_at=order.ordered_at or datetime.now(),
            )
        )
        self._state.last_processed_row = max(self._state.last_processed_row, order.row)
        self._state.save(self.path)

    def mark_failed(self, order: Order) -> None:
        self._ensure_state()
        assert self._state is not None
        self._state.failed = [f for f in self._state.failed if f.row != order.row]
        self._state.failed.append(
            FailedEntry(
                row=order.row,
                error=order.error_message or "unknown",
                screenshot_path=order.screenshot_path,
                failed_at=datetime.now(),
            )
        )
        self._state.last_processed_row = max(self._state.last_processed_row, order.row)
        self._state.save(self.path)

    def completed_rows(self) -> set[int]:
        return self._state.completed_rows() if self._state else set()

    @property
    def state(self) -> AppState | None:
        return self._state

    def _ensure_state(self) -> None:
        if self._state is None:
            raise RuntimeError(
                "StateManager.start_session() 또는 load_previous() 먼저 호출해야 합니다"
            )

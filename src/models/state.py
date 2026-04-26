"""크래시 복구용 애플리케이션 상태."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from src.exceptions import StateError


class CompletedEntry(BaseModel):
    row: int
    order_number: str | None = None
    completed_at: datetime


class FailedEntry(BaseModel):
    row: int
    error: str
    screenshot_path: str | None = None
    failed_at: datetime


class AppState(BaseModel):
    session_id: str
    excel_path: str | None = None
    last_processed_row: int = 0
    completed: list[CompletedEntry] = Field(default_factory=list)
    failed: list[FailedEntry] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.now)

    def save(self, path: str | Path = "data/state.json") -> None:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        self.updated_at = datetime.now()
        try:
            tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
            tmp.replace(out)  # atomic
        except Exception as exc:
            raise StateError(f"state.json 저장 실패: {exc}") from exc

    @classmethod
    def load(cls, path: str | Path = "data/state.json") -> AppState | None:
        p = Path(path)
        if not p.exists():
            return None
        try:
            return cls.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StateError(f"state.json 로드 실패: {exc}") from exc

    def completed_rows(self) -> set[int]:
        return {e.row for e in self.completed}

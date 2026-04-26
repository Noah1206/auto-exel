"""StateManager 테스트."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from src.core.state_manager import StateManager
from src.models.order import Order


def make_order(row: int, status: str = "pending") -> Order:
    return Order.model_validate(
        {
            "row": row,
            "product_url": "https://www.11st.co.kr/products/1",
            "name": "김",
            "phone": "010-1234-5678",
            "customs_id": "P123456789012",
            "postal_code": "06236",
            "address": "서울",
            "quantity": 1,
            "english_name": "KIM",
            "status": status,
        }
    )


def test_session_lifecycle(tmp_path: Path):
    sm = StateManager(tmp_path / "state.json")
    sm.start_session("/tmp/x.xlsx")
    o = make_order(2)
    o.status = "completed"
    o.order_number = "12345"
    o.ordered_at = datetime.now()
    sm.mark_completed(o)
    assert 2 in sm.completed_rows()
    sm.clear()
    assert sm.state is None


def test_reload(tmp_path: Path):
    path = tmp_path / "state.json"
    sm1 = StateManager(path)
    sm1.start_session("/tmp/x.xlsx")
    o = make_order(5)
    o.status = "failed"
    o.error_message = "timeout"
    sm1.mark_failed(o)

    sm2 = StateManager(path)
    loaded = sm2.load_previous()
    assert loaded is not None
    assert loaded.failed[0].row == 5

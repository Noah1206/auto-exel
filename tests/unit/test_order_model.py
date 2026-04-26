"""Order 모델 검증 테스트."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.models.order import Order


def valid_row() -> dict:
    return {
        "row": 2,
        "product_url": "https://www.11st.co.kr/products/12345",
        "name": "김철수",
        "phone": "010-1234-5678",
        "customs_id": "P123456789012",
        "postal_code": "06236",
        "address": "서울시 강남구 테헤란로 123",
        "quantity": 1,
        "english_name": "KIM CHUL SOO",
    }


def test_valid_order():
    o = Order.model_validate(valid_row())
    assert o.phone == "010-1234-5678"
    assert o.status == "pending"
    assert o.quantity == 1
    assert o.postal_code == "06236"


def test_phone_normalization():
    data = valid_row()
    data["phone"] = "01012345678"
    o = Order.model_validate(data)
    assert o.phone == "010-1234-5678"


def test_invalid_customs():
    data = valid_row()
    data["customs_id"] = "12345"
    with pytest.raises(ValidationError):
        Order.model_validate(data)


def test_invalid_postal():
    data = valid_row()
    data["postal_code"] = "123"
    with pytest.raises(ValidationError):
        Order.model_validate(data)


def test_quantity_string_coerced():
    data = valid_row()
    data["quantity"] = "3"
    o = Order.model_validate(data)
    assert o.quantity == 3


def test_quantity_zero_rejected():
    data = valid_row()
    data["quantity"] = 0
    with pytest.raises(ValidationError):
        Order.model_validate(data)


def test_total_price_computation():
    data = valid_row()
    data["quantity"] = 3
    o = Order.model_validate(data)
    assert o.compute_total() is None  # unit_price 없으면 None
    o.unit_price = 10000
    assert o.compute_total() == 30000
    assert o.total_price == 30000


def test_needs_price():
    o = Order.model_validate(valid_row())
    assert o.needs_price()
    o.total_price = 15000
    assert not o.needs_price()


def test_is_retryable_includes_paused():
    o = Order.model_validate(valid_row())
    assert o.is_retryable()
    o.status = "paused"
    assert o.is_retryable()
    o.status = "completed"
    assert not o.is_retryable()


def test_unavailable_status():
    """판매 불가 상태는 자동 재시도 대상에서 제외된다."""
    o = Order.model_validate(valid_row())
    o.status = "unavailable"
    assert not o.is_retryable()
    assert not o.is_done()

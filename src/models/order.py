"""주문(Order) 데이터 모델."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.utils.validators import (
    clean_recipient_name,
    clean_address,
    normalize_phone,
    validate_11st_url,
    validate_customs_id,
    validate_english_name,
    validate_postal_code,
    validate_quantity,
)

OrderStatus = Literal[
    "pending",
    "in_progress",
    "paused",
    "completed",
    "failed",
    "unavailable",  # 판매중지/품절/삭제 — 자동 주문 불가, 사용자 확인 필요
]


class Order(BaseModel):
    """엑셀 1행을 나타내는 주문 모델.

    입력 8컬럼: 구매처(URL) / 수취인 / 수취인번호 / 통관번호 /
                우편번호 / 수취인 주소 / 수량 / 영문이름
    자동 채움:   단가(unit_price), 토탈가격(total_price), 주문번호(order_number)
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    row: int = Field(ge=1, description="엑셀 행 번호 (1-base)")

    # 입력 필드 (8개)
    product_url: str = Field(description="구매처 URL")
    name: str = Field(min_length=1, max_length=30, description="수취인")
    phone: str = Field(description="수취인번호")
    customs_id: str = Field(description="통관번호")
    postal_code: str = Field(description="우편번호")
    address: str = Field(min_length=1, description="수취인 주소")
    quantity: int = Field(ge=1, description="수량")
    english_name: str = Field(description="영문이름")

    # 자동 채워지는 필드
    unit_price: int | None = Field(default=None, description="단가 (판매가 1개)")
    total_price: int | None = Field(
        default=None,
        description="토탈가격 컬럼 — 정책상 '개당 단가' 를 저장 (수량 곱 X)",
    )
    order_number: str | None = None
    ordered_at: datetime | None = None
    status: OrderStatus = "pending"
    error_message: str | None = None
    screenshot_path: str | None = None

    @field_validator("product_url", mode="before")
    @classmethod
    def _v_url(cls, v) -> str:
        return validate_11st_url(v)

    @field_validator("phone", mode="before")
    @classmethod
    def _v_phone(cls, v) -> str:
        return normalize_phone(v)

    @field_validator("customs_id", mode="before")
    @classmethod
    def _v_customs(cls, v) -> str:
        return validate_customs_id(v)

    @field_validator("english_name", mode="before")
    @classmethod
    def _v_eng(cls, v) -> str:
        return validate_english_name(v)

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, v) -> str:
        return clean_recipient_name(v)

    @field_validator("address", mode="before")
    @classmethod
    def _v_address(cls, v) -> str:
        return clean_address(v)

    @field_validator("postal_code", mode="before")
    @classmethod
    def _v_postal(cls, v) -> str:
        return validate_postal_code(v)

    @field_validator("quantity", mode="before")
    @classmethod
    def _v_qty(cls, v) -> int:
        return validate_quantity(v)

    def compute_total(self) -> int | None:
        """'토탈가격' 컬럼에는 단가를 그대로 저장한다 (수량 곱셈 안 함)."""
        if self.unit_price is None:
            return None
        self.total_price = self.unit_price
        return self.total_price

    def is_done(self) -> bool:
        return self.status == "completed"

    def is_retryable(self) -> bool:
        return self.status in ("pending", "failed", "paused")

    def needs_price(self) -> bool:
        return self.total_price is None

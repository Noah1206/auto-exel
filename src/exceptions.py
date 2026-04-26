"""커스텀 예외 계층."""
from __future__ import annotations


class AppError(Exception):
    """모든 애플리케이션 예외의 기저 클래스."""


class ConfigError(AppError):
    """설정 파일 관련 오류."""


class ExcelError(AppError):
    """엑셀 로드/저장 오류."""


class InvalidExcelSchemaError(ExcelError):
    """엑셀 컬럼 누락/형식 오류."""


class BrowserError(AppError):
    """브라우저 초기화/관리 오류."""


class ElementNotFoundError(BrowserError):
    """모든 fallback 셀렉터에서 요소를 찾지 못함."""


class LoginExpiredError(BrowserError):
    """11번가 로그인 세션 만료."""


class CaptchaDetectedError(BrowserError):
    """캡차 페이지 감지."""


class OutOfStockError(BrowserError):
    """상품 품절."""


class ProductUnavailableError(BrowserError):
    """상품이 판매중지/판매종료/삭제되어 주문 불가능한 상태.

    품절(OutOfStockError)과 구분: 품절은 일시적이지만 unavailable은
    상품 자체가 더 이상 존재하지 않거나 영구히 판매하지 않는 상태.
    """

    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason or message


class PaymentTimeoutError(BrowserError):
    """사용자가 제한 시간 내 결제를 완료하지 않음."""


class StateError(AppError):
    """state.json 관련 오류."""


class UserInterventionRequired(BrowserError):
    """자동화가 진행할 수 없어 사용자 개입 대기로 전환해야 하는 상황.

    예: 주소 검색 팝업이 열려 있고 검색 결과가 불확실한 경우.
    프로그램 종료 대신 주문을 paused 상태로 전환하고,
    사용자가 브라우저에서 직접 수정 후 "이어서 진행"을 호출하게 한다.
    """

    def __init__(self, message: str, checkpoint: str = "", detail: str = ""):
        super().__init__(message)
        self.checkpoint = checkpoint
        self.detail = detail

"""validators.py 단위 테스트."""
from __future__ import annotations

import pytest

from src.utils.validators import (
    clean_address,
    clean_price,
    clean_recipient_name,
    normalize_phone,
    validate_11st_url,
    validate_customs_id,
    validate_english_name,
    validate_postal_code,
    validate_quantity,
)


class TestNormalizePhone:
    def test_standard_010(self):
        assert normalize_phone("010-1234-5678") == "010-1234-5678"

    def test_no_dash(self):
        assert normalize_phone("01012345678") == "010-1234-5678"

    def test_with_spaces(self):
        assert normalize_phone("010 1234 5678") == "010-1234-5678"

    def test_old_10digit_011(self):
        assert normalize_phone("0111234567") == "011-123-4567"

    def test_invalid_prefix(self):
        with pytest.raises(ValueError):
            normalize_phone("02-1234-5678")

    def test_too_short(self):
        with pytest.raises(ValueError):
            normalize_phone("010-1234")

    def test_excel_number_0_dropped_11digit(self):
        """엑셀이 숫자로 저장해 010...이 10...1234567890으로 들어온 경우."""
        assert normalize_phone(1012345678) == "010-1234-5678"
        assert normalize_phone("1012345678") == "010-1234-5678"

    def test_country_code_plus_82(self):
        assert normalize_phone("+82-10-1234-5678") == "010-1234-5678"

    def test_country_code_82_no_plus(self):
        assert normalize_phone("82 10 1234 5678") == "010-1234-5678"

    def test_parentheses(self):
        assert normalize_phone("(010) 1234-5678") == "010-1234-5678"

    def test_nbsp_and_zero_width(self):
        # NBSP + ZERO WIDTH SPACE 섞여도 처리
        assert normalize_phone("010\u00A01234\u200B5678") == "010-1234-5678"

    def test_050_safe_number_12digit(self):
        """050 안심번호 (쇼핑몰 배송 정보에서 실제 쓰임)."""
        assert normalize_phone("0504-1234-5678") == "0504-1234-5678"
        assert normalize_phone("050412345678") == "0504-1234-5678"

    def test_050_safe_number_excel_dropped_zero(self):
        assert normalize_phone("504 1234 5678") == "0504-1234-5678"

    def test_landline_rejected(self):
        with pytest.raises(ValueError):
            normalize_phone("02-123-4567")

    def test_empty(self):
        with pytest.raises(ValueError):
            normalize_phone("")

    def test_none(self):
        with pytest.raises(ValueError):
            normalize_phone(None)

    def test_010_wrong_length(self):
        with pytest.raises(ValueError) as exc_info:
            normalize_phone("010-1234-567")  # 10자리
        assert "11자리" in str(exc_info.value)


class TestCustomsId:
    def test_valid(self):
        assert validate_customs_id("P123456789012") == "P123456789012"

    def test_lowercase_normalized(self):
        assert validate_customs_id("p123456789012") == "P123456789012"

    def test_too_short_has_helpful_message(self):
        with pytest.raises(ValueError) as exc_info:
            validate_customs_id("P12345")
        assert "12자리" in str(exc_info.value)

    def test_wrong_prefix(self):
        with pytest.raises(ValueError):
            validate_customs_id("X123456789012")

    def test_inner_whitespace_removed(self):
        assert validate_customs_id("P 1234 5678 9012") == "P123456789012"

    def test_hyphen_removed(self):
        assert validate_customs_id("P-123-456-789-012") == "P123456789012"

    def test_quoted(self):
        assert validate_customs_id("'P123456789012'") == "P123456789012"

    def test_12digits_without_p_gets_p_prefixed(self):
        """엑셀이 앞 P를 빼먹고 숫자만 저장한 경우."""
        assert validate_customs_id("123456789012") == "P123456789012"

    def test_float_from_excel(self):
        assert validate_customs_id(123456789012.0) == "P123456789012"

    def test_empty(self):
        with pytest.raises(ValueError):
            validate_customs_id("")

    def test_nbsp(self):
        assert validate_customs_id("P123456789012\u00A0") == "P123456789012"


class TestEnglishName:
    def test_valid(self):
        assert validate_english_name("KIM CHUL SOO") == "KIM CHUL SOO"

    def test_lowercase_normalized(self):
        assert validate_english_name("kim chul soo") == "KIM CHUL SOO"

    def test_with_korean_helpful_message(self):
        with pytest.raises(ValueError) as exc_info:
            validate_english_name("KIM 철수")
        msg = str(exc_info.value)
        # 에러 메시지에 어떤 글자가 문제인지 포함
        assert "철" in msg or "한글" in msg

    def test_with_japanese(self):
        with pytest.raises(ValueError):
            validate_english_name("KIM さん")

    def test_with_chinese(self):
        with pytest.raises(ValueError):
            validate_english_name("金哲洙")

    def test_with_digits(self):
        with pytest.raises(ValueError):
            validate_english_name("KIM1")

    def test_hyphenated_name(self):
        """JEAN-PAUL 같은 하이픈 이름 허용."""
        assert validate_english_name("Jean-Paul Sartre") == "JEAN-PAUL SARTRE"

    def test_apostrophe_name(self):
        """O'BRIEN 같은 아포스트로피 이름 허용."""
        assert validate_english_name("O'Brien") == "O'BRIEN"

    def test_multiple_spaces_collapsed(self):
        assert validate_english_name("KIM   CHUL    SOO") == "KIM CHUL SOO"

    def test_nbsp_and_full_width_space(self):
        assert validate_english_name("KIM\u00A0CHUL\u3000SOO") == "KIM CHUL SOO"

    def test_single_letter(self):
        assert validate_english_name("A") == "A"

    def test_empty(self):
        with pytest.raises(ValueError):
            validate_english_name("")

    def test_whitespace_only(self):
        with pytest.raises(ValueError):
            validate_english_name("  \u00A0\u3000  ")


class TestUrl:
    def test_valid_https(self):
        url = "https://www.11st.co.kr/products/12345"
        assert validate_11st_url(url) == url

    def test_adds_https(self):
        assert validate_11st_url("11st.co.kr/x").startswith("https://")

    def test_mobile_subdomain(self):
        assert validate_11st_url("https://m.11st.co.kr/products/12345").startswith(
            "https://m.11st.co.kr"
        )

    def test_share_subdomain(self):
        result = validate_11st_url("https://share.11st.co.kr/p/12345")
        assert "share.11st.co.kr" in result

    def test_legacy_011st_mobile(self):
        """c.m.011st.com 같은 옛 모바일 도메인."""
        result = validate_11st_url("https://c.m.011st.com/MW/product/1234")
        assert "011st.com" in result

    def test_shortener_11e_kr(self):
        """11번가 공식 단축 11e.kr 도메인."""
        result = validate_11st_url("https://11e.kr/abc123")
        assert "11e.kr" in result

    def test_uppercase_domain_lowered(self):
        result = validate_11st_url("HTTPS://WWW.11ST.CO.KR/products/ABC")
        # 도메인만 소문자가 되어야 함, 경로의 대소문자는 유지
        assert result.startswith("https://www.11st.co.kr/")
        assert "ABC" in result

    def test_quoted(self):
        assert validate_11st_url("'https://11st.co.kr/x'") == "https://11st.co.kr/x"

    def test_nbsp_leading(self):
        assert (
            validate_11st_url("\u00A0https://11st.co.kr/x\u200B")
            == "https://11st.co.kr/x"
        )

    def test_non_11st(self):
        with pytest.raises(ValueError):
            validate_11st_url("https://coupang.com")

    def test_empty(self):
        with pytest.raises(ValueError):
            validate_11st_url("")


class TestPostalCode:
    def test_valid_5digit(self):
        assert validate_postal_code("06236") == "06236"

    def test_valid_5digit_no_leading_zero(self):
        assert validate_postal_code("48094") == "48094"

    def test_hyphen_in_5digit_stripped(self):
        # 사용자가 습관적으로 062-36 이렇게 쓸 수 있다
        assert validate_postal_code("062-36") == "06236"

    def test_excel_number_dropped_leading_zero(self):
        """엑셀이 숫자형으로 저장하면 06236 → 6236 으로 0이 잘림.
        이 경우 자동으로 0을 복원해 준다."""
        assert validate_postal_code(6236) == "06236"
        assert validate_postal_code("6236") == "06236"

    def test_float_from_excel(self):
        """엑셀 셀이 숫자형이면 openpyxl이 float로 넘길 수 있다."""
        assert validate_postal_code(48094.0) == "48094"

    def test_old_6digit_rejected_with_helpful_message(self):
        """2015.8.1 폐지된 6자리 형식은 명시적으로 거부하고 안내 메시지 제공."""
        with pytest.raises(ValueError) as exc_info:
            validate_postal_code("123-456")
        msg = str(exc_info.value)
        assert "6자리" in msg or "폐지" in msg
        assert "5자리" in msg

    def test_old_6digit_no_hyphen_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            validate_postal_code("123456")
        assert "6자리" in str(exc_info.value) or "폐지" in str(exc_info.value)

    def test_too_short(self):
        with pytest.raises(ValueError):
            validate_postal_code("123")

    def test_too_long(self):
        with pytest.raises(ValueError):
            validate_postal_code("1234567")

    def test_empty(self):
        with pytest.raises(ValueError):
            validate_postal_code("")

    def test_none(self):
        with pytest.raises(ValueError):
            validate_postal_code(None)  # type: ignore[arg-type]

    def test_non_numeric(self):
        with pytest.raises(ValueError):
            validate_postal_code("ABCDE")


class TestQuantity:
    def test_int(self):
        assert validate_quantity(3) == 3

    def test_string(self):
        assert validate_quantity("5") == 5

    def test_float_integer(self):
        assert validate_quantity(2.0) == 2

    def test_float_fractional_rejected(self):
        with pytest.raises(ValueError) as exc_info:
            validate_quantity(1.5)
        assert "소수" in str(exc_info.value) or "정수" in str(exc_info.value)

    def test_with_unit_ea(self):
        assert validate_quantity("3ea") == 3
        assert validate_quantity("3EA") == 3

    def test_with_unit_korean(self):
        assert validate_quantity("3개") == 3

    def test_thousand_separator(self):
        # 콤마 처리 자체 검증 — 현실적 범위 안에서 (999 상한 이하)
        assert validate_quantity("1,00") == 100
        assert validate_quantity("2,0") == 20

    def test_surrounding_spaces(self):
        assert validate_quantity("  7  ") == 7

    def test_nbsp(self):
        assert validate_quantity("3\u00A0") == 3

    def test_zero(self):
        with pytest.raises(ValueError):
            validate_quantity(0)

    def test_negative(self):
        with pytest.raises(ValueError):
            validate_quantity(-1)

    def test_non_numeric(self):
        with pytest.raises(ValueError):
            validate_quantity("abc")

    def test_over_max_rejected(self):
        """오타 방지용 상한선 (999). 1000 이상은 거부."""
        with pytest.raises(ValueError) as exc_info:
            validate_quantity(1000)
        assert "999" in str(exc_info.value) or "초과" in str(exc_info.value)

    def test_bool_rejected(self):
        """True/False가 int 서브클래스라 통과되면 안 됨."""
        with pytest.raises(ValueError):
            validate_quantity(True)  # type: ignore[arg-type]


class TestCleanRecipientName:
    def test_valid(self):
        assert clean_recipient_name("김철수") == "김철수"

    def test_strip(self):
        assert clean_recipient_name("  김철수  ") == "김철수"

    def test_nbsp(self):
        assert clean_recipient_name("김\u00A0철수") == "김 철수"

    def test_multiple_spaces_collapsed(self):
        assert clean_recipient_name("김   철수") == "김 철수"

    def test_empty(self):
        with pytest.raises(ValueError):
            clean_recipient_name("")

    def test_whitespace_only(self):
        with pytest.raises(ValueError):
            clean_recipient_name("\u00A0\u3000 ")

    def test_too_long(self):
        with pytest.raises(ValueError):
            clean_recipient_name("가" * 31)

    def test_foreign_name_allowed(self):
        assert clean_recipient_name("Nguyen Van A") == "Nguyen Van A"

    def test_hanja_allowed(self):
        assert clean_recipient_name("金哲洙") == "金哲洙"


class TestCleanAddress:
    def test_valid(self):
        assert clean_address("서울시 강남구 테헤란로 123") == "서울시 강남구 테헤란로 123"

    def test_newline_collapsed(self):
        assert clean_address("서울시 강남구\n테헤란로 123") == "서울시 강남구 테헤란로 123"

    def test_crlf_collapsed(self):
        assert clean_address("서울\r\n강남") == "서울 강남"

    def test_multiple_spaces(self):
        assert clean_address("서울   강남") == "서울 강남"

    def test_nbsp_and_full_width(self):
        assert clean_address("서울\u00A0강남\u3000구") == "서울 강남 구"

    def test_empty(self):
        with pytest.raises(ValueError):
            clean_address("")

    def test_whitespace_only(self):
        with pytest.raises(ValueError):
            clean_address("   \n  ")

    def test_too_long(self):
        with pytest.raises(ValueError):
            clean_address("가" * 201)


class TestCleanPrice:
    def test_comma_and_won(self):
        assert clean_price("15,000원") == 15000

    def test_int_passthrough(self):
        assert clean_price(12345) == 12345

    def test_none(self):
        assert clean_price(None) is None

    def test_empty(self):
        assert clean_price("") is None

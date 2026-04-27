"""입력 검증 유틸리티.

현장에서 들어오는 "지저분한" 엑셀 값들을 최대한 구제하면서,
정말로 잘못된 값은 명확한 안내 메시지와 함께 거부한다.

공통 원칙:
- 엑셀이 숫자형으로 저장해 앞자리 0이 잘린 경우 자동 복원
- 엑셀이 float로 넘기면 소수점이 0이면 int로 변환
- 공백/줄바꿈/숨은 유니코드 공백(\u00A0, \u200B 등) 정규화
- 오타/형식 오류는 버리기보다 "왜 실패했는지"를 메시지로 알려주기
"""
from __future__ import annotations

import re

_PHONE_DIGIT_RE = re.compile(r"\D")
_CUSTOMS_RE = re.compile(r"^P\d{12}$")
# 한글/한자/히라가나/가타카나 등 동아시아 문자 감지 (영문이름 혼입 검출용)
_CJK_RE = re.compile(
    r"[\u3040-\u309F\u30A0-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]"
)
# 영문이름 허용 문자: 대문자 + 공백 + 하이픈 + 아포스트로피 (JEAN-PAUL, O'BRIEN)
_ENG_NAME_ALLOWED_RE = re.compile(r"^[A-Z][A-Z\s'\-]*[A-Z]$|^[A-Z]$")
_POSTAL_5_RE = re.compile(r"^\d{5}$")
_POSTAL_OLD_6_RE = re.compile(r"^\d{6}$")

# 유니코드 공백류 (널리보이지 않는 공백 포함)를 일반 공백으로 정규화
_INVISIBLE_SPACES = {
    "\u00A0",  # NBSP
    "\u200B",  # ZERO WIDTH SPACE
    "\u200C",  # ZERO WIDTH NON-JOINER
    "\u200D",  # ZERO WIDTH JOINER
    "\u2060",  # WORD JOINER
    "\uFEFF",  # BOM
    "\u3000",  # IDEOGRAPHIC SPACE (전각 공백)
}


def _clean_text(raw) -> str:
    """엑셀에서 흔히 섞여 들어오는 보이지 않는 공백/전각 공백을 제거하고 strip."""
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    s = str(raw)
    for ch in _INVISIBLE_SPACES:
        s = s.replace(ch, " ")
    return s.strip()


def normalize_phone(raw) -> str:
    """국내 휴대폰 / 050 안심번호 정규화.

    허용하는 prefix (2026년 기준):
      - **010**: 현행 표준 (2004년 통합)
      - **011, 016, 017, 018, 019**: 기존 번호 유지자 (2021-06-30 이후 신규 발급은 중단되었으나
        기존 번호는 계속 유효). 현장에서는 점점 드물지만 아직 존재.
      - **050X** (0502~0508): 안심번호. 배송 수취인 연락처로 실제 사용됨.
        쇼핑몰 개인정보 보호 목적. 길이 12자리(050XNNNNNNNN)인 경우도 있다.

    처리:
      1) 엑셀 숫자형 저장으로 앞자리 0이 잘린 경우 → `10` / `11` 로 시작하면 0 복원
      2) 국가번호 `+82` / `82` → `0` 으로 치환
      3) 하이픈/공백/괄호 제거
      4) 길이가 10~12자리인지 확인 후 3-4-4 / 3-3-4 포맷으로 재조합

    Sources:
      - https://ko.wikipedia.org/wiki/%EB%8C%80%ED%95%9C%EB%AF%BC%EA%B5%AD%EC%9D%98_%EC%A0%84%ED%99%94%EB%B2%88%ED%98%B8_%EC%B2%B4%EA%B3%84
      - https://product.kt.com/wDic/productDetail.do?ItemCode=1454 (050 안심번호)
    """
    text = _clean_text(raw)
    if not text:
        raise ValueError("휴대폰 번호가 비어있습니다")

    # 국가번호 정규화: +82-10-... / 82-10-... → 010-...
    text = re.sub(r"^\s*\+?82[-\s]?", "0", text)

    digits = _PHONE_DIGIT_RE.sub("", text)
    if not digits:
        raise ValueError(f"휴대폰 번호에 숫자가 없습니다: {raw!r}")

    # 엑셀 숫자형 저장으로 선행 0 누락된 케이스 보정
    # - 010/011/016~019 (11자리 or 10자리) → 앞에 0
    # - 050X 안심번호 (12자리 or 11자리) → 앞에 0
    if digits.startswith(("10", "11", "16", "17", "18", "19")) and len(digits) == 10:
        digits = "0" + digits
    elif digits.startswith("50") and len(digits) in (10, 11):
        digits = "0" + digits

    # 010~019 (이동전화)
    if digits.startswith("010"):
        if len(digits) == 11:
            return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
        raise ValueError(
            f"010 번호는 11자리여야 합니다 (현재 {len(digits)}자리): {raw!r}"
        )

    if digits.startswith(("011", "016", "017", "018", "019")):
        if len(digits) == 11:
            return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        raise ValueError(
            f"01X 번호는 10~11자리여야 합니다 (현재 {len(digits)}자리): {raw!r}"
        )

    # 050X 안심번호 (050N + 8자리 = 12자리가 대표적, 일부 11자리)
    if digits.startswith("050"):
        if len(digits) == 12:
            return f"{digits[:4]}-{digits[4:8]}-{digits[8:]}"
        if len(digits) == 11:
            return f"{digits[:4]}-{digits[4:7]}-{digits[7:]}"
        raise ValueError(
            f"050 안심번호는 11~12자리여야 합니다 (현재 {len(digits)}자리): {raw!r}"
        )

    raise ValueError(
        f"국내 휴대폰(010/011/016~019) 또는 050 안심번호만 지원됩니다: {raw!r}"
    )


def validate_customs_id(raw) -> str:
    """개인통관고유부호: P + 12자리 숫자.

    현장 이슈:
      - 소문자 p 로 시작 → 대문자로 정규화
      - 중간 공백/하이픈 섞임 (P 1234 5678 9012) → 제거
      - 앞뒤 따옴표/괄호 → 제거
      - 엑셀이 P를 빼먹고 숫자만 저장한 경우 → 12자리 숫자면 P 자동 부여
    """
    text = _clean_text(raw)
    if not text:
        raise ValueError("통관번호가 비어있습니다")
    # 내부 공백/하이픈/점/따옴표 제거
    text = re.sub(r"[\s\-.'\"\(\)]", "", text).upper()

    # P가 누락된 순수 12자리 숫자면 P 자동 부여
    if re.fullmatch(r"\d{12}", text):
        text = "P" + text

    if not _CUSTOMS_RE.match(text):
        # 왜 실패했는지 힌트 제공
        digits_only = re.sub(r"\D", "", text)
        if text.startswith("P") and len(digits_only) != 12:
            raise ValueError(
                f"통관번호는 P 다음 숫자 12자리여야 합니다 (현재 숫자 {len(digits_only)}자리): {raw!r}"
            )
        raise ValueError(
            f"통관번호 형식 오류. 'P + 12자리 숫자' 여야 합니다: {raw!r}"
        )
    return text


def validate_english_name(raw) -> str:
    """영문 이름 정규화.

    허용:
      - 대문자 A-Z
      - 공백 (GIL DONG HONG)
      - 하이픈 (JEAN-PAUL, MARY-JANE)
      - 아포스트로피 (O'BRIEN, D'ANGELO)

    처리:
      - 소문자 → 대문자 변환
      - 연속 공백 단일화
      - 유니코드 공백 제거
      - 한글 섞이면 **어떤 글자가 문제인지** 안내
      - 숫자 섞이면 거부
    """
    text = _clean_text(raw).upper()
    if not text:
        raise ValueError("영문 이름이 비어있습니다")

    # 한글 / 한자 섞인 경우 구체적 안내
    cjk_chars = _CJK_RE.findall(text)
    if cjk_chars:
        sample = "".join(dict.fromkeys(cjk_chars))[:10]
        raise ValueError(
            f"영문 이름에 한글/한자가 섞여 있습니다 ({sample!r}). 영문으로만 입력해 주세요: {raw!r}"
        )

    # 숫자 섞임
    if re.search(r"\d", text):
        raise ValueError(
            f"영문 이름에 숫자가 포함되어 있습니다: {raw!r}"
        )

    # 연속 공백 정리
    text = re.sub(r"\s+", " ", text).strip()

    if not _ENG_NAME_ALLOWED_RE.match(text):
        raise ValueError(
            f"영문 이름은 대문자 알파벳과 공백/하이픈/아포스트로피만 허용됩니다: {raw!r}"
        )
    return text


def validate_11st_url(raw) -> str:
    """11번가 상품 URL 검증/정규화.

    허용 도메인 (대소문자 무시):
      - www.11st.co.kr / 11st.co.kr  (PC 표준)
      - m.11st.co.kr                 (모바일)
      - c.m.011st.com                (구 모바일 서브도메인)
      - share.11st.co.kr             (공유용)
      - deal.11st.co.kr              (딜 전용)
      - buy.11st.co.kr               (구매/장바구니)
      - 11st.kr / 11e.kr             (단축 도메인)

    처리:
      - 앞뒤 따옴표/공백/보이지 않는 공백 제거
      - http(s):// 없으면 https:// 자동 부여
      - 대문자 도메인은 소문자로 (경로는 그대로 — 상품 ID 보존)

    Sources:
      - https://www.11st.co.kr
      - https://m.11st.co.kr
      - https://www.11e.kr  (11번가 공식 단축기)
    """
    text = _clean_text(raw)
    if not text:
        raise ValueError("구매처 URL이 비어있습니다")
    # 따옴표 제거
    text = text.strip("'\"")

    # 스킴 보정
    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        text = "https://" + text

    # 도메인 부분만 소문자로 (경로의 대소문자는 중요할 수 있음)
    m = re.match(r"^(https?://)([^/]+)(/.*)?$", text, flags=re.IGNORECASE)
    if m:
        scheme, host, path = m.group(1).lower(), m.group(2).lower(), m.group(3) or ""
        text = f"{scheme}{host}{path}"
        host_lower = host
    else:
        host_lower = text.lower()

    allowed_hosts = (
        "11st.co.kr",    # www/m/share/deal/buy 등 모든 서브도메인 커버
        "011st.com",     # 구 모바일
        "11st.kr",       # 단축
        "11e.kr",        # 11번가 공식 단축
    )
    if not any(h in host_lower for h in allowed_hosts):
        raise ValueError(
            f"11번가 상품 URL이 아닙니다 (허용 도메인: {', '.join(allowed_hosts)}): {raw!r}"
        )
    return text


def validate_postal_code(raw: str | int) -> str:
    """대한민국 우편번호 정규화.

    2015-08-01부터 전국 우편번호는 **5자리 숫자**로 통일됨 (국가기초구역번호).
    현장에서 실제로 만나는 케이스를 관대하게 처리:

    1) 5자리 숫자 → 그대로 반환 (정상)
    2) 엑셀이 숫자로 저장해서 앞자리 0이 잘린 4자리 → 5자리로 zero-pad
       (예: 서울 서초 06236 → 엑셀 셀이 숫자형이면 6236 으로 저장됨)
    3) 하이픈 포함 (06236, 062-36 등) → 하이픈 제거 후 재검증
    4) 구 6자리 우편번호 (XXX-XXX, 1988~2015) → 2015년 이후 폐지되었으므로
       사용자에게 **명시적 오류**를 내어 5자리로 재입력을 유도
       (구 6자리 → 신 5자리는 1:1 매핑이 아니므로 자동 변환 불가)
    5) 3자리 이하 / 7자리 이상 → 오류

    Sources:
      - https://www.epost.go.kr/search/zipcode/cmzcd003k01.jsp
      - https://ko.wikipedia.org/wiki/%EB%8C%80%ED%95%9C%EB%AF%BC%EA%B5%AD%EC%9D%98_%EC%9A%B0%ED%8E%B8%EB%B2%88%ED%98%B8
    """
    if raw is None:
        raise ValueError("우편번호가 비어있습니다")

    # float('6236.0') 같은 엑셀 셀 값을 고려
    if isinstance(raw, float):
        if raw != raw or raw == float("inf"):  # NaN / inf
            raise ValueError(f"우편번호 값이 숫자가 아닙니다: {raw!r}")
        raw = int(raw)

    value = _clean_text(raw)
    if not value:
        raise ValueError("우편번호가 비어있습니다")

    digits = re.sub(r"\D", "", value)
    if not digits:
        raise ValueError(f"우편번호에 숫자가 없습니다: {raw!r}")

    # 엑셀 숫자형으로 0이 잘린 경우 복원 (4자리 → 앞에 0 하나)
    # 서울 대부분(0~08xxx), 부산(46xxx~48xxx) 등 앞자리 0이 흔함.
    # 3자리 이하는 확신이 없으므로 거부.
    if len(digits) == 4:
        digits = "0" + digits

    if _POSTAL_5_RE.match(digits):
        return digits

    if _POSTAL_OLD_6_RE.match(digits):
        raise ValueError(
            f"구 6자리 우편번호({raw!r})는 2015년 8월 1일부로 폐지되었습니다. "
            "5자리 우편번호로 재입력해 주세요 (우체국 홈페이지 또는 epost.go.kr 에서 조회 가능)."
        )

    raise ValueError(
        f"우편번호는 5자리 숫자여야 합니다 (2015년 이후 표준): {raw!r} → 숫자 {len(digits)}자리"
    )


_MAX_SAFE_QTY = 999  # 개인 주문 상한 (오타 방지)


def validate_quantity(raw) -> int:
    """수량 정규화.

    허용:
      - int / float (1.0 → 1)
      - 문자열 ``"3"`` / ``"3개"`` / ``"3ea"`` / ``"1,000"`` / ``" 2 "``

    거부:
      - 0 이하
      - 999 초과 (개인 주문으로 비현실적 → 오타 가능성, 명시적 차단)
      - 소수점(1.5 등) — 실수로 입력된 소수는 거부
      - 숫자 없음
    """
    if raw is None:
        raise ValueError("수량이 비어있습니다")

    if isinstance(raw, bool):  # bool 은 int 의 서브클래스라 걸러냄
        raise ValueError(f"수량은 정수여야 합니다: {raw!r}")

    if isinstance(raw, (int, float)):
        if isinstance(raw, float):
            if raw != raw or raw == float("inf"):
                raise ValueError(f"수량 값이 올바르지 않습니다: {raw!r}")
            if not raw.is_integer():
                raise ValueError(f"수량은 정수여야 합니다 (소수점 불가): {raw!r}")
            value = int(raw)
        else:
            value = raw
    else:
        text = _clean_text(raw)
        if not text:
            raise ValueError("수량이 비어있습니다")
        # 천단위 콤마 제거, '개' / 'ea' / 'pcs' 등 단위 제거
        cleaned = re.sub(r"[,\s]", "", text)
        cleaned = re.sub(r"(개|ea|EA|pcs|PCS|ea\.?|EA\.?)$", "", cleaned)
        if not cleaned:
            raise ValueError(f"수량에 숫자가 없습니다: {raw!r}")
        try:
            f = float(cleaned)
        except ValueError as exc:
            raise ValueError(f"수량은 정수여야 합니다: {raw!r}") from exc
        if not f.is_integer():
            raise ValueError(f"수량은 정수여야 합니다 (소수점 불가): {raw!r}")
        value = int(f)

    if value < 1:
        raise ValueError(f"수량은 1 이상이어야 합니다: {raw!r}")
    if value > _MAX_SAFE_QTY:
        raise ValueError(
            f"수량이 {_MAX_SAFE_QTY}개를 초과합니다 ({value}). "
            "오타가 아닌지 확인해 주세요."
        )
    return value


def clean_recipient_name(raw) -> str:
    """수취인 이름 정규화.

    처리:
      - 보이지 않는 공백/전각 공백 제거
      - 앞뒤 공백 제거, 내부 연속 공백 단일화
      - 빈 값 거부, 80자 초과 거부 (스리랑카/인도식 긴 이름 허용)
    검증 완화:
      - 한자 이름(예: 金哲洙)이나 외국인 이름(예: Dissanayaka mudiyanselage ...) 허용
      - 숫자/기호가 섞여 있어도 허용 — 11번가 주문서 자체는 폭넓게 받음
    """
    text = _clean_text(raw)
    if not text:
        raise ValueError("수취인 이름이 비어있습니다")
    text = re.sub(r"\s+", " ", text)
    if len(text) > 80:
        raise ValueError(f"수취인 이름이 너무 깁니다 (80자 초과): {text!r}")
    return text


def clean_address(raw) -> str:
    """수취인 주소 정규화.

    처리:
      - 보이지 않는 공백/전각 공백 제거
      - 앞뒤 공백 제거, 내부 연속 공백 단일화
      - 줄바꿈(\\n, \\r\\n)은 공백 하나로 치환 (엑셀 셀에서 줄바꿈으로 저장한 경우)
      - 빈 값 거부
    """
    text = _clean_text(raw)
    # 줄바꿈을 공백으로
    text = re.sub(r"[\r\n]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise ValueError("수취인 주소가 비어있습니다")
    if len(text) > 200:
        raise ValueError(f"수취인 주소가 너무 깁니다 (200자 초과): {text[:50]!r}...")
    return text


def clean_price(raw: str | int | float | None) -> int | None:
    """'15,000원' -> 15000 형태 정리."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    digits = re.sub(r"[^\d]", "", str(raw))
    return int(digits) if digits else None

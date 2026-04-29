"""첫 실행 온보딩 마법사 (5단계)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from src.ui.widgets.animated_button import PressAnimationFilter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TITLE_STYLE = "QLabel { font-size: 22px; font-weight: 700; color: #1F2937; }"
_SUBTITLE_STYLE = "QLabel { font-size: 14px; color: #4B5563; }"
_CARD_STYLE = (
    "QFrame#card { background: #F9FAFB; border: 1px solid #E5E7EB; "
    "border-radius: 10px; padding: 16px; }"
)
_BADGE_STYLE = (
    "QLabel { background: #111827; color: #FFFFFF; border-radius: 16px; "
    "min-width: 32px; max-width: 32px; min-height: 32px; max-height: 32px; "
    "font-weight: 700; font-size: 16px; qproperty-alignment: AlignCenter; }"
)


def _card(title_text: str, body_html: str, badge: str | None = None) -> QFrame:
    frame = QFrame()
    frame.setObjectName("card")
    frame.setStyleSheet(_CARD_STYLE)

    outer = QHBoxLayout(frame)
    outer.setContentsMargins(12, 12, 12, 12)
    outer.setSpacing(12)

    if badge:
        lbl = QLabel(badge)
        lbl.setStyleSheet(_BADGE_STYLE)
        lbl.setAlignment(Qt.AlignCenter)
        outer.addWidget(lbl, 0, Qt.AlignTop)

    inner = QVBoxLayout()
    inner.setSpacing(4)
    t = QLabel(title_text)
    t.setStyleSheet("QLabel { font-weight: 700; font-size: 15px; color: #111827; }")
    b = QLabel(body_html)
    b.setWordWrap(True)
    b.setTextFormat(Qt.RichText)
    b.setStyleSheet("QLabel { font-size: 13px; color: #374151; }")
    inner.addWidget(t)
    inner.addWidget(b)

    outer.addLayout(inner, 1)
    return frame


def _title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_TITLE_STYLE)
    return lbl


def _subtitle(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_SUBTITLE_STYLE)
    lbl.setWordWrap(True)
    return lbl


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(" ")  # 공백: 기본 QWizard 헤더 최소화
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        layout.addWidget(_title("환영합니다"))
        layout.addWidget(
            _subtitle(
                "11번가 자동 주문 프로그램은 반복되는 주문 정보 입력을 자동화합니다.<br>"
                "다음 5단계로 프로그램 사용 방법을 안내해드립니다."
            )
        )

        layout.addSpacing(12)
        layout.addWidget(
            _card(
                "이 프로그램이 하는 일",
                "• 엑셀에 저장된 주문 정보를 11번가에 자동 입력<br>"
                "• 상품 판매가 자동 조회 → 엑셀 저장<br>"
                "• 주문 완료 후 주문번호 자동 추출 → 엑셀 저장<br>"
                "• 11번가 로그인 + 샵백 확장 유지 (매번 로그인 불필요)",
                badge="·",
            )
        )
        layout.addWidget(
            _card(
                "예상 절감 시간",
                "주문 1건당 <b>약 3-5분 → 12-20초</b>로 단축 (최대 95% 절감)",
                badge="·",
            )
        )

        layout.addStretch()
        hint = QLabel("아래 <b>다음</b> 버튼을 눌러 계속 진행하세요.")
        hint.setStyleSheet("QLabel { color: #6B7280; font-size: 12px; }")
        layout.addWidget(hint)


class ExcelFormatPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(" ")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(_title("1단계: 엑셀 파일 준비"))
        layout.addWidget(
            _subtitle("주문할 항목이 담긴 엑셀 파일을 먼저 준비해 주세요.")
        )

        layout.addSpacing(8)
        layout.addWidget(
            _card(
                "필수 컬럼 (8개)",
                "엑셀 첫 행에 아래 컬럼명이 정확히 있어야 합니다:<br>"
                "<b>구매처 · 수취인 · 수취인번호 · 통관번호 · "
                "우편번호 · 수취인 주소 · 수량 · 영문이름</b><br>"
                "<span style='color:#6B7280;'>(순서는 상관없습니다)</span>",
                badge="·",
            )
        )

        # 예시 테이블
        example = QFrame()
        example.setObjectName("card")
        example.setStyleSheet(_CARD_STYLE)
        ex_lay = QVBoxLayout(example)
        ex_title = QLabel("예시")
        ex_title.setStyleSheet("QLabel { font-weight: 700; font-size: 13px; }")
        ex_lay.addWidget(ex_title)
        ex_table = QLabel(
            "<table cellspacing='0' cellpadding='6' border='1' "
            "style='border-collapse:collapse; border-color:#D1D5DB;'>"
            "<tr style='background:#F3F4F6; font-weight:700;'>"
            "<td>구매처</td><td>수취인</td><td>수취인번호</td>"
            "<td>통관번호</td><td>우편번호</td><td>수취인 주소</td>"
            "<td>수량</td><td>영문이름</td></tr>"
            "<tr>"
            "<td>11st.co.kr/...</td>"
            "<td>김철수</td>"
            "<td>010-1234-5678</td>"
            "<td>P123456789012</td>"
            "<td>06236</td>"
            "<td>서울시 강남구...</td>"
            "<td>1</td>"
            "<td>KIM CHUL SOO</td>"
            "</tr></table>"
        )
        ex_table.setTextFormat(Qt.RichText)
        ex_lay.addWidget(ex_table)
        layout.addWidget(example)

        layout.addWidget(
            _card(
                "자동 채워지는 컬럼",
                "프로그램이 실행하며 아래 컬럼을 <b>자동으로 추가</b>합니다:<br>"
                "<b>토탈가격</b> (판매가 × 수량) · <b>주문번호</b><br>"
                "<span style='color:#6B7280;'>"
                "가격 조회 후 중간 저장, 주문 완료 후 최종 저장이 가능합니다.</span>",
                badge="·",
            )
        )

        layout.addStretch()


class BrowserSetupPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(" ")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(_title("2단계: 브라우저 준비 (1회만)"))
        layout.addWidget(
            _subtitle(
                "프로그램이 사용할 <b>전용 Chrome</b>을 준비합니다. 개인 크롬과 분리되어 안전합니다."
            )
        )

        layout.addSpacing(8)
        layout.addWidget(
            _card(
                "상단 메뉴의 <b>브라우저 열기</b> 클릭",
                "새 Chrome 창이 열립니다. (최초 1회 약 3-5초 소요)",
                badge="1",
            )
        )
        layout.addWidget(
            _card(
                "Chrome에서 <b>11번가 로그인</b>",
                "평소처럼 11st.co.kr에 접속해 로그인합니다.",
                badge="2",
            )
        )
        layout.addWidget(
            _card(
                "<b>샵백 확장프로그램 설치</b> (선택)",
                "상단 메뉴 <b>도구 → 샵백 확장프로그램 설치</b> 를 누르면 "
                "앱 브라우저에서 Chrome 웹스토어 샵백 페이지가 열립니다.<br>"
                "<b>'Chrome에 추가'</b> 클릭 → 샵백 로그인까지 마치면 끝.<br>"
                "<span style='color:#6B7280;'>* 적립이 필요 없다면 건너뛰어도 됩니다.</span>",
                badge="3",
            )
        )

        layout.addSpacing(8)
        note = QLabel(
            "<b>한 번만 설정</b>하면 이후에는 로그인과 확장이 유지됩니다."
        )
        note.setTextFormat(Qt.RichText)
        note.setStyleSheet(
            "QLabel { background: #F9FAFB; border: 1px solid #E5E7EB; "
            "border-radius: 8px; padding: 10px; color: #111827; }"
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        layout.addStretch()


class OrderFlowPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(" ")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(_title("3단계: 주문 진행"))
        layout.addWidget(_subtitle("엑셀 불러온 뒤 주문할 행을 더블클릭하면 자동 진행됩니다."))

        layout.addSpacing(8)
        layout.addWidget(
            _card(
                "<b>엑셀 불러오기</b>",
                "상단 툴바에서 엑셀 선택 → 주문 목록이 테이블에 표시됩니다.",
                badge="1",
            )
        )
        layout.addWidget(
            _card(
                "<b>일괄 가격 조회</b>",
                "상단 툴바 클릭 → 모든 상품의 판매가를 자동 수집하고 "
                "수량을 곱한 <b>토탈가격</b>을 엑셀에 저장합니다.<br>"
                "<span style='color:#6B7280;'>주문 시작 시 누락분은 자동 재조회됩니다.</span>",
                badge="2",
            )
        )
        layout.addWidget(
            _card(
                "테이블 행 <b>더블클릭</b>",
                "선택한 행의 상품페이지로 이동 → 옵션·수량은 직접 선택하고 "
                "구매하기를 누르면 수취인·주소·통관번호·영문이름이 자동 입력됩니다.<br>"
                "<span style='color:#6B7280;'>오류가 나도 프로그램은 종료되지 않습니다. "
                "브라우저에서 직접 수정 후 우클릭 → '이어서 진행' 으로 계속할 수 있습니다.</span>",
                badge="3",
            )
        )
        layout.addWidget(
            _card(
                "마지막 <b>결제하기</b>는 직접 클릭",
                "안전을 위해 최종 결제 버튼은 사용자가 직접 누릅니다.<br>"
                "결제 완료 후 <b>주문번호가 자동으로 엑셀에 저장</b>됩니다.",
                badge="4",
            )
        )

        layout.addStretch()


class SafetyFinalPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(" ")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(_title("안전 기능 및 주의사항"))
        layout.addWidget(
            _subtitle("프로그램은 아래와 같이 안전하게 작동하도록 설계되었습니다.")
        )

        layout.addSpacing(8)
        layout.addWidget(
            _card(
                "자동 저장 + 크래시 복구",
                "매 주문마다 엑셀이 자동 저장되며, 프로그램이 갑자기 꺼져도 "
                "재시작 시 <b>이어하기</b>가 가능합니다.",
                badge="·",
            )
        )
        layout.addWidget(
            _card(
                "에러 스크린샷",
                "오류 발생 시 페이지 스크린샷이 <code>data/screenshots/</code>에 자동 저장됩니다. "
                "행 우클릭 → <b>스크린샷 열기</b>로 확인 가능합니다.",
                badge="·",
            )
        )
        layout.addWidget(
            _card(
                "로그인/샵백 유지",
                "Chrome 프로필이 <code>data/chrome_profile/</code>에 영구 저장됩니다. "
                "재실행해도 로그인이 유지됩니다.",
                badge="·",
            )
        )

        warning = QLabel(
            "<b>유의사항</b><br>"
            "• 이 프로그램은 <b>개인 사용자 편의</b>를 위한 도구입니다.<br>"
            "• 11번가 및 샵백 <b>이용약관을 준수</b>해주세요.<br>"
            "• 주문 결과에 대한 최종 책임은 <b>사용자</b>에게 있습니다."
        )
        warning.setTextFormat(Qt.RichText)
        warning.setStyleSheet(
            "QLabel { background: #F3F4F6; border: 1px solid #D1D5DB; "
            "border-radius: 8px; padding: 12px; color: #111827; }"
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        layout.addSpacing(8)
        self.dont_show_again = QCheckBox("다음 실행부터 이 안내를 표시하지 않음")
        self.dont_show_again.setChecked(True)
        layout.addWidget(self.dont_show_again)

        end = QLabel(
            "준비가 끝났습니다. <b>완료</b>를 누르면 프로그램 메인 화면으로 이동합니다.<br>"
            "도움이 필요하시면 메뉴의 <b>도움말 → 처음부터 다시 보기</b>로 이 안내를 다시 볼 수 있습니다."
        )
        end.setTextFormat(Qt.RichText)
        end.setWordWrap(True)
        end.setStyleSheet("QLabel { color: #065F46; font-size: 13px; padding-top: 6px; }")
        layout.addWidget(end)

        layout.addStretch()


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


class OnboardingWizard(QWizard):
    """5단계 첫 실행 안내 마법사."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("11번가 자동 주문 - 사용 안내")
        self.resize(720, 640)
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.NoCancelButtonOnLastPage, True)
        self.setOption(QWizard.HaveHelpButton, False)

        # 한글 버튼
        self.setButtonText(QWizard.BackButton, "\u25C1 이전")  # ◁
        self.setButtonText(QWizard.NextButton, "다음 \u25B7")  # ▷
        self.setButtonText(QWizard.FinishButton, "완료")
        self.setButtonText(QWizard.CancelButton, "닫기")

        self._final_page = SafetyFinalPage()
        self.addPage(WelcomePage())
        self.addPage(ExcelFormatPage())
        self.addPage(BrowserSetupPage())
        self.addPage(OrderFlowPage())
        self.addPage(self._final_page)

        # 마법사 하단 버튼(Back/Next/Finish/Cancel)에 클릭 애니메이션
        self._press_filter = PressAnimationFilter(self)
        for btn_id in (
            QWizard.BackButton,
            QWizard.NextButton,
            QWizard.FinishButton,
            QWizard.CancelButton,
        ):
            btn = self.button(btn_id)
            if btn is not None:
                btn.installEventFilter(self._press_filter)

    def dont_show_again(self) -> bool:
        """완료 후 '다시 표시하지 않음' 체크 상태."""
        return bool(self._final_page.dont_show_again.isChecked())

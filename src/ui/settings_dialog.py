"""설정 다이얼로그."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from src.models.settings import AppSettings
from src.ui.widgets.animated_button import PressAnimationFilter


class SettingsDialog(QDialog):
    """사용자 설정 편집 다이얼로그."""

    def __init__(self, settings: AppSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("설정")
        self.resize(480, 420)
        self.settings = settings

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # 브라우저
        self.profile_dir = QLineEdit(settings.browser.profile_dir)
        form.addRow("Chrome 프로필 경로:", self.profile_dir)

        self.channel = QLineEdit(settings.browser.channel)
        form.addRow("Chrome 채널 (chrome/msedge):", self.channel)

        # 자동화
        self.typing_delay = QSpinBox()
        self.typing_delay.setRange(0, 1000)
        self.typing_delay.setValue(settings.automation.typing_delay_ms)
        self.typing_delay.setSuffix(" ms")
        form.addRow("타이핑 딜레이:", self.typing_delay)

        self.retry_attempts = QSpinBox()
        self.retry_attempts.setRange(1, 10)
        self.retry_attempts.setValue(settings.automation.retry_attempts)
        form.addRow("재시도 횟수:", self.retry_attempts)

        self.inter_order = QSpinBox()
        self.inter_order.setRange(0, 60000)
        self.inter_order.setValue(settings.automation.inter_order_delay_ms)
        self.inter_order.setSuffix(" ms")
        form.addRow("주문 간 딜레이:", self.inter_order)

        self.auto_pay = QCheckBox(
            "최종 결제 버튼 자동 클릭 (무인 모드 - 주문번호까지 자동 추출)"
        )
        self.auto_pay.setChecked(settings.automation.auto_click_final_payment)
        form.addRow(self.auto_pay)

        self.skip_on_error = QCheckBox("에러 발생 시 그 행을 건너뛰고 다음 행 진행")
        self.skip_on_error.setChecked(
            getattr(settings.automation, "skip_on_error", True)
        )
        form.addRow(self.skip_on_error)

        self.skip_on_pause = QCheckBox(
            "사용자 개입 필요한 행도 건너뛰기 (주소 검색 실패 등)"
        )
        self.skip_on_pause.setChecked(
            getattr(settings.automation, "skip_on_pause", True)
        )
        form.addRow(self.skip_on_pause)

        self.verify_shopback = QCheckBox(
            "샵백 추적 검증 (결제 직전 적립 가능 여부 확인 + 진단 파일 저장)"
        )
        self.verify_shopback.setChecked(
            getattr(settings.automation, "verify_shopback", True)
        )
        form.addRow(self.verify_shopback)

        self.abort_if_no_shopback = QCheckBox(
            "샵백 미감지 시 결제 중단 (사용자가 샵백 활성화 후 이어서 진행)"
        )
        self.abort_if_no_shopback.setChecked(
            getattr(settings.automation, "abort_if_no_shopback", False)
        )
        form.addRow(self.abort_if_no_shopback)

        self.stealth = QCheckBox("Stealth 모드 사용")
        self.stealth.setChecked(settings.automation.stealth_enabled)
        form.addRow(self.stealth)

        self.screenshot = QCheckBox("에러 시 스크린샷 자동 저장")
        self.screenshot.setChecked(settings.automation.screenshot_on_error)
        form.addRow(self.screenshot)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # 버튼박스 내 모든 버튼에 클릭 애니메이션
        self._press_filter = PressAnimationFilter(self)
        for b in buttons.buttons():
            b.installEventFilter(self._press_filter)

    def _on_save(self) -> None:
        self.settings.browser.profile_dir = self.profile_dir.text().strip()
        self.settings.browser.channel = self.channel.text().strip() or "chrome"
        self.settings.automation.typing_delay_ms = self.typing_delay.value()
        self.settings.automation.retry_attempts = self.retry_attempts.value()
        self.settings.automation.inter_order_delay_ms = self.inter_order.value()
        self.settings.automation.auto_click_final_payment = self.auto_pay.isChecked()
        self.settings.automation.skip_on_error = self.skip_on_error.isChecked()
        self.settings.automation.skip_on_pause = self.skip_on_pause.isChecked()
        self.settings.automation.verify_shopback = self.verify_shopback.isChecked()
        self.settings.automation.abort_if_no_shopback = (
            self.abort_if_no_shopback.isChecked()
        )
        self.settings.automation.stealth_enabled = self.stealth.isChecked()
        self.settings.automation.screenshot_on_error = self.screenshot.isChecked()
        self.settings.save()
        self.accept()

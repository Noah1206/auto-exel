"""사용자 설정 Pydantic 모델."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from src.exceptions import ConfigError
from src.utils.resource_path import resource_path, user_settings_path


class ViewportConfig(BaseModel):
    width: int = 1400
    height: int = 900


class BrowserConfig(BaseModel):
    profile_dir: str = "data/chrome_profile"
    channel: str = "chrome"
    headless: bool = False
    # True 면 창을 화면 밖(-3000px)에 띄워 사용자 시야에서 숨김.
    # headless 와 달리 Chrome 확장(샵백 등)은 정상 동작.
    # 자동화 중 포커스는 어떤 경우에도 Chrome 으로 이동하지 않음.
    hide_window: bool = True
    viewport: ViewportConfig = Field(default_factory=ViewportConfig)
    locale: str = "ko-KR"
    timezone: str = "Asia/Seoul"
    default_timeout_ms: int = 15000
    navigation_timeout_ms: int = 30000


class AutomationConfig(BaseModel):
    typing_delay_ms: int = 50
    retry_attempts: int = 3
    retry_backoff_base: float = 1.0
    # 결제하기 버튼 자동 클릭 여부. 기본값 False — 프로그램은 정보 입력까지만 하고
    # 결제하기 버튼은 사용자가 직접 눌러야 함 (안전장치).
    # True 로 바꾸면 약관동의/결제수단/결제버튼까지 프로그램이 자동 클릭.
    auto_click_final_payment: bool = False
    # 자동 진행 시 에러가 난 행은 건너뛰고 다음 행으로 계속한다.
    skip_on_error: bool = True
    # paused(사용자 개입 필요) 상태도 자동 진행 시에는 건너뛴다.
    skip_on_pause: bool = True
    inter_order_delay_ms: int = 1500
    screenshot_on_error: bool = True
    stealth_enabled: bool = True
    # 샵백 적립 추적 검증 옵션
    verify_shopback: bool = True
    # 샵백 미활성 시 결제를 보류하고 사용자 확인 받기 (False면 그냥 경고만)
    abort_if_no_shopback: bool = False


class PriceScraperConfig(BaseModel):
    concurrent: int = 12  # 동시 처리 개수 (1 이면 순차). HTTP fast path 라 12 까지 안전.
    per_product_timeout_ms: int = 8000
    inter_request_delay_ms: int = 0


class ExcelConfig(BaseModel):
    auto_save: bool = True
    backup_on_load: bool = True
    output_suffix: str = "_완료"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    rotation: str = "00:00"
    retention_days: int = 30
    max_file_mb: int = 10


class UIConfig(BaseModel):
    theme: str = "light"
    log_max_lines: int = 500
    table_row_height: int = 32
    first_run: bool = True           # 최초 실행 여부 (온보딩 마법사 표시)
    show_empty_state_help: bool = True  # 테이블 비어있을 때 안내 배너 표시
    shopback_install_prompted: bool = False  # 샵백 설치 안내 팝업 노출 여부
    # 최근 열었던 엑셀 파일들 — 절대경로, 가장 최근이 맨 앞. 최대 10개.
    recent_excel_files: list[str] = Field(default_factory=list)
    recent_excel_max: int = 10


class AppSettings(BaseModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    price_scraper: PriceScraperConfig = Field(default_factory=PriceScraperConfig)
    excel: ExcelConfig = Field(default_factory=ExcelConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ui: UIConfig = Field(default_factory=UIConfig)

    @classmethod
    def load(
        cls,
        user_path: str | Path | None = None,
        default_path: str | Path | None = None,
    ) -> AppSettings:
        """settings.yaml(사용자) 우선, 없으면 default_settings.yaml.

        PyInstaller 동결 환경에선 user_path 는 ~/Library/Application Support/...,
        default_path 는 _MEIPASS 안의 번들된 default_settings.yaml 을 가리킨다.
        """
        if user_path is None:
            user_path = user_settings_path("settings.yaml")
        if default_path is None:
            default_path = resource_path("config", "default_settings.yaml")
        for path in (Path(user_path), Path(default_path)):
            if path.exists():
                try:
                    with path.open(encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    return cls.model_validate(data)
                except Exception as exc:
                    raise ConfigError(f"설정 파일 로드 실패 [{path}]: {exc}") from exc
        # 파일 둘 다 없으면 순수 기본값
        return cls()

    def save(self, path: str | Path | None = None) -> None:
        if path is None:
            path = user_settings_path("settings.yaml")
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                self.model_dump(mode="json"), f, allow_unicode=True, sort_keys=False
            )

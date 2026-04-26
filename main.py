"""11번가 자동 주문 프로그램 엔트리포인트.

실행:
    python main.py

빌드:
    pyinstaller build/build.spec --clean --noconfirm
"""
from __future__ import annotations

import sys
from pathlib import Path

# src 패키지 import 가능하도록
sys.path.insert(0, str(Path(__file__).parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from src.models.settings import AppSettings  # noqa: E402
from src.ui.main_window import MainWindow  # noqa: E402
from src.ui.theme import apply_light_theme  # noqa: E402
from src.utils.logger import get_logger, setup_logger  # noqa: E402


def main() -> int:
    # 0) 설정 로드 (가장 먼저 - logger 설정에 필요)
    settings = AppSettings.load()

    # 1) 로거 초기화
    setup_logger(
        level=settings.logging.level,
        rotation=settings.logging.rotation,
        retention_days=settings.logging.retention_days,
        max_file_mb=settings.logging.max_file_mb,
    )
    log = get_logger()
    log.info("=" * 60)
    log.info("11번가 자동 주문 프로그램 시작")
    log.info(f"Python: {sys.version}")

    # 2) Qt 앱 생성 + 라이트 테마 강제 (OS 다크모드 무시)
    app = QApplication(sys.argv)
    app.setApplicationName("11번가 자동 주문")
    app.setOrganizationName("KmongOrderApp")
    apply_light_theme(app)

    # 3) 메인 윈도우 (Playwright 는 내부의 AsyncRunner 백그라운드 스레드에서 실행)
    window = MainWindow(settings)
    window.show()

    # 4) 표준 Qt 이벤트루프 (qasync 제거 — Playwright 와 reentry 충돌 회피)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

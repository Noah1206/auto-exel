"""PyInstaller 동결 환경과 개발 환경 모두에서 동작하는 리소스 경로 헬퍼.

동작:
    - 개발 환경: 프로젝트 루트의 config/... 를 가리킨다 (작업 디렉토리 무관).
    - PyInstaller --onefile: sys._MEIPASS 의 임시 추출 디렉토리를 우선 가리킨다.
    - 사용자 설정(config/settings.yaml) 같이 쓰기 가능해야 하는 파일은
      `user_data_path()` 로 ~/Library/Application Support/... 또는
      %APPDATA%\... 같은 사용자 폴더를 사용한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _frozen_base() -> Path | None:
    """PyInstaller 환경이면 추출된 리소스 폴더 (없으면 None)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return None


def _project_root() -> Path:
    """개발 환경의 프로젝트 루트 (이 파일 기준 두 단계 위)."""
    return Path(__file__).resolve().parent.parent.parent


def resource_path(*parts: str) -> Path:
    """읽기 전용 번들 리소스 경로.

    예: resource_path("config", "selectors.yaml")

    PyInstaller --onefile 환경에선 _MEIPASS/<parts...> 를 우선 사용.
    개발 환경에선 프로젝트 루트의 <parts...> 를 사용.
    """
    base = _frozen_base() or _project_root()
    return base.joinpath(*parts)


def _has_non_ascii(p: Path) -> bool:
    """경로에 비-ASCII 문자(한글 등) 가 있는지 검사."""
    try:
        str(p).encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def user_data_dir(app_name: str = "11st_auto_order") -> Path:
    """사용자별 쓰기 가능 데이터 폴더 (settings.yaml, logs 등).

    macOS: ~/Library/Application Support/<app_name>
    Windows: %APPDATA%/<app_name> (한글 사용자명이면 C:\\11st_auto_order 로 fallback)
    Linux: ~/.config/<app_name>
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / app_name
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / app_name if appdata else Path.home() / app_name
        # 한글 사용자명 등 ASCII 외 경로면 일부 라이브러리(특히 Chrome) 가 실패 →
        # ProgramData 또는 C:\ 직하로 fallback. PUBLIC 환경변수도 대안.
        if _has_non_ascii(base):
            programdata = os.environ.get("PROGRAMDATA") or "C:\\ProgramData"
            fallback = Path(programdata) / app_name
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                base = fallback
            except Exception:
                # 마지막 fallback: 임시 디렉토리
                import tempfile
                base = Path(tempfile.gettempdir()) / app_name
    else:
        base = Path.home() / ".config" / app_name
    base.mkdir(parents=True, exist_ok=True)
    return base


def user_settings_path(filename: str = "settings.yaml") -> Path:
    """사용자가 수정 가능한 설정 파일 경로.

    개발 환경에선 기존 config/settings.yaml 그대로 (편집 편의),
    동결 환경에선 user_data_dir 안의 settings.yaml.
    """
    if _frozen_base() is not None:
        return user_data_dir() / filename
    return _project_root() / "config" / filename

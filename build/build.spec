# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: system Chrome 사용 → Playwright 번들 Chromium 제외 → ~50MB
# 빌드: pyinstaller build/build.spec --clean --noconfirm
import sys
from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).parent
ENTRY = str(PROJECT_ROOT / "main.py")
IS_MAC = sys.platform == "darwin"

block_cipher = None

a = Analysis(
    [ENTRY],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=[
        (str(PROJECT_ROOT / "config"), "config"),
    ],
    hiddenimports=[
        "playwright",
        "playwright.async_api",
        "playwright._impl",
        "playwright_stealth",
        "PySide6.QtCore",
        "PySide6.QtWidgets",
        "PySide6.QtGui",
        "qasync",
        "loguru",
        "openpyxl",
        "openpyxl.cell._writer",
        "pydantic",
        "pydantic.deprecated.decorator",
        "yaml",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "pandas",  # ExcelManager는 openpyxl만 사용
        "numpy",
        "scipy",
        "IPython",
        "jupyter",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icon_ico = PROJECT_ROOT / "build" / "icon.ico"
_icon_icns = PROJECT_ROOT / "build" / "icon.icns"
_icon = str(_icon_icns) if (IS_MAC and _icon_icns.exists()) else (
    str(_icon_ico) if _icon_ico.exists() else None
)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="11st_auto_order",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,               # GUI 앱
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

if IS_MAC:
    app = BUNDLE(
        exe,
        name="11st_auto_order.app",
        icon=_icon,
        bundle_identifier="com.kmong.eleven-st-auto-order",
        info_plist={
            "CFBundleName": "11번가 자동 주문",
            "CFBundleDisplayName": "11번가 자동 주문",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            # macOS Big Sur 이상에서 다크모드/라이트모드 자동
            "NSRequiresAquaSystemAppearance": False,
            # 사용자가 시스템 Chrome 을 띄울 때 osascript / Apple Events 사용
            "NSAppleEventsUsageDescription":
                "Chrome 창의 위치를 조정하여 포커스를 빼앗지 않도록 합니다.",
        },
    )

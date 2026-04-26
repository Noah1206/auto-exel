@echo off
REM 11번가 자동 주문 — 릴리스 빌드 스크립트 (Windows)
REM 사용법: scripts\build_release.bat
REM 산출물: dist\11st_auto_order.exe

setlocal
cd /d "%~dp0\.."

if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

where pyinstaller >nul 2>nul
if errorlevel 1 (
    echo [ERROR] pyinstaller 가 설치되어 있지 않습니다. pip install pyinstaller
    exit /b 1
)

echo ==^> 이전 빌드 산출물 정리
if exist build\build rmdir /s /q build\build
if exist dist\11st_auto_order.exe del /q dist\11st_auto_order.exe

echo ==^> PyInstaller 빌드
pyinstaller build\build.spec --clean --noconfirm
if errorlevel 1 (
    echo [ERROR] 빌드 실패
    exit /b 1
)

if not exist dist\11st_auto_order.exe (
    echo [ERROR] dist\11st_auto_order.exe 가 만들어지지 않았습니다.
    exit /b 1
)

echo ==^> 완료: dist\11st_auto_order.exe
dir dist\11st_auto_order.exe
endlocal

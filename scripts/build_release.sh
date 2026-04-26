#!/usr/bin/env bash
# 11번가 자동 주문 — 릴리스 빌드 스크립트 (macOS / Linux)
#
# 사용법:
#   ./scripts/build_release.sh            # 빌드 + .app 생성
#   ./scripts/build_release.sh --dmg      # 빌드 + .dmg 패키징 (macOS only)
#   ./scripts/build_release.sh --zip      # 빌드 + .zip 패키징
#
# 산출물:
#   dist/11st_auto_order.app              (macOS)
#   dist/11st_auto_order                  (Linux 단일 실행파일)
#   dist/11st_auto_order-<version>-mac.dmg
#   dist/11st_auto_order-<version>-mac.zip

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION="1.0.0"
DO_DMG=0
DO_ZIP=0
for arg in "$@"; do
  case "$arg" in
    --dmg) DO_DMG=1 ;;
    --zip) DO_ZIP=1 ;;
  esac
done

# 가상환경 활성화 (있으면)
if [ -d ".venv" ]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

# PyInstaller 자동 설치 (venv 의 python 기준)
if ! python -c "import PyInstaller" 2>/dev/null; then
  echo "==> PyInstaller 설치 중..."
  python -m pip install pyinstaller
fi

# 의존성 확인
if ! python -c "import PySide6" 2>/dev/null; then
  echo "[ERROR] PySide6 가 설치되어 있지 않습니다. pip install -r requirements-dev.txt"
  exit 1
fi

echo "    python:      $(which python)"
echo "    pyinstaller: $(python -m PyInstaller --version 2>&1)"

echo "==> 이전 빌드 산출물 정리"
rm -rf build/build dist/11st_auto_order dist/11st_auto_order.app

echo "==> PyInstaller 빌드 (python -m PyInstaller 사용 - venv 안전)"
python -m PyInstaller build/build.spec --clean --noconfirm

OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  if [ ! -d "dist/11st_auto_order.app" ]; then
    echo "[ERROR] dist/11st_auto_order.app 가 만들어지지 않았습니다."
    exit 1
  fi
  echo "==> macOS .app 완료: dist/11st_auto_order.app"

  if [ "$DO_ZIP" = "1" ]; then
    OUT="dist/11st_auto_order-${VERSION}-mac.zip"
    echo "==> ZIP 패키징: $OUT"
    (cd dist && /usr/bin/ditto -c -k --sequesterRsrc --keepParent \
      "11st_auto_order.app" "$(basename "$OUT")")
    echo "    완료: $OUT"
  fi

  if [ "$DO_DMG" = "1" ]; then
    if ! command -v hdiutil >/dev/null 2>&1; then
      echo "[ERROR] hdiutil 이 없습니다 (macOS 필요)"
      exit 1
    fi
    OUT="dist/11st_auto_order-${VERSION}-mac.dmg"
    echo "==> DMG 패키징: $OUT"
    rm -f "$OUT"
    STAGING="dist/_dmg_staging"
    rm -rf "$STAGING"
    mkdir -p "$STAGING"
    cp -R "dist/11st_auto_order.app" "$STAGING/"
    ln -s /Applications "$STAGING/Applications"
    hdiutil create -volname "11st Auto Order" \
      -srcfolder "$STAGING" \
      -ov -format UDZO "$OUT"
    rm -rf "$STAGING"
    echo "    완료: $OUT"
  fi
else
  if [ ! -f "dist/11st_auto_order" ]; then
    echo "[ERROR] dist/11st_auto_order 가 만들어지지 않았습니다."
    exit 1
  fi
  echo "==> Linux 단일 실행파일 완료: dist/11st_auto_order"

  if [ "$DO_ZIP" = "1" ]; then
    OUT="dist/11st_auto_order-${VERSION}-linux.zip"
    echo "==> ZIP 패키징: $OUT"
    (cd dist && zip -r "$(basename "$OUT")" "11st_auto_order")
    echo "    완료: $OUT"
  fi
fi

echo
echo "==> 모든 작업 완료. dist/ 폴더를 확인하세요."
ls -lh dist/

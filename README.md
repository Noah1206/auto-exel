# 11번가 자동 주문 프로그램

엑셀에 기입된 주문 정보를 11번가 페이지에 자동 입력하여 반복 작업을 줄여주는 데스크톱 프로그램.

## 주요 기능

- 📂 엑셀 파일 업로드 (상품링크/이름/번호/통관번호/주소/영문이름)
- 💰 일괄 가격 자동 조회 → 판매가 엑셀 자동 기입
- 🌐 크롬 프로필 영구 유지 → 11번가 로그인 + 샵백 확장 1회 설정 후 재사용
- 🖱 행 더블클릭 → 상품 페이지 이동 + 주문자 정보 자동 입력
- ✅ 주문번호 자동 추출 → 엑셀에 자동 저장
- 🔄 크래시 후 이어하기 (state.json)
- 🛡 안정성: fallback 셀렉터, 재시도, 에러 스크린샷

## 시스템 요구사항

- **OS**: Windows 10 (1809+) / Windows 11 / macOS 12+
- **Chrome**: 120 이상 설치 필수 (프로그램이 시스템 Chrome 사용)
- 저장공간: 100MB+

## 개발자용 설정

### 설치

```bash
# 1. 가상환경
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 2. 의존성
pip install -r requirements-dev.txt

# 3. Playwright 번들 Chromium (개발 중 테스트용 - 배포 시엔 system Chrome 사용)
playwright install chromium
```

### 실행

```bash
python main.py
```

### 테스트

```bash
pytest tests/unit/ -v
```

### 배포용 빌드

**macOS (.app / .dmg)**

```bash
./scripts/build_release.sh           # dist/11st_auto_order.app 생성
./scripts/build_release.sh --zip     # + dist/11st_auto_order-<ver>-mac.zip
./scripts/build_release.sh --dmg     # + dist/11st_auto_order-<ver>-mac.dmg
```

**Windows (.exe)** — Windows PC 에서 빌드 필요 (PyInstaller 는 크로스 컴파일 미지원)

```bat
scripts\build_release.bat
:: 결과: dist\11st_auto_order.exe (~50MB)
```

**자동 빌드 (GitHub Actions)** — Windows / macOS PC 없어도 됨

1. GitHub 에 push
2. Actions 탭 → "Build Windows Release" 또는 "Build macOS Release" → Run workflow
3. 또는 `git tag v1.0.0 && git push --tags` → 자동 빌드 + GitHub Release 생성

산출물은 Actions 페이지의 Artifacts 섹션 또는 Releases 페이지에서 다운로드.

**직접 실행 (수동)**

```bash
pyinstaller build/build.spec --clean --noconfirm
```

> 💡 시스템에 Chrome 이 설치되어 있어야 동작합니다 (Playwright 번들 Chromium 미포함, 용량 절감 목적).
> 사용자에게 배포할 때 README 의 "사용자용 가이드" 섹션을 함께 전달하세요.

## 프로젝트 구조

```
kmong_11st_order/
├── main.py                   # 엔트리포인트
├── config/
│   ├── default_settings.yaml # 기본 설정
│   └── selectors.yaml        # 11번가 셀렉터 (유지보수 포인트)
├── src/
│   ├── core/                 # 자동화 핵심 로직
│   ├── ui/                   # PySide6 UI
│   ├── models/               # Pydantic 모델
│   └── utils/                # 로거/검증/재시도
├── tests/
└── build/
```

## 사용법 (클라이언트용 요약)

### 첫 실행

1. `11st_auto_order` 실행 (macOS: `11st_auto_order.app`, Windows: `11st_auto_order.exe`)
2. 상단 `🌐 브라우저 열기` 버튼 → 새 크롬 창 오픈
3. 크롬에서 **11번가 로그인** (1회)
4. Chrome 웹스토어에서 **샵백 확장** 설치 (1회)
5. 프로그램 창 닫지 않고 사용 시작

### 주문 진행

1. `📂 엑셀 불러오기` → 주문 목록 엑셀 선택
2. `💰 일괄 가격 조회` → 모든 상품의 판매가 × 수량 = **토탈가격** 자동 수집
3. `▶ 주문 시작 (전체)` 또는 행 **더블클릭** → 주문 자동 진행
4. 크롬이 자동으로 상품 페이지 이동 + 수량 선택 + 주문자 정보(받는사람/우편번호/주소/통관번호 등) 입력
5. **결제하기 + 카드 인증은 사용자가 직접** 진행
6. **결제 완료 페이지가 보이면** 메인 프로그램의 해당 행 상태칸의 **`▶ 다음으로`** 버튼 클릭
7. → 주문번호가 **자동으로 엑셀에 저장** + 다음 행 자동 진행

### 오류가 나도 프로그램을 끄지 않아요

- 주소 검색창이 안 닫히거나 주소 선택이 안 될 때 → 테이블 행이 **⏸ 수정 필요** 상태로 바뀌고 브라우저 탭은 그대로 유지됩니다.
- 사용자가 브라우저에서 직접 주소를 골라 입력 완료 → 테이블에서 **우클릭 → ▶ 이어서 진행** 으로 나머지 자동 입력이 재개됩니다.
- 엑셀 데이터 자체가 잘못된 경우 → 테이블 셀을 더블클릭해 **인라인 편집** 후 재시도 가능합니다.

### 엑셀 형식

**입력 (사용자가 기입, 8개 컬럼)**

| 컬럼명 | 설명 |
|--------|------|
| 구매처 | 11번가 상품 URL |
| 수취인 | 받는 분 이름 |
| 수취인번호 | 010-XXXX-XXXX |
| 통관번호 | P + 12자리 숫자 |
| 우편번호 | 5자리 숫자 (2015.8.1부터 표준). 엑셀이 `06236` → `6236` 으로 0을 잘라도 자동 복원. 구 6자리(`XXX-XXX`)는 재입력 필요 |
| 수취인 주소 | 전체 주소 |
| 수량 | 1 이상 정수 |
| 영문이름 | 영문 대문자 |

**출력 (프로그램이 자동 추가, 2개 컬럼)**
- **토탈가격** (판매가 × 수량)
- **주문번호** (결제 완료 후)

저장 시점:
- **가격조회 후** → 토탈가격만 채워진 엑셀로 저장 가능
- **주문 완료 후** → 토탈가격 + 주문번호 모두 채워진 엑셀로 저장 가능

## 주의사항

- 개인 사용자 편의 도구입니다. 11번가 및 샵백 이용약관을 준수해주세요.
- 프로그램에서 제공하는 독립 Chrome 프로필을 사용하세요 (기본 프로필과 분리).
- 주문 결과에 대한 최종 책임은 사용자에게 있습니다.

## 기술 스택

- Python 3.11+
- Playwright 1.58 (system Chrome 채널)
- PySide6 6.11 (Qt GUI)
- qasync (asyncio ↔ Qt 통합)
- openpyxl (Excel I/O)
- Pydantic v2 (데이터 검증)
- loguru (로깅)

## 라이선스

비공개 (클라이언트 전용)

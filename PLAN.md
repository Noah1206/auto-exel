# 11번가 자동 주문 프로그램 개발 계획서 (v2.0 - MCP 조사 반영)

**작성일**: 2026-04-21
**버전**: 2.0 (클라이언트 제출용 / MCP 기술 검증 완료)
**예상 납기**: 계약일로부터 3주 (17 영업일)

> 본 계획서는 2026년 최신 Playwright 공식 문서, PyInstaller 이슈 트래커, PySide6 튜토리얼 자료를 MCP 도구(WebSearch, Sequential Thinking)로 검증하여 작성되었습니다.

---

## 1. 프로젝트 개요

### 1.1 목적
11번가 해외직구 상품 주문 시 반복되는 정보 입력 작업을 자동화하여 업무 시간을 90% 이상 단축하고, 기존 Selenium 프로그램의 고질적 불안정성(크래시, 드라이버 불일치, 로그인 만료)을 근본적으로 제거합니다.

### 1.2 핵심 개선사항 (이전 프로그램 대비)

| 항목 | 이전 (Selenium 기반) | 신규 (Playwright 기반) |
|------|---------------------|------------------------|
| 안정성 | 크래시, ChromeDriver 버전 불일치 | Playwright auto-wait + 시스템 Chrome 직접 사용 |
| 대기 처리 | `time.sleep()` 하드코딩 | 동적 auto-wait (요소 준비까지) |
| 로그인 유지 | 매번 로그인 | `launch_persistent_context` 영구 유지 |
| 확장프로그램 | 불안정 / 미지원 | **공식 지원** (샵백 MV3 완전 호환) |
| 에러 복구 | 수동 재시작 | 자동 재시도 + 크래시 이어하기 |
| 가격 조회 | 미지원 | 신규 기능 |
| 주문번호 수집 | 미지원 | 자동 추출 + 엑셀 저장 |
| 셀렉터 변경 대응 | 재컴파일 필요 | `selectors.yaml` 외부 파일로 즉시 수정 |

---

## 2. 기술 스택 (MCP 검증 완료)

```
Language      : Python 3.11+
GUI           : PySide6 (Qt6) — pythonguis.com 2026 최신 패턴 적용
Automation    : Playwright Python 1.40+ (system Chrome 채널)
Async-Qt      : qasync (asyncio ↔ Qt 이벤트루프 브릿지)
Excel I/O     : openpyxl 3.1+ + pandas 2.x
Data Model    : Pydantic v2 (설정 검증)
Logging       : loguru (일별 로테이션)
Stealth       : playwright-stealth (경량 패치만)
Packaging     : PyInstaller 6.x (one-file .exe)
```

### 2.1 중요 기술적 결정 (조사 결과 반영)

**결정 1: 시스템 Chrome 직접 사용 (`channel="chrome"`)**
- **근거**: Playwright 공식 문서상 Chrome 확장(샵백)은 `launch_persistent_context` + headed 모드 필수. 번들 Chromium보다 실제 Chrome이 확장 호환성 우위.
- **.exe 크기 효과**: 600MB → **약 50MB** (번들 Chromium 제외)
- **요구사항**: 클라이언트 PC에 Chrome 설치 (이미 사용 중이므로 OK)

**결정 2: 독립된 프로필 디렉토리 강제**
- **근거**: 2026년 Chrome 정책 변경으로 기본 "User Data" 디렉토리 자동화 차단됨
- **해결**: 프로그램 폴더 내 `./data/chrome_profile/` 사용
- 사용자 개인 Chrome과 완전 분리 → 프로그램 안정성 + 프라이버시 보호

**결정 3: Headed 모드만 지원**
- **근거**: Playwright 공식 문서 — "extensions are unsupported in headless Chromium"
- 샵백 사용을 위해서는 headed 필수
- UX: 창 최소화 옵션으로 시각적 부담 완화

**결정 4: Stealth는 최소한만 적용**
- **근거**: 2026년 stealth 분석 — 기본 패치는 쉽게 탐지되지만, **실제 로그인된 프로필**이 최고의 stealth
- `playwright-stealth` 기본 패치 적용 (navigator.webdriver 등)
- 공격적 bot 회피 시도 안 함 → ToS 준수

---

## 3. 핵심 기능 명세

### 3.1 엑셀 파일 관리

**입력 스키마 (클라이언트 제공, 8개 컬럼):**
| 컬럼 | 타입 | 필수 | 검증 규칙 |
|------|------|------|----------|
| 구매처 | URL | ✓ | 11st.co.kr 도메인 포함 |
| 수취인 | Text | ✓ | 한글 1-20자 |
| 수취인번호 | Text | ✓ | 010/011로 시작, 하이픈 자동 정리 |
| 통관번호 | Text | ✓ | P + 12자리 숫자 |
| 우편번호 | Text | ✓ | 5자리 숫자 (2015.8.1~). 엑셀 숫자형 저장으로 앞자리 0 잘린 경우 자동 복원. 구 6자리(XXX-XXX)는 1:1 매핑 불가로 명시적 거부 + 재입력 안내 |
| 수취인 주소 | Text | ✓ | 전체 주소 |
| 수량 | Int  | ✓ | 1 이상 정수 |
| 영문이름 | Text | ✓ | 영문 대문자만 허용 |

**출력 스키마 (자동 추가, 2개 컬럼):**
- **토탈가격** = 판매가(단가) × 수량
- **주문번호** (결제 완료 시)

> 구 버전(상품링크/이름/번호/주소)으로 저장된 엑셀도 자동 매핑되어 로드됩니다.

**저장 정책 (2단계 저장):**
- 1단계: **가격 조회 후** — 토탈가격이 모두 채워진 상태로 저장 가능 (주문 시작 전)
- 2단계: **주문 완료 후** — 토탈가격 + 주문번호까지 채워진 상태로 저장
- 매 주문 완료마다 자동 저장 (atomic write: 임시파일 → rename)
- 원본 파일 백업: `원본_BACKUP_20260421_143021.xlsx`
- 결과 파일: `원본_완료_20260421_143021.xlsx`

**주문 시작 전 가드:**
- 전체 주문 시작 버튼을 누르면 토탈가격 누락 행을 **자동으로 재조회**한 뒤 진행한다.
- 토탈가격은 주문 전 100% 채워져 있어야 한다는 요구사항을 프로그램 차원에서 강제한다.

### 3.2 크롬 프로필 영구 유지

```python
# 핵심 구현 (검증된 패턴)
context = await playwright.chromium.launch_persistent_context(
    user_data_dir="./data/chrome_profile",
    channel="chrome",  # system Chrome
    headless=False,    # extensions require headed
    viewport={"width": 1400, "height": 900},
    args=[
        "--disable-blink-features=AutomationControlled",
        # 샵백 확장 자동 로드 (선택)
        # "--load-extension=./data/extensions/shopback",
    ],
    ignore_default_args=["--enable-automation"],
)
```

**첫 실행 마법사:**
1. 프로그램 실행 → "🌐 브라우저 준비" 클릭
2. 새 Chrome 창 오픈 → 사용자가 11번가 직접 로그인 (1회)
3. Chrome 웹스토어에서 샵백 확장 설치 (1회)
4. "✅ 준비 완료" 버튼 → 프로필 저장
5. 이후 재실행 시 자동으로 로그인/확장 유지

### 3.3 자동 주문 워크플로우

**상태 머신:**
```
  [IDLE] 
    ↓ 사용자 더블클릭
  [OPEN_PRODUCT] → 상품 페이지 로드
    ↓ 
  [CHECK_LOGIN] → 로그인 상태 검증
    ↓ 만료 시 → [PROMPT_LOGIN]
  [CLICK_BUY] → 바로구매 버튼
    ↓
  [FILL_FORM] → 주문자 정보 자동 입력
    ↓
  [VERIFY] → 입력값 검증 (사용자 재확인 화면)
    ↓
  [WAIT_PAYMENT] → 사용자가 직접 결제 클릭
    ↓
  [EXTRACT_ORDER_NO] → 주문번호 추출
    ↓
  [SAVE_EXCEL] → 엑셀 자동 저장
    ↓
  [COMPLETE] ✅
```

**무인(unattended) 자동 진행:**
- 약관동의 + 결제수단(카드) + 결제하기 버튼까지 **모두 자동 클릭** (기본값)
- 주문번호 자동 추출 → 엑셀 자동 저장 → 다음 행 자동 시작
- **에러/주소 검색 실패/판매중지/캡차** 등 어떤 사유든 발생 시 해당 행은 건너뛰고 **다음 행으로 자동 진행** (중단 없음)
- 끝까지 모든 행을 처리한 뒤 결과 요약 다이얼로그 표시
- 사용자가 안전을 위해 무인 모드를 끄려면 설정에서 "최종 결제 버튼 자동 클릭" / "에러 시 건너뛰기" / "사용자 개입 필요한 행도 건너뛰기" 체크 해제

### 3.4 일괄 가격 조회 (신규)

- 모든 상품링크를 **순차** 방문 (병렬 아님 → bot 의심 회피)
- 창 최소화 옵션 (백그라운드 실행감)
- 상품당 평균 **2-3초** (Playwright auto-wait)
- 50건 기준 **2-3분** 소요
- 진행률 프로그레스바 + 취소 버튼

### 3.5 크래시 복구 (신규)

`state.json` 구조:
```json
{
  "session_id": "2026-04-21_14-23-01",
  "excel_path": "C:/Users/.../orders.xlsx",
  "last_processed_row": 12,
  "completed_orders": [
    {"row": 1, "order_no": "202504210001", "timestamp": "..."}
  ],
  "failed_orders": [
    {"row": 5, "error": "...", "screenshot": "screenshots/err_5.png"}
  ]
}
```

프로그램 재시작 시 → "이어하기?" 다이얼로그 → 완료된 주문 스킵

---

## 4. UI/UX 설계

### 4.1 메인 화면
```
┌─────────────────────────────────────────────────────────┐
│ 11번가 자동 주문 프로그램 v1.0                  _ □ ×   │
├─────────────────────────────────────────────────────────┤
│ 파일  도구  설정  도움말                                 │
├─────────────────────────────────────────────────────────┤
│ [📂 엑셀 불러오기] [💾 저장] [💰 가격조회] [🌐 브라우저] │
├─────────────────────────────────────────────────────────┤
│ 연결: 정상 | 총 50 | 완료 12 | 진행 1 | 대기 37 | 실패 0│
├─────────────────────────────────────────────────────────┤
│ # │상태│이름   │번호       │상품링크│판매가│주문번호    │
│ 1 │✅ │김철수 │010-1234...│...    │15,000│2025042...  │
│ 2 │🔄 │이영희 │010-2345...│...    │23,000│진행 중     │
│ 3 │⏳ │박민수 │010-3456...│...    │ -    │ -          │
│ 4 │❌ │최지원 │010-4567...│...    │ -    │오류 (재시도)│
├─────────────────────────────────────────────────────────┤
│ 📜 실시간 로그:                                          │
│ [14:23:01] ✅ 주문 #1 완료 - 주문번호 202504210001      │
│ [14:24:15] 🔄 주문 #2 진행 중 - 결제 대기                │
│ [14:24:22] ℹ️ 샵백 추적 URL 감지됨 - 적립 예상: 1.5%    │
└─────────────────────────────────────────────────────────┘
```

### 4.2 QAbstractTableModel 구현 (편집 가능)
- pythonguis.com 2026 패턴 적용
- 셀 클릭 → 편집 가능 (실수 수정)
- 색상 코딩: 완료(초록), 진행중(파랑), 대기(회색), 실패(빨강)
- 정렬/필터링 지원

### 4.3 주요 조작
| 조작 | 동작 |
|------|------|
| 더블클릭 행 | 해당 주문 시작 |
| 우클릭 행 | 재시도 / 건너뛰기 / 상세보기 / 스크린샷 열기 |
| Ctrl+O | 엑셀 열기 |
| Ctrl+S | 저장 |
| F5 | 상태 새로고침 |
| Esc | 진행 중 작업 취소 |

---

## 5. 안정성 설계

### 5.1 셀렉터 외부화 (`selectors.yaml`)
11번가 페이지가 업데이트되어도 `selectors.yaml`만 수정하면 대응 가능. 재컴파일 불필요.

각 필드당 3개 이상의 fallback 셀렉터 제공:
```yaml
order_page:
  recipient_name:
    - 'input[name="recvNm"]'
    - 'input#recvNm'
    - 'input[placeholder*="받는분"]'
```

### 5.2 재시도 정책
| 에러 유형 | 재시도 | 백오프 |
|----------|-------|-------|
| 셀렉터 찾기 실패 | 2회 | 페이지 새로고침 후 재시도 |
| 네트워크 타임아웃 | 3회 | 지수 백오프 (1s → 2s → 4s) |
| 로그인 만료 | 0회 | 즉시 사용자 알림 |
| 캡차 감지 | 0회 | 일시정지 + 알림 |

### 5.3 로깅 & 진단
- `data/logs/YYYY-MM-DD.log` (일별 로테이션, 10MB 제한)
- 에러 시 `data/screenshots/error_ROW_TIMESTAMP.png` 자동 저장
- 로그 레벨: DEBUG/INFO/WARNING/ERROR
- "진단 파일 내보내기" 메뉴 → zip으로 묶어서 개발자에게 전송 가능

### 5.4 성능 목표 (MCP 검증 기반)

| 지표 | 목표 | 근거 |
|------|------|------|
| .exe 크기 | ~50MB | system Chrome 사용 시 (bundled Chromium 600MB+ 회피) |
| 주문 1건 처리 | 12-20초 | Playwright auto-wait 최적화 |
| 가격 조회 1건 | 2-3초 | HTTP 캐시 + 순차 실행 |
| 메모리 사용 | <500MB | 단일 브라우저 컨텍스트 재사용 |
| 연속 무중단 | 8시간+ | MV3 service worker auto-suspend 허용 |
| 주문 성공률 | 97%+ | 3단계 fallback 셀렉터 |

---

## 6. 개발 일정 (3주 / 17영업일)

### Week 1: 기반 & 자동화 코어
| Day | 작업 | 산출물 |
|-----|------|-------|
| 1 | 프로젝트 셋업, Playwright + system Chrome 연결 테스트 | 동작하는 hello world |
| 2 | 엑셀 로더 + Pydantic 스키마 검증 | ExcelManager 모듈 |
| 3 | **11번가 실제 페이지 셀렉터 수집** (클라이언트 협조 필요) | `selectors.yaml` v1 |
| 4 | 셀렉터 fallback 로직 + 단위테스트 | 검증된 셀렉터 헬퍼 |
| 5 | 가격 스크래퍼 프로토타입 | 50건 일괄 조회 동작 |
| 6 | 주문 페이지 자동 입력 v1 | 1건 end-to-end |
| 7 | 주문번호 추출 + 엑셀 저장 | 전체 플로우 완성 |

### Week 2: UI & 통합
| Day | 작업 | 산출물 |
|-----|------|-------|
| 8 | PySide6 메인 윈도우 + QAbstractTableModel | 편집 가능 테이블 |
| 9 | qasync 통합, 신호/슬롯 배선 | UI-자동화 연결 |
| 10 | 진행 상태 UI, 실시간 로그 패널 | 완성된 UX |
| 11 | 설정 다이얼로그, 우클릭 메뉴 | 사용자 커스터마이징 |
| 12 | 에러 처리 + 스크린샷 자동화 | 견고한 에러 복구 |
| 13 | `state.json` 크래시 복구 | 이어하기 기능 |
| 14 | playwright-stealth 통합 + 메모리 최적화 | 튜닝된 빌드 |

### Week 3: 테스트 & 납품
| Day | 작업 | 산출물 |
|-----|------|-------|
| 15 | **실제 11번가 주문 E2E (최소 5건)** | 검증 리포트 |
| 16 | 클라이언트 UAT, 피드백 반영 | 수정본 |
| 17 | 버그 픽스, PyInstaller 빌드, 매뉴얼 작성, 납품 | **최종 배포** |

---

## 7. 납품물

| 번호 | 산출물 | 형식 |
|------|--------|------|
| 1 | 실행파일 `11st_auto_order.exe` | Windows 64bit, 설치 불필요 |
| 2 | 사용자 매뉴얼 | 한글 PDF (스크린샷 포함) |
| 3 | 샘플 엑셀 템플릿 | `template.xlsx` |
| 4 | 셀렉터 설정파일 | `selectors.yaml` (유지보수 가능) |
| 5 | 소스코드 | 비공개 GitHub 초대 |
| 6 | 설치 가이드 | Chrome 최소 버전 안내 포함 |
| 7 | 1개월 무상 유지보수 | 버그 + 셀렉터 업데이트 |

---

## 8. 리스크 & 대응 (MCP 조사 기반 확장)

| 리스크 | 영향 | 확률 | 대응 방안 |
|--------|------|------|----------|
| 11번가 DOM 변경 | 높음 | 중 | `selectors.yaml` 즉시 수정 가능, 유지보수 기간 대응 |
| 봇 탐지 | 높음 | 낮음 | 실제 로그인 프로필 + 사람같은 딜레이 + 최소 stealth |
| Chrome 정책 변경 | 높음 | 낮음 | 독립 프로필 디렉토리 사용 (정책 준수) |
| 샵백 확장 MV3 suspend | 중간 | 중 | Playwright 자동 처리, 3초 wake-up 허용 |
| PyInstaller 빌드 실패 | 중 | 낮음 | `--collect-all playwright`, 주간 테스트빌드 |
| Qt 스레드 충돌 | 중 | 중 | qasync 사용, 명시적 `QMetaObject.invokeMethod` |
| 사용자 기존 Chrome 프로필 지정 | 중 | 낮음 | 자동 경고 + 분리 프로필 강제 |
| 로그인 세션 만료 | 중 | 낮음 | 감지 → 일시정지 → 재로그인 → 이어하기 |
| 캡차 출현 | 높음 | 낮음 | 알림 + 사용자 해결 대기 |
| 엑셀 포맷 오류 | 중 | 중 | Pydantic 검증, 라인별 상세 에러 |

---

## 9. 법적 / 정책 준수

- ✅ **개인 사용자 편의 도구**로 개발 (대량 상업 매매 대행 아님)
- ✅ 11번가 이용약관 준수 (과도한 요청 방지, 사람같은 패턴)
- ✅ 샵백 이용약관 준수
- ✅ 2026 Chrome 자동화 정책 준수 (별도 프로필 사용)
- ✅ 프로그램 첫 실행 시 면책 동의 화면 제공

---

## 10. 추가 옵션 (별도 협의)

| 옵션 | 설명 |
|------|------|
| 다중 계정 전환 | 여러 11번가 계정 순환 주문 |
| 타 쇼핑몰 확장 | 쿠팡, G마켓, 옥션, 지마켓 |
| Google Drive 백업 | 주문 엑셀 자동 백업 |
| 텔레그램 알림 | 주문 완료 시 실시간 푸시 |
| 정기 주문 예약 | 특정 시간 자동 실행 (cron) |
| 엑셀 ↔ Google Sheets 연동 | 클라우드 동기화 |
| 이미지 OCR | 통관번호 이미지 자동 판독 |

---

## 11. 계약 전 확인 필요 사항

1. **OS 확인**: Windows 10 (빌드 1809+) / Windows 11
2. **Chrome 버전**: Chrome 120+ 설치 여부
3. **상품 유형**: 주로 해외직구? (통관번호 필드로 추정)
4. **일일 주문량**: 평균 몇 건? (성능 튜닝 기준)
5. **결제 수단**: 카드 / 계좌이체 / 11Pay 중 주로 사용하는 것
6. **샵백 외 확장**: 추가 설치 필요한 것 있는지
7. **결제 자동화 수준**: 완전 자동 vs 최종 확인 수동 (기본값 수동 권장)
8. **기존 엑셀 샘플**: 실제 사용 중인 엑셀 1개 제공 가능한지

---

## 12. 견적 및 지급

| 항목 | 금액 (KRW) |
|------|-----------|
| 기본 개발 (1-7장 전체) | 협의 |
| 유지보수 1개월 포함 | 포함 |
| 추가 유지보수 (월) | 별도 협의 |
| 기능 추가 (10장 옵션) | 별도 협의 |

**지급 일정**: 계약금 50% / 중간 점검 (Week 2 종료) 30% / 납품 완료 20%

---

## 참고 자료 (MCP 검증)

본 계획서는 다음 공식 자료를 MCP 도구로 검증하여 작성했습니다:

- [Playwright Python - Chrome Extensions](https://playwright.dev/python/docs/chrome-extensions)
- [Playwright - BrowserType API](https://playwright.dev/docs/api/class-browsertype)
- [PyInstaller + Playwright Packaging Guide](https://github.com/microsoft/playwright-python/issues/1001)
- [Playwright Stealth 2026 Analysis](https://dicloak.com/blog-detail/playwright-stealth-what-works-in-2026-and-where-it-falls-short)
- [PySide6 QTableView Tutorial (2026 Update)](https://www.pythonguis.com/tutorials/pyside6-qtableview-modelviews-numpy-pandas/)
- [Qt for Python Pandas Example](https://doc.qt.io/qtforpython-6/examples/example_external_pandas.html)

---

**문의사항은 언제든지 편하게 연락 주세요. 빠르고 안정적인 프로그램으로 보답하겠습니다.** 🚀

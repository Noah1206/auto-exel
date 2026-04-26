"""셀렉터 진단 도구.

실제 로그인된 Chrome 프로필을 사용해 지정한 상품 URL을 열고,
현재 페이지에서 어떤 셀렉터들이 가격/바로구매/수량 요소로 매칭되는지 탐색한다.
결과를 콘솔에 출력하고 `data/diagnostics/`에 HTML/스크린샷을 저장한다.

사용법::

    source .venv/bin/activate
    python scripts/diagnose_selectors.py "https://www.11st.co.kr/products/1234567890"

출력 예::

    [PRICE]
      ✓ strong.c_product_detail__price  → "15,900"
      ✓ em.price-value                   → "15,900"
      ✗ span.price_detail strong         (매칭 없음)
    ...
"""
from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

import yaml
from playwright.async_api import async_playwright

# 가격 후보 셀렉터 (2026년 기준으로 현장에서 쓰이는 것들 포괄적으로)
PRICE_CANDIDATES = [
    # 클래스명 패턴
    "strong.c_product_detail__price",
    ".c_product_detail__price strong",
    ".c_product_detail__price",
    "em.price-value",
    "span.price-value",
    ".price-value",
    "strong[class*='price' i]",
    "em[class*='price' i]",
    "span[class*='price' i]",
    "div[class*='price' i] strong",
    "div[class*='SellPrice' i]",
    # data-attribute
    "[data-log-actionid-label='price']",
    "[data-price]",
    # 옛 셀렉터 (과거 기록 유지)
    "span.price_detail strong",
    ".c_product_price_detail .price",
    "em.price_sell",
    # itemprop (SEO)
    "[itemprop='price']",
    "meta[itemprop='price']",
]

BUY_NOW_CANDIDATES = [
    "button:has-text('바로구매')",
    "a:has-text('바로구매')",
    "button:has-text('바로 구매')",
    "button[class*='BuyNow' i]",
    "button[class*='buy_now' i]",
    "button.buy_now",
    "[data-log-actionid-label*='buy_now']",
    "[data-log-body*='buy_now']",
]

QTY_CANDIDATES = [
    "input.qty_input",
    "input[name='selectQuantity']",
    "input[name='buyQty']",
    "input[type='number'][class*='qty' i]",
    "select[name='selectQuantity']",
    "select.qty_select",
    "input[data-log-actionid-label*='qty' i]",
]


async def probe(page, name: str, selectors: list[str]) -> list[tuple[str, str | None]]:
    """각 셀렉터로 첫 요소를 찾고 텍스트를 반환. 매칭 없으면 None."""
    results: list[tuple[str, str | None]] = []
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                results.append((sel, None))
                continue
            # 텍스트 또는 value 속성
            text = (await loc.inner_text()).strip() if await loc.is_visible() else ""
            if not text:
                # input이면 value 속성도 체크
                try:
                    val = await loc.get_attribute("value")
                    if val:
                        text = f"(value) {val}"
                except Exception:
                    pass
            if not text:
                try:
                    text = f"(content) {(await loc.get_attribute('content')) or '?'}"
                except Exception:
                    text = "(요소만 있고 텍스트 없음)"
            results.append((sel, text[:80]))
        except Exception as exc:
            results.append((sel, f"ERR: {exc}"))
    return results


async def dump_top_price_candidates(page) -> list[str]:
    """페이지에서 "원" 이 들어간 strong/em/span 중 상위 10개를 CSS path와 함께 덤프.

    기존 셀렉터가 전부 실패할 때, 진짜 "가격"으로 보이는 요소를 자동으로 찾아준다.
    """
    js = r"""
() => {
  const out = [];
  const candidates = document.querySelectorAll('strong, em, span, b');
  for (const el of candidates) {
    const t = (el.innerText || el.textContent || '').trim();
    // "15,900원" / "15,900" 패턴만
    if (!/^[\d,]{2,}(원)?$/.test(t)) continue;
    // 숫자 값이 의미있는 크기 (>= 100)
    const num = parseInt(t.replace(/[^\d]/g, ''), 10);
    if (!num || num < 100) continue;

    // 요소 고유 경로 생성
    function path(e) {
      if (!e || e === document.body) return 'body';
      let s = e.tagName.toLowerCase();
      if (e.id) s += '#' + e.id;
      if (e.className && typeof e.className === 'string') {
        const cls = e.className.trim().split(/\s+/).slice(0,3).join('.');
        if (cls) s += '.' + cls;
      }
      const parent = e.parentElement;
      if (!parent) return s;
      const sibs = Array.from(parent.children).filter(c => c.tagName === e.tagName);
      if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(e)+1) + ')';
      return path(parent) + ' > ' + s;
    }

    // 표시 여부
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;

    out.push({
      text: t,
      num: num,
      cssPath: path(el),
      tagName: el.tagName.toLowerCase(),
      className: el.className || '',
      id: el.id || '',
    });
  }
  // 큰 숫자부터 (보통 본 가격이 가장 크다)
  out.sort((a,b) => b.num - a.num);
  return out.slice(0, 15);
}
"""
    items = await page.evaluate(js)
    return items


def suggest_selector(item: dict) -> str:
    """덤프된 요소에서 안정적인 셀렉터 하나 제안."""
    tag = item["tagName"]
    cls = item["className"].strip()
    el_id = item["id"].strip()
    if el_id:
        return f"#{el_id}"
    if cls:
        # 첫 번째 클래스만 사용 (너무 길면 브리틀)
        first_class = cls.split()[0]
        # 숫자/해시 suffix가 붙은 랜덤 클래스면 회피 (like 'abc123_xy')
        if re.match(r"^[a-zA-Z_][\w-]*$", first_class) and not re.search(
            r"_[a-z0-9]{5,}$", first_class
        ):
            return f"{tag}.{first_class}"
    return item["cssPath"]


async def main(url: str) -> None:
    out_dir = Path("data/diagnostics")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    async with async_playwright() as pw:
        profile = Path("data/chrome_profile").resolve()
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        page = await ctx.new_page()
        print(f"→ 페이지 로드: {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=10000)

        # HTML / screenshot 저장
        html_path = out_dir / f"product_{ts}.html"
        png_path = out_dir / f"product_{ts}.png"
        html_path.write_text(await page.content(), encoding="utf-8")
        await page.screenshot(path=str(png_path), full_page=True)
        print(f"  📄 HTML   : {html_path}")
        print(f"  🖼  Screen : {png_path}\n")

        # 후보 셀렉터 매칭
        for label, cands in [
            ("PRICE", PRICE_CANDIDATES),
            ("BUY_NOW", BUY_NOW_CANDIDATES),
            ("QUANTITY", QTY_CANDIDATES),
        ]:
            print(f"=== [{label}] ===")
            results = await probe(page, label, cands)
            for sel, text in results:
                mark = "✓" if text and not text.startswith("ERR") else "✗"
                print(f"  {mark} {sel:<55s} → {text!r}")
            print()

        # 자동 덤프: 숫자/원 패턴 요소 상위
        print("=== [AUTO-DETECTED PRICE-LIKE ELEMENTS] ===")
        items = await dump_top_price_candidates(page)
        if not items:
            print("  (발견 실패)")
        else:
            suggested = []
            for it in items:
                sel = suggest_selector(it)
                suggested.append(sel)
                print(f"  {it['num']:>10,}원 → {sel}")
                print(f"     path: {it['cssPath'][:160]}")
            print()
            print("=== [제안: selectors.yaml → product_page.price 에 추가] ===")
            for sel in suggested[:5]:
                print(f"  - '{sel}'")

        print("\n브라우저를 직접 확인한 후 Enter를 누르면 종료합니다...")
        try:
            input()
        except EOFError:
            pass
        await ctx.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python scripts/diagnose_selectors.py <11번가_상품_URL>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))

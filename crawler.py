"""
올리브영 리뷰 크롤러 - 순수 HTTP 버전 (Playwright 없음)
Streamlit Cloud 배포용

전략:
1. 모바일 API GET (m.oliveyoung.co.kr/review/api/v2/reviews)
2. JS 번들에서 리뷰 엔드포인트 탐색
3. 탐색된 엔드포인트 사용
"""

import re
import time
import random
import html as html_lib
from urllib.parse import urlparse, parse_qs

# curl_cffi: Cloudflare 우회를 위한 TLS fingerprinting
try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cf_requests
    HAS_CURL_CFFI = False


# ──────────── 상수 ────────────
MOBILE_REVIEW_URL = "https://m.oliveyoung.co.kr/review/api/v2/reviews"
PAGE_SIZE = 10
MAX_PAGES = 200

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)

PC_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.oliveyoung.co.kr",
    "Referer": "https://www.oliveyoung.co.kr/",
    "User-Agent": PC_UA,
    "X-Requested-With": "XMLHttpRequest",
}
MOBILE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.oliveyoung.co.kr",
    "Referer": "https://www.oliveyoung.co.kr/",
    "User-Agent": MOBILE_UA,
    "X-Requested-With": "XMLHttpRequest",
}


def _create_session():
    if HAS_CURL_CFFI:
        session = cf_requests.Session(impersonate="chrome")
    else:
        session = cf_requests.Session()
        session.headers.update({"User-Agent": PC_UA})
    return session


def extract_goods_no(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "goodsNo" in params:
        return params["goodsNo"][0]
    match = re.search(r"[/=](A\d{12})", url)
    if match:
        return match.group(1)
    return None


def fetch_product_name(session, goods_no: str) -> str:
    """PC 상품 페이지에서 상품명 추출 + 쿠키 취득"""
    url = (
        f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do"
        f"?goodsNo={goods_no}"
    )
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code != 200:
            return ""
        html = resp.text
        title_match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        if title_match:
            title_text = title_match.group(1).strip()
            for sep in [" | ", " - ", " │ "]:
                if sep in title_text:
                    return title_text.split(sep)[0].strip()
        name_match = re.search(
            r'class="prd_name[^"]*"[^>]*>(.*?)</[^>]+>', html, re.DOTALL
        )
        if name_match:
            name = re.sub(r"<[^>]+>", "", name_match.group(1)).strip()
            if name:
                return name
    except Exception:
        pass
    return ""


def find_review_url_in_js(session, html: str) -> str | None:
    """상품 페이지 JS 번들 파일에서 리뷰 API URL 탐색"""
    script_srcs = re.findall(
        r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', html
    )
    for src in script_srcs[:15]:
        # 외부 CDN / 공통 라이브러리 제외
        if any(x in src for x in ["jquery", "bootstrap", "google", "naver", "kakao", "//cdn"]):
            continue
        if not src.startswith("http"):
            src = "https://www.oliveyoung.co.kr" + src
        try:
            resp = session.get(src, timeout=10)
            js = resp.text[:200_000]  # 최대 200KB만 검색
            # 리뷰/gdas 관련 .do 또는 API URL 탐색
            candidates = re.findall(
                r'["\`]([^"\'`\s]{5,80}(?:gdas|review|Review|Gdas)[^"\'`\s]*)["\`]',
                js,
            )
            for c in candidates:
                if any(k in c.lower() for k in ["list", "search", "get"]):
                    return c if c.startswith("http") else f"https://www.oliveyoung.co.kr{c}"
        except Exception:
            continue
    return None


def _try_mobile_api_get(session, goods_no: str, page: int) -> dict | None:
    """모바일 API GET 방식 시도"""
    param_variants = [
        {"goodsNo": goods_no, "page": page, "size": PAGE_SIZE},
        {"goodsNumber": goods_no, "page": page, "size": PAGE_SIZE},
        {"goodsNo": goods_no, "pageNo": page, "pageSize": PAGE_SIZE},
    ]
    for params in param_variants:
        try:
            resp = session.get(
                MOBILE_REVIEW_URL, params=params, headers=MOBILE_HEADERS, timeout=15
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            reviews = data.get("data") or []
            total = data.get("totalCnt")
            # 리뷰가 있거나 totalCnt가 숫자면 올바른 응답
            if reviews or (total is not None and total != "None"):
                return data
        except Exception:
            continue
    return None


def _extract_reviews_flexible(data: dict) -> list[dict]:
    """JSON 응답에서 리뷰 리스트 추출 (여러 키 구조 대응)"""
    for key in ["data", "gdasList", "reviewList", "reviews", "list", "items", "contents"]:
        val = data.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def _parse_review(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    review = {}

    for k in ["reviewContent", "gdasContent", "content", "contText", "reviewText", "body"]:
        if item.get(k) and str(item[k]).strip():
            review["content"] = str(item[k]).strip()
            break

    for k in ["reviewScore", "gdasStar", "rating", "score", "starScore", "starPoint"]:
        if item.get(k) is not None:
            review["rating"] = str(item[k])
            break

    # 작성자 (profileDto 중첩 포함)
    if isinstance(item.get("profileDto"), dict):
        for k in ["memberNickname", "nickname", "nickName"]:
            if item["profileDto"].get(k):
                review["author"] = str(item["profileDto"][k]).strip()
                break
    if "author" not in review:
        for k in ["membNickName", "nickName", "nickname", "memberNickname", "userName"]:
            if item.get(k):
                review["author"] = str(item[k]).strip()
                break

    for k in ["registDate", "createDate", "regDate", "createdDateTime", "writtenDate", "createdAt"]:
        if item.get(k):
            review["date"] = str(item[k]).strip()
            break

    # 구매옵션 (goodsDto 중첩 포함)
    if isinstance(item.get("goodsDto"), dict):
        for k in ["optionName", "goodsName", "optNm"]:
            if item["goodsDto"].get(k) and str(item["goodsDto"][k]).strip():
                review["option"] = str(item["goodsDto"][k]).strip()
                break
    if "option" not in review:
        for k in ["selOptNm", "optionName", "option", "optNm"]:
            if item.get(k) and str(item[k]).strip():
                review["option"] = str(item[k]).strip()
                break

    for k in ["usefulPoint", "recommendCount", "helpCount", "likeCount", "likeCnt"]:
        if item.get(k) is not None:
            review["helpful"] = str(item[k])
            break

    if review.get("content") and len(review["content"]) > 5:
        return review
    return None


def deduplicate_reviews(reviews):
    seen = set()
    result = []
    for r in reviews:
        key = r.get("content", "")[:50]
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


SORT_MAP = {
    "최신순": "date",
    "추천순": "useful",
    "별점높은순": "star_desc",
    "별점낮은순": "star_asc",
}


def crawl_reviews(
    goods_no: str,
    max_pages: int = MAX_PAGES,
    sort_type: str | None = None,
    progress_callback=None,
    log_callback=None,
):
    def log(msg):
        if log_callback:
            log_callback(msg)

    def progress(value):
        if progress_callback:
            progress_callback(min(value, 1.0))

    session = _create_session()
    log(f"ℹ️ curl_cffi: {'사용 중' if HAS_CURL_CFFI else '미설치 (requests 폴백)'}")

    # 1. PC 상품 페이지 접속 (쿠키 + 상품명)
    log("📦 상품 정보를 가져오는 중...")
    progress(0.03)
    product_name = fetch_product_name(session, goods_no)
    if product_name:
        log(f"✅ 상품명: {product_name}")
    else:
        log("⚠️ 상품명을 가져오지 못했습니다.")

    # 2. 모바일 사이트 warm-up
    try:
        session.get(
            f"https://m.oliveyoung.co.kr/m/goods/getGoodsDetail.do?goodsNo={goods_no}",
            timeout=10,
        )
    except Exception:
        pass

    # 3. 모바일 API GET 방식 1페이지 테스트
    log("🔍 API 탐색 중...")
    progress(0.08)
    test_data = _try_mobile_api_get(session, goods_no, 1)

    if test_data is not None:
        log(f"✅ 모바일 API GET 성공: {list(test_data.keys())}")
        use_mobile = True
    else:
        log("⚠️ 모바일 API GET 실패 → JS 번들에서 엔드포인트 탐색 중...")
        use_mobile = False

        # JS 번들에서 리뷰 URL 탐색
        try:
            pc_resp = session.get(
                f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={goods_no}",
                timeout=15,
            )
            js_review_url = find_review_url_in_js(session, pc_resp.text)
        except Exception:
            js_review_url = None

        if js_review_url:
            log(f"🔗 JS에서 발견: {js_review_url}")
        else:
            log("⚠️ 리뷰 엔드포인트를 찾지 못했습니다.")
            progress(1.0)
            log("🎉 수집 완료! 총 0개 리뷰")
            return product_name, []

    # 4. 리뷰 수집
    log("📋 리뷰 수집 시작...")
    all_reviews = []
    consecutive_empty = 0
    estimated_pages = max_pages

    # 1페이지 데이터가 이미 있으면 처리
    start_page = 1
    if use_mobile and test_data is not None:
        items = _extract_reviews_flexible(test_data)
        total_cnt = test_data.get("totalCnt")
        if total_cnt and str(total_cnt).isdigit():
            estimated_pages = min(max_pages, -(-int(total_cnt) // PAGE_SIZE))
            log(f"📊 전체 리뷰 수: {total_cnt}개 (최대 {estimated_pages}페이지)")
        page_reviews = [r for r in (_parse_review(i) for i in items) if r]
        for r in page_reviews:
            key = r.get("content", "")[:50]
            if not any(e.get("content", "")[:50] == key for e in all_reviews):
                all_reviews.append(r)
        log(f"📄 페이지 1: 수집 {len(page_reviews)}개 | 누적 {len(all_reviews)}개")
        if not page_reviews:
            log(f"🔎 1페이지 응답(디버그): {html_lib.escape(str(test_data)[:300])}")
            consecutive_empty += 1
        start_page = 2

    for page_idx in range(start_page, max_pages + 1):
        try:
            if use_mobile:
                data = _try_mobile_api_get(session, goods_no, page_idx)
                if data is None:
                    consecutive_empty += 1
                    log(f"⚠️ 페이지 {page_idx}: 응답 없음")
                    if consecutive_empty >= 3:
                        break
                    continue
                items = _extract_reviews_flexible(data)
            else:
                # JS에서 발견한 엔드포인트 사용
                resp = session.get(
                    js_review_url,
                    params={"goodsNo": goods_no, "pagingIndex": page_idx, "pagingSize": PAGE_SIZE},
                    headers=PC_HEADERS,
                    timeout=15,
                )
                if resp.status_code != 200:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        break
                    continue
                data = resp.json()
                items = _extract_reviews_flexible(data)

            page_reviews = [r for r in (_parse_review(i) for i in items) if r]

            before = len(all_reviews)
            for r in page_reviews:
                key = r.get("content", "")[:50]
                if not any(e.get("content", "")[:50] == key for e in all_reviews):
                    all_reviews.append(r)
            new = len(all_reviews) - before

            if page_idx <= 5 or page_idx % 10 == 0:
                log(
                    f"📄 페이지 {page_idx}: "
                    f"수집 {len(page_reviews)}개 | 신규 {new}개 | 누적 {len(all_reviews)}개"
                )

            progress(0.1 + 0.85 * (page_idx / estimated_pages))

            if len(page_reviews) == 0 or new == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log(f"✅ 리뷰 끝! (페이지 {page_idx})")
                    break
            else:
                consecutive_empty = 0

            time.sleep(random.uniform(0.3, 0.7))

        except Exception as e:
            consecutive_empty += 1
            log(f"❌ 페이지 {page_idx} 오류: {str(e)[:80]}")
            if consecutive_empty >= 3:
                break

    all_reviews = deduplicate_reviews(all_reviews)
    progress(1.0)
    log(f"🎉 수집 완료! 총 {len(all_reviews)}개 리뷰")
    return product_name, all_reviews

"""
올리브영 리뷰 크롤러 - 순수 HTTP 버전 (Playwright 없음)
Streamlit Cloud 배포용

curl_cffi로 Cloudflare 우회 + 올리브영 PC GDAS 리뷰 API 호출
"""

import re
import time
import random

from urllib.parse import urlparse, parse_qs

# curl_cffi: Cloudflare 우회를 위한 TLS fingerprinting
try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cf_requests
    HAS_CURL_CFFI = False


# ──────────── 상수 ────────────
GDAS_API_URL = "https://www.oliveyoung.co.kr/store/goods/getGdasSearchList.do"
PAGE_SIZE = 10
MAX_PAGES = 200

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.oliveyoung.co.kr",
    "Referer": "https://www.oliveyoung.co.kr/",
    "User-Agent": PC_UA,
    "X-Requested-With": "XMLHttpRequest",
}


def _create_session():
    """Cloudflare 우회 가능한 세션 생성"""
    if HAS_CURL_CFFI:
        session = cf_requests.Session(impersonate="chrome")
    else:
        session = cf_requests.Session()

    session.headers.update({"User-Agent": PC_UA})
    return session


def extract_goods_no(url: str) -> str | None:
    """상품 URL에서 goodsNo 추출"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if "goodsNo" in params:
        return params["goodsNo"][0]

    match = re.search(r"[/=](A\d{12})", url)
    if match:
        return match.group(1)

    return None


def fetch_product_name(session, goods_no: str) -> str:
    """상품명 + PC 쿠키 취득"""
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
            r'class="prd_name[^"]*"[^>]*>(.*?)</[^>]+>',
            html, re.DOTALL
        )
        if name_match:
            name = re.sub(r"<[^>]+>", "", name_match.group(1)).strip()
            if name:
                return name

    except Exception:
        pass
    return ""


def parse_gdas_review(item: dict) -> dict | None:
    """GDAS 리뷰 객체 파싱"""
    if not isinstance(item, dict):
        return None

    review = {}

    # 리뷰 내용
    for k in ["gdasContent", "reviewContent", "content", "contText"]:
        if item.get(k) and str(item[k]).strip():
            review["content"] = str(item[k]).strip()
            break

    # 별점
    for k in ["gdasStar", "reviewScore", "rating", "starScore", "score"]:
        if item.get(k) is not None:
            review["rating"] = str(item[k])
            break

    # 작성자
    for k in ["membNickName", "nickName", "nickname", "memberNickname", "userName"]:
        if item.get(k):
            review["author"] = str(item[k]).strip()
            break

    # 작성일
    for k in ["registDate", "createDate", "regDate", "createdDateTime", "writtenDate"]:
        if item.get(k):
            review["date"] = str(item[k]).strip()
            break

    # 구매옵션
    for k in ["selOptNm", "optionName", "option", "optNm"]:
        if item.get(k) and str(item[k]).strip():
            review["option"] = str(item[k]).strip()
            break

    # 도움수
    for k in ["usefulPoint", "recommendCount", "helpCount", "likeCount", "likeCnt"]:
        if item.get(k) is not None:
            review["helpful"] = str(item[k])
            break

    if review.get("content") and len(review["content"]) > 5:
        return review
    return None


def deduplicate_reviews(reviews):
    """리뷰 중복 제거"""
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
    """
    올리브영 리뷰 크롤링 (PC GDAS API)

    Args:
        goods_no: 상품번호 (예: "A000000235192")
        max_pages: 최대 페이지 수
        sort_type: 정렬 타입 (date, useful, star_desc, star_asc)
        progress_callback: 진행률 콜백 (0.0 ~ 1.0)
        log_callback: 로그 메시지 콜백

    Returns:
        tuple: (product_name, reviews_list)
    """
    def log(msg):
        if log_callback:
            log_callback(msg)

    def progress(value):
        if progress_callback:
            progress_callback(min(value, 1.0))

    session = _create_session()

    # 1. 상품 페이지 접속 (PC 쿠키 취득 + 상품명)
    log("📦 상품 정보를 가져오는 중...")
    progress(0.05)
    product_name = fetch_product_name(session, goods_no)
    if product_name:
        log(f"✅ 상품명: {product_name}")
    else:
        log("⚠️ 상품명을 가져오지 못했습니다. (크롤링은 계속됩니다)")

    # 2. 리뷰 수집
    log("🔍 리뷰를 수집하는 중...")
    all_reviews = []
    consecutive_empty = 0
    sort = sort_type or "date"

    for page_idx in range(1, max_pages + 1):
        params = {
            "goodsNo": goods_no,
            "pagingIndex": str(page_idx),
            "pagingSize": str(PAGE_SIZE),
            "sort": sort,
        }

        try:
            resp = session.get(
                GDAS_API_URL,
                params=params,
                timeout=15,
                headers=HEADERS,
            )

            if resp.status_code != 200:
                consecutive_empty += 1
                log(f"⚠️ 페이지 {page_idx}: HTTP {resp.status_code}")
                if consecutive_empty >= 3:
                    log(f"⚠️ 연속 오류로 수집 종료")
                    break
                continue

            try:
                data = resp.json()
            except Exception:
                if page_idx == 1:
                    import html as html_lib
                    raw = resp.text[:400].replace("\n", " ").replace("\r", "")
                    preview = html_lib.escape(raw)
                    log(f"🔎 응답(HTML이스케이프): {preview}")
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
                continue

            # 1페이지 디버그
            if page_idx == 1:
                top_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                log(f"🔎 API 응답 키: {top_keys}")

            gdas_list = data.get("gdasList") or []

            # totalCnt로 전체 리뷰 수 파악
            if page_idx == 1:
                total_cnt = data.get("totalCnt")
                if total_cnt:
                    log(f"📊 전체 리뷰 수: {total_cnt}개")
                    estimated_pages = min(max_pages, -(-int(total_cnt) // PAGE_SIZE))
                else:
                    estimated_pages = max_pages

            page_reviews = [r for r in (parse_gdas_review(item) for item in gdas_list) if r]

            before = len(all_reviews)
            for r in page_reviews:
                key = r.get("content", "")[:50]
                if not any(e.get("content", "")[:50] == key for e in all_reviews):
                    all_reviews.append(r)
            new = len(all_reviews) - before

            if page_idx <= 5 or page_idx % 10 == 0:
                log(
                    f"📄 페이지 {page_idx}: "
                    f"수집 {len(page_reviews)}개 | "
                    f"신규 {new}개 | "
                    f"누적 {len(all_reviews)}개"
                )

            progress(0.1 + 0.85 * (page_idx / estimated_pages))

            if len(page_reviews) == 0 or new == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log(f"✅ 리뷰 끝! (페이지 {page_idx})")
                    break
            else:
                consecutive_empty = 0

            time.sleep(random.uniform(0.3, 0.8))

        except Exception as e:
            consecutive_empty += 1
            log(f"❌ 페이지 {page_idx} 오류: {str(e)[:80]}")
            if consecutive_empty >= 3:
                break
            continue

    all_reviews = deduplicate_reviews(all_reviews)
    progress(1.0)
    log(f"🎉 수집 완료! 총 {len(all_reviews)}개 리뷰")

    return product_name, all_reviews

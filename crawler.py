"""
올리브영 리뷰 크롤러 - Streamlit Cloud 배포용
전략: PC GDAS API 직접 호출 (curl_cffi Chrome 지문 위조)
"""

import re
import json
import time
import random
import html as html_lib
from urllib.parse import urlparse, parse_qs

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
except ImportError:
    import requests as cf_requests
    HAS_CURL_CFFI = False


PAGE_SIZE = 10
MAX_PAGES = 200

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

SORT_MAP = {
    "최신순": "date",
    "추천순": "useful",
    "별점높은순": "star_desc",
    "별점낮은순": "star_asc",
}

# 정렬 코드 → GDAS order 파라미터
_SORT_ORDER = {
    "date": "NEW",
    "useful": "RECOMMEND",
    "star_desc": "HIGH",
    "star_asc": "LOW",
}

# 시도할 리뷰 API 엔드포인트 목록 (POST/GET 모두 시도)
_REVIEW_ENDPOINTS = [
    "https://www.oliveyoung.co.kr/store/goods/getGdasReviewList.do",
    "https://www.oliveyoung.co.kr/store/goods/getGoodsGdasList.do",
    "https://www.oliveyoung.co.kr/store/review/getReviewList.do",
    "https://www.oliveyoung.co.kr/store/goods/getGdasSearchList.do",
    "https://www.oliveyoung.co.kr/store/goods/getGoodsCriteriaReviewList.do",
]


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


# ──────────── 리뷰 dict 파싱 ────────────

_HTML_META_PATTERN = re.compile(
    r'width=device|initial-scale|viewport-fit|charset|user-scalable'
    r'|text/javascript|text/css|IE=edge',
    re.IGNORECASE,
)


def _parse_review_dict(item: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    review = {}

    # 구체적인 리뷰 필드 우선 (오탐 없음)
    specific_keys = ["reviewContent", "gdasContent", "reviewText", "reviewBody", "contText"]
    generic_keys = ["content", "body", "text"]

    for k in specific_keys:
        if item.get(k) and str(item[k]).strip():
            review["content"] = str(item[k]).strip()
            break

    if "content" not in review:
        for k in generic_keys:
            val = str(item.get(k) or "").strip()
            if not val or len(val) < 6:
                continue
            if _HTML_META_PATTERN.search(val):
                continue
            has_korean = bool(re.search(r'[가-힣]', val))
            has_review_field = any(item.get(f) for f in [
                "reviewScore", "gdasStar", "rating", "score", "starScore",
                "membNickName", "nickName", "nickname", "registDate", "createDate",
            ])
            if has_korean or has_review_field:
                review["content"] = val
                break

    for k in ["reviewScore", "gdasStar", "rating", "score", "starScore", "starPoint",
              "ratingValue"]:
        if item.get(k) is not None:
            review["rating"] = str(item[k])
            break
    if "rating" not in review and isinstance(item.get("reviewRating"), dict):
        rv = item["reviewRating"].get("ratingValue")
        if rv is not None:
            review["rating"] = str(rv)

    if isinstance(item.get("profileDto"), dict):
        for k in ["memberNickname", "nickname", "nickName"]:
            if item["profileDto"].get(k):
                review["author"] = str(item["profileDto"][k]).strip()
                break
    if "author" not in review:
        author_val = item.get("author")
        if isinstance(author_val, dict):
            review["author"] = str(author_val.get("name", "")).strip()
        elif author_val:
            review["author"] = str(author_val).strip()
    if "author" not in review:
        for k in ["membNickName", "nickName", "nickname", "memberNickname", "userName"]:
            if item.get(k):
                review["author"] = str(item[k]).strip()
                break

    for k in ["registDate", "createDate", "regDate", "createdDateTime",
              "writtenDate", "createdAt", "datePublished"]:
        if item.get(k):
            review["date"] = str(item[k]).strip()
            break

    if isinstance(item.get("goodsDto"), dict):
        for k in ["optionName", "goodsName", "optNm"]:
            if item["goodsDto"].get(k):
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


def _extract_from_json_structure(data, reviews: list, depth: int = 0):
    """JSON 구조에서 재귀적으로 리뷰 탐색"""
    if depth > 8:
        return
    if isinstance(data, list):
        for item in data:
            r = _parse_review_dict(item) if isinstance(item, dict) else None
            if r:
                reviews.append(r)
            elif isinstance(item, (dict, list)):
                _extract_from_json_structure(item, reviews, depth + 1)
    elif isinstance(data, dict):
        for key in ["reviewList", "gdasList", "reviews", "data", "list", "items",
                    "contents", "review", "commentList", "goodsReviewList"]:
            if key in data and data[key]:
                _extract_from_json_structure(data[key], reviews, depth + 1)


# ──────────── API 호출 ────────────

def _parse_api_response(resp) -> tuple[list[dict] | None, int]:
    """
    API 응답 파싱.
    Returns (reviews, total_count).  reviews=None → 유효한 JSON 응답 아님.
    """
    if resp.status_code != 200:
        return None, 0
    text = resp.text
    if not text or len(text) < 10:
        return None, 0
    # HTML 응답 감지
    if text.lstrip().startswith('<'):
        return None, 0
    try:
        data = resp.json()
    except Exception:
        return None, 0

    reviews = []
    _extract_from_json_structure(data, reviews)

    total = 0
    if isinstance(data, dict):
        for k in ["totalCnt", "totalCount", "total", "count", "totalReviewCount"]:
            v = data.get(k)
            if v is not None:
                try:
                    total = int(v)
                    break
                except Exception:
                    pass

    return reviews, total


def _try_review_api(
    session,
    goods_no: str,
    page: int,
    sort_code: str,
    referer: str,
    endpoint: str | None = None,
    log=None,
) -> tuple[list[dict], int, str | None]:
    """
    리뷰 API 호출.
    endpoint 지정 시 해당 URL만 시도, 없으면 _REVIEW_ENDPOINTS 전체 시도.
    Returns (reviews, total_count, working_endpoint)
    """
    order = _SORT_ORDER.get(sort_code, "NEW")

    ajax_headers = {
        "User-Agent": PC_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": "https://www.oliveyoung.co.kr",
    }
    get_headers = {k: v for k, v in ajax_headers.items() if k != "Content-Type"}

    endpoints_to_try = [endpoint] if endpoint else _REVIEW_ENDPOINTS

    param_sets = [
        {"goodsNo": goods_no, "pagingIndex": page, "pagingSize": PAGE_SIZE, "order": order},
        {"goodsNo": goods_no, "pagingIndex": page, "pagingSize": PAGE_SIZE, "sortType": order},
        {"goodsNo": goods_no, "pagingIndex": page, "pagingSize": PAGE_SIZE},
        {"goodsNo": goods_no, "page": page, "size": PAGE_SIZE, "order": order},
    ]

    for ep in endpoints_to_try:
        for params in param_sets:
            # POST 시도
            try:
                resp = session.post(ep, data=params, headers=ajax_headers, timeout=15)
                reviews, total = _parse_api_response(resp)
                if reviews is not None:
                    return reviews, total, ep
                if log and page == 1:
                    preview = resp.text[:120].replace("\n", " ")
                    log(f"🔎 POST {ep.split('/')[-1]} → HTTP {resp.status_code} | {html_lib.escape(preview)}")
            except Exception as e:
                if log and page == 1:
                    log(f"⚠️ POST {ep.split('/')[-1]} 예외: {str(e)[:60]}")

            # GET 시도
            try:
                resp = session.get(ep, params=params, headers=get_headers, timeout=15)
                reviews, total = _parse_api_response(resp)
                if reviews is not None:
                    return reviews, total, ep
                if log and page == 1:
                    preview = resp.text[:120].replace("\n", " ")
                    log(f"🔎 GET {ep.split('/')[-1]} → HTTP {resp.status_code} | {html_lib.escape(preview)}")
            except Exception as e:
                if log and page == 1:
                    log(f"⚠️ GET {ep.split('/')[-1]} 예외: {str(e)[:60]}")

    return [], 0, None


def _scan_js_for_review_api(session, html_text: str, log=None) -> str | None:
    """JS 번들 파일에서 리뷰 API 엔드포인트 패턴 탐색"""
    bundle_urls = []

    # CDN 번들
    cdn_matches = re.findall(
        r'https://cf-static\.oliveyoung\.co\.kr/[^\s"\'<>]+?\.js',
        html_text,
    )
    bundle_urls.extend(cdn_matches)

    # 상대 경로 번들
    for m in re.findall(r'"(/_next/static/[^"]+?\.js)"', html_text):
        bundle_urls.append(f"https://www.oliveyoung.co.kr{m}")

    # 중복 제거 + 우선순위 정렬
    seen: set = set()
    unique: list = []
    for u in bundle_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    priority_kw = ['review', 'goods', 'detail', 'page', 'gdas']
    priority = [u for u in unique if any(k in u.lower() for k in priority_kw)]
    rest = [u for u in unique if u not in priority]
    ordered = priority + rest

    if log:
        log(f"ℹ️ JS 번들 {len(ordered)}개 탐색 시작...")

    review_api_re = re.compile(
        r'["\`](/store/[^"\'`\s]{5,100}?(?:gdas|review|Review|Gdas)[^"\'`\s]*?\.do)["\`]',
    )

    for url in ordered[:10]:
        try:
            resp = session.get(url, headers={"User-Agent": PC_UA, "Accept": "*/*"}, timeout=15)
            if resp.status_code != 200:
                continue
            matches = review_api_re.findall(resp.text)
            if matches:
                api_path = matches[0]
                if log:
                    log(f"🔑 JS 번들 API 발견: {api_path}")
                return f"https://www.oliveyoung.co.kr{api_path}"
        except Exception:
            pass

    return None


# ──────────── 중복 제거 ────────────

def deduplicate_reviews(reviews: list) -> list:
    seen: set = set()
    result = []
    for r in reviews:
        key = r.get("content", "")[:50]
        if key not in seen:
            seen.add(key)
            result.append(r)
    return result


# ──────────── 메인 크롤 ────────────

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
    sort = sort_type or "date"

    # ── 1. 상품 페이지 방문 (쿠키 획득 + 상품명) ──
    log("📦 상품 페이지 방문 중...")
    progress(0.05)
    product_url = (
        f"https://www.oliveyoung.co.kr/store/goods/getGoodsDetail.do"
        f"?goodsNo={goods_no}&tab=review"
    )
    html_text = ""
    try:
        resp = session.get(product_url, headers={
            "User-Agent": PC_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }, timeout=20)
        if resp.status_code == 200:
            html_text = resp.text
        else:
            log(f"⚠️ 상품 페이지 HTTP {resp.status_code}")
    except Exception as e:
        log(f"❌ 상품 페이지 접속 실패: {str(e)[:80]}")
        progress(1.0)
        return "", []

    # 상품명 추출
    product_name = ""
    title_m = re.search(r"<title>(.*?)</title>", html_text, re.DOTALL)
    if title_m:
        t = re.sub(r"<[^>]+>", "", title_m.group(1)).strip()
        for sep in [" | ", " - ", " │ "]:
            if sep in t:
                product_name = t.split(sep)[0].strip()
                break
        if not product_name:
            product_name = t
    if not product_name:
        nm = re.search(r'class="prd_name[^"]*"[^>]*>(.*?)</[^>]+>', html_text, re.DOTALL)
        if nm:
            product_name = re.sub(r"<[^>]+>", "", nm.group(1)).strip()

    if product_name:
        log(f"✅ 상품명: {product_name}")
    else:
        log("⚠️ 상품명 추출 실패")
    log(f"ℹ️ HTML 크기: {len(html_text)} chars")

    # ── 2. 리뷰 API 직접 호출 ──
    log("🔍 리뷰 API 호출 중...")
    progress(0.1)

    page1_reviews, total_count, working_endpoint = _try_review_api(
        session, goods_no, 1, sort, product_url, log=log,
    )

    if working_endpoint:
        log(f"✅ API 엔드포인트: {working_endpoint.split('/')[-1]}")
    else:
        # JS 번들에서 엔드포인트 탐색
        log("⚠️ 기본 API 실패. JS 번들 탐색 중...")
        progress(0.2)
        discovered_ep = _scan_js_for_review_api(session, html_text, log)

        if discovered_ep:
            page1_reviews, total_count, working_endpoint = _try_review_api(
                session, goods_no, 1, sort, product_url,
                endpoint=discovered_ep, log=log,
            )
            if working_endpoint:
                log(f"✅ JS 번들 API 성공: {working_endpoint.split('/')[-1]}")

    if total_count:
        log(f"📊 전체 리뷰 수: {total_count}개")
    log(f"📄 1페이지: {len(page1_reviews)}개 리뷰")
    if page1_reviews:
        sample = page1_reviews[0].get("content", "")[:60]
        log(f"🔎 샘플: {html_lib.escape(sample)}")

    all_reviews = list(page1_reviews)

    if not all_reviews:
        # HTML 앞부분 디버그 출력
        if html_text:
            preview = html_lib.escape(html_text[:300].replace("\n", " "))
            log(f"🔎 HTML 앞부분: {preview}")
        log("😢 리뷰를 가져오지 못했습니다.")
        progress(1.0)
        return product_name, []

    # ── 3. 페이지네이션 ──
    if working_endpoint:
        if total_count:
            estimated_pages = min(max_pages, -(-total_count // PAGE_SIZE))
        else:
            estimated_pages = max_pages

        log(f"📋 페이지네이션 시작 (최대 {estimated_pages}페이지)...")
        consecutive_empty = 0

        for page_idx in range(2, estimated_pages + 1):
            page_reviews, _, _ = _try_review_api(
                session, goods_no, page_idx, sort, product_url,
                endpoint=working_endpoint,
            )

            before = len(all_reviews)
            for r in page_reviews:
                key = r.get("content", "")[:50]
                if not any(e.get("content", "")[:50] == key for e in all_reviews):
                    all_reviews.append(r)
            new = len(all_reviews) - before

            if page_idx <= 5 or page_idx % 10 == 0:
                log(f"📄 페이지 {page_idx}: {len(page_reviews)}개 | 신규 {new}개 | 누적 {len(all_reviews)}개")

            progress(0.15 + 0.8 * min(page_idx / estimated_pages, 1.0))

            if not page_reviews or new == 0:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log(f"✅ 페이지네이션 완료 (페이지 {page_idx})")
                    break
            else:
                consecutive_empty = 0

            time.sleep(random.uniform(0.3, 0.7))

    all_reviews = deduplicate_reviews(all_reviews)
    progress(1.0)
    log(f"🎉 수집 완료! 총 {len(all_reviews)}개 리뷰")
    return product_name, all_reviews

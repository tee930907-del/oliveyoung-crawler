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
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
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

# 시도할 리뷰 API 엔드포인트 목록
_REVIEW_ENDPOINTS = [
    # ★ 핵심: /store/product/ 경로 (pageIdx/rowsPerPage/sortCode 파라미터)
    "https://www.oliveyoung.co.kr/store/product/getGdasReviewList.do",
    # 변형 경로들
    "https://www.oliveyoung.co.kr/store/goods/getGdasReviewList.do",
    "https://www.oliveyoung.co.kr/store/product/getGoodsGdasList.do",
    "https://www.oliveyoung.co.kr/store/goods/getGoodsGdasList.do",
    "https://www.oliveyoung.co.kr/store/review/getReviewList.do",
    "https://www.oliveyoung.co.kr/store/goods/getGdasSearchList.do",
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
    specific_keys = ["gdasContents", "reviewContent", "gdasContent", "reviewText", "reviewBody", "contText"]
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

    for k in ["gdasScore", "reviewScore", "gdasStar", "rating", "score", "starScore", "starPoint",
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
        for k in ["memberNickName", "membNickName", "nickName", "nickname", "memberNickname", "userName"]:
            if item.get(k):
                review["author"] = str(item[k]).strip()
                break

    for k in ["regDate", "registDate", "createDate", "createdDateTime",
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


def _extract_csrf(html_text: str, cookies) -> str | None:
    """HTML/쿠키에서 Spring Security CSRF 토큰 추출"""
    # 1. <meta name="_csrf" content="..."> 패턴
    m = re.search(r'<meta\s+name=["\']_csrf["\']\s+content=["\']([^"\']+)["\']', html_text)
    if m:
        return m.group(1)
    # 2. JS 변수: _csrf:"token" 또는 csrfToken:"token"
    m = re.search(r'(?:_csrf|csrfToken|csrf_token)["\s:]+["\']([a-zA-Z0-9\-_]{20,})["\']', html_text)
    if m:
        return m.group(1)
    # 3. 쿠키: XSRF-TOKEN
    try:
        for c in cookies:
            if c.name in ("XSRF-TOKEN", "_csrf", "CSRF-TOKEN"):
                return c.value
    except Exception:
        pass
    return None


def _try_review_api(
    session,
    goods_no: str,
    page: int,
    sort_code: str,
    referer: str,
    endpoint: str | None = None,
    html_text: str = "",
    log=None,
) -> tuple[list[dict], int, str | None]:
    """
    리뷰 API 호출.
    endpoint 지정 시 해당 URL만 시도, 없으면 _REVIEW_ENDPOINTS 전체 시도.
    Returns (reviews, total_count, working_endpoint)
    """
    order = _SORT_ORDER.get(sort_code, "NEW")

    # CSRF 토큰 추출
    csrf_token = _extract_csrf(html_text, getattr(session, "cookies", []))
    if csrf_token and log and page == 1:
        log(f"ℹ️ CSRF 토큰 발견: {csrf_token[:16]}...")

    ajax_headers = {
        "User-Agent": PC_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": "https://www.oliveyoung.co.kr",
    }
    if csrf_token:
        ajax_headers["X-CSRF-TOKEN"] = csrf_token
    get_headers = {k: v for k, v in ajax_headers.items() if k != "Content-Type"}

    endpoints_to_try = [endpoint] if endpoint else _REVIEW_ENDPOINTS

    param_sets = [
        # 커뮤니티 역공학 결과 (pageIdx/rowsPerPage/sortCode)
        {"goodsNo": goods_no, "pageIdx": page, "rowsPerPage": PAGE_SIZE, "sortCode": order},
        # 기존 형태 (pagingIndex/pagingSize/order)
        {"goodsNo": goods_no, "pagingIndex": page, "pagingSize": PAGE_SIZE, "order": order},
        {"goodsNo": goods_no, "pagingIndex": page, "pagingSize": PAGE_SIZE},
        {"goodsNo": goods_no, "pageIdx": page, "rowsPerPage": PAGE_SIZE},
    ]

    # X-Requested-With 없는 헤더 변형도 시도
    ajax_no_xhr = {k: v for k, v in ajax_headers.items() if k not in ("X-Requested-With", "Content-Type")}
    ajax_no_xhr["Accept"] = "application/json"

    # 엔드포인트당 첫 번째 응답만 로그 (노이즈 감소)
    logged_eps: set = set()

    for ep in endpoints_to_try:
        ep_label = ep.split("/")[-1]
        for params in param_sets:
            for h_post, h_get in [(ajax_headers, get_headers), (ajax_no_xhr, ajax_no_xhr)]:
                # POST 시도
                try:
                    resp = session.post(ep, data=params, headers=h_post, timeout=15)
                    reviews, total = _parse_api_response(resp)
                    if reviews is not None:
                        return reviews, total, ep
                    if log and page == 1 and ep not in logged_eps:
                        logged_eps.add(ep)
                        preview = resp.text[:80].replace("\n", " ")
                        log(f"🔎 PC [{resp.status_code}] {ep_label}: {html_lib.escape(preview)}")
                except Exception as e:
                    if log and page == 1 and ep not in logged_eps:
                        logged_eps.add(ep)
                        log(f"⚠️ PC 예외 {ep_label}: {str(e)[:50]}")

                # GET 시도
                try:
                    resp = session.get(ep, params=params, headers=h_get, timeout=15)
                    reviews, total = _parse_api_response(resp)
                    if reviews is not None:
                        return reviews, total, ep
                except Exception:
                    pass

    return [], 0, None


def _scan_js_for_review_api(session, html_text: str, rsc_text: str = "", log=None) -> str | None:
    """JS 번들 파일에서 리뷰 API 엔드포인트 탐색 (다양한 패턴)"""
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

    # 중복 제거
    seen: set = set()
    unique: list = []
    for u in bundle_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    # RSC 스트림에서 모듈 ID 추출 → 해당 번들 우선
    rsc_ids = set(re.findall(r'I\[(\d+),', rsc_text))
    rsc_priority = [u for u in unique if any(f"/{i}" in u or f"-{i}" in u or f"{i}-" in u for i in rsc_ids)]
    kw_priority = [u for u in unique if u not in rsc_priority and any(k in u.lower() for k in ['review', 'goods', 'gdas', 'detail'])]
    rest = [u for u in unique if u not in rsc_priority and u not in kw_priority]
    ordered = rsc_priority + kw_priority + rest

    if log:
        log(f"ℹ️ JS 번들 {len(ordered)}개 탐색 (RSC모듈 {len(rsc_ids)}개, 우선 {len(rsc_priority)}개)...")

    # 다양한 URL 패턴
    url_patterns = [
        # Spring MVC .do
        re.compile(r'["\`](/store/[^"\'`\s]{5,100}?(?:gdas|review|Review|Gdas)[^"\'`\s]*?\.do)["\`]'),
        # Next.js API routes
        re.compile(r'["\`](/api/[^"\'`\s]{3,100}?(?:review|gdas|Review|Gdas)[^"\'`\s]*)["\`]'),
        # fetch() 호출
        re.compile(r'fetch\s*\(\s*["\`]([^"\'`\s]+(?:review|gdas|Review|Gdas)[^"\'`\s]*)["\`]', re.IGNORECASE),
        # 일반 경로 (review/gdas 세그먼트 포함)
        re.compile(r'["\`]((?:/[a-zA-Z0-9_-]{1,40}){1,6}/(?:review|gdas)(?:s|List|[a-zA-Z0-9_-]*)?)["\`]', re.IGNORECASE),
    ]

    for url in ordered[:15]:
        try:
            resp = session.get(url, headers={"User-Agent": PC_UA, "Accept": "*/*"}, timeout=15)
            if resp.status_code != 200:
                continue
            bundle_text = resp.text

            for p in url_patterns:
                matches = p.findall(bundle_text)
                for m in matches:
                    if 'review' in m.lower() or 'gdas' in m.lower():
                        full_url = m if m.startswith('http') else f"https://www.oliveyoung.co.kr{m}"
                        if log:
                            log(f"🔑 JS 번들 API 발견: {m}")
                        return full_url

            # 번들에서 review/gdas 관련 문자열 찾아 디버그 출력
            if log:
                fname = url.split('/')[-1][:25]
                for kw in ['review', 'gdas', 'Review', 'GDAS']:
                    idx = bundle_text.find(f'"{kw}')
                    if idx < 0:
                        idx = bundle_text.find(f'/{kw}')
                    if idx >= 0:
                        ctx = bundle_text[max(0, idx - 20):idx + 80].replace('\n', ' ')
                        log(f"📦 [{fname}] {html_lib.escape(ctx[:90])}")
                        break
        except Exception as e:
            if log:
                log(f"⚠️ 번들 오류: {str(e)[:50]}")

    return None


# ──────────── 모바일 API ────────────

def _try_mobile_review_api(
    session,
    goods_no: str,
    page: int,
    sort_code: str,
    log=None,
    rsc_text: str = "",
) -> tuple[list[dict], int, str | None]:
    """
    m.oliveyoung.co.kr 모바일 리뷰 API 시도.
    Returns (reviews, total_count, endpoint)
    """
    order_map = {"date": "NEW", "useful": "RECOMMEND", "star_desc": "HIGH", "star_asc": "LOW"}
    order = order_map.get(sort_code, "NEW")
    mobile_product_url = f"https://m.oliveyoung.co.kr/store/goods/getGoodsDetail.do?goodsNo={goods_no}"

    # 모바일 세션 별도 생성 (chrome 지문 사용 — safari_ios 미지원 환경 대비)
    if HAS_CURL_CFFI:
        msession = cf_requests.Session(impersonate="chrome")
    else:
        msession = cf_requests.Session()
        msession.headers.update({"User-Agent": MOBILE_UA})

    # 모바일 상품 페이지 방문 (m. 도메인 쿠키 획득)
    try:
        msession.get(mobile_product_url, headers={
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }, timeout=15)
    except Exception:
        pass

    # goodsNo 변형 (A + 12자리 숫자 → 숫자만, 또는 그대로)
    goods_no_num = goods_no[1:] if goods_no.startswith("A") else goods_no

    # RSC 스트림에서 다른 goodsNo 형식 탐색 (예: 소문자 a, 다른 접두어 등)
    rsc_ids: list[str] = []
    if rsc_text:
        # RSC 데이터에서 goodsNo 패턴 탐색
        for pat in [
            r'"goodsNo"\s*:\s*"([^"]{6,20})"',
            r'"goodsCd"\s*:\s*"([^"]{6,20})"',
            r'"itemNo"\s*:\s*"([^"]{6,20})"',
        ]:
            for m in re.findall(pat, rsc_text):
                if m not in rsc_ids and m != goods_no:
                    rsc_ids.append(m)
        if rsc_ids and log and page == 1:
            log(f"ℹ️ RSC 내 상품ID 변형: {rsc_ids[:5]}")

    # (endpoint, method, origin, body) — origin=None이면 헤더 미포함
    attempts = [
        # ★ v2 + www origin (이전에 data:[] JSON 응답 확인됨 — 파라미터 변형 시도)
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "POST",
         "https://www.oliveyoung.co.kr",
         {"goodsNo": goods_no, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "POST",
         "https://www.oliveyoung.co.kr",
         {"goodsNo": goods_no, "pageIdx": page, "rowsPerPage": PAGE_SIZE, "sortCode": order}),
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "POST",
         "https://www.oliveyoung.co.kr",
         {"goodsNo": goods_no, "pageNum": page, "pageSize": PAGE_SIZE}),
        # v2 GET + www origin (POST가 빈 배열 반환 → GET도 시도)
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "GET",
         "https://www.oliveyoung.co.kr",
         {"goodsNo": goods_no, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        # v2 + no origin
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "POST",
         None,
         {"goodsNo": goods_no, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        # v1 POST (JSON 응답 확인됨 — NOT_FOUND)
        ("https://m.oliveyoung.co.kr/review/api/v1/reviews", "POST",
         "https://m.oliveyoung.co.kr",
         {"goodsNo": goods_no, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        # v1 + goodsNo 숫자만 (A 제거)
        ("https://m.oliveyoung.co.kr/review/api/v1/reviews", "POST",
         "https://m.oliveyoung.co.kr",
         {"goodsNo": goods_no_num, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        # v1 GET (쿼리 파라미터)
        ("https://m.oliveyoung.co.kr/review/api/v1/reviews", "GET",
         "https://m.oliveyoung.co.kr",
         {"goodsNo": goods_no, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        # ★ REST 경로 패턴: /reviews/{goodsNo}
        (f"https://m.oliveyoung.co.kr/review/api/v1/reviews/{goods_no}", "GET",
         "https://m.oliveyoung.co.kr",
         {"pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        (f"https://m.oliveyoung.co.kr/review/api/v1/reviews/{goods_no_num}", "GET",
         "https://m.oliveyoung.co.kr",
         {"pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        # v2 경로 패턴: /reviews/{goodsNo}
        (f"https://m.oliveyoung.co.kr/review/api/v2/reviews/{goods_no}", "GET",
         "https://www.oliveyoung.co.kr",
         {"pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
        # v2 + m origin
        ("https://m.oliveyoung.co.kr/review/api/v2/reviews", "POST",
         "https://m.oliveyoung.co.kr",
         {"goodsNo": goods_no, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order}),
    ]

    # RSC에서 찾은 대체 goodsNo로 v1 추가 시도
    for alt_id in rsc_ids[:3]:
        attempts.append((
            "https://m.oliveyoung.co.kr/review/api/v1/reviews", "POST",
            "https://m.oliveyoung.co.kr",
            {"goodsNo": alt_id, "pageNum": page, "pageSize": PAGE_SIZE, "orderType": order},
        ))

    for api_url, method, origin, body in attempts:
        h = {
            "User-Agent": MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Referer": mobile_product_url,
            "Content-Type": "application/json",
        }
        if origin:
            h["Origin"] = origin

        try:
            if method == "POST":
                resp = msession.post(api_url, json=body, headers=h, timeout=15)
            else:
                h.pop("Content-Type", None)
                resp = msession.get(api_url, params=body, headers=h, timeout=15)

            if log and page == 1:
                preview = resp.text[:160].replace("\n", " ")
                ver = "v2" if "v2" in api_url else "v1"
                orig = origin.split("/")[2].replace("m.oliveyoung.co.kr", "m").replace("www.oliveyoung.co.kr", "www") if origin else "none"
                path_style = "path" if api_url.split("/reviews/", 1)[-1] not in ("", "reviews") and "/reviews/" in api_url else method
                label = f"{ver} {path_style} {orig}"
                log(f"🔎 모바일[{resp.status_code}] {label}: {html_lib.escape(preview[:100])}")

            if resp.status_code == 200:
                text = resp.text.lstrip()
                if text.startswith(("{", "[")):
                    data = resp.json()
                    # NOT_FOUND 같은 에러 응답 건너뜀
                    if isinstance(data, dict) and data.get("status") in ("NOT_FOUND", "ERROR", "FAIL"):
                        if log and page == 1:
                            log(f"ℹ️ 모바일 API error: {data.get('message','')[:60]}")
                        continue
                    reviews: list = []
                    _extract_from_json_structure(data, reviews)
                    total = 0
                    if isinstance(data, dict):
                        for k in ["totalCnt", "totalCount", "total"]:
                            if data.get(k) is not None:
                                try:
                                    total = int(data[k])
                                    break
                                except Exception:
                                    pass
                        if log and page == 1 and not reviews:
                            log(f"ℹ️ 모바일 JSON 키: {list(data.keys())[:8]} | data={str(data.get('data'))[:60]}")
                    if reviews:
                        return reviews, total, api_url
        except Exception as e:
            if log and page == 1:
                log(f"⚠️ 모바일 오류: {str(e)[:60]}")

    return [], 0, None


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

    # RSC 스트림 취득 (번들 탐색에서 모듈 ID 활용)
    rsc_text = ""
    try:
        rsc_resp = session.get(
            product_url,
            headers={
                "User-Agent": PC_UA,
                "Accept": "text/x-component",
                "RSC": "1",
                "Accept-Language": "ko-KR,ko;q=0.9",
            },
            timeout=20,
        )
        if rsc_resp.status_code == 200:
            rsc_text = rsc_resp.text
            log(f"ℹ️ RSC 스트림: {len(rsc_text)} chars")
    except Exception:
        pass

    # ── 2. 리뷰 API 직접 호출 ──
    log("🔍 리뷰 API 호출 중...")
    progress(0.1)

    page1_reviews, total_count, working_endpoint = _try_review_api(
        session, goods_no, 1, sort, product_url, html_text=html_text, log=log,
    )

    if working_endpoint:
        log(f"✅ API 엔드포인트: {working_endpoint.split('/')[-1]}")
    else:
        # JS 번들에서 엔드포인트 탐색
        log("⚠️ 기본 API 실패. JS 번들 탐색 중...")
        progress(0.2)
        discovered_ep = _scan_js_for_review_api(session, html_text, rsc_text=rsc_text, log=log)

        if discovered_ep:
            page1_reviews, total_count, working_endpoint = _try_review_api(
                session, goods_no, 1, sort, product_url,
                endpoint=discovered_ep, html_text=html_text, log=log,
            )
            if working_endpoint:
                log(f"✅ JS 번들 API 성공: {working_endpoint.split('/')[-1]}")

    # ── 2b. 모바일 API 폴백 ──
    if not working_endpoint:
        log("🔍 모바일 API 시도 중 (m.oliveyoung.co.kr)...")
        progress(0.35)
        try:
            page1_reviews, total_count, working_endpoint = _try_mobile_review_api(
                session, goods_no, 1, sort, log=log, rsc_text=rsc_text,
            )
        except Exception as e:
            log(f"❌ 모바일 API 예외: {str(e)[:80]}")
        if working_endpoint:
            log(f"✅ 모바일 API 성공: {working_endpoint.split('/')[-1]}")
        else:
            log("❌ 모바일 API도 실패")

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

        is_mobile_ep = "m.oliveyoung.co.kr" in working_endpoint

        for page_idx in range(2, estimated_pages + 1):
            if is_mobile_ep:
                page_reviews, _, _ = _try_mobile_review_api(
                    session, goods_no, page_idx, sort,
                )
            else:
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
